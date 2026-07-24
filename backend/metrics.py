"""
Best-effort Prometheus exporter for ground-station telemetry.

The telemetry path never performs network or disk I/O for observability. Updates
are held in memory and use a non-blocking lock; if a Prometheus scrape is taking
a snapshot at the same instant, that one update is skipped rather than delaying
packet processing.
"""
from __future__ import annotations

import math
import os
import threading
import time
from collections import defaultdict
from typing import Any


PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _env_enabled(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _escape_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**labels: str) -> str:
    body = ",".join(
        f'{key}="{_escape_label(value)}"' for key, value in sorted(labels.items())
    )
    return "{" + body + "}"


class TelemetryMetrics:
    """Small, dependency-free Prometheus collector for latest telemetry values."""

    def __init__(self, enabled: bool | None = None) -> None:
        self.enabled = (
            _env_enabled("ALTAIR_METRICS_ENABLED", default=False)
            if enabled is None
            else enabled
        )
        self._lock = threading.Lock()
        self._telemetry: dict[tuple[str, str, str], float] = {}
        self._packet_count: defaultdict[str, int] = defaultdict(int)
        self._packet_dropped: defaultdict[str, int] = defaultdict(int)
        self._packet_last_received: dict[str, float] = {}
        self._alarm_count: defaultdict[tuple[str, str, str], int] = defaultdict(int)
        self._event_count: defaultdict[tuple[str, str], int] = defaultdict(int)
        self._connected = 0.0
        self._emulating = 0.0
        self._skipped_updates = 0

    def observe_message(self, message: dict[str, Any]) -> None:
        """Observe a broadcast message without ever raising into core processing."""
        if not self.enabled:
            return
        try:
            message_type = message.get("type")
            if message_type == "packet":
                self._observe_packet(message)
            elif message_type == "status":
                self._observe_status(message)
        except Exception:
            self._skipped_updates += 1

    def observe_alarm(self, alarm: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            key = (
                str(alarm.get("label", "unknown")),
                str(alarm.get("field", "unknown")),
                str(alarm.get("severity", "unknown")),
            )
        except Exception:
            self._skipped_updates += 1
            return
        if not self._lock.acquire(blocking=False):
            self._skipped_updates += 1
            return
        try:
            self._alarm_count[key] += 1
        finally:
            self._lock.release()

    def observe_event(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            key = (str(event.get("field", "unknown")), str(event.get("new_val", "unknown")))
        except Exception:
            self._skipped_updates += 1
            return
        if not self._lock.acquire(blocking=False):
            self._skipped_updates += 1
            return
        try:
            self._event_count[key] += 1
        finally:
            self._lock.release()

    def _observe_packet(self, packet: dict[str, Any]) -> None:
        label = str(packet.get("label", "unknown"))
        values: list[tuple[tuple[str, str, str], float]] = []
        for field in packet.get("fields", []):
            try:
                value = float(field["value"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            values.append(
                (
                    (
                        label,
                        str(field.get("name", "unknown")),
                        str(field.get("unit", "")),
                    ),
                    value,
                )
            )

        if not self._lock.acquire(blocking=False):
            self._skipped_updates += 1
            return
        try:
            for key, value in values:
                self._telemetry[key] = value
            self._packet_count[label] += 1
            dropped = packet.get("dropped", 0)
            try:
                self._packet_dropped[label] += max(0, int(dropped))
            except (TypeError, ValueError):
                pass
            self._packet_last_received[label] = time.time()
        finally:
            self._lock.release()

    def _observe_status(self, status: dict[str, Any]) -> None:
        if not self._lock.acquire(blocking=False):
            self._skipped_updates += 1
            return
        try:
            self._connected = 1.0 if status.get("connected") else 0.0
            if "emulating" in status:
                self._emulating = 1.0 if status.get("emulating") else 0.0
        finally:
            self._lock.release()

    def render(self) -> str:
        """Return a consistent Prometheus text snapshot."""
        with self._lock:
            telemetry = dict(self._telemetry)
            packet_count = dict(self._packet_count)
            packet_dropped = dict(self._packet_dropped)
            packet_last_received = dict(self._packet_last_received)
            alarm_count = dict(self._alarm_count)
            event_count = dict(self._event_count)
            connected = self._connected
            emulating = self._emulating
            skipped_updates = self._skipped_updates

        lines = [
            "# HELP altair_metrics_exporter_enabled Whether telemetry metrics collection is enabled.",
            "# TYPE altair_metrics_exporter_enabled gauge",
            f"altair_metrics_exporter_enabled {1 if self.enabled else 0}",
            "# HELP altair_metrics_exporter_skipped_updates_total Updates skipped to avoid blocking telemetry processing.",
            "# TYPE altair_metrics_exporter_skipped_updates_total counter",
            f"altair_metrics_exporter_skipped_updates_total {skipped_updates}",
            "# HELP altair_ground_station_connected Whether the radio serial link is connected.",
            "# TYPE altair_ground_station_connected gauge",
            f"altair_ground_station_connected {connected}",
            "# HELP altair_ground_station_emulating Whether the packet emulator is active.",
            "# TYPE altair_ground_station_emulating gauge",
            f"altair_ground_station_emulating {emulating}",
            "# HELP altair_telemetry_value Latest numeric telemetry field value.",
            "# TYPE altair_telemetry_value gauge",
        ]

        for (packet, field, unit), value in sorted(telemetry.items()):
            lines.append(
                "altair_telemetry_value"
                + _labels(packet=packet, field=field, unit=unit)
                + f" {value}"
            )

        lines.extend(
            [
                "# HELP altair_telemetry_packets_total Telemetry packets processed.",
                "# TYPE altair_telemetry_packets_total counter",
            ]
        )
        for packet, value in sorted(packet_count.items()):
            lines.append(f"altair_telemetry_packets_total{_labels(packet=packet)} {value}")

        lines.extend(
            [
                "# HELP altair_telemetry_packets_dropped_total Packets inferred lost from sequence gaps.",
                "# TYPE altair_telemetry_packets_dropped_total counter",
            ]
        )
        for packet, value in sorted(packet_dropped.items()):
            lines.append(
                f"altair_telemetry_packets_dropped_total{_labels(packet=packet)} {value}"
            )

        lines.extend(
            [
                "# HELP altair_telemetry_packet_last_received_seconds UNIX time of the latest packet.",
                "# TYPE altair_telemetry_packet_last_received_seconds gauge",
            ]
        )
        for packet, value in sorted(packet_last_received.items()):
            lines.append(
                f"altair_telemetry_packet_last_received_seconds{_labels(packet=packet)} {value}"
            )

        lines.extend(
            [
                "# HELP altair_telemetry_alarms_total Alarm notifications generated.",
                "# TYPE altair_telemetry_alarms_total counter",
            ]
        )
        for (packet, field, severity), value in sorted(alarm_count.items()):
            lines.append(
                "altair_telemetry_alarms_total"
                + _labels(packet=packet, field=field, severity=severity)
                + f" {value}"
            )

        lines.extend(
            [
                "# HELP altair_flight_events_total Flight event transitions observed.",
                "# TYPE altair_flight_events_total counter",
            ]
        )
        for (field, state), value in sorted(event_count.items()):
            lines.append(
                f"altair_flight_events_total{_labels(field=field, state=state)} {value}"
            )

        return "\n".join(lines) + "\n"
