"""
Camera calibration support: exposure-window telemetry buffering and
calibration-capture sidecar file writers.

Used by the Calibration tab to correlate a camera exposure against the
integrating sphere's source telemetry (Lighting packet: DAC code, drive
current, temperature) and the UVIC PDRO photodiode readout (PhotodiodeSignal
packet: per-sample sergeant/soldier ADC codes and voltages), both logged
independently to the session CSVs by backend/logging_manager.py already —
this module exists to additionally bundle exactly the samples that fall
within one specific exposure window alongside that exposure's FITS file, so
a calibration data point is self-contained without cross-referencing the
session-wide CSVs by timestamp after the fact.

Timestamping: every sample kept here is stamped with the GS wall-clock
receive time (time.time(), UTC), the same clock backend/camera.py uses for
FITS DATE-OBS/capture_utc. The PDRO's own per-sample time_unix_us (FC clock)
is preserved alongside it in the CSV for cross-checking, but window
membership is decided on GS receive time so it lines up with when the shutter
was actually open on the GS side, independent of FC/GS clock offset.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("gs.calibration")


@dataclass
class _ExposureBuffer:
    start_t: float
    end_t: float | None = None   # None while still collecting
    lighting_samples: list[dict[str, Any]] = field(default_factory=list)
    photodiode_samples: list[dict[str, Any]] = field(default_factory=list)


class CalibrationRecorder:
    """
    Owns zero or one active exposure-window buffer at a time.

    Usage:
        recorder = CalibrationRecorder()
        recorder.start_window(margin_pre_s=0.5)
        ... camera exposes; recorder.ingest(telemetry_result) called for every
            packet as it arrives, no-ops when idle ...
        await recorder.finish_window(margin_post_s=0.5)   # waits out the
                                                            # post-margin, then
                                                            # returns collected
                                                            # samples

    Only one window is tracked at a time — starting a new one while another
    is active discards the previous (calibration captures are taken
    serially by a single operator, never concurrently).
    """

    def __init__(self) -> None:
        self._buffer: _ExposureBuffer | None = None

    @property
    def active(self) -> bool:
        return self._buffer is not None

    def start_window(self, *, margin_pre_s: float = 0.5) -> float:
        """Begin collecting. Returns the window's start_t (GS wall-clock, UTC)."""
        start_t = time.time() - margin_pre_s
        self._buffer = _ExposureBuffer(start_t=start_t)
        return start_t

    def ingest(self, result: dict[str, Any]) -> None:
        """Feed one decoded telemetry packet. No-op unless a window is active
        and the packet is one of the two calibration-relevant labels."""
        buf = self._buffer
        if buf is None or buf.end_t is not None:
            return
        label = result.get("label")
        now = time.time()

        if label == "Lighting":
            fields = {f["name"]: f["value"] for f in result.get("fields", [])}
            buf.lighting_samples.append({"gs_wall_time_utc": now, **fields})

        elif label == "PhotodiodeSignal":
            for sample in result.get("samples", []):
                buf.photodiode_samples.append({
                    "gs_wall_time_utc": now,
                    "fc_time_unix_us":  sample.get("time_unix_us"),
                    "sequence":         sample.get("sequence"),
                    "valid_flags":      sample.get("valid_flags"),
                    "sergeant_code":        sample.get("sergeant_code"),
                    "soldier_code":         sample.get("soldier_code"),
                    "sergeant_voltage_v":   sample.get("sergeant_voltage_v"),
                    "soldier_voltage_v":    sample.get("soldier_voltage_v"),
                })

    async def finish_window(self, *, margin_post_s: float = 0.5) -> dict[str, Any]:
        """
        Stop collecting after waiting out margin_post_s (so PDRO/Lighting
        samples transmitted right after the shutter closes are still
        captured), and return the buffered samples. Safe to call even if no
        window is active (returns empty lists).
        """
        buf = self._buffer
        if buf is None:
            return {"lighting_samples": [], "photodiode_samples": []}
        if margin_post_s > 0:
            await asyncio.sleep(margin_post_s)
        buf.end_t = time.time()
        self._buffer = None
        return {
            "start_t":            buf.start_t,
            "end_t":              buf.end_t,
            "lighting_samples":   buf.lighting_samples,
            "photodiode_samples": buf.photodiode_samples,
        }


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.9g}"
    return str(v)


def write_sidecar_text(path: Path, *, capture_meta: dict[str, Any], sensor: dict[str, Any],
                        sphere: dict[str, Any], window: dict[str, Any]) -> None:
    """
    Write a human-readable key=value text file alongside a calibration
    capture, covering everything not already in the FITS header: full sphere
    source state at capture time, exposure-window bounds, and sample counts.
    Mirrors the KEY=VALUE convention backend/camera.py._build_description
    uses for the FITS COMMENT block, so both are parseable the same way.
    """
    lines = ["ALTAIR V2 -- Camera Calibration Capture", ""]

    lines.append(f"CaptureUTC={capture_meta.get('capture_utc', time.time())}")
    lines.append(f"ExposureWindowStartUTC={window.get('start_t')}")
    lines.append(f"ExposureWindowEndUTC={window.get('end_t')}")
    if window.get("start_t") is not None and window.get("end_t") is not None:
        lines.append(f"ExposureWindowDuration_s={window['end_t'] - window['start_t']:.6f}")
    lines.append(f"LightingSampleCount={len(window.get('lighting_samples', []))}")
    lines.append(f"PhotodiodeSampleCount={len(window.get('photodiode_samples', []))}")
    lines.append("")

    lines.append("-- Sphere source (Lighting packet, live at capture time) --")
    for key in ("sphere_on", "sphere_dac_code", "sphere_current_a", "sphere_temperature_c",
                "beacon_on", "beacon_dac_code", "beacon_current_a", "observation_active"):
        if key in sphere:
            lines.append(f"{key}={_fmt(sphere[key])}")
    lines.append("")

    lines.append("-- Camera / sensor controls --")
    for key, value in sensor.items():
        safe_key = key.replace(" ", "_").replace("(", "").replace(")", "")
        lines.append(f"Sensor_{safe_key}={_fmt(value)}")
    lines.append("")

    lines.append("-- Pointing / GPS / mount (same values written to the FITS header) --")
    for key, value in capture_meta.items():
        lines.append(f"{key}={_fmt(value)}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_sidecar_csv(path: Path, *, window: dict[str, Any]) -> None:
    """
    Write one CSV with every Lighting + PhotodiodeSignal sample collected
    during the exposure window, in a single time-ordered file (row source
    distinguished by the `source` column) so the whole calibration data
    point — sphere brightness alongside photodiode readout — is
    reconstructable from one file without joining across the session-wide
    per-packet-type CSVs backend/logging_manager.py writes separately.
    """
    columns = [
        "source", "gs_wall_time_utc", "fc_time_unix_us", "sequence",
        "sphere_on", "sphere_dac_code", "sphere_current_a", "sphere_temperature_c",
        "beacon_on", "beacon_dac_code", "beacon_current_a",
        "valid_flags", "sergeant_code", "soldier_code",
        "sergeant_voltage_v", "soldier_voltage_v",
    ]
    rows: list[dict[str, Any]] = []
    for s in window.get("lighting_samples", []):
        rows.append({"source": "lighting", **s})
    for s in window.get("photodiode_samples", []):
        rows.append({"source": "photodiode", **s})
    rows.sort(key=lambda r: r.get("gs_wall_time_utc") or 0.0)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    path.write_text(buf.getvalue(), encoding="utf-8")
