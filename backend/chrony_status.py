"""
Queries chronyd's sync status via `chronyc -c` (CSV mode) for display in the
frontend. Read-only — never modifies chrony's configuration or state.

Requires chrony to be installed and chronyd running (see
backend/lib/README.md / the GPS passthrough setup for how this ground
station's chrony + gpsd + GPS refclock are wired together). On a machine
without chrony, get_chrony_status() just returns available=False rather
than raising, so the frontend indicator degrades to "not available" instead
of breaking anything.
"""
from __future__ import annotations

import subprocess

_CHRONYC_TIMEOUT_S = 2.0


def get_chrony_status() -> dict:
    """
    Returns:
        {
            "available":       bool          — chronyc ran successfully at all
            "synced":          bool          — system clock is synchronized
            "leap_status":     str            — "Normal" | "Not synchronised" | ...
            "stratum":         int | None
            "ref_name":        str            — name/IP of the currently selected best source
            "using_gps":       bool           — the selected best source is our "GPS" refclock
            "system_offset_s": float | None
            "gps_present":     bool           — a "GPS" refclock source exists in chrony's source list
            "gps_reachable":   bool           — that GPS source has been reached (nonzero reach) at least once
        }
    """
    result = {
        "available": False, "synced": False, "leap_status": "", "stratum": None,
        "ref_name": "", "using_gps": False, "system_offset_s": None,
        "gps_present": False, "gps_reachable": False,
    }

    try:
        tracking = subprocess.run(
            ["chronyc", "-c", "tracking"],
            capture_output=True, text=True, timeout=_CHRONYC_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return result

    if tracking.returncode != 0 or not tracking.stdout.strip():
        return result

    # ref_id, ref_addr, stratum, ref_time, system_offset, last_offset, rms_offset,
    # freq_ppm, resid_freq_ppm, skew_ppm, root_delay, root_dispersion, update_interval, leap_status
    fields = tracking.stdout.strip().split(",")
    if len(fields) < 14:
        return result

    result["available"] = True
    try:
        result["stratum"] = int(fields[2])
        result["system_offset_s"] = float(fields[4])
    except ValueError:
        pass
    result["ref_name"]    = fields[1]
    result["leap_status"] = fields[13]
    result["synced"]      = fields[13] != "Not synchronised"

    try:
        sources = subprocess.run(
            ["chronyc", "-c", "sources"],
            capture_output=True, text=True, timeout=_CHRONYC_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return result

    if sources.returncode == 0:
        for line in sources.stdout.strip().splitlines():
            parts = line.split(",")
            if len(parts) < 6:
                continue
            state, name, reach = parts[1], parts[2], parts[5]
            if name == "GPS":
                result["gps_present"]   = True
                result["gps_reachable"] = reach not in ("", "0")
            if state == "*":
                result["ref_name"]  = name
                result["using_gps"] = (name == "GPS")

    return result
