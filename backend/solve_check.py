"""
Plate-solve-and-compare harness — read-only measurement of mount pointing
accuracy against captured FITS frames. Never commands the mount.

Purpose: verify the solve half of a future auto-alignment pipeline is
correct (star detection, ASTAP invocation, WCS parsing, coordinate math)
using existing captures, entirely independent of the AM3/AM5 mount and
usable before ever connecting to one. See backend/lib/README.md for the
ASTAP install this depends on.

For each input FITS file:
  1. Run ASTAP (astap_cli.exe / astap_cli) against it, seeded with the
     commanded target RA/Dec from the header if present (much faster and
     more reliable than a blind solve when a rough position is known).
  2. Parse the solved RA/Dec out of ASTAP's .ini result file.
  3. Compare solved vs. the header's HIERARCH ALTAIR TARGET RA/DEC (what
     tracking.py computed the mount should point at) and separately vs.
     HIERARCH ALTAIR MOUNT RA/DEC (what the mount itself reported) --
     these are two different error signals; see the printed report.

Usage:
    python -m backend.solve_check captures/*.fits
    python -m backend.solve_check --astap "C:\\Program Files\\astap\\astap_cli.exe" captures/manual*.fits

Bayer (un-demosaiced OSC) frames -- see camera_canon.py -- are solved with
ASTAP's -check flag, which is designed for exactly this case (raw one-shot-
colour data at 1x1 binning, no debayer needed for star centroiding).
"""
from __future__ import annotations

import argparse
import glob
import math
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


