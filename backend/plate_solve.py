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

Uses astroquery's AstrometryNet client rather than a local ASTAP install:
on the frames this was validated against (very wide field, ~36"/px) ASTAP
could not find a solution at any field-of-view or star database tried,
while the astrometry.net web solver succeeded. The discrepancy was never
root-caused, so don't assume a local solver would behave the same on this
data if ASTAP is revisited later.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path


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
        result.message = f"timed out after {solve_timeout}s (submission id: {submission_id})"
        return result
    except Exception as e:
        result.message = f"solve request failed -- {e}"
        return result

    if not wcs_header:
        result.message = "no solution (astrometry.net could not solve this field)"
        return result

    # IMPORTANT: CRVAL1/2 is the WCS reference pixel's coordinate, NOT the
    # image center -- astrometry.net's solver picks CRPIX wherever the
    # matched star quad landed, which is frequently off-center (confirmed:
    # one solve here put CRPIX at (1975, 834) on a 3840x2160 frame). Using
    # CRVAL directly as "where the frame points" silently measures the
    # wrong point. Evaluate the WCS at the true image-center pixel instead.
    try:
        import warnings

        from astropy.io import fits as _fits
        from astropy.wcs import WCS as _WCS, FITSFixedWarning

        with _fits.open(fits_path) as hdul:
            naxis1 = hdul[0].header.get("NAXIS1")
            naxis2 = hdul[0].header.get("NAXIS2")
        # astrometry.net's WCS header carries WCSAXES=2 but no NAXIS/pixel
        # array of its own (it's a bare header, not a full HDU) -- harmless,
        # astropy just wants to warn about it.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FITSFixedWarning)
            w = _WCS(wcs_header)
        center_ra_deg, center_dec_deg = w.all_pix2world(naxis1 / 2.0, naxis2 / 2.0, 0)
        result.solved_ra_h  = float(center_ra_deg) / 15.0
        result.solved_dec_d = float(center_dec_deg)
    except Exception as e:
        result.message = f"solved but could not evaluate WCS at image center -- {e}"
        return result

    result.solved = True
    return result
