"""
Plate-solving core: submit a FITS frame to the astrometry.net web API and
get back where the frame center actually points, plus the offset from
where the mount says it's pointing.

Shared by:
  - backend/solve_check.py — offline CLI harness for batches of captures
  - backend/main.py's POST /api/telescope/solve — live in-app "solve this
    capture and show me the pointing offset" button (see that endpoint's
    docstring for the field-use case this exists for: when the payload is
    out of frame or the mount looks slightly off, solve the frame you just
    took and get back a correction to dial in by hand, rather than
    guessing or re-aiming blind).

Never commands the mount — this module (and the endpoint built on it) is
strictly read/measure. Any correction is surfaced for a human to apply.

Setup: set the ASTROMETRY_API_KEY environment variable (free key at
https://nova.astrometry.net/api_help). Never pass it on a command line or
put it in a file — env var only.

Uses astroquery's AstrometryNet client by default rather than a local ASTAP
install: on the frames this was validated against (very wide field, ~36"/px)
ASTAP could not find a solution at any field-of-view or star database tried,
while the astrometry.net web solver succeeded. The discrepancy was never
root-caused, so don't assume ASTAP would behave differently if revisited.

A second, local solver path (solve_one_local / LocalSolver) is also
available: it runs astrometry.net's own `solve-field` engine -- the same
matching algorithm the web API runs, just not ASTAP -- inside Docker, via
the dstndstn/astrometry image (https://hub.docker.com/r/dstndstn/astrometry
-- published by the astrometry.net project's own maintainer; verified by
pulling it and inspecting /usr/local/bin/solve-field and
/usr/local/etc/astrometry.cfg directly, since Docker Hub has several
unrelated same-named images from other publishers). This avoids the
round-trip to nova.astrometry.net (queueing + upload can be tens of
seconds to minutes) once index files are downloaded locally, but it needs
Docker Desktop running and the right index-file scale for this rig's
~36"/px, very-wide FOV (roughly 30-40 degrees across on a full ASI585MC/T3i
frame) -- that means the largest index series (4200/4100, covering
skymarks from ~10 degrees up to 60+ degrees), not the default series most
astrometry.net tutorials assume. See LocalSolver's docstring for setup.
"""
from __future__ import annotations

import math
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


def _docker_mount_path(p: Path) -> str:
    """
    Format a local path as a Docker `-v` bind-mount source. Docker Desktop
    (npipe/WSL2 backends on Windows) expects drive-letter paths as
    `//c/Users/...`, not `C:\\Users\\...` -- the raw Windows form's own
    drive-letter colon collides with the `-v host:container` separator, so
    e.g. `-v C:\\foo:/work` gets misparsed. Passing through unchanged on
    non-Windows.
    """
    s = str(p)
    if os.name == "nt" and len(s) > 1 and s[1] == ":":
        return "//" + s[0].lower() + s[2:].replace("\\", "/")
    return s


def angsep_deg(ra1_h: float, dec1_d: float, ra2_h: float, dec2_d: float) -> float:
    """Great-circle separation between two RA(hours)/Dec(deg) points, in degrees."""
    ra1, dec1 = math.radians(ra1_h * 15.0), math.radians(dec1_d)
    ra2, dec2 = math.radians(ra2_h * 15.0), math.radians(dec2_d)
    cos_sep = math.sin(dec1) * math.sin(dec2) + math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_sep))))


@dataclass
class SolveResult:
    filename: str
    solved: bool
    message: str = ""
    solved_ra_h: float | None = None
    solved_dec_d: float | None = None
    target_ra_h: float | None = None
    target_dec_d: float | None = None
    mount_ra_h: float | None = None
    mount_dec_d: float | None = None
    # Wall-clock time the solve attempt took, success or failure -- set by
    # solve_one() / LocalSolver.solve() around the whole attempt (upload +
    # queue + solve for the web path; container startup + solve for local).
    elapsed_s: float | None = None
    # Image dimensions and pixel position of the true frame center (NAXIS1/2
    # halved) -- the frontend overlay draws its solved-center crosshair here
    # rather than assuming the image midpoint, since letterboxing/cropping in
    # display could otherwise misplace it relative to the actual FITS pixels.
    naxis1: int | None = None
    naxis2: int | None = None
    center_px_x: float | None = None
    center_px_y: float | None = None
    # Pixel position of the mount's reported RA/Dec on this same frame, if
    # header pointing was present -- lets the frontend draw a second marker
    # for "where the mount thought it was" without needing its own WCS math.
    mount_px_x: float | None = None
    mount_px_y: float | None = None

    @property
    def err_vs_target_arcsec(self) -> float | None:
        if self.solved_ra_h is None or self.target_ra_h is None:
            return None
        return angsep_deg(self.solved_ra_h, self.solved_dec_d, self.target_ra_h, self.target_dec_d) * 3600

    @property
    def err_vs_mount_arcsec(self) -> float | None:
        if self.solved_ra_h is None or self.mount_ra_h is None:
            return None
        return angsep_deg(self.solved_ra_h, self.solved_dec_d, self.mount_ra_h, self.mount_dec_d) * 3600


