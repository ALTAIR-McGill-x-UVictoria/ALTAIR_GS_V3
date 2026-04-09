"""
ALTAIR V2 Ground Station — Telemetry Logger + Alarm Engine

Responsibilities
----------------
1. CSV logging
   - One CSV file per packet label, written to:
       logs/<session>/csv/<Label>.csv
   - Columns: wall_time_utc, seq, timestamp, <field1>, <field2>, ...
   - File created on the first received packet of that type.
   - Headers are written once; subsequent rows are appended.

2. Alarm engine
   - Evaluates ALARM_RULES (defined in backend/alarms.py) on every packet.
   - Supports three rule types: threshold, rate-of-change, state.
   - Fires an alarm event dict that is:
       a) written to logs/<session>/alarms.log  (plain-text, one line per event)
       b) returned to the caller so it can be broadcast over WebSocket

3. Session management
   - A new session directory is created by calling open_session().
   - Sessions are named by UTC timestamp: 2025-01-15_143022
   - Calling close_session() flushes and closes all open CSV files.

Directory layout
----------------
logs/
└── 2025-01-15_143022/          ← session dir (one per GS run)
    ├── csv/
    │   ├── Attitude.csv
    │   ├── Power.csv
    │   ├── VESC.csv
    │   ├── Photodiode.csv
    │   ├── GPS.csv
    │   └── Heartbeat.csv
    └── alarms.log

Usage (in main.py)
------------------
    from backend.logging_manager import TelemetryLogger
    _telem_logger = TelemetryLogger()
    _telem_logger.open_session()

    # Inside _broadcast(), after a packet is decoded:
    alarms = _telem_logger.ingest(packet_dict)
    for alarm in alarms:
        await _broadcast_alarm(alarm)

    # On shutdown:
    _telem_logger.close_session()
"""
from __future__ import annotations

import csv
import io
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.alarms import ALARM_RULES

logger = logging.getLogger("gs.logger")

_REPO_ROOT = Path(__file__).parent.parent
_LOGS_ROOT = _REPO_ROOT / "logs"


# ---------------------------------------------------------------------------
# Alarm engine
# ---------------------------------------------------------------------------

class AlarmEngine:
    """
    Stateful evaluator for ALARM_RULES.

    State tracked per (label, field):
      - last value and timestamp (for rate-of-change)
      - last fired severity (to detect transitions and avoid re-firing)
      - last state name (for state rules)
    """

    def __init__(self) -> None:
        # (label, field) -> {"last_value": float, "last_ts": float,
        #                    "last_severity": str | None, "last_state": str | None}
        self._state: dict[tuple[str, str], dict] = {}

    def reset(self) -> None:
        self._state.clear()

    def evaluate(self, packet: dict[str, Any]) -> list[dict]:
        """
        Evaluate all rules that match this packet's label.
        Returns a (possibly empty) list of alarm event dicts:
        {
          "label":    str,   # packet label
          "field":    str,   # field name
          "value":    float,
          "severity": "warning" | "critical" | "ok",
          "message":  str,
          "rule_type": str,
          "timestamp": float,  # packet timestamp
        }
        Only emits an event when severity *changes* (avoids flooding).
        """
        label     = packet.get("label", "")
        timestamp = packet.get("timestamp", 0.0)
        fields    = {f["name"]: f["value"] for f in packet.get("fields", [])}
        events    = []

        for rule in ALARM_RULES:
            if rule["label"] != label:
                continue
            field_name = rule["field"]
            if field_name not in fields:
                continue

            value   = fields[field_name]
            key     = (label, field_name)
            st      = self._state.setdefault(key, {
                "last_value":    None,
                "last_ts":       None,
                "last_severity": None,
                "last_state":    None,
            })

            rule_type = rule["type"]

            if rule_type == "threshold":
                severity = self._eval_threshold(value, rule)
            elif rule_type == "rate":
                severity = self._eval_rate(value, timestamp, st, rule)
            elif rule_type == "state":
                severity, state_name = self._eval_state(value, rule)
                # Store current state for message formatting
                st["last_state"] = state_name
            else:
                continue

            # First evaluation: record baseline silently — don't fire on defaults/zero
            if st["last_severity"] is None:
                st["last_severity"] = severity
            # Emit only when severity actually changes after baseline is established
            elif severity != st["last_severity"]:
                st["last_severity"] = severity
                msg = rule["message"]
                if rule_type == "state":
                    msg = msg.format(state=st["last_state"])
                events.append({
                    "label":     label,
                    "field":     field_name,
                    "value":     value,
                    "severity":  severity,
                    "message":   msg,
                    "rule_type": rule_type,
                    "timestamp": timestamp,
                })

            st["last_value"] = value
            st["last_ts"]    = timestamp

        return events

    # ------------------------------------------------------------------
    # Rule evaluators
    # ------------------------------------------------------------------

    @staticmethod
    def _eval_threshold(value: float, rule: dict) -> str:
        lo       = rule.get("min")
        hi       = rule.get("max")
        margin   = rule.get("margin", 0.10)

        # Compute warning zone edges
        lo_warn = None
        hi_warn = None
        if lo is not None and hi is not None:
            span = hi - lo
            lo_warn = lo + span * margin
            hi_warn = hi - span * margin
        elif lo is not None:
            lo_warn = lo + abs(lo) * margin
        elif hi is not None:
            hi_warn = hi - abs(hi) * margin

        # Hard limit check (critical)
        if lo is not None and value < lo:
            return "critical"
        if hi is not None and value > hi:
            return "critical"

        # Warning zone check
        if lo_warn is not None and value < lo_warn:
            return "warning"
        if hi_warn is not None and value > hi_warn:
            return "warning"

        return "ok"

    @staticmethod
    def _eval_rate(value: float, timestamp: float, st: dict, rule: dict) -> str:
        last_v  = st["last_value"]
        last_ts = st["last_ts"]

        if last_v is None or last_ts is None:
            return "ok"

        dt = timestamp - last_ts
        if dt <= 0:
            return "ok"

        rate = abs(value - last_v) / dt
        if rate > rule["max_rate"]:
            return "warning"
        return "ok"

    @staticmethod
    def _eval_state(value: float, rule: dict) -> tuple[str, str]:
        state_name = "unknown"
        for band in rule["states"]:
            if band["min"] <= value < band["max"]:
                state_name = band["name"]
                break

        if state_name in rule.get("crit_on", []):
            return "critical", state_name
        if state_name in rule.get("warn_on", []):
            return "warning", state_name
        return "ok", state_name


