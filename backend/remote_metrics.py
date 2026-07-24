"""
Failure-isolated OTLP reporting for a hosted Grafana metrics stack.

Core telemetry processing only calls ``submit_*`` methods, which use a bounded
in-memory queue and never wait. OpenTelemetry instruments and all network I/O
live on background threads. If the queue is full, the oldest reporting item is
dropped rather than delaying ground-station work.
"""
from __future__ import annotations

import logging
import math
import os
import queue
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

logger = logging.getLogger("gs.remote_metrics")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class RemoteMetricsConfig:
    enabled: bool
    endpoint_configured: bool
    export_interval_s: float
    export_timeout_s: float
    queue_size: int
    service_name: str
    service_instance_id: str
    deployment_environment: str

    @classmethod
    def from_env(cls) -> "RemoteMetricsConfig":
        endpoint_configured = bool(
            os.getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
            or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        )
        return cls(
            # Reporting is an explicit opt-in even when generic OTLP variables
            # happen to be present in the process environment.
            enabled=_env_bool("ALTAIR_REMOTE_METRICS_ENABLED", default=False),
            endpoint_configured=endpoint_configured,
            export_interval_s=_env_float(
                "ALTAIR_REMOTE_METRICS_INTERVAL_S", default=5.0, minimum=1.0
            ),
            export_timeout_s=_env_float(
                "ALTAIR_REMOTE_METRICS_TIMEOUT_S", default=3.0, minimum=0.1
            ),
            queue_size=_env_int(
                "ALTAIR_REMOTE_METRICS_QUEUE_SIZE", default=1024, minimum=16
            ),
            service_name=os.getenv("OTEL_SERVICE_NAME", "altair-ground-station"),
            service_instance_id=os.getenv(
                "ALTAIR_STATION_ID",
                os.getenv("OTEL_SERVICE_INSTANCE_ID", socket.gethostname()),
            ),
            deployment_environment=os.getenv(
                "ALTAIR_DEPLOYMENT_ENVIRONMENT", "flight"
            ),
        )


class MetricSink(Protocol):
    def record(self, item_type: str, payload: dict[str, Any]) -> None: ...

    def record_reporter_status(self, queue_depth: int, dropped: int) -> None: ...

    def shutdown(self) -> None: ...


