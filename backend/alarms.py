"""
Alarm rule definitions for ALTAIR V2 Ground Station.

Each rule targets a specific packet label + field name and defines one or more
conditions. Rules are evaluated on every incoming packet by AlarmEngine in
logging_manager.py.

Rule types
----------
threshold   — fires when value crosses a hard limit (min/max).
              severity: 'warning' when approaching (within margin%), 'critical' when exceeded.
rate        — fires when |Δvalue/Δtime| exceeds a rate limit (sudden changes).
state       — fires when a float field transitions between enumerated ranges.

Adding new rules
----------------
Append a dict to ALARM_RULES following the schemas below. No other files need
to be changed.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Rule schema reference
# ---------------------------------------------------------------------------
# Threshold rule:
# {
#   "label":    str,          # packet label (e.g. "Power")
#   "field":    str,          # field name (e.g. "voltage_bus")
#   "type":     "threshold",
#   "min":      float | None, # lower hard limit (None = no lower limit)
#   "max":      float | None, # upper hard limit (None = no upper limit)
#   "margin":   float,        # 0–1, fraction of range to warn before limit
#                             # e.g. 0.1 = warn when within 10% of limit
#   "message":  str,          # human-readable alarm description
# }
#
# Rate-of-change rule:
# {
#   "label":    str,
#   "field":    str,
#   "type":     "rate",
#   "max_rate": float,        # absolute value of Δfield/Δt that triggers alarm
#   "message":  str,
# }
#
# State-change rule:
# {
#   "label":    str,
#   "field":    str,
#   "type":     "state",
#   "states":   list[{"name": str, "min": float, "max": float}],
#               # ordered list; first matching band wins
#   "warn_on":  list[str],    # state names that generate 'warning'
#   "crit_on":  list[str],    # state names that generate 'critical'
#   "message":  str,          # template; {state} is substituted
# }
# ---------------------------------------------------------------------------

ALARM_RULES: list[dict] = [

    # ── Power ──────────────────────────────────────────────────────────────
    {
        "label":   "Power",
        "field":   "voltage_bus",
        "type":    "threshold",
        "min":     10.5,    # LiPo 3S cutoff ~10.5 V
        "max":     13.1,    # overcharge threshold
        "margin":  0.10,    # warn within 10% of limit
        "message": "Bus voltage out of range",
    },
    {
        "label":   "Power",
        "field":   "current_total",
        "type":    "threshold",
        "min":     None,
        "max":     15.0,    # A — system rated limit
        "margin":  0.15,
        "message": "Total current draw too high",
    },
    {
        "label":   "Power",
        "field":   "temperature",
        "type":    "threshold",
        "min":     None,
        "max":     70.0,    # degC — power board rated max
        "margin":  0.15,
        "message": "Power board temperature high",
    },

    # ── VESC ───────────────────────────────────────────────────────────────
    {
        "label":   "VESC",
        "field":   "temperature_mos",
        "type":    "threshold",
        "min":     None,
        "max":     85.0,    # degC — MOSFET rated max
        "margin":  0.12,
        "message": "VESC MOSFET temperature high",
    },
    {
        "label":   "VESC",
        "field":   "input_voltage",
        "type":    "threshold",
        "min":     10.5,
        "max":     13.1,
        "margin":  0.10,
        "message": "VESC input voltage out of range",
    },
    {
        "label":   "VESC",
        "field":   "motor_current",
        "type":    "threshold",
        "min":     None,
        "max":     60.0,    # A — motor rated peak
        "margin":  0.15,
        "message": "VESC motor current high",
    },
    {
        "label":   "VESC",
        "field":   "rpm",
        "type":    "rate",
        "max_rate": 5000.0, # rpm/s — sudden RPM spike
        "message": "VESC RPM changed rapidly",
    },

    # ── Attitude ───────────────────────────────────────────────────────────
    {
        "label":   "Attitude",
        "field":   "roll",
        "type":    "threshold",
        "min":     -1.047,  # ±60°  (rad)
        "max":      1.047,
        "margin":  0.10,
        "message": "Roll angle out of bounds",
    },
    {
        "label":   "Attitude",
        "field":   "pitch",
        "type":    "threshold",
        "min":     -0.785,  # ±45° (rad)
        "max":      0.785,
        "margin":  0.10,
        "message": "Pitch angle out of bounds",
    },

    # ── GPS ────────────────────────────────────────────────────────────────
    {
        "label":   "GPS",
        "field":   "relative_alt",
        "type":    "threshold",
        "min":     None,
        "max":     500.0,   # m AGL — operational ceiling
        "margin":  0.10,
        "message": "Payload altitude approaching ceiling",
    },
    {
        "label":   "GPS",
        "field":   "relative_alt",
        "type":    "rate",
        "max_rate": 20.0,   # m/s — unexpected rapid climb/descent
        "message": "Payload altitude changing rapidly",
    },

    # ── Heartbeat ──────────────────────────────────────────────────────────
    {
        "label":   "Heartbeat",
        "field":   "cpu_load_pct",
        "type":    "threshold",
        "min":     None,
        "max":     90.0,    # % of one core (1-min load avg)
        "margin":  0.10,
        "message": "Flight computer CPU load high",
    },
    {
        "label":   "Heartbeat",
        "field":   "mem_used_pct",
        "type":    "threshold",
        "min":     None,
        "max":     85.0,
        "margin":  0.10,
        "message": "Flight computer memory usage high",
    },

    # ── Photodiode — state example ─────────────────────────────────────────
    {
        "label":  "Photodiode",
        "field":  "channel_0",
        "type":   "state",
        "states": [
            {"name": "dark",      "min": 0.0,  "max": 0.1},
            {"name": "low",       "min": 0.1,  "max": 1.0},
            {"name": "nominal",   "min": 1.0,  "max": 3.5},
            {"name": "saturated", "min": 3.5,  "max": 999.0},
        ],
        "warn_on": ["low", "saturated"],
        "crit_on": ["dark"],
        "message": "Photodiode ch0 state: {state}",
    },
]