def read_header_pointing(path: Path) -> dict:
    from astropy.io import fits

    hdr = fits.getheader(path)

    def g(key: str):
        return hdr.get(f"HIERARCH ALTAIR {key}", hdr.get(f"ALTAIR {key}"))

    return {
        "target_ra_h":  g("TARGET RA"),
        "target_dec_d": g("TARGET DEC"),
        "mount_ra_h":   g("MOUNT RA"),
        "mount_dec_d":  g("MOUNT DEC"),
    }


def get_client():
    """
    Build an authenticated AstrometryNet client from ASTROMETRY_API_KEY.
    Returns None (rather than raising) if the key isn't set, so callers can
    surface a clean "not configured" message instead of a stack trace.
    """
    from astroquery.astrometry_net import AstrometryNet

    api_key = os.getenv("ASTROMETRY_API_KEY")
    if not api_key:
        return None

    ast = AstrometryNet()
    ast.api_key = api_key
    return ast


def _wcs_and_dims(wcs_header, fits_path: Path):
    """
    Build an astropy WCS from a solved header plus the original FITS file's
    NAXIS1/2 (astrometry.net's WCS header is a bare header with no pixel
    array of its own, so dimensions come from the source file). Returns
    (wcs, naxis1, naxis2).
    """
    import warnings

    from astropy.io import fits as _fits
    from astropy.wcs import WCS as _WCS, FITSFixedWarning

    with _fits.open(fits_path) as hdul:
        naxis1 = hdul[0].header.get("NAXIS1")
        naxis2 = hdul[0].header.get("NAXIS2")
    # astrometry.net's WCS header carries WCSAXES=2 but no NAXIS/pixel
    # array of its own -- harmless, astropy just wants to warn about it.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        w = _WCS(wcs_header)
    return w, naxis1, naxis2


def _center_radec_from_wcs_header(wcs_header, fits_path: Path) -> tuple[float, float]:
    """
    Evaluate a solved WCS header at the FITS file's true center pixel and
    return (ra_hours, dec_deg).

    IMPORTANT: CRVAL1/2 is the WCS reference pixel's coordinate, NOT the
    image center -- astrometry.net's solver (web or local) picks CRPIX
    wherever the matched star quad landed, which is frequently off-center
    (confirmed: one solve here put CRPIX at (1975, 834) on a 3840x2160
    frame). Using CRVAL directly as "where the frame points" silently
    measures the wrong point. Evaluate the WCS at the true image-center
    pixel instead.
    """
    w, naxis1, naxis2 = _wcs_and_dims(wcs_header, fits_path)
    center_ra_deg, center_dec_deg = w.all_pix2world(naxis1 / 2.0, naxis2 / 2.0, 0)
    return float(center_ra_deg) / 15.0, float(center_dec_deg)


def _populate_overlay_fields(result: "SolveResult", wcs_header, fits_path: Path) -> None:
    """
    Fill in the pixel-space fields SolveResult carries for the frontend's
    plate-solve overlay: image dimensions, the true-center pixel (same
    point _center_radec_from_wcs_header evaluates), and -- if this capture's
    header carried a mount-reported RA/Dec -- that position's pixel
    coordinate on the same frame, via the inverse WCS transform.

    Mutates result in place; assumes result.solved_ra_h/dec_d are already
    set (they anchor nothing here, but keeping this call after that keeps
    call order obvious at each call site).
    """
    w, naxis1, naxis2 = _wcs_and_dims(wcs_header, fits_path)
    result.naxis1, result.naxis2 = naxis1, naxis2
    result.center_px_x, result.center_px_y = naxis1 / 2.0, naxis2 / 2.0

    if result.mount_ra_h is not None and result.mount_dec_d is not None:
        px, py = w.all_world2pix(result.mount_ra_h * 15.0, result.mount_dec_d, 0)
        result.mount_px_x, result.mount_px_y = float(px), float(py)