class _OpenTelemetrySink:
    """OpenTelemetry SDK adapter. Imported lazily on the reporting thread."""

    def __init__(self, config: RemoteMetricsConfig) -> None:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        exporter = OTLPMetricExporter(timeout=config.export_timeout_s)
        reader = PeriodicExportingMetricReader(
            exporter,
            export_interval_millis=int(config.export_interval_s * 1000),
            export_timeout_millis=int(config.export_timeout_s * 1000),
        )
        resource = Resource.create(
            {
                "service.name": config.service_name,
                "service.namespace": "altair",
                "service.instance.id": config.service_instance_id,
                "deployment.environment": config.deployment_environment,
            }
        )
        self._provider = MeterProvider(resource=resource, metric_readers=[reader])
        meter = self._provider.get_meter("altair.ground_station.telemetry")

        # Units are kept as attributes on the generic telemetry instrument so
        # Grafana's OTLP-to-Prometheus conversion preserves the dashboard name.
        self._telemetry_value = meter.create_gauge(
            "altair_telemetry_value",
            description="Latest numeric telemetry field value.",
        )
        self._packet_last_received = meter.create_gauge(
            "altair_telemetry_packet_last_received_seconds",
            description="UNIX time of the latest packet.",
        )
        self._connected = meter.create_gauge(
            "altair_ground_station_connected",
            description="Whether the radio serial link is connected.",
        )
        self._emulating = meter.create_gauge(
            "altair_ground_station_emulating",
            description="Whether the packet emulator is active.",
        )
        self._queue_depth = meter.create_gauge(
            "altair_remote_metrics_queue_depth",
            description="Items waiting for hosted metrics processing.",
        )
        self._packets = meter.create_counter(
            "altair_telemetry_packets",
            description="Telemetry packets processed.",
        )
        self._packets_dropped = meter.create_counter(
            "altair_telemetry_packets_dropped",
            description="Packets inferred lost from sequence gaps.",
        )
        self._alarms = meter.create_counter(
            "altair_telemetry_alarms",
            description="Alarm notifications generated.",
        )
        self._events = meter.create_counter(
            "altair_flight_events",
            description="Flight event transitions observed.",
        )
        self._reporting_dropped = meter.create_counter(
            "altair_remote_metrics_queue_dropped",
            description="Hosted reporting items dropped to protect core processing.",
        )
        self._last_reported_drop_count = 0

    def record(self, item_type: str, payload: dict[str, Any]) -> None:
        if item_type == "message":
            message_type = payload.get("type")
            if message_type == "packet":
                self._record_packet(payload)
            elif message_type == "status":
                self._connected.set(1 if payload.get("connected") else 0)
                if "emulating" in payload:
                    self._emulating.set(1 if payload.get("emulating") else 0)
        elif item_type == "alarm":
            self._alarms.add(
                1,
                {
                    "packet": str(payload.get("label", "unknown")),
                    "field": str(payload.get("field", "unknown")),
                    "severity": str(payload.get("severity", "unknown")),
                },
            )
        elif item_type == "event":
            self._events.add(
                1,
                {
                    "field": str(payload.get("field", "unknown")),
                    "state": str(payload.get("new_val", "unknown")),
                },
            )

    def _record_packet(self, packet: dict[str, Any]) -> None:
        packet_label = str(packet.get("label", "unknown"))
        for field in packet.get("fields", []):
            try:
                value = float(field["value"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            self._telemetry_value.set(
                value,
                {
                    "packet": packet_label,
                    "field": str(field.get("name", "unknown")),
                    "unit": str(field.get("unit", "")),
                },
            )

        self._packets.add(1, {"packet": packet_label})
        try:
            dropped = max(0, int(packet.get("dropped", 0)))
        except (TypeError, ValueError):
            dropped = 0
        if dropped:
            self._packets_dropped.add(dropped, {"packet": packet_label})
        self._packet_last_received.set(time.time(), {"packet": packet_label})

    def record_reporter_status(self, queue_depth: int, dropped: int) -> None:
        self._queue_depth.set(queue_depth)
        new_drops = max(0, dropped - self._last_reported_drop_count)
        if new_drops:
            self._reporting_dropped.add(new_drops)
            self._last_reported_drop_count = dropped

    def shutdown(self) -> None:
        try:
            self._provider.shutdown(timeout_millis=1000)
        except TypeError:
            self._provider.shutdown()


SinkFactory = Callable[[RemoteMetricsConfig], MetricSink]


class RemoteMetricsReporter:
    """Bounded, non-blocking handoff from telemetry to hosted OTLP export."""

    def __init__(
        self,
        config: RemoteMetricsConfig | None = None,
        sink_factory: SinkFactory = _OpenTelemetrySink,
    ) -> None:
        self.config = config or RemoteMetricsConfig.from_env()
        self._sink_factory = sink_factory
        self._queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(
            maxsize=self.config.queue_size
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._accepted = 0
        self._dropped = 0
        self._last_error = ""
        self._ready = False

    def start(self) -> bool:
        if not self.config.enabled:
            logger.info("Hosted OTLP metrics reporting is disabled")
            return False
        if not self.config.endpoint_configured:
            self._last_error = "OTLP endpoint is not configured"
            logger.warning(
                "Hosted metrics enabled but no OTEL_EXPORTER_OTLP_ENDPOINT or "
                "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT is configured"
            )
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="hosted-metrics-reporter",
        )
        self._thread.start()
        logger.info(
            "Hosted OTLP metrics reporter starting (interval=%.1fs, queue=%d)",
            self.config.export_interval_s,
            self.config.queue_size,
        )
        return True

    def submit_message(self, message: dict[str, Any]) -> None:
        self._submit("message", message)

    def submit_alarm(self, alarm: dict[str, Any]) -> None:
        self._submit("alarm", alarm)

    def submit_event(self, event: dict[str, Any]) -> None:
        self._submit("event", event)

    def _submit(self, item_type: str, payload: dict[str, Any]) -> None:
        if not self.config.enabled or not self.config.endpoint_configured:
            return
        item = (item_type, payload)
        try:
            self._queue.put_nowait(item)
            self._accepted += 1
            return
        except queue.Full:
            pass

        # Prefer fresh flight data. Removing and replacing are both non-blocking.
        try:
            self._queue.get_nowait()
            self._queue.task_done()
        except queue.Empty:
            pass
        self._dropped += 1
        try:
            self._queue.put_nowait(item)
            self._accepted += 1
        except queue.Full:
            self._dropped += 1

    def _run(self) -> None:
        try:
            sink = self._sink_factory(self.config)
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.error("Could not initialize hosted metrics exporter", exc_info=True)
            return

        self._ready = True
        try:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    item_type, payload = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    sink.record(item_type, payload)
                    sink.record_reporter_status(self._queue.qsize(), self._dropped)
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    logger.warning("Hosted metrics item was discarded", exc_info=True)
                finally:
                    self._queue.task_done()
        finally:
            self._ready = False
            try:
                sink.shutdown()
            except Exception:
                logger.warning("Hosted metrics shutdown failed", exc_info=True)

    def stop(self, timeout_s: float = 1.5) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout_s)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "configured": self.config.endpoint_configured,
            "ready": self._ready,
            "worker_alive": bool(self._thread and self._thread.is_alive()),
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self.config.queue_size,
            "accepted": self._accepted,
            "dropped": self._dropped,
            "last_error": self._last_error or None,
        }
