"""
ALTAIR V2 Ground Station — Event definitions.

Events differ from alarms: they represent discrete state transitions in the
flight mission rather than out-of-range sensor readings.  An event fires once
when a boolean flag transitions 0→1 or 1→0, or when flight_stage changes.

EVENT_DEFS
----------
Maps (field_name, new_value) -> human-readable log message.
  - Boolean fields use new_value = 1 (asserted) or 0 (de-asserted).
  - flight_stage uses new_value = the integer stage number.
  - A missing (field, value) pair is silently ignored (no log entry).

FLIGHT_STAGE_NAMES
------------------
Maps flight_stage integer → display name shown in the dashboard.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Flight stage enumeration (9 stages)
# ---------------------------------------------------------------------------
FLIGHT_STAGE_NAMES: dict[int, str] = {
    0: "Pre-flight",
    1: "Armed",
    2: "Launch",
    3: "Ascent",
    4: "Termination",
    5: "Burst",
    6: "Descent",
    7: "Landing",
    8: "Recovery",
}

# ---------------------------------------------------------------------------
# Boolean event flag messages
# (field_name, new_int_value) → message string
# ---------------------------------------------------------------------------
EVENT_DEFS: dict[tuple[str, int], str] = {
    # arm_state
    ("arm_state",           1): "Motors armed",
    ("arm_state",           0): "Motors disarmed",

    # launch_detected
    ("launch_detected",     1): "Launch detected — liftoff confirmed",

    # ascent_active
    ("ascent_active",       1): "Ascent phase begun",
    ("ascent_active",       0): "Ascent phase ended",

    # termination_fired  (cutdown confirmed by observed altitude drop)
    ("termination_fired",   1): "Termination mechanism fired — descent initiated",

    # burst_detected  (natural balloon burst or unconfirmed cutdown)
    ("burst_detected",      1): "Balloon burst detected — natural apogee",

    # descent_active
    ("descent_active",      1): "Descent phase begun — parachute deployment expected",
    ("descent_active",      0): "Descent phase ended",

    # landing_detected
    ("landing_detected",    1): "Landing detected — touchdown confirmed",

    # cutdown_fired  (mechanism triggered; confirmation via termination_fired)
    ("cutdown_fired",       1): "Cutdown mechanism triggered",

    # recovery_active
    ("recovery_active",     1): "Recovery beacon activated",
    ("recovery_active",     0): "Recovery beacon deactivated",

    # data_logging_active
    ("data_logging_active", 1): "Onboard data logging started",
    ("data_logging_active", 0): "Onboard data logging stopped",
}

# ---------------------------------------------------------------------------
# Fields that are boolean flags (0/1) — used to detect transitions
# All other numeric fields in the Events packet are treated as enums.
# ---------------------------------------------------------------------------
BOOLEAN_EVENT_FIELDS: frozenset[str] = frozenset([
    "arm_state",
    "launch_detected",
    "ascent_active",
    "termination_fired",
    "burst_detected",
    "descent_active",
    "landing_detected",
    "cutdown_fired",
    "recovery_active",
    "data_logging_active",
])