# ---------------------------------------------------------------------------
# CSV logger
# ---------------------------------------------------------------------------

class _CsvFile:
    """Manages a single open CSV file for one packet label."""

    def __init__(self, path: Path, field_names: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path   = path
        self._fh     = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)

        header = ["wall_time_utc", "seq", "timestamp"] + field_names
        self._writer.writerow(header)
        self._fh.flush()
        self._fields = field_names
        logger.info("CSV log opened: %s", path)

    def write(self, packet: dict[str, Any]) -> None:
        wall = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        seq  = packet.get("seq", "")
        ts   = packet.get("timestamp", "")
        fmap = {f["name"]: f["value"] for f in packet.get("fields", [])}
        row  = [wall, seq, ts] + [fmap.get(n, "") for n in self._fields]
        self._writer.writerow(row)

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        self._fh.flush()
        self._fh.close()
        logger.info("CSV log closed: %s", self._path)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

class TelemetryLogger:
    """
    Combined CSV logger and alarm engine.

    Call open_session() once when telemetry starts (or at GS startup).
    Call ingest(packet) for every decoded packet — returns any alarm events.
    Call close_session() on shutdown.
    """

    def __init__(self) -> None:
        self._session_dir: Path | None = None
        self._csv_files:   dict[str, _CsvFile] = {}
        self._alarm_log_fh: io.TextIOWrapper | None = None
        self._alarm_engine  = AlarmEngine()
        self._flush_counter = 0
        self._FLUSH_EVERY   = 10   # flush to disk every N packets

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def open_session(self) -> Path:
        """
        Create a new session directory under logs/ and open alarms.log.
        Returns the session Path.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        self._session_dir = _LOGS_ROOT / stamp
        csv_dir           = self._session_dir / "csv"
        csv_dir.mkdir(parents=True, exist_ok=True)

        alarm_path = self._session_dir / "alarms.log"
        self._alarm_log_fh = open(alarm_path, "w", encoding="utf-8")
        self._alarm_log_fh.write(
            f"# ALTAIR V2 Alarm Log — session {stamp}\n"
            "# wall_time_utc | severity | label.field | value | message\n"
        )
        self._alarm_log_fh.flush()

        self._csv_files.clear()
        self._alarm_engine.reset()
        logger.info("Telemetry log session opened: %s", self._session_dir)
        return self._session_dir

    def close_session(self) -> None:
        for f in self._csv_files.values():
            f.close()
        self._csv_files.clear()

        if self._alarm_log_fh:
            self._alarm_log_fh.flush()
            self._alarm_log_fh.close()
            self._alarm_log_fh = None

        logger.info("Telemetry log session closed: %s", self._session_dir)
        self._session_dir = None

    # ------------------------------------------------------------------
    # Packet ingestion
    # ------------------------------------------------------------------

    def ingest(self, packet: dict[str, Any]) -> list[dict]:
        """
        Process one decoded packet dict (as emitted by decode_frame()).
        - Appends a row to the appropriate CSV file.
        - Evaluates alarm rules.
        - Writes any triggered alarms to alarms.log.
        Returns list of alarm event dicts (may be empty).
        """
        if self._session_dir is None:
            return []

        label = packet.get("label", "unknown")
        self._csv_write(label, packet)

        alarms = self._alarm_engine.evaluate(packet)
        for ev in alarms:
            self._write_alarm(ev)

        # Periodic flush
        self._flush_counter += 1
        if self._flush_counter >= self._FLUSH_EVERY:
            self._flush_counter = 0
            for f in self._csv_files.values():
                f.flush()

        return alarms

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _csv_write(self, label: str, packet: dict[str, Any]) -> None:
        if label not in self._csv_files:
            csv_path    = self._session_dir / "csv" / f"{label}.csv"
            field_names = [f["name"] for f in packet.get("fields", [])]
            self._csv_files[label] = _CsvFile(csv_path, field_names)

        self._csv_files[label].write(packet)

    def _write_alarm(self, ev: dict) -> None:
        if self._alarm_log_fh is None:
            return

        wall = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        line = (
            f"{wall} | {ev['severity'].upper():<8} | "
            f"{ev['label']}.{ev['field']} = {ev['value']:.4g} | "
            f"{ev['message']}\n"
        )
        self._alarm_log_fh.write(line)
        self._alarm_log_fh.flush()
        logger.warning("ALARM [%s] %s.%s=%.4g — %s",
                       ev["severity"], ev["label"], ev["field"],
                       ev["value"],   ev["message"])
