"""
Plate-solve-and-compare CLI -- read-only measurement of mount pointing
accuracy against captured FITS frames. Never commands the mount.

Thin CLI wrapper around backend/plate_solve.py (the shared solve core used
by this script and by main.py's POST /api/telescope/solve live endpoint).
Useful for batch-checking a directory of existing captures offline; for the
live "solve my last capture" workflow during a flight, use the in-app
button instead.

For each input FITS file:
  1. Submit it to astrometry.net, seeded with the mount/target RA/Dec from
     the header if present (much faster than a blind solve).
  2. Get back the true RA/Dec of the frame center.
  3. Compare against the header's HIERARCH ALTAIR MOUNT RA/DEC (what the
     mount itself reported) and separately TARGET RA/DEC (what
     tracking.py computed from payload GPS) -- two different error
     signals; see the printed report.

Setup (web solver, the default):
    Set the ASTROMETRY_API_KEY environment variable (get a key at
    https://nova.astrometry.net/api_help after creating a free account).
    Never pass the key on the command line -- it'd land in shell history.

Setup (local solver, --method local):
    Docker Desktop running, and ASTROMETRY_INDEX_DIR pointed at a directory
    of downloaded astrometry.net index files sized for this rig's FOV --
    see backend/plate_solve.py's LocalSolver docstring for index selection
    and the Docker image used.

Usage:
    set ASTROMETRY_API_KEY=xxxx   (or export on Linux/macOS)
    python -m backend.solve_check captures/*.fits
    python -m backend.solve_check --method local captures/*.fits

Note: the web method hits a shared public web service with queued jobs --
expect tens of seconds to a few minutes per file, and be considerate of how
many files you submit at once. The local method has no such queue once
index files are cached, but must solve the full field itself each time.
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

from backend.plate_solve import LocalSolver, get_client, solve_one


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="FITS files (globs expanded by the shell or passed literally)")
    ap.add_argument("--timeout", type=int, default=300, help="Per-file solve timeout in seconds (default 300)")
    ap.add_argument("--method", choices=["web", "local"], default="web",
                     help="'web' = astrometry.net API (default), 'local' = self-hosted solve-field via Docker")
    args = ap.parse_args(argv)

    ast = None
    local_solver = None
    if args.method == "web":
        ast = get_client()
        if ast is None:
            print(
                "ASTROMETRY_API_KEY is not set. Get a free key at "
                "https://nova.astrometry.net/api_help and set it as an "
                "environment variable (never pass it on the command line).",
                file=sys.stderr,
            )
            return 2
    else:
        local_solver = LocalSolver()
        not_ready = local_solver.available()
        if not_ready:
            print(f"Local solver not ready: {not_ready}", file=sys.stderr)
            return 2

    paths: list[Path] = []
    for pattern in args.files:
        matches = glob.glob(pattern)
        paths.extend(Path(m) for m in matches) if matches else paths.append(Path(pattern))
    paths = sorted(set(paths))

    if not paths:
        print("No files matched.", file=sys.stderr)
        return 2

    if args.method == "web":
        print(f"astrometry.net web solver -- {len(paths)} file(s)")
        print("(each solve is a queued web job; this can take a while per file)\n")
    else:
        print(f"astrometry.net local solver (Docker) -- {len(paths)} file(s)\n")

    header = (f"{'file':<20} {'solved':<7} {'time_s':>7} {'solved_ra_h':>12} {'solved_dec':>11} "
              f"{'mount_ra_h':>12} {'mount_dec':>11} {'err_vs_mount':>13}  detail")
    print(header)
    print("-" * len(header))

    results = []
    for p in paths:
        if not p.exists():
            print(f"{p.name:<20} {'skip':<7} file not found")
            continue
        r = (solve_one(ast, p, solve_timeout=args.timeout) if args.method == "web"
             else local_solver.solve(p, solve_timeout=args.timeout))
        results.append(r)

        secs = f"{r.elapsed_s:7.1f}" if r.elapsed_s is not None else f"{'--':>7}"
        sra = f"{r.solved_ra_h:12.4f}" if r.solved_ra_h is not None else f"{'--':>12}"
        sdec = f"{r.solved_dec_d:11.4f}" if r.solved_dec_d is not None else f"{'--':>11}"
        mra = f"{r.mount_ra_h:12.4f}" if r.mount_ra_h is not None else f"{'--':>12}"
        mdec = f"{r.mount_dec_d:11.4f}" if r.mount_dec_d is not None else f"{'--':>11}"
        evm = f"{r.err_vs_mount_arcsec:13.1f}" if r.err_vs_mount_arcsec is not None else f"{'--':>13}"
        detail = r.message if not r.solved else (
            "" if (r.target_ra_h is not None or r.mount_ra_h is not None)
            else "solved, but no header pointing to compare against"
        )
        print(f"{r.filename:<20} {('yes' if r.solved else 'no'):<7} {secs} {sra} {sdec} {mra} {mdec} {evm}  {detail}")

    solved = [r for r in results if r.solved]
    print(f"\n{len(solved)}/{len(results)} solved")

    times = [r.elapsed_s for r in results if r.elapsed_s is not None]
    if times:
        times.sort()
        print(f"time per solve: median={times[len(times)//2]:.1f}s  "
              f"min={min(times):.1f}s  max={max(times):.1f}s")

    mount_errs = [r.err_vs_mount_arcsec for r in solved if r.err_vs_mount_arcsec is not None]
    target_errs = [r.err_vs_target_arcsec for r in solved if r.err_vs_target_arcsec is not None]
    if mount_errs:
        mount_errs.sort()
        print(f"err vs MOUNT  (reported):  median={mount_errs[len(mount_errs)//2]:.1f}\"  "
              f"min={min(mount_errs):.1f}\"  max={max(mount_errs):.1f}\"")
    if target_errs:
        target_errs.sort()
        print(f"err vs TARGET (commanded): median={target_errs[len(target_errs)//2]:.1f}\"  "
              f"min={min(target_errs):.1f}\"  max={max(target_errs):.1f}\"")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