def _find_astap() -> str | None:
    """Locate the ASTAP command-line solver. Windows install path first,
    then PATH, matching how backend/camera.py resolves the ASI SDK."""
    candidates = [
        r"C:\Program Files\astap\astap_cli.exe",
        r"C:\Program Files (x86)\astap\astap_cli.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    for name in ("astap_cli", "astap_cli.exe", "astap"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _angsep_deg(ra1_h: float, dec1_d: float, ra2_h: float, dec2_d: float) -> float:
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
    stars_used: int | None = None
    target_ra_h: float | None = None
    target_dec_d: float | None = None
    mount_ra_h: float | None = None
    mount_dec_d: float | None = None

    @property
    def err_vs_target_arcsec(self) -> float | None:
        if self.solved_ra_h is None or self.target_ra_h is None:
            return None
        return _angsep_deg(self.solved_ra_h, self.solved_dec_d, self.target_ra_h, self.target_dec_d) * 3600

    @property
    def err_vs_mount_arcsec(self) -> float | None:
        if self.solved_ra_h is None or self.mount_ra_h is None:
            return None
        return _angsep_deg(self.solved_ra_h, self.solved_dec_d, self.mount_ra_h, self.mount_dec_d) * 3600


def _read_header_pointing(path: Path) -> dict:
    from astropy.io import fits

    hdr = fits.getheader(path)

    def g(key: str):
        return hdr.get(f"HIERARCH ALTAIR {key}", hdr.get(f"ALTAIR {key}"))

    return {
        "target_ra_h":  g("TARGET RA"),
        "target_dec_d": g("TARGET DEC"),
        "mount_ra_h":   g("MOUNT RA"),
        "mount_dec_d":  g("MOUNT DEC"),
        "is_bayer":     bool(hdr.get("BAYERPAT")),
    }


def _run_astap(astap_path: str, fits_path: Path, out_base: Path, hint: dict) -> subprocess.CompletedProcess:
    cmd = [astap_path, "-f", str(fits_path), "-o", str(out_base), "-wcs"]

    # Seed the search near the commanded/reported position when we have one
    # -- this is a *search hint*, not ground truth, so it doesn't bias the
    # measurement: ASTAP still solves independently from star positions and
    # simply searches a small radius around the hint first. Falls back to a
    # wider blind-ish search if neither is present.
    ra_hint  = hint.get("target_ra_h")  if hint.get("target_ra_h")  is not None else hint.get("mount_ra_h")
    dec_hint = hint.get("target_dec_d") if hint.get("target_dec_d") is not None else hint.get("mount_dec_d")
    if ra_hint is not None and dec_hint is not None:
        cmd += ["-ra", str(ra_hint), "-spd", str(90.0 - dec_hint), "-r", "10"]

    if hint.get("is_bayer"):
        cmd += ["-check"]

    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def solve_one(astap_path: str, fits_path: Path) -> SolveResult:
    hdr_info = _read_header_pointing(fits_path)
    result = SolveResult(
        filename=fits_path.name,
        solved=False,
        target_ra_h=hdr_info["target_ra_h"],
        target_dec_d=hdr_info["target_dec_d"],
        mount_ra_h=hdr_info["mount_ra_h"],
        mount_dec_d=hdr_info["mount_dec_d"],
    )

    with tempfile.TemporaryDirectory() as td:
        out_base = Path(td) / fits_path.stem
        try:
            proc = _run_astap(astap_path, fits_path, out_base, hdr_info)
        except subprocess.TimeoutExpired:
            result.message = "ASTAP timed out after 120s"
            return result

        ini_path = out_base.with_suffix(".ini")
        if not ini_path.exists():
            # ASTAP normally writes filename.ini (PLTSOLVD=F) even when it
            # can't solve -- a missing .ini means it didn't run at all
            # (bad path, crash, etc.), which is worth distinguishing from a
            # clean "no solution" result.
            tail = (proc.stdout or "")[-400:] + (proc.stderr or "")[-400:]
            result.message = f"ASTAP did not produce a .ini file at all -- {tail.strip()}"
            return result

        values = {}
        for line in ini_path.read_text(errors="replace").splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip().strip('"')

        # PLTSOLVD=F carries no separate error/reason key -- the reason
        # lives only in stdout ("No solution found!", "Not enough stars
        # detected", a crash message, ...), so surface the tail of that
        # instead of a key that doesn't exist.
        if values.get("PLTSOLVD", "").upper() not in ("TRUE", "T"):
            stdout_tail = (proc.stdout or "").strip().splitlines()
            reason = stdout_tail[-1] if stdout_tail else "(no output)"
            result.message = f"no solution -- {reason}"
            return result

        try:
            result.solved_ra_h  = float(values["CRVAL1"]) / 15.0   # CRVAL1 is degrees
            result.solved_dec_d = float(values["CRVAL2"])
        except (KeyError, ValueError) as e:
            result.message = f"solved but could not parse CRVAL1/2 -- {e}"
            return result

        result.stars_used = int(values.get("STARS", 0) or 0)
        result.solved = True
        return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="FITS files (globs expanded by the shell or passed literally)")
    ap.add_argument("--astap", default=None, help="Path to astap_cli(.exe); auto-detected if omitted")
    args = ap.parse_args(argv)

    astap_path = args.astap or _find_astap()
    if not astap_path:
        print(
            "ASTAP not found. Install it (https://www.hnsky.org/astap.htm or the "
            "SourceForge mirror) and a star database, or pass --astap <path to astap_cli.exe>.",
            file=sys.stderr,
        )
        return 2

    # Expand any globs the shell didn't (e.g. when called from Python directly).
    paths: list[Path] = []
    for pattern in args.files:
        matches = glob.glob(pattern)
        paths.extend(Path(m) for m in matches) if matches else paths.append(Path(pattern))
    paths = sorted(set(paths))

    if not paths:
        print("No files matched.", file=sys.stderr)
        return 2

    print(f"ASTAP: {astap_path}")
    print(f"{len(paths)} file(s)\n")

    header = (f"{'file':<20} {'solved':<7} {'stars':>5} {'err_vs_target':>14} "
              f"{'err_vs_mount':>13}  detail")
    print(header)
    print("-" * len(header))

    results: list[SolveResult] = []
    for p in paths:
        if not p.exists():
            print(f"{p.name:<20} {'skip':<7} {'':>5} {'':>14} {'':>13}  file not found")
            continue
        r = solve_one(astap_path, p)
        results.append(r)

        evt = f"{r.err_vs_target_arcsec:14.1f}" if r.err_vs_target_arcsec is not None else f"{'--':>14}"
        evm = f"{r.err_vs_mount_arcsec:13.1f}" if r.err_vs_mount_arcsec is not None else f"{'--':>13}"
        detail = r.message if not r.solved else (
            "" if (r.target_ra_h is not None or r.mount_ra_h is not None)
            else "solved, but no header pointing to compare against"
        )
        print(f"{r.filename:<20} {('yes' if r.solved else 'no'):<7} "
              f"{(r.stars_used if r.stars_used is not None else ''):>5} {evt} {evm}  {detail}")

    solved = [r for r in results if r.solved]
    print(f"\n{len(solved)}/{len(results)} solved")

    target_errs = [r.err_vs_target_arcsec for r in solved if r.err_vs_target_arcsec is not None]
    mount_errs  = [r.err_vs_mount_arcsec  for r in solved if r.err_vs_mount_arcsec  is not None]
    if target_errs:
        target_errs.sort()
        print(f"err vs TARGET (commanded): median={target_errs[len(target_errs)//2]:.1f}\"  "
              f"min={min(target_errs):.1f}\"  max={max(target_errs):.1f}\"")
    if mount_errs:
        mount_errs.sort()
        print(f"err vs MOUNT  (reported):  median={mount_errs[len(mount_errs)//2]:.1f}\"  "
              f"min={min(mount_errs):.1f}\"  max={max(mount_errs):.1f}\"")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