def solve_one(ast, fits_path: Path, solve_timeout: int = 300) -> SolveResult:
    """
    Solve a single FITS file and report its true frame-center RA/Dec,
    alongside whatever mount/target pointing the file's header carries.
    """
    hdr_info = read_header_pointing(fits_path)
    result = SolveResult(
        filename=fits_path.name,
        solved=False,
        target_ra_h=hdr_info["target_ra_h"],
        target_dec_d=hdr_info["target_dec_d"],
        mount_ra_h=hdr_info["mount_ra_h"],
        mount_dec_d=hdr_info["mount_dec_d"],
    )
    started = time.monotonic()

    # Seed the search near the commanded/reported position when we have one
    # -- this is a *search hint*, not ground truth: astrometry.net still
    # solves independently from star geometry, the hint just narrows and
    # speeds up its search. Falls back to a blind solve if neither is
    # present.
    kwargs: dict = {"solve_timeout": solve_timeout, "publicly_visible": "n"}
    ra_hint  = hdr_info.get("mount_ra_h")  if hdr_info.get("mount_ra_h")  is not None else hdr_info.get("target_ra_h")
    dec_hint = hdr_info.get("mount_dec_d") if hdr_info.get("mount_dec_d") is not None else hdr_info.get("target_dec_d")
    if ra_hint is not None and dec_hint is not None:
        kwargs.update(center_ra=ra_hint * 15.0, center_dec=dec_hint, radius=15.0)

    try:
        wcs_header = ast.solve_from_image(str(fits_path), **kwargs)
    except TimeoutError as e:
        submission_id = e.args[1] if len(e.args) > 1 else None
        result.elapsed_s = time.monotonic() - started
        result.message = f"timed out after {solve_timeout}s (submission id: {submission_id})"
        return result
    except Exception as e:
        result.elapsed_s = time.monotonic() - started
        result.message = f"solve request failed -- {e}"
        return result

    if not wcs_header:
        result.elapsed_s = time.monotonic() - started
        result.message = "no solution (astrometry.net could not solve this field)"
        return result

    try:
        result.solved_ra_h, result.solved_dec_d = _center_radec_from_wcs_header(wcs_header, fits_path)
        _populate_overlay_fields(result, wcs_header, fits_path)
    except Exception as e:
        result.elapsed_s = time.monotonic() - started
        result.message = f"solved but could not evaluate WCS at image center -- {e}"
        return result

    result.elapsed_s = time.monotonic() - started
    result.solved = True
    return result


