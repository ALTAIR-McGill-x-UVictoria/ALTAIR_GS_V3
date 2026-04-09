"""
ALTAIR V2 Ground Station — Packet Emulator

Generates synthetic telemetry packets for all registered packet types and
broadcasts them through the same pipeline as real serial data.  This allows
the full UI (panels, sparklines, alarms, map, threshold gauges) to be tested
without a radio link or flight computer.

Activation
----------
Set the environment variable before starting the backend:

    ALTAIR_DEBUG=1 python -m backend.main

Or toggle at runtime via the REST endpoint:

    POST /api/debug/emulate   {"enabled": true}

Signal model
------------
Each field is driven by a sinusoidal function of time with packet-specific
frequency, amplitude, and phase offset:

    value(t) = base + amplitude * sin(2π * t / period + phase)

- base / amplitude are derived from the alarm rule limits for that
  (label, field) pair, so values sweep through warning and critical zones
  over a ~60 s cycle.
- Fields with no alarm rule oscillate ±10% around zero.
- GPS fields trace a small circle (radius ~1 km) around the ground station
  position so the Map tab and telescope tracking both exercise live data.

Emit rates
----------
    Heartbeat   1 Hz
    Attitude   20 Hz
    Power       5 Hz
    VESC        5 Hz
    Photodiode  5 Hz
    GPS         2 Hz
    (any future packet)  5 Hz  ← default
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from typing import Any, Awaitable, Callable

from backend.packets  import REGISTRY
from backend.alarms   import ALARM_RULES
from backend.tracking import GS_LAT, GS_LON

logger = logging.getLogger("gs.emulator")

# ---------------------------------------------------------------------------
# Emit intervals per packet label (seconds between packets)
# ---------------------------------------------------------------------------
_INTERVALS: dict[str, float] = {
    "Heartbeat":  1.00,
    "Attitude":   0.05,   # 20 Hz
    "Power":      0.20,   # 5 Hz
    "VESC":       0.20,
    "Photodiode": 0.20,
    "GPS":        0.50,   # 2 Hz
    "Event":      1.00,   # 1 Hz — event flags change slowly
}
_DEFAULT_INTERVAL = 0.20

# ---------------------------------------------------------------------------
# Build a lookup: (label, field) -> threshold rule, for value synthesis
# ---------------------------------------------------------------------------
def _build_rule_map() -> dict[tuple[str, str], dict]:
    m: dict[tuple[str, str], dict] = {}
    for rule in ALARM_RULES:
        if rule["type"] == "threshold":
            key = (rule["label"], rule["field"])
            m[key] = rule
    return m

_RULE_MAP = _build_rule_map()

# Sweep period: values complete one full oscillation over this many seconds,
# passing through warning and critical zones.
_SWEEP_PERIOD = 60.0


def _field_params(label: str, field_name: str, field_index: int) -> tuple[float, float]:
    """
    Return (base, amplitude) for a field's sine wave.

    If a threshold rule exists, base = midpoint of [min, max] and amplitude
    slightly exceeds the warning margin so alarms actually fire.
    Otherwise fall back to a small default oscillation.
    """
    rule = _RULE_MAP.get((label, field_name))

    if rule:
        lo = rule.get("min")
        hi = rule.get("max")
        margin = rule.get("margin", 0.10)

        if lo is not None and hi is not None:
            span      = hi - lo
            base      = (lo + hi) / 2.0
            # Amplitude = half-span * 1.15 so we go 15% past the hard limit
            amplitude = span / 2.0 * 1.15
            return base, amplitude

        if hi is not None:
            # Only upper limit: oscillate 0 → 1.2 * hi
            base      = hi * 0.6
            amplitude = hi * 0.65
            return base, amplitude

        if lo is not None:
            # Only lower limit: oscillate around lo
            base      = lo + abs(lo) * 0.5
            amplitude = abs(lo) * 0.65
            return base, amplitude

    # No rule — gentle oscillation around 0, scaled by field index for variety
    return 0.0, 1.0 + field_index * 0.3


# ---------------------------------------------------------------------------
# Balloon flight simulation
# ---------------------------------------------------------------------------
# Timeline (all times in seconds from emulator start):
#   t=0  .. t=30   Pre-flight on the ground
#   t=30 .. t=60   Armed + final checks
#   t=60           Launch
#   t=60 .. t=90   Launch phase (gaining speed)
#   t=90 .. T_APEX Ascent
#   T_APEX         Apogee event (termination or burst, chosen randomly)
#   T_APEX .. T_LAND  Descent (3× slower than ascent)
#   T_LAND         Landing — horizontal movement stops
#   T_LAND+30      Recovery beacon on
#
# Apogee scenario chosen once at import time so the whole session is consistent.
# ---------------------------------------------------------------------------

# Ground-level constants
_M_PER_DEG_LAT = 111_000.0
_M_PER_DEG_LON =  80_000.0   # at 45 °N
_ALT_MSL_LAUNCH = 30.0        # m MSL at launch (Mont-Royal, ~30 m elev.)

# Horizontal wind — slow NE drift with gentle sinusoidal wander
_DRIFT_SPEED_N = 1.5          # m/s northward
_DRIFT_SPEED_E = 2.5          # m/s eastward
_WANDER_R      = 250.0        # wander amplitude (m)
_WANDER_P      = 120.0        # wander period (s)

# Flight timing
_T_LAUNCH  = 60.0             # t at liftoff
_T_ASCENT  = 90.0             # t when ascent phase begins (launch confirmed)
_ASCENT_DUR = 180.0           # seconds of powered ascent (3 min)
_T_APEX    = _T_ASCENT + _ASCENT_DUR   # t=270 s — apogee
_DESCENT_FACTOR = 3.0         # descent takes 3× as long as ascent

_DESCENT_DUR = _ASCENT_DUR * _DESCENT_FACTOR   # 540 s
_T_LAND    = _T_APEX + _DESCENT_DUR             # t=810 s
_T_RECOVERY = _T_LAND + 30.0

# Apogee altitude scenarios — chosen randomly at import
_TERMINATION_ALT = 25_000.0   # matches settings.toml termination_altitude_m
_BURST_ALT       = 30_000.0   # matches settings.toml burst_altitude_m

# True = cutdown fires at _TERMINATION_ALT; False = balloon bursts at _BURST_ALT
_USE_TERMINATION: bool = random.random() < 0.5
_APOGEE_ALT: float = _TERMINATION_ALT if _USE_TERMINATION else _BURST_ALT

logger.info(
    "Emulator flight scenario: %s at %.0f m",
    "TERMINATION" if _USE_TERMINATION else "BURST",
    _APOGEE_ALT,
)

# Ascent rate derived from apogee altitude and ascent duration
_ASCENT_RATE = _APOGEE_ALT / _ASCENT_DUR        # m/s upward
_DESCENT_RATE = _APOGEE_ALT / _DESCENT_DUR       # m/s downward (slower)


def _flight_alt(t: float) -> float:
    """Barometric / GPS altitude MSL as a function of emulator time."""
    if t < _T_LAUNCH:
        return _ALT_MSL_LAUNCH
    if t < _T_APEX:
        # Linear ascent from launch
        elapsed = t - _T_LAUNCH
        return _ALT_MSL_LAUNCH + _ASCENT_RATE * elapsed
    if t < _T_LAND:
        # Linear descent, slower
        elapsed = t - _T_APEX
        return max(_ALT_MSL_LAUNCH, _APOGEE_ALT - _DESCENT_RATE * elapsed)
    return _ALT_MSL_LAUNCH   # on the ground


def _flight_pos(t: float) -> tuple[float, float]:
    """
    Returns (lat, lon) of the balloon at time t.
    Drift continues during flight, freezes at landing position.
    """
    t_move = min(t, _T_LAND)   # stop moving after landing

    # Subtract pre-launch static time so drift starts at liftoff
    t_flying = max(0.0, t_move - _T_LAUNCH)

    wander_n = _WANDER_R * math.sin(2 * math.pi * t_flying / _WANDER_P)
    wander_e = _WANDER_R * math.cos(2 * math.pi * t_flying / _WANDER_P)

    offset_n = _DRIFT_SPEED_N * t_flying + wander_n
    offset_e = _DRIFT_SPEED_E * t_flying + wander_e

    lat = GS_LAT + offset_n / _M_PER_DEG_LAT
    lon = GS_LON + offset_e / _M_PER_DEG_LON
    return lat, lon


def _gps_value(field_name: str, t: float) -> float:
    alt_msl = _flight_alt(t)
    relative_alt = max(0.0, alt_msl - _ALT_MSL_LAUNCH)
    lat, lon = _flight_pos(t)

    if field_name == "lat":
        return lat
    if field_name == "lon":
        return lon
    if field_name == "alt":
        return alt_msl
    if field_name == "relative_alt":
        return relative_alt
    if field_name == "hdg":
        if t >= _T_LAND:
            return 0.0   # stationary — heading undefined, report North
        # Instantaneous heading from drift + wander derivative
        t_flying = max(0.0, t - _T_LAUNCH)
        dw = 2 * math.pi / _WANDER_P
        vel_n = _DRIFT_SPEED_N + _WANDER_R * dw * math.cos(2 * math.pi * t_flying / _WANDER_P)
        vel_e = _DRIFT_SPEED_E - _WANDER_R * dw * math.sin(2 * math.pi * t_flying / _WANDER_P)
        return math.degrees(math.atan2(vel_e, vel_n)) % 360
    return 0.0


def _event_value(field_name: str, t: float) -> float:
    """
    Simulates flight events tied to the altitude-based timeline.

    Termination scenario (cutdown_fired sent by FC at _TERMINATION_ALT):
        The FC detects the altitude threshold and sends the cutdown signal
        (cutdown_fired=1).  A few seconds later, the confirmed altitude drop
        sets termination_fired=1 and advances to stage 4.

    Burst scenario:
        The balloon reaches _BURST_ALT and pops naturally.
        burst_detected=1, stage→5.  No cutdown signal is sent.

    Both scenarios then follow the same descent/landing/recovery path.
    """
    # Derived event timestamps
    t_cutdown  = _T_APEX - 5.0   # FC sends cutdown signal 5 s before measured apogee
    t_term_confirm = _T_APEX      # termination confirmed at apogee (altitude drop evident)
    t_descent  = _T_APEX + 5.0   # descent phase begins shortly after apogee
    t_ascent_end = _T_APEX
    t_landing  = _T_LAND
    t_recovery = _T_RECOVERY

    # Flight stage
    if field_name == "flight_stage":
        if _USE_TERMINATION:
            milestones = [
                (0,              0),   # Pre-flight
                (30,             1),   # Armed
                (_T_LAUNCH,      2),   # Launch
                (_T_ASCENT,      3),   # Ascent
                (t_term_confirm, 4),   # Termination
                (t_descent,      6),   # Descent
                (t_landing,      7),   # Landing
                (t_recovery,     8),   # Recovery
            ]
        else:
            milestones = [
                (0,          0),
                (30,         1),
                (_T_LAUNCH,  2),
                (_T_ASCENT,  3),
                (_T_APEX,    5),   # Burst
                (t_descent,  6),
                (t_landing,  7),
                (t_recovery, 8),
            ]
        stage = 0
        for ts, s in milestones:
            if t >= ts:
                stage = s
        return float(stage)

    if field_name == "arm_state":
        return 1.0 if t >= 30 else 0.0
    if field_name == "launch_detected":
        return 1.0 if t >= _T_LAUNCH else 0.0
    if field_name == "ascent_active":
        return 1.0 if _T_ASCENT <= t < t_ascent_end else 0.0
    if field_name == "data_logging_active":
        return 1.0 if t >= 10 else 0.0

    # Apogee-scenario-specific flags
    if _USE_TERMINATION:
        if field_name == "cutdown_fired":
            # FC sends cutdown signal when it detects termination_altitude_m
            return 1.0 if t >= t_cutdown else 0.0
        if field_name == "termination_fired":
            # Confirmed by observed altitude drop a few seconds later
            return 1.0 if t >= t_term_confirm else 0.0
        if field_name == "burst_detected":
            return 0.0
    else:
        if field_name == "cutdown_fired":
            return 0.0   # no cutdown in burst scenario
        if field_name == "termination_fired":
            return 0.0
        if field_name == "burst_detected":
            return 1.0 if t >= _T_APEX else 0.0

    if field_name == "descent_active":
        return 1.0 if t_descent <= t < t_landing else 0.0
    if field_name == "landing_detected":
        return 1.0 if t >= t_landing else 0.0
    if field_name == "recovery_active":
        return 1.0 if t >= t_recovery else 0.0

    return 0.0


def _environment_value(field_name: str, t: float) -> float:
    """Drive Environment packet fields from the flight simulation."""
    alt = _flight_alt(t)
    if field_name == "baro_alt":
        return alt
    if field_name == "climb":
        # Positive = ascending, negative = descending, 0 on ground / after landing
        if t < _T_LAUNCH:
            return 0.0
        if t < _T_APEX:
            return _ASCENT_RATE
        if t < _T_LAND:
            return -_DESCENT_RATE
        return 0.0
    # Remaining fields (pressure, temperature, airspeed, groundspeed) use generic oscillator
    return None   # sentinel — caller falls back to sine wave


def _heartbeat_value(field_name: str, t: float) -> float:
    """Heartbeat uses realistic system-metric values."""
    if field_name == "time_unix":
        return time.time()
    if field_name == "uptime_s":
        return t
    if field_name == "cpu_load_pct":
        # Oscillates 5 % → 95 % over sweep period
        return 50.0 + 45.0 * math.sin(2 * math.pi * t / _SWEEP_PERIOD)
    if field_name == "mem_used_pct":
        return 40.0 + 40.0 * math.sin(2 * math.pi * t / _SWEEP_PERIOD + 1.0)
    if field_name == "tasks_running":
        return 5.0
    return 0.0


# ---------------------------------------------------------------------------
# Emulator
# ---------------------------------------------------------------------------

BroadcastFn = Callable[[dict[str, Any]], Awaitable[None]]


class PacketEmulator:
    """
    Asyncio-based packet emulator.

    Usage:
        emulator = PacketEmulator(serial_reader._broadcast)
        await emulator.start()
        # ... later ...
        await emulator.stop()
    """

    def __init__(self, broadcast_fn: BroadcastFn) -> None:
        self._broadcast = broadcast_fn
        self._tasks:    list[asyncio.Task] = []
        self._start_t:  float = 0.0
        self._running:  bool  = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._start_t = time.monotonic()
        logger.info("Packet emulator starting — %d packet type(s) registered", len(REGISTRY))

        for pid, entry in REGISTRY.items():
            interval = _INTERVALS.get(entry["label"], _DEFAULT_INTERVAL)
            task = asyncio.create_task(
                self._emit_loop(pid, entry, interval),
                name=f"emulator_{entry['label']}",
            )
            self._tasks.append(task)

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Packet emulator stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _emit_loop(self, pid: int, entry: dict, interval_s: float) -> None:
        seq = 0
        label = entry["label"]
        logger.debug("Emulator loop started: %s @ %.2f Hz", label, 1.0 / interval_s)

        while self._running:
            t = time.monotonic() - self._start_t
            packet = self._generate(pid, entry, t, seq)
            await self._broadcast(packet)
            seq = (seq + 1) & 0xFF
            await asyncio.sleep(interval_s)

    def _generate(self, pid: int, entry: dict, t: float, seq: int) -> dict:
        label  = entry["label"]
        fields = []

        for i, fd in enumerate(entry["fields"]):
            name = fd["name"]

            if label.lower() == "gps":
                value = _gps_value(name, t)
            elif label == "Heartbeat":
                value = _heartbeat_value(name, t)
            elif label == "Event":
                value = _event_value(name, t)
            elif label == "Environment":
                env_val = _environment_value(name, t)
                if env_val is not None:
                    value = env_val
                else:
                    base, amplitude = _field_params(label, name, i)
                    phase = i * (2 * math.pi / max(len(entry["fields"]), 1))
                    value = base + amplitude * math.sin(2 * math.pi * t / _SWEEP_PERIOD + phase)
            else:
                base, amplitude = _field_params(label, name, i)
                # Each field gets a distinct phase so they don't all peak together
                phase = i * (2 * math.pi / max(len(entry["fields"]), 1))
                value = base + amplitude * math.sin(2 * math.pi * t / _SWEEP_PERIOD + phase)

            fields.append({
                "name":  name,
                "label": fd["label"],
                "unit":  fd["unit"],
                "value": round(value, 6),
            })

        return {
            "type":      "packet",
            "packet_id": pid,
            "label":     label,
            "seq":       seq,
            "timestamp": round(t, 4),
            "wall_ms":   round(time.time() * 1000),
            "fields":    fields,
            "dropped":   0,
        }