class LocalSolver:
    """
    Runs astrometry.net's own `solve-field` engine locally via Docker,
    instead of the nova.astrometry.net web API. Same underlying matching
    algorithm as the web solver (NOT the ASTAP install this module's
    docstring found unable to solve these frames) -- just self-hosted, so
    once index files are cached there's no queue/upload round-trip.

    Setup (one-time):
      1. Install & start Docker Desktop (WSL2 backend on Windows).
      2. `docker pull dstndstn/astrometry` (or build your own image with
         `apt install astrometry.net` -- either works, this class only
         needs `solve-field` on PATH inside the container). Verified:
         solve-field lives at /usr/local/bin in this image, and its
         astrometry.cfg has `add_path /usr/local/data` + `autoindex`, so
         index files just need to be dropped in a directory bind-mounted
         to /usr/local/data -- no config file editing needed.
      3. Pick index files sized to this rig's field of view. At ~36"/px a
         full ASI585MC or T3i frame spans roughly 30-40 degrees -- that
         needs the wide-angle series:
             4200-series (large): index-4204 through index-4219
             4100-series (very large, all-sky scale): as needed
         Download from http://data.astrometry.net/4200/ (or the mirror
         linked from astrometry.net's docs) into a local directory, e.g.
         C:\\astrometry-index\\.
      4. Point this class at that directory via the ASTROMETRY_INDEX_DIR
         env var, or pass index_dir= explicitly.

    This never falls back to the web API silently -- if Docker, the image,
    or the index directory isn't available, solve_local() reports a clear
    "not configured" message via SolveResult.message so the caller can
    decide whether to retry with the web solver instead.
    """

    DEFAULT_IMAGE = "dstndstn/astrometry"

    def __init__(self, index_dir: str | Path | None = None, image: str | None = None):
        self.index_dir = Path(index_dir or os.getenv("ASTROMETRY_INDEX_DIR", "")).resolve() \
            if (index_dir or os.getenv("ASTROMETRY_INDEX_DIR")) else None
        self.image = image or os.getenv("ASTROMETRY_DOCKER_IMAGE", self.DEFAULT_IMAGE)

    def available(self) -> str | None:
        """Returns an error message if this solver can't run right now, else None."""
        if self.index_dir is None:
            return ("ASTROMETRY_INDEX_DIR is not set -- point it at a directory of "
                    "downloaded astrometry.net index files (see LocalSolver's docstring).")
        if not self.index_dir.is_dir():
            return f"Index directory does not exist: {self.index_dir}"
        try:
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=15, check=True,
            )
        except FileNotFoundError:
            return "Docker is not installed or not on PATH."
        except subprocess.CalledProcessError:
            return "Docker is installed but the daemon isn't running (start Docker Desktop)."
        except subprocess.TimeoutExpired:
            return "Docker did not respond -- is Docker Desktop running?"
        return None

    def solve(self, fits_path: Path, solve_timeout: int = 300) -> SolveResult:
        """
        Solve a single FITS file locally. Same seeding/center-evaluation
        behavior as solve_one() (the web-API path), so results from either
        solver are directly comparable.
        """
        hdr_info = read_header_pointing(fits_path)
        result = SolveResult(
            filename=fits_path.name,
            solved=False,
            target_ra_h=hdr_info["target_ra_h"],
            target_dec_d=hdr_info["target_dec_d"],
            mount_ra_h=hdr_info["mount_ra_h"],
            mount_dec_d=hdr_info["mount_dec_d"],
        )

        not_ready = self.available()
        if not_ready:
            result.message = not_ready
            return result

        started = time.monotonic()
        ra_hint  = hdr_info.get("mount_ra_h")  if hdr_info.get("mount_ra_h")  is not None else hdr_info.get("target_ra_h")
        dec_hint = hdr_info.get("mount_dec_d") if hdr_info.get("mount_dec_d") is not None else hdr_info.get("target_dec_d")

        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            input_name = fits_path.name
            (work_dir / input_name).write_bytes(fits_path.read_bytes())

            cmd = [
                "docker", "run", "--rm",
                "-v", f"{_docker_mount_path(work_dir)}:/work",
                "-v", f"{_docker_mount_path(self.index_dir)}:/usr/local/data:ro",
                self.image,
                "solve-field",
                "--dir", "/work",
                "--no-plots",
                "--overwrite",
                "--downsample", "2",
                f"/work/{input_name}",
            ]
            if ra_hint is not None and dec_hint is not None:
                cmd[-1:-1] = ["--ra", str(ra_hint * 15.0), "--dec", str(dec_hint), "--radius", "15"]

            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=solve_timeout,
                )
            except subprocess.TimeoutExpired:
                result.elapsed_s = time.monotonic() - started
                result.message = f"local solve timed out after {solve_timeout}s"
                return result
            except Exception as e:
                result.elapsed_s = time.monotonic() - started
                result.message = f"local solve failed to run -- {e}"
                return result

            wcs_path = work_dir / (Path(input_name).stem + ".wcs")
            if proc.returncode != 0 or not wcs_path.exists():
                result.elapsed_s = time.monotonic() - started
                tail = "\n".join(proc.stdout.splitlines()[-10:]) if proc.stdout else proc.stderr[-500:]
                result.message = f"no solution (solve-field exit {proc.returncode}) -- {tail}"
                return result

            from astropy.io import fits as _fits
            wcs_header = _fits.getheader(wcs_path)

            try:
                result.solved_ra_h, result.solved_dec_d = _center_radec_from_wcs_header(wcs_header, fits_path)
                _populate_overlay_fields(result, wcs_header, fits_path)
            except Exception as e:
                result.elapsed_s = time.monotonic() - started
                result.message = f"solved but could not evaluate WCS at image center -- {e}"
                return result

        result.elapsed_s = time.monotonic() - started
        result.solved = True
        return result


def solve_one_local(fits_path: Path, solve_timeout: int = 300, index_dir=None, image=None) -> SolveResult:
    """Convenience wrapper mirroring solve_one()'s signature for the local Docker solver."""
    return LocalSolver(index_dir=index_dir, image=image).solve(fits_path, solve_timeout=solve_timeout)
