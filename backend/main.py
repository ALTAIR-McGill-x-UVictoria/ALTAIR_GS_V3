"""
ALTAIR V2 Ground Station — Python Backend

Responsibilities:
  - Scan for / open the LR-900p serial port (CP210x auto-detect)
  - Decode binary telemetry frames (mirrors altairfc wire format)
  - Stream decoded packets to connected browser clients via WebSocket
  - Expose a REST API for port listing and connection control

Start with:
    uvicorn backend.main:app --reload --port 8000 --reload-dir backend --reload-dir src

Or via the helper script:
    python -m backend.main
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import threading
import tomllib

import serial
import serial.tools.list_ports
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response

from backend.packets import REGISTRY, HEADER_SIZE, CRC_SIZE, MIN_FRAME, SYNC_BYTE, ACK_PACKET_ID, decode_frame, decode_ack_frame
from backend.packets import _HEADER, _CRC
from backend.tracking import calculate_tracking_params
from backend.mount import BaseMountController, create_mount
from backend.camera import CameraController
from backend.logging_manager import TelemetryLogger
from backend.alarms import ALARM_RULES
from backend.emulator import PacketEmulator
from backend.events import EVENT_DEFS, FLIGHT_STAGE_NAMES, BOOLEAN_EVENT_FIELDS
from backend.gps_reader import GsGpsReader
from backend.metrics import PROMETHEUS_CONTENT_TYPE, TelemetryMetrics
from backend.remote_metrics import RemoteMetricsReporter

logger = logging.getLogger("gs.backend")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)

# ---------------------------------------------------------------------------
# LR-900p modem protocol (runs on the same UART as telemetry)
#
# Two protocols share one UART:
#   • Telemetry frames  SOF=0xAA  (defined in altairfc/telemetry/serializer.py)
#   • Modem config frames  SOF=0xEF  (LR-900p proprietary)
#
# The modem config protocol is muxed transparently by the radio — telemetry
# payloads pass straight through; 0xEF frames talk to the modem's local CPU.
# ---------------------------------------------------------------------------
_LR_SOF           = 0xEF
_LR_TYPE_REQUEST  = 0x01
_LR_TYPE_RESPONSE = 0x1E
_LR_CMD_HEARTBEAT   = 0x01
_LR_CMD_CONFIG_REQ  = 0x02
_LR_CMD_CONFIG_RESP = 0x03
_LR_SUBBLOCK_READ   = 0x03
_LR_SUBBLOCK_WRITE  = 0x04
_LR_CONFIG_BLOCK_ID = 0x12
_LR_FREQ_WORD_LO    = 0xE8
_LR_FREQ_WORD_HI    = 0x03
_LR_HB_INTERVAL     = 0.625   # seconds between heartbeats

# Exact packet lengths keyed by (TYPE, CMD)
_LR_PACKET_LENGTHS: dict[tuple[int, int], int] = {
    (_LR_TYPE_REQUEST,  _LR_CMD_HEARTBEAT):   20,
    (_LR_TYPE_RESPONSE, _LR_CMD_HEARTBEAT):   20,
    (_LR_TYPE_REQUEST,  _LR_CMD_CONFIG_REQ):  25,
    (_LR_TYPE_RESPONSE, _LR_CMD_CONFIG_RESP): 25,
}


def _lr_checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def _lr_build_heartbeat(seq: int, pc_uptime_ms: int, link_flag: int = 0x00) -> bytes:
    body = bytes([
        _LR_SOF, _LR_TYPE_REQUEST, 0x00, _LR_CMD_HEARTBEAT,
        seq & 0xFF,
        0x0D,
        pc_uptime_ms & 0xFF, (pc_uptime_ms >> 8) & 0xFF,
        0x0E, 0x00,
        link_flag,  # 0x1E = config mode, 0x00 = transparent bridge (pass-through)
        0x01, 0x00, 0x01,
        0x00, 0x00, 0x00, 0x00, 0x00,
    ])
    return body + bytes([_lr_checksum(body)])


def _lr_build_config_read(seq: int) -> bytes:
    body = bytes([
        _LR_SOF, _LR_TYPE_REQUEST, 0x00, _LR_CMD_CONFIG_REQ,
        seq & 0xFF,
        _LR_CONFIG_BLOCK_ID,
        _LR_SUBBLOCK_READ,
    ]) + bytes(17)
    return body + bytes([_lr_checksum(body)])


def _lr_build_config_write(seq: int, data_rate: int, tx_power: int, channel: int) -> bytes:
    body = bytes([
        _LR_SOF, _LR_TYPE_REQUEST, 0x00, _LR_CMD_CONFIG_REQ,
        seq & 0xFF,
        _LR_CONFIG_BLOCK_ID,
        _LR_SUBBLOCK_WRITE,
        0x00, 0x00,
        data_rate & 0xFF,
        tx_power  & 0xFF,
        channel   & 0xFF,
        _LR_FREQ_WORD_LO, _LR_FREQ_WORD_HI,
        0x03, 0x00,
    ]) + bytes(8)
    return body + bytes([_lr_checksum(body)])


def _lr_parse_config_response(raw: bytes) -> dict | None:
    """Return parsed config dict or None if the frame is not a valid config-read response."""
    if len(raw) != 25:
        return None
    if raw[1] != _LR_TYPE_RESPONSE or raw[3] != _LR_CMD_CONFIG_RESP:
        return None
    if raw[6] != _LR_SUBBLOCK_READ:
        return None
    p = raw[5:-1]
    return {
        "data_rate":  p[4],
        "tx_power":   p[5],
        "channel":    p[6],
        "session_id": raw[20:24].hex(),
    }


def _lr_parse_write_ack(raw: bytes) -> bool:
    """Return True if raw is a valid write-ack response."""
    return (
        len(raw) == 25
        and raw[1] == _LR_TYPE_RESPONSE
        and raw[3] == _LR_CMD_CONFIG_RESP
        and raw[6] == _LR_SUBBLOCK_WRITE
    )


# ---------------------------------------------------------------------------
# CP210x auto-detect (LR-900p)
# ---------------------------------------------------------------------------
_CP210X_VID = 0x10C4
_CP210X_PID = 0xEA60


def list_serial_ports() -> list[dict]:
    return [
        {
            "device":      p.device,
            "description": p.description or "",
            "vid":         p.vid,
            "pid":         p.pid,
            "is_lr900p":   p.vid == _CP210X_VID and p.pid == _CP210X_PID,
        }
        for p in serial.tools.list_ports.comports()
    ]


def find_lr900p() -> str | None:
    matches = [p for p in serial.tools.list_ports.comports()
               if p.vid == _CP210X_VID and p.pid == _CP210X_PID]
    if not matches:
        return None
    return matches[0].device


# ---------------------------------------------------------------------------
# Serial reader — runs as a background asyncio task
# ---------------------------------------------------------------------------

class SerialReader:
    def __init__(self) -> None:
        self._port: serial.Serial | None = None
        self._task: asyncio.Task | None  = None
        self._hb_task: asyncio.Task | None = None
        self._buf  = bytearray()
        self._lr_buf = bytearray()   # accumulates LR-900p frames (SOF=0xEF)
        self._seq_prev: dict[int, int]   = {}
        self._photodiode_sample_prev: int | None = None
        self.connected = False
        self.port_name = ""
        self._clients: set[WebSocket]    = set()
        # Restart coalescing: track pending gaps to detect FC reboot vs link loss
        self._pending_gaps: dict[int, int] = {}   # pkt_id → dropped count
        self._pending_gap_time: float = 0.0
        self._diag_frame_count: int = 0            # total frames successfully decoded

        # Single threading.Lock serialises all port.write() calls regardless of
        # whether the caller is the asyncio event loop or a run_in_executor thread.
        # Queue of frames waiting to be written. The read loop drains this
        # between reads so all port I/O stays on the event loop thread,
        # eliminating concurrent read+write on the Windows CP210x driver.
        self._write_queue: asyncio.Queue[bytes] = asyncio.Queue()

        # threading.Lock only used by _lr_read_response (executor thread) and
        # the heartbeat writer (also executor) — not needed for normal writes
        # now that send_command posts to _write_queue instead.
        self._port_write_lock = threading.Lock()

        # Effective TX rate scale from FC's radio config — updated when a
        # RadioConfigPacket (0x0C) is received. Mirrors the FC's rate_scale[]
        # lookup from settings.toml so target_hz shown in the GUI matches reality.
        self._current_rate_scale: float = 1.0

        # LR-900p modem state
        self._lr_seq: int = 0
        self._lr_start_t: float = 0.0
        self._lr_linked: bool = False
        # True only while a config read/write is in progress.
        # The heartbeat loop sends link_flag=0x1E (config mode) when set,
        # and link_flag=0x00 (transparent bridge / pass-through) otherwise.
        self._lr_config_active: bool = False
        # Cached result of the last successful read_radio_config call.
        # Returned by GET /api/radio/config without touching the modem again.
        self._lr_live_config: dict | None = None
        # Signalled by _process_lr_buf when a config read/write response arrives
        self._lr_cfg_read_event:  asyncio.Event = asyncio.Event()
        self._lr_cfg_write_event: asyncio.Event = asyncio.Event()
        self._lr_cfg_read_result:  dict | None  = None
        self._lr_cfg_write_ok:     bool         = False

    def add_client(self, ws: WebSocket)    -> None: self._clients.add(ws)
    def remove_client(self, ws: WebSocket) -> None: self._clients.discard(ws)

    def _lr_next_seq(self) -> int:
        s = self._lr_seq
        self._lr_seq = (self._lr_seq + 1) & 0xFF
        return s

    def _lr_uptime_ms(self) -> int:
        return int((time.monotonic() - self._lr_start_t) * 1000) & 0xFFFF

    async def connect(self, port: str, baud: int = 57600) -> None:
        if self.connected:
            await self.disconnect()
        try:
            self._port = serial.Serial(port, baud, timeout=0)
            self.connected = True
            self.port_name = port
            self._buf.clear()
            self._lr_buf.clear()
            self._seq_prev.clear()
            self._photodiode_sample_prev = None
            self._lr_seq = 0
            self._lr_start_t = time.monotonic()
            self._lr_linked = False
            self._task    = asyncio.create_task(self._read_loop())
            self._hb_task = asyncio.create_task(self._lr_heartbeat_loop())
            logger.info("Serial port %s opened @ %d baud", port, baud)
            await self._broadcast({"type": "status", "connected": True, "port": port})
        except serial.SerialException as e:
            logger.error("Could not open %s: %s", port, e)
            raise

    async def disconnect(self) -> None:
        for task in (self._task, self._hb_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = self._hb_task = None
        if self._port and self._port.is_open:
            self._port.close()
        self.connected = False
        self.port_name = ""
        self._lr_linked = False
        self._lr_config_active = False
        self._lr_live_config = None
        # Discard any queued writes — they belong to the closed session
        while not self._write_queue.empty():
            try:
                self._write_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        logger.info("Serial port closed")
        await self._broadcast({"type": "status", "connected": False, "port": ""})

    async def _read_loop(self) -> None:
        _consecutive_errors = 0
        _MAX_CONSECUTIVE_ERRORS = 10
        _diag_bytes   = 0      # raw bytes received since last log
        _diag_frames  = 0      # decoded frames since last log
        _diag_last_t  = time.monotonic()
        _DIAG_INTERVAL = 30.0  # log a throughput line every 30 s
        while True:
            try:
                # Drain pending writes first so TX and RX never overlap on the
                # Windows CP210x driver (concurrent read+write corrupts the FIFO).
                # All port I/O is on the event loop thread; send_command() posts
                # frames to _write_queue instead of writing directly.
                while not self._write_queue.empty() and not self._lr_config_active:
                    frame = self._write_queue.get_nowait()
                    if self._port and self._port.is_open:
                        self._port.write(frame)
                        logger.info("send_command: sent %d bytes (CMD_ID=0x%02X)",
                                    len(frame), frame[1] if len(frame) > 1 else 0xFF)

                chunk = self._read_chunk()
                if chunk:
                    _consecutive_errors = 0
                    _diag_bytes += len(chunk)
                    self._buf.extend(chunk)
                    prev_frames = self._diag_frame_count
                    await self._process_buffer()
                    _diag_frames += self._diag_frame_count - prev_frames
                else:
                    await asyncio.sleep(0.002)

                now = time.monotonic()
                if now - _diag_last_t >= _DIAG_INTERVAL:
                    elapsed = now - _diag_last_t
                    logger.info("Serial RX: %.0f B/s  ~%d frames in %.0fs",
                                _diag_bytes / elapsed, _diag_frames, elapsed)
                    _diag_bytes  = 0
                    _diag_frames = 0
                    _diag_last_t = now
            except serial.SerialException as e:
                _consecutive_errors += 1
                if _consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    logger.error("Serial read: %d consecutive errors — closing port: %s",
                                 _consecutive_errors, e)
                    await self._broadcast({"type": "status", "connected": False, "port": "", "error": str(e)})
                    break
                logger.warning("Serial read transient error (%d/%d): %s",
                               _consecutive_errors, _MAX_CONSECUTIVE_ERRORS, e)
                self._buf.clear()
                await asyncio.sleep(0.1)

    def _read_chunk(self) -> bytes:
        if self._port is None or not self._port.is_open:
            return b""
        # Yield port to _lr_read_response (executor thread) during config ops.
        if self._lr_config_active:
            return b""
        waiting = self._port.in_waiting
        if waiting:
            return self._port.read(waiting)
        return b""

    def send_command(self, frame: bytes) -> bool:
        """
        Enqueue a command frame for transmission. The read loop writes it
        between reads so TX and RX never overlap on the same thread.
        Safe to call from any thread.
        """
        if not self.connected:
            logger.warning("send_command: serial port not open")
            return False
        try:
            self._write_queue.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            logger.error("send_command: write queue full, frame dropped")
            return False

    # Largest plausible frame: header + 512 payload bytes + CRC.
    # A length field larger than this means we locked onto a false sync byte
    # inside a payload — skip it and search for the next real sync byte.
    _MAX_PAYLOAD = 512

    async def _process_buffer(self) -> None:
        while len(self._buf) >= MIN_FRAME:
            sync_pos = self._buf.find(SYNC_BYTE)
            if sync_pos == -1:
                self._buf.clear()
                return
            if sync_pos > 0:
                del self._buf[:sync_pos]

            if len(self._buf) < 13:
                return

            _, _, _, _, length = _HEADER.unpack_from(self._buf, 0)

            if length > self._MAX_PAYLOAD:
                # Bogus length — this sync byte is inside a payload, skip it
                del self._buf[0]
                continue

            frame_size = HEADER_SIZE + length + CRC_SIZE

            if len(self._buf) < frame_size:
                return

            frame = bytes(self._buf[:frame_size])
            del self._buf[:frame_size]

            _, pkt_id_peek, _, _, _ = _HEADER.unpack_from(frame, 0)
            if pkt_id_peek == ACK_PACKET_ID:
                ack = decode_ack_frame(frame)
                if ack:
                    await self._broadcast({"type": "ack", **ack})
                continue

            result = decode_frame(frame)
            if result is None:
                continue

            pkt_id = result["packet_id"]
            seq    = result["seq"]
            prev   = self._seq_prev.get(pkt_id)
            if prev is not None:
                expected = (prev + 1) & 0xFF
                if seq != expected:
                    dropped = (seq - expected) & 0xFF
                    result["dropped"] = dropped
                    now = time.time()
                    if now - self._pending_gap_time > 1.0:
                        if self._pending_gaps:
                            logger.warning(
                                "SEQ gap (link loss) on %d packet type(s): %s",
                                len(self._pending_gaps),
                                ", ".join(f"0x{k:02X}={v}dropped"
                                          for k, v in self._pending_gaps.items()),
                            )
                        self._pending_gaps.clear()
                    self._pending_gaps[pkt_id] = dropped
                    self._pending_gap_time = now
                    known = set(self._seq_prev.keys())
                    if known and known.issubset(self._pending_gaps.keys()):
                        logger.info("FC restart detected — seq counters reset across all %d packet types",
                                    len(known))
                        self._pending_gaps.clear()
                        self._seq_prev.clear()
            self._seq_prev[pkt_id] = seq

            if pkt_id == 0x0D and result.get("samples"):
                first_sample_seq = int(result["samples"][0]["sequence"])
                if self._photodiode_sample_prev is not None:
                    expected_sample_seq = (
                        self._photodiode_sample_prev + 1
                    ) & 0xFFFFFFFF
                    missing_samples = (
                        first_sample_seq - expected_sample_seq
                    ) & 0xFFFFFFFF
                    # A backwards jump is a task/FC restart, not billions of
                    # missing samples. Forward gaps remain visible in the log/UI.
                    if 0 < missing_samples < 0x80000000:
                        result["sample_dropped"] = missing_samples
                        logger.warning(
                            "Photodiode sample gap: expected %d got %d "
                            "(%d sample(s) missing)",
                            expected_sample_seq,
                            first_sample_seq,
                            missing_samples,
                        )
                    elif missing_samples >= 0x80000000:
                        logger.info(
                            "Photodiode sample sequence reset: %d -> %d",
                            self._photodiode_sample_prev,
                            first_sample_seq,
                        )
                self._photodiode_sample_prev = int(
                    result["samples"][-1]["sequence"]
                )

            self._diag_frame_count += 1
            result["wall_ms"] = round(time.time() * 1000)

            # Update rate scale when RadioConfigPacket (0x0C) arrives so that
            # target_hz shown in the GUI reflects the effective scaled rate.
            if pkt_id == 0x0C:
                data_rate_field = next(
                    (f["value"] for f in result.get("fields", []) if f["name"] == "data_rate"),
                    None,
                )
                if data_rate_field is not None:
                    rate_scale = _radio_defaults.get("rate_scale", [0.1, 0.33, 1.0])
                    idx = int(data_rate_field)
                    if 0 <= idx < len(rate_scale):
                        self._current_rate_scale = float(rate_scale[idx])

            # Scale target_hz to match the FC's effective TX rate at the current data_rate.
            if "target_hz" in result and result["target_hz"] is not None:
                result["target_hz"] = round(result["target_hz"] * self._current_rate_scale, 3)

            await self._broadcast({"type": "packet", **result})

    def _lr_read_response(self, subblock: int, timeout: float) -> bytes | None:
        """
        Blocking. Read from the port until a valid LR-900p response frame for
        the given subblock arrives or timeout expires. Called from a thread via
        run_in_executor while _lr_config_active is True (heartbeats running).
        """
        buf  = bytearray()
        dead = time.monotonic() + timeout
        while time.monotonic() < dead:
            chunk = self._port.read(64)
            if chunk:
                buf.extend(chunk)
            else:
                time.sleep(0.005)
            # Scan for a complete LR-900p frame
            while len(buf) >= 6:
                idx = buf.find(_LR_SOF)
                if idx < 0:
                    buf.clear()
                    break
                if idx > 0:
                    del buf[:idx]
                type_byte = buf[1] if len(buf) > 1 else 0
                cmd       = buf[3] if len(buf) > 3 else 0
                expected  = _LR_PACKET_LENGTHS.get((type_byte, cmd))
                if expected is None:
                    del buf[0]
                    continue
                if len(buf) < expected:
                    break
                raw = bytes(buf[:expected])
                del buf[:expected]
                if _lr_checksum(raw[:-1]) != raw[-1]:
                    continue
                if (type_byte == _LR_TYPE_RESPONSE
                        and cmd == _LR_CMD_CONFIG_RESP
                        and raw[6] == subblock):
                    return raw
                if type_byte == _LR_TYPE_RESPONSE and cmd == _LR_CMD_HEARTBEAT:
                    if not self._lr_linked:
                        self._lr_linked = True
                        logger.info("LR-900p modem link established")
        return None

    async def _lr_heartbeat_loop(self) -> None:
        """Send 0xEF heartbeat frames only while a config operation is active."""
        _hb_count = 0
        while True:
            if not _LR_CONFIG_ENABLED or not self._lr_config_active:
                _hb_count = 0
                await asyncio.sleep(0.05)
                continue
            next_tick = time.monotonic()
            while self._lr_config_active:
                await asyncio.sleep(max(0, next_tick - time.monotonic()))
                next_tick += _LR_HB_INTERVAL
                if self._port is None or not self._port.is_open:
                    break
                _hb_count += 1
                frame = _lr_build_heartbeat(self._lr_next_seq(), self._lr_uptime_ms(), 0x1E)
                logger.info("LR-900p HB #%d → modem (link_flag=0x1E, config mode)", _hb_count)
                try:
                    with self._port_write_lock:
                        self._port.write(frame)
                except serial.SerialException as e:
                    logger.warning("LR-900p heartbeat write error: %s", e)

    def _lr_exit_config_mode(self) -> None:
        """Clear the config-active flag. The modem returns to transparent bridge
        mode on its own once heartbeats with link_flag=0x1E stop arriving."""
        logger.info("LR-900p config mode OFF — modem will auto-return to transparent bridge")
        self._lr_config_active = False

    async def read_radio_config(self, timeout: float = 3.0) -> dict | None:
        """
        Put the modem into config mode, send a read request, and return the
        parsed config. Blocks the calling coroutine via run_in_executor.
        """
        if not _LR_CONFIG_ENABLED:
            logger.info("LR-900p modem config disabled (_LR_CONFIG_ENABLED=False) — skipping read")
            return None
        if self._port is None or not self._port.is_open:
            return None
        logger.info("LR-900p config mode ON — reading modem config (telemetry paused ~%.1fs)", timeout)
        self._lr_config_active = True
        # Wait for at least one config-mode heartbeat to be sent and acknowledged
        await asyncio.sleep(_LR_HB_INTERVAL * 1.5)
        try:
            def _do() -> dict | None:
                logger.info("LR-900p sending config-read request to modem")
                frame = _lr_build_config_read(self._lr_next_seq())
                with self._port_write_lock:
                    self._port.write(frame)
                raw = self._lr_read_response(_LR_SUBBLOCK_READ, timeout)
                if raw:
                    logger.info("LR-900p config-read response received (%d bytes)", len(raw))
                else:
                    logger.warning("LR-900p config-read timed out — no response from modem")
                return _lr_parse_config_response(raw) if raw else None
            result = await asyncio.get_event_loop().run_in_executor(None, _do)
        except serial.SerialException as e:
            logger.error("LR-900p read_radio_config error: %s", e)
            result = None
        finally:
            self._lr_exit_config_mode()
        if result is not None:
            logger.info("LR-900p live config: data_rate=%d tx_power=%d channel=%d",
                        result["data_rate"], result["tx_power"], result["channel"])
            self._lr_live_config = result
        return result

    async def write_radio_config(
        self,
        data_rate: int,
        tx_power: int,
        channel: int,
        fc_ack_event: asyncio.Event,
        timeout: float = 5.0,
    ) -> bool:
        """
        ACK-gated GS modem channel switch. Waits for the FC to ACK on the old
        channel before switching the GS modem, then exits config mode.
        """
        if not _LR_CONFIG_ENABLED:
            logger.info("LR-900p modem config disabled (_LR_CONFIG_ENABLED=False) — skipping write")
            return False
        if self._port is None or not self._port.is_open:
            return False
        logger.info("LR-900p write_radio_config: waiting for FC ACK (timeout=%.1fs)", timeout)
        try:
            await asyncio.wait_for(fc_ack_event.wait(), timeout)
        except asyncio.TimeoutError:
            logger.warning("LR-900p write_radio_config: timed out waiting for FC ACK")
            return False

        logger.info("LR-900p config mode ON — writing modem config dr=%d pwr=%d ch=%d",
                    data_rate, tx_power, channel)
        self._lr_config_active = True
        await asyncio.sleep(_LR_HB_INTERVAL * 1.5)
        try:
            def _do() -> bool:
                logger.info("LR-900p sending config-write request to modem")
                frame = _lr_build_config_write(self._lr_next_seq(), data_rate, tx_power, channel)
                with self._port_write_lock:
                    self._port.write(frame)
                raw = self._lr_read_response(_LR_SUBBLOCK_WRITE, timeout)
                if raw:
                    logger.info("LR-900p config-write ACK received (%d bytes)", len(raw))
                else:
                    logger.warning("LR-900p config-write timed out — no ACK from modem")
                return raw is not None and _lr_parse_write_ack(raw)
            ok = await asyncio.get_event_loop().run_in_executor(None, _do)
        except serial.SerialException as e:
            logger.error("LR-900p write_radio_config error: %s", e)
            ok = False
        finally:
            self._lr_exit_config_mode()
        if ok:
            self._lr_live_config = {"data_rate": data_rate, "tx_power": tx_power, "channel": channel}
            logger.info("LR-900p GS modem reconfigured: data_rate=%d tx_power=%d channel=%d",
                        data_rate, tx_power, channel)
        else:
            logger.warning("LR-900p GS modem write failed — config not applied")
        return ok

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        # Best-effort, in-memory metrics only. This call never performs I/O and
        # skips updates instead of waiting if a Prometheus scrape holds its lock.
        metrics_exporter.observe_message(msg)
        remote_metrics.submit_message(msg)

        # Signal the radio-config REST handler when the FC ACKs cmd 0xC5
        if msg.get("type") == "ack" and msg.get("cmd_id") == 0xC5:
            global _fc_radio_ack_event
            if _fc_radio_ack_event is not None:
                _fc_radio_ack_event.set()

        # Intercept GPS packets to keep _latest_gps up-to-date for telescope tracking
        if msg.get("type") == "packet" and msg.get("label", "").lower() == "gps":
            global _latest_gps
            fields = {f["name"]: f["value"] for f in msg.get("fields", [])}
            _latest_gps = fields  # keys: lat, lon, alt, relative_alt, hdg

        # Log packet to CSV and evaluate alarms
        if msg.get("type") == "packet":
            global _latest_packets, _latest_alarms
            _latest_packets[msg["label"]] = msg

            alarms = telem_logger.ingest(msg)
            for alarm in alarms:
                metrics_exporter.observe_alarm(alarm)
                remote_metrics.submit_alarm(alarm)
                alarm_msg = json.dumps({"type": "alarm", **alarm})
                # Mirror alarm in server-side list (replace by label+field, cap at 50)
                entry = {k: v for k, v in alarm.items()}
                _latest_alarms = [a for a in _latest_alarms
                                  if not (a["label"] == alarm["label"] and a["field"] == alarm["field"])]
                _latest_alarms = ([entry] + _latest_alarms)[:50]
                for ws in list(self._clients):
                    try:
                        await ws.send_text(alarm_msg)
                    except Exception:
                        pass

        # Detect event flag transitions and flight stage changes
        if msg.get("type") == "packet" and msg.get("label", "").lower() == "event":
            global _event_prev
            fields = {f["name"]: int(f["value"]) for f in msg.get("fields", [])}
            events_to_emit: list[dict] = []

            for field_name, new_val in fields.items():
                old_val = _event_prev.get(field_name)
                if old_val is None:
                    # First packet — record baseline without emitting
                    continue
                if new_val == old_val:
                    continue

                if field_name == "flight_stage":
                    stage_name = FLIGHT_STAGE_NAMES.get(new_val, f"Stage {new_val}")
                    events_to_emit.append({
                        "field":   field_name,
                        "old_val": old_val,
                        "new_val": new_val,
                        "message": f"Flight stage → {stage_name}",
                        "stage":   new_val,
                    })
                elif field_name in BOOLEAN_EVENT_FIELDS:
                    message = EVENT_DEFS.get((field_name, new_val))
                    if message:
                        events_to_emit.append({
                            "field":   field_name,
                            "old_val": old_val,
                            "new_val": new_val,
                            "message": message,
                            "stage":   fields.get("flight_stage", 0),
                        })

            _event_prev = fields

            global _latest_events
            for ev in events_to_emit:
                metrics_exporter.observe_event(ev)
                remote_metrics.submit_event(ev)
                full_ev = {"type": "event", "wall_time": time.time(), **ev}
                ev_msg = json.dumps(full_ev)
                # Mirror in server-side event list (cap at 200)
                _latest_events = ([full_ev] + _latest_events)[:200]
                for ws in list(self._clients):
                    try:
                        await ws.send_text(ev_msg)
                    except Exception:
                        pass

        if not self._clients:
            return
        data = json.dumps(msg)
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.add(ws)
        self._clients -= dead


serial_reader   = SerialReader()
telem_logger    = TelemetryLogger()
metrics_exporter = TelemetryMetrics()
remote_metrics = RemoteMetricsReporter()
_emulator: PacketEmulator | None = None
_emulating = False

# Previous values of Event packet fields — used to detect transitions
_event_prev: dict[str, int] = {}

# In-memory mirrors of broadcast state — persisted to state.json on restart
_latest_packets: dict[str, dict] = {}   # label → last full packet dict
_latest_alarms:  list[dict]      = []   # last 50 alarms (same dedup as frontend)
_latest_events:  list[dict]      = []   # last 200 events

# ---------------------------------------------------------------------------
# Telescope hardware controllers
# ---------------------------------------------------------------------------

mount_controller: BaseMountController | None = None
camera_controller = CameraController()

# Latest GPS data from telemetry — updated by _broadcast, read by tracking poll
_latest_gps: dict | None = None

# Latest computed tracking params — updated by _tracking_poll_loop, read at capture time
_latest_tracking: dict | None = None

# Telescope WebSocket clients (separate from telemetry WS)
_telescope_clients: set[WebSocket] = set()

_tracking_task: asyncio.Task | None = None
_gs_gps_task: asyncio.Task | None = None
_tracking_enabled = False

# ---------------------------------------------------------------------------
# Radio config defaults — loaded from FC settings.toml at startup
# ---------------------------------------------------------------------------
# Set to False to disable all LR-900p modem config activity (no 0xEF frames
# sent, read/write endpoints return immediately).  Useful for diagnosing
# whether modem config traffic is causing RF link disruptions.
_LR_CONFIG_ENABLED: bool = False
_FC_SETTINGS_TOML = (
    Path(__file__).parent.parent.parent / "Altairfc_V2" / "altairfc" / "config" / "settings.toml"
).resolve()

def _load_radio_defaults() -> dict:
    try:
        with open(_FC_SETTINGS_TOML, "rb") as f:
            cfg = tomllib.load(f)
        rc = cfg.get("radio_config", {})
        return {
            "data_rate":  int(rc.get("data_rate",  1)),
            "tx_power":   int(rc.get("tx_power",   2)),
            "channel":    int(rc.get("channel",    0)),
            "watchdog_s": float(rc.get("watchdog_s", 30.0)),
            "rate_scale": list(rc.get("rate_scale", [0.1, 0.33, 1.0])),
        }
    except Exception as e:
        logger.warning("Could not load radio defaults from settings.toml: %s", e)
        return {"data_rate": 1, "tx_power": 2, "channel": 0, "watchdog_s": 30.0, "rate_scale": [0.1, 0.33, 1.0]}

_radio_defaults: dict = _load_radio_defaults()

# One-shot asyncio.Event signalled when the FC ACKs a RadioConfigCommand (0xC5).
# The REST handler creates a fresh event per request; write_radio_config waits on it.
_fc_radio_ack_event: asyncio.Event | None = None

# ---------------------------------------------------------------------------
# Ground station GPS (u-blox 7)
# ---------------------------------------------------------------------------

async def _on_gs_fix(fix: dict) -> None:
    await serial_reader._broadcast({"type": "gs_gps", **fix})

async def _on_gs_status(connected: bool, has_fix: bool, port: str) -> None:
    await serial_reader._broadcast({
        "type":      "gs_gps_status",
        "connected": connected,
        "has_fix":   has_fix,
        "port":      port,
    })

gs_gps_reader = GsGpsReader(on_fix=_on_gs_fix, on_status=_on_gs_status)

# ---------------------------------------------------------------------------
# Image gallery — capture directory + JPEG cache
# ---------------------------------------------------------------------------

_REPO_ROOT    = Path(__file__).parent.parent
_capture_dir  = _REPO_ROOT / "captures"   # default; overridable at runtime

# In-memory JPEG cache: filename -> (mtime, jpeg_thumb_bytes, jpeg_full_bytes)
_jpeg_cache: dict[str, tuple[float, bytes, bytes]] = {}

_THUMB_SIZE = (400, 400)   # max thumbnail dimensions
_TIFF_EXTS  = {".tif", ".tiff"}


def _list_images() -> list[dict]:
    """Return image metadata sorted newest-first."""
    results = []
    try:
        for entry in _capture_dir.iterdir():
            if entry.suffix.lower() in _TIFF_EXTS and entry.is_file():
                st = entry.stat()
                results.append({
                    "filename":  entry.name,
                    "url":       f"/api/gallery/thumb/{entry.name}",
                    "full_url":  f"/api/gallery/full/{entry.name}",
                    "mtime":     st.st_mtime,
                    "size_kb":   round(st.st_size / 1024),
                })
    except FileNotFoundError:
        pass
    results.sort(key=lambda x: x["mtime"], reverse=True)
    return results


def _get_jpegs(filename: str) -> tuple[bytes, bytes] | None:
    """Return (thumb_jpeg, full_jpeg) for filename, using cache when mtime unchanged."""
    path = _capture_dir / filename
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    cached = _jpeg_cache.get(filename)
    if cached and cached[0] == mtime:
        return cached[1], cached[2]

    try:
        from PIL import Image
        img = Image.open(path)
        # Normalise 16-bit to 8-bit for JPEG encoding
        if img.mode == "I;16" or img.mode == "I":
            import numpy as np
            arr = np.array(img, dtype=np.float32)
            arr = ((arr - arr.min()) / (arr.max() - arr.min() + 1e-9) * 255).astype(np.uint8)
            img = Image.fromarray(arr)
        elif img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        # Full-size JPEG
        buf_full = io.BytesIO()
        img.save(buf_full, format="JPEG", quality=90)

        # Thumbnail JPEG
        thumb = img.copy()
        thumb.thumbnail(_THUMB_SIZE)
        buf_thumb = io.BytesIO()
        thumb.save(buf_thumb, format="JPEG", quality=85)

        thumb_bytes = buf_thumb.getvalue()
        full_bytes  = buf_full.getvalue()
        _jpeg_cache[filename] = (mtime, thumb_bytes, full_bytes)
        return thumb_bytes, full_bytes
    except Exception as e:
        logger.warning("Gallery: failed to convert %s: %s", filename, e)
        return None


async def _broadcast_telescope(msg: dict) -> None:
    if not _telescope_clients:
        return
    data = json.dumps(msg)
    dead: set[WebSocket] = set()
    for ws in _telescope_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.add(ws)
    _telescope_clients.difference_update(dead)


async def _tracking_poll_loop(interval_s: float = 1.0) -> None:
    """
    Periodically compute tracking params from latest GPS and broadcast to
    telescope clients. Also commands the mount if tracking is enabled.
    """
    global _latest_gps, _latest_tracking, _tracking_enabled
    while True:
        await asyncio.sleep(interval_s)
        gps = _latest_gps
        if gps is None:
            continue
        try:
            params = calculate_tracking_params(
                payload_lat=gps["lat"],
                payload_lon=gps["lon"],
                payload_alt_m=gps["alt"],
            )
            _latest_tracking = params
            msg = {"type": "tracking", **params}
            await _broadcast_telescope(msg)

            if _tracking_enabled and mount_controller is not None and mount_controller.connected:
                if mount_controller.mount_type == "am5":
                    await mount_controller.goto(
                        ra_hours=params["ra_hours"],
                        dec_deg=params["dec_deg"],
                    )
                else:
                    await mount_controller.goto(
                        azimuth=params["azimuth"],
                        elevation=params["elevation"],
                    )
        except Exception as e:
            logger.warning("Tracking poll error: %s", e)


async def _gs_gps_beacon_loop(interval_s: float = 5.0) -> None:
    """
    Broadcast the GS position to the FC every 2 seconds while the serial
    link is open. The FC uses this to aim its downward light source at us.
    """
    from backend.commands import build_command_frame
    from telemetry.commands.gs_gps import GsGpsCommandPacket
    import backend.tracking as _tracking

    while True:
        await asyncio.sleep(interval_s)
        if serial_reader.port_name is None:
            continue
        try:
            pkt = GsGpsCommandPacket(
                lat=_tracking.GS_LAT,
                lon=_tracking.GS_LON,
                alt=_tracking.GS_ALT,
            )
            frame = build_command_frame(pkt)
            serial_reader.send_command(frame)
        except Exception as e:
            logger.warning("GS GPS beacon error: %s", e)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

async def _sync_system_clock() -> None:
    """Force a W32TM clock resync on Windows to minimise GS/FC timestamp skew."""
    if sys.platform != "win32":
        return
    import subprocess as _sp
    def _do_resync():
        return _sp.run(
            ["w32tm", "/resync", "/force"],
            capture_output=True, timeout=10,
        )
    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _do_resync)
        if result.returncode == 0:
            logger.info("W32TM resync succeeded: %s", result.stdout.decode().strip())
        else:
            logger.warning("W32TM resync failed (rc=%d) — run backend as administrator for clock sync: %s",
                           result.returncode, result.stderr.decode().strip())
    except _sp.TimeoutExpired:
        logger.warning("W32TM resync timed out")
    except FileNotFoundError:
        logger.warning("w32tm not found — clock sync skipped")
    except Exception:
        logger.warning("W32TM resync error", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _tracking_task, _gs_gps_task, _emulator, _emulating, _latest_packets, _latest_alarms, _latest_events

    await _sync_system_clock()

    # Attempt to resume the most recent session if it was saved within the last 5 minutes.
    # Skip resume in emulator mode — emulator packets have synthetic timestamps that
    # don't match a real session, and we always want a clean slate for debug runs.
    _emulating_now = os.getenv("ALTAIR_DEBUG", "0") == "1"
    if not _emulating_now:
        resumable = TelemetryLogger.find_resumable_session(max_age_s=300)
        if resumable:
            try:
                restored = telem_logger.resume_session(resumable)
                _latest_packets.update(restored.get("packets", {}))
                _latest_alarms[:] = restored.get("alarms", [])
                _latest_events[:] = restored.get("events", [])
                logger.info("Resumed session from %s", resumable)
            except Exception:
                logger.warning("Failed to resume session — starting fresh", exc_info=True)
                telem_logger.open_session()
        else:
            telem_logger.open_session()
    else:
        telem_logger.open_session()

    # All OTLP setup and network activity runs outside the asyncio event loop.
    # Without hosted endpoint configuration this is a no-op.
    remote_metrics.start()

    if os.getenv("ALTAIR_DEBUG", "0") == "1":
        # Debug mode: synthesise packets instead of reading from serial
        _emulating = True
        _emulator  = PacketEmulator(serial_reader._broadcast)
        asyncio.create_task(_emulator.start())
        logger.info("ALTAIR_DEBUG=1 — packet emulator active, serial skipped")
        await serial_reader._broadcast({
            "type": "status", "connected": False,
            "port": "EMULATOR", "emulating": True,
        })
    else:
        # Normal mode: auto-connect to LR-900p if present
        port = find_lr900p()
        if port:
            try:
                await serial_reader.connect(port)
            except Exception:
                pass

    # Start GS GPS reader (non-blocking; logs a warning if dongle absent)
    await gs_gps_reader.start()

    # Start telescope tracking poll loop
    _tracking_task = asyncio.create_task(_tracking_poll_loop())

    # Periodically beacon GS position to FC for optical pointing
    _gs_gps_task = asyncio.create_task(_gs_gps_beacon_loop())
    yield

    await gs_gps_reader.stop()

    if _tracking_task:
        _tracking_task.cancel()
        try:
            await _tracking_task
        except asyncio.CancelledError:
            pass
    if _gs_gps_task:
        _gs_gps_task.cancel()
        try:
            await _gs_gps_task
        except asyncio.CancelledError:
            pass
    if _emulator:
        await _emulator.stop()
    await serial_reader.disconnect()
    if mount_controller is not None and mount_controller.connected:
        await mount_controller.disconnect()
    if camera_controller.connected:
        await camera_controller.disconnect()
    telem_logger.close_session()
    await asyncio.to_thread(remote_metrics.stop)


app = FastAPI(title="ALTAIR GS", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/ports")
def get_ports():
    return list_serial_ports()


@app.get("/api/status")
def get_status():
    return {"connected": serial_reader.connected, "port": serial_reader.port_name}


@app.get("/metrics", include_in_schema=False)
async def get_metrics():
    """Prometheus scrape endpoint; reads only an in-memory telemetry snapshot."""
    return Response(
        content=metrics_exporter.render(),
        headers={"Content-Type": PROMETHEUS_CONTENT_TYPE},
    )


@app.get("/api/metrics/remote")
def get_remote_metrics_status():
    """Hosted metrics health without exposing endpoint credentials."""
    return remote_metrics.status()


@app.get("/api/gs/gps")
def get_gs_gps():
    """Current ground station GPS fix from the u-blox 7 dongle."""
    return gs_gps_reader.status_dict()


@app.get("/api/gs/gps/scan")
def get_gs_gps_scan():
    """List all COM ports visible to the backend, for debugging dongle detection."""
    import serial.tools.list_ports as lp
    from backend.gps_reader import _UBLOX7_VID, _UBLOX7_PIDS
    ports = []
    for p in lp.comports():
        ports.append({
            "device":      p.device,
            "description": p.description,
            "vid":         hex(p.vid) if p.vid else None,
            "pid":         hex(p.pid) if p.pid else None,
            "is_ublox7":   p.vid == _UBLOX7_VID and p.pid in _UBLOX7_PIDS,
        })
    return {"ports": ports}


@app.post("/api/gs/gps/connect")
async def post_gs_gps_connect(body: dict):
    """Manually open the GS GPS dongle on a specific port."""
    port = body.get("port") or None
    ok = await gs_gps_reader.start(port=port)
    return {"ok": ok, **gs_gps_reader.status_dict()}


@app.post("/api/connect")
async def post_connect(body: dict):
    port = body.get("port", "")
    baud = int(body.get("baud", 57600))
    if not port:
        port = find_lr900p() or ""
    if not port:
        return {"ok": False, "error": "No port specified and auto-detect found nothing"}
    try:
        await serial_reader.connect(port, baud)
        return {"ok": True, "port": port}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/disconnect")
async def post_disconnect():
    await serial_reader.disconnect()
    return {"ok": True}


@app.post("/api/system/restart")
async def post_system_restart():
    """Save state then trigger a reload by touching this file so watchfiles picks it up."""
    telem_logger.save_state(_latest_packets, _latest_alarms, _latest_events)
    logger.info("Restart requested via API — touching main.py to trigger reload")
    Path(__file__).touch()
    return {"restarting": True}


@app.get("/api/state/snapshot")
async def get_state_snapshot():
    """Return last-known packets, alarms, and events for frontend pre-population."""
    return {
        "packets": _latest_packets,
        "alarms":  _latest_alarms,
        "events":  _latest_events,
    }


async def _emulated_ack(cmd_id: int, cmd_seq: int = 0, status: int = 0) -> None:
    """Broadcast a synthetic ACK after a short delay so the HTTP response
    reaches the frontend before the WebSocket ACK message."""
    await asyncio.sleep(0.05)
    await serial_reader._broadcast({
        "type":    "ack",
        "cmd_id":  cmd_id,
        "cmd_seq": cmd_seq,
        "status":  status,
    })


@app.post("/api/fc/command/arm")
async def post_fc_arm():
    if _emulating:
        await _emulated_ack(0xC0)
        return {"ok": True, "emulated": True}
    from backend.commands import build_command_frame
    from telemetry.commands.arm import ArmCommandPacket
    frame = build_command_frame(ArmCommandPacket(arm_state=1))
    ok = serial_reader.send_command(frame)
    return {"ok": ok, "error": None if ok else "Serial port not connected"}


@app.post("/api/fc/command/launch_ok")
async def post_fc_launch_ok():
    if _emulating:
        await _emulated_ack(0xC1)
        return {"ok": True, "emulated": True}
    from backend.commands import build_command_frame
    from telemetry.commands.launch_ok import LaunchOkCommandPacket
    frame = build_command_frame(LaunchOkCommandPacket(confirm=1))
    ok = serial_reader.send_command(frame)
    return {"ok": ok, "error": None if ok else "Serial port not connected"}


@app.post("/api/fc/command/ping")
async def post_fc_ping():
    if _emulating:
        await _emulated_ack(0xC2)
        return {"ok": True, "emulated": True}
    from backend.commands import build_command_frame
    from telemetry.commands.ping import PingCommandPacket
    frame = build_command_frame(PingCommandPacket(token=0))
    ok = serial_reader.send_command(frame)
    return {"ok": ok, "error": None if ok else "Serial port not connected"}


@app.post("/api/fc/command/update_setting")
async def post_fc_update_setting(body: dict):
    try:
        field_id = int(body["field_id"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "field_id must be an integer"}
    if not (0 <= field_id <= 17):
        return {"ok": False, "error": f"field_id {field_id} out of range [0, 17]"}
    try:
        value = float(body["value"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "value must be a number"}
    if not math.isfinite(value):
        return {"ok": False, "error": "value must be finite"}

    if _emulating:
        await _emulated_ack(0xC3)
        return {"ok": True, "emulated": True}

    from backend.commands import build_command_frame
    from telemetry.commands.update_setting import UpdateSettingCommandPacket
    frame = build_command_frame(UpdateSettingCommandPacket(field_id=field_id, value=value))
    ok = serial_reader.send_command(frame)
    return {"ok": ok, "error": None if ok else "Serial port not connected"}


@app.get("/api/radio/config")
async def get_radio_config(refresh: bool = False):
    """Return radio defaults from settings.toml plus the live GS modem config.

    By default returns the cached live config from the last successful modem
    read — no serial traffic is generated.  Pass ?refresh=1 to force a fresh
    read from the modem (suspends telemetry reception for ~1-4 s).
    """
    if refresh and serial_reader.connected:
        await serial_reader.read_radio_config(timeout=3.0)
    return {
        "defaults": _radio_defaults,
        "live":     serial_reader._lr_live_config,
        "linked":   serial_reader._lr_linked,
    }


@app.post("/api/radio/config")
async def post_radio_config(body: dict):
    """
    Apply a new radio configuration to both FC and GS modem.

    Body: { "data_rate": 0|1|2, "tx_power": 0|1|2, "channel": 0-63 }

    Blocked when the FC is in ARM state or later (flight_stage >= 1).
    The FC is commanded first; its ACK gates the GS modem switch so the
    ACK itself is always transmitted on the old channel.
    """
    global _fc_radio_ack_event

    # Validate
    try:
        data_rate = int(body["data_rate"])
        tx_power  = int(body["tx_power"])
        channel   = int(body["channel"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "data_rate, tx_power, and channel are required integers"}

    if not (0 <= data_rate <= 2):
        return {"ok": False, "error": "data_rate must be 0, 1, or 2"}
    if not (0 <= tx_power <= 2):
        return {"ok": False, "error": "tx_power must be 0, 1, or 2"}
    if not (0 <= channel <= 63):
        return {"ok": False, "error": "channel must be 0-63"}

    # Block if armed or beyond (flight_stage >= 1)
    event_pkt = _latest_packets.get("Event") or _latest_packets.get("event")
    if event_pkt:
        stage = next(
            (int(f["value"]) for f in event_pkt.get("fields", []) if f["name"] == "flight_stage"),
            0,
        )
        if stage >= 1:
            return {"ok": False, "error": "Radio config locked — vehicle is armed or in flight"}

    if _emulating:
        # In emulator mode, synthesise both ACKs without touching hardware
        await _emulated_ack(0xC5)
        return {"ok": True, "emulated": True, "gs_switched": False}

    if not serial_reader.connected:
        return {"ok": False, "error": "Serial port not connected"}

    # Arm a fresh ACK event before sending the command so we can't miss it
    _fc_radio_ack_event = asyncio.Event()

    from backend.commands import build_command_frame
    from telemetry.commands.radio_config import RadioConfigCommandPacket
    frame = build_command_frame(RadioConfigCommandPacket(
        data_rate=data_rate, tx_power=tx_power, channel=channel,
    ))
    ok = serial_reader.send_command(frame)
    if not ok:
        _fc_radio_ack_event = None
        return {"ok": False, "error": "Serial write failed"}

    # Fire off the GS modem switch concurrently — it will wait for the FC ACK event
    gs_switched = False
    if serial_reader._lr_linked:
        gs_switched = await serial_reader.write_radio_config(
            data_rate=data_rate,
            tx_power=tx_power,
            channel=channel,
            fc_ack_event=_fc_radio_ack_event,
            timeout=10.0,
        )
    else:
        logger.warning("post_radio_config: GS modem not linked — FC commanded but GS modem not reconfigured")

    _fc_radio_ack_event = None
    await serial_reader._broadcast({
        "type":       "radio_config",
        "data_rate":  data_rate,
        "tx_power":   tx_power,
        "channel":    channel,
        "gs_switched": gs_switched,
    })
    return {"ok": True, "gs_switched": gs_switched}


@app.post("/api/debug/emulate")
async def post_debug_emulate(body: dict):
    """Toggle the packet emulator on or off at runtime."""
    global _emulator, _emulating
    enabled = bool(body.get("enabled", False))

    if enabled and not _emulating:
        _emulating = True
        _emulator  = PacketEmulator(serial_reader._broadcast)
        asyncio.create_task(_emulator.start())
        await serial_reader.disconnect()   # ensure serial is not competing
        await serial_reader._broadcast({
            "type": "status", "connected": False,
            "port": "EMULATOR", "emulating": True,
        })
        logger.info("Packet emulator enabled via REST")
        return {"ok": True, "emulating": True}

    if not enabled and _emulating:
        _emulating = False
        if _emulator:
            await _emulator.stop()
            _emulator = None
        await serial_reader._broadcast({
            "type": "status", "connected": False,
            "port": "", "emulating": False,
        })
        logger.info("Packet emulator disabled via REST")
        return {"ok": True, "emulating": False}

    return {"ok": True, "emulating": _emulating}


@app.get("/api/debug/registry_fields")
async def get_debug_registry_fields():
    from backend.packets import REGISTRY
    out = {}
    for pid, entry in REGISTRY.items():
        out[entry["label"]] = entry["fields"]
    return out


@app.get("/api/debug/emulate")
async def get_debug_emulate():
    return {"emulating": _emulating}


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    serial_reader.add_client(ws)
    logger.info("WebSocket client connected (%d total)", len(serial_reader._clients))
    # Send known packet labels so the frontend can show placeholders immediately
    await ws.send_text(json.dumps({
        "type": "registry",
        "labels": [entry["label"] for entry in REGISTRY.values()],
    }))
    # Send alarm rules so the frontend can draw threshold markers and rule context
    await ws.send_text(json.dumps({
        "type": "alarm_rules",
        "rules": ALARM_RULES,
    }))
    # Send event metadata so the frontend knows stage names
    await ws.send_text(json.dumps({
        "type":         "event_meta",
        "stage_names":  FLIGHT_STAGE_NAMES,
    }))
    # Send current connection status immediately on connect
    await ws.send_text(json.dumps({
        "type": "status",
        "connected": serial_reader.connected,
        "port": serial_reader.port_name,
    }))
    # Send current GS GPS status so the badge is accurate on page load
    await ws.send_text(json.dumps({
        "type":      "gs_gps_status",
        "connected": gs_gps_reader.connected,
        "has_fix":   gs_gps_reader.fix is not None,
        "port":      gs_gps_reader.port_name,
    }))
    # Send the current fix if one exists — without this, clients that connect
    # after the fix was acquired never receive the coordinates
    if gs_gps_reader.fix is not None:
        await ws.send_text(json.dumps({"type": "gs_gps", **gs_gps_reader.fix}))
    # Tell frontend to fetch the state snapshot (packets/alarms/events from last session)
    await ws.send_text(json.dumps({"type": "snapshot_ready"}))
    try:
        while True:
            # Keep the connection alive; client messages are not expected
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        serial_reader.remove_client(ws)
        logger.info("WebSocket client disconnected (%d remaining)", len(serial_reader._clients))


# ---------------------------------------------------------------------------
# Telescope WebSocket endpoint
# ---------------------------------------------------------------------------

@app.websocket("/api/ws/telescope")
async def telescope_ws_endpoint(ws: WebSocket):
    await ws.accept()
    _telescope_clients.add(ws)
    logger.info("Telescope WS client connected (%d total)", len(_telescope_clients))
    # Send current status immediately
    await ws.send_text(json.dumps({
        "type":   "telescope_status",
        "mount":  mount_controller.status_dict() if mount_controller else None,
        "camera": camera_controller.status_dict(),
        "tracking_enabled": _tracking_enabled,
    }))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _telescope_clients.discard(ws)
        logger.info("Telescope WS client disconnected (%d remaining)", len(_telescope_clients))


# ---------------------------------------------------------------------------
# Telescope REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/telescope/status")
def get_telescope_status():
    return {
        "mount":            mount_controller.status_dict() if mount_controller else None,
        "camera":           camera_controller.status_dict(),
        "tracking_enabled": _tracking_enabled,
        "latest_gps":       _latest_gps,
    }


@app.post("/api/telescope/mount/connect")
async def post_mount_connect(body: dict):
    """
    Body: { "mount_type": "nexstar"|"am5", "port": "COM10" }
    For AM5, port is optional (ASCOM driver owns the COM port).
    For AM5, an optional "progid" key overrides the default ASCOM ProgID.
    """
    global mount_controller
    mount_type = body.get("mount_type", "nexstar")
    port       = body.get("port", "")
    progid     = body.get("progid", "")

    if not port and mount_type == "nexstar":
        return {"ok": False, "error": "port required for NexStar"}

    # Disconnect any existing mount first
    if mount_controller is not None and mount_controller.connected:
        await mount_controller.disconnect()

    try:
        mount_controller = create_mount(mount_type)
        if mount_type == "am5":
            await mount_controller.connect(port=port, progid=progid)
        else:
            await mount_controller.connect(port=port)
        await _broadcast_telescope({"type": "telescope_status",
                                    "mount": mount_controller.status_dict()})
        return {"ok": True, "mount_type": mount_type}
    except Exception as e:
        mount_controller = None
        return {"ok": False, "error": str(e)}


@app.post("/api/telescope/mount/disconnect")
async def post_mount_disconnect():
    global mount_controller
    if mount_controller is not None:
        await mount_controller.disconnect()
    await _broadcast_telescope({"type": "telescope_status",
                                 "mount": mount_controller.status_dict() if mount_controller else None})
    return {"ok": True}


@app.post("/api/telescope/mount/goto")
async def post_mount_goto(body: dict):
    if mount_controller is None or not mount_controller.connected:
        return {"ok": False, "error": "Mount not connected"}
    if mount_controller.mount_type == "am5":
        ra  = float(body.get("ra_hours", 0))
        dec = float(body.get("dec_deg",  0))
        asyncio.create_task(mount_controller.goto(ra_hours=ra, dec_deg=dec))
        return {"ok": True, "ra_hours": ra, "dec_deg": dec}
    else:
        az = float(body.get("azimuth",   0))
        el = float(body.get("elevation", 0))
        asyncio.create_task(mount_controller.goto(azimuth=az, elevation=el))
        return {"ok": True, "azimuth": az, "elevation": el}


@app.post("/api/telescope/tracking")
async def post_tracking(body: dict):
    global _tracking_enabled
    if mount_controller is None or not mount_controller.connected:
        return {"ok": False, "error": "Mount not connected"}
    _tracking_enabled = bool(body.get("enabled", False))
    logger.info("Telescope tracking %s", "enabled" if _tracking_enabled else "disabled")
    await _broadcast_telescope({"type": "telescope_status",
                                 "tracking_enabled": _tracking_enabled})
    return {"ok": True, "tracking_enabled": _tracking_enabled}


@app.post("/api/telescope/camera/connect")
async def post_camera_connect():
    try:
        await camera_controller.connect()
        await _broadcast_telescope({"type": "telescope_status",
                                    "camera": camera_controller.status_dict()})
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/telescope/camera/disconnect")
async def post_camera_disconnect():
    await camera_controller.disconnect()
    await _broadcast_telescope({"type": "telescope_status",
                                 "camera": camera_controller.status_dict()})
    return {"ok": True}


@app.post("/api/telescope/camera/settings")
async def post_camera_settings(body: dict):
    if "gain" in body:
        await camera_controller.set_gain(int(body["gain"]))
    if "exposure_ms" in body:
        await camera_controller.set_exposure_ms(int(body["exposure_ms"]))
    return {"ok": True, "camera": camera_controller.status_dict()}


@app.post("/api/telescope/camera/capture")
async def post_camera_capture(body: dict):
    # Build output path inside _capture_dir; caller may override filename only
    filename = body.get("filename") or f"frame_{int(time.time())}.tif"
    output_path = _capture_dir / filename
    _capture_dir.mkdir(parents=True, exist_ok=True)

    # Assemble capture metadata from all available live sources
    capture_meta: dict = {"capture_utc": time.time()}

    gps = _latest_gps
    if gps:
        capture_meta["payload_lat"]       = gps.get("lat")
        capture_meta["payload_lon"]       = gps.get("lon")
        capture_meta["payload_alt_m"]     = gps.get("alt")
        capture_meta["payload_alt_rel_m"] = gps.get("relative_alt")
        capture_meta["payload_hdg_deg"]   = gps.get("hdg")

    tracking = _latest_tracking
    if tracking:
        capture_meta["azimuth"]    = tracking.get("azimuth")
        capture_meta["elevation"]  = tracking.get("elevation")
        capture_meta["ra_hours"]   = tracking.get("ra_hours")
        capture_meta["dec_deg"]    = tracking.get("dec_deg")
        capture_meta["distance_m"] = tracking.get("distance_m")
        capture_meta["slant_m"]    = tracking.get("slant_m")
        capture_meta["gs_lat"]     = tracking.get("gs_lat")
        capture_meta["gs_lon"]     = tracking.get("gs_lon")
        capture_meta["gs_alt"]     = tracking.get("gs_alt")

    if mount_controller is not None:
        capture_meta["mount_type"] = mount_controller.mount_type
        capture_meta["mount_port"] = mount_controller.port_name

    # Remove None values — EXIF injection skips missing keys anyway,
    # but this keeps the description block clean
    capture_meta = {k: v for k, v in capture_meta.items() if v is not None}

    try:
        saved = await camera_controller.capture(output_path, metadata=capture_meta)
        return {"ok": True, "path": saved}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Gallery REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/gallery/images")
def get_gallery_images():
    return _list_images()


@app.get("/api/gallery/config")
def get_gallery_config():
    return {"capture_dir": str(_capture_dir)}


@app.post("/api/gallery/config")
def post_gallery_config(body: dict):
    global _capture_dir
    raw = body.get("capture_dir", "")
    if not raw:
        return {"ok": False, "error": "capture_dir required"}
    path = Path(raw)
    if not path.is_absolute():
        path = _REPO_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    _capture_dir = path
    _jpeg_cache.clear()
    logger.info("Gallery capture dir set to %s", _capture_dir)
    return {"ok": True, "capture_dir": str(_capture_dir)}


@app.get("/api/gallery/browse")
def get_gallery_browse(path: str = ""):
    """
    List the contents of a directory for the save-directory file picker.
    Returns parent path and a sorted list of subdirectories.
    Query param: path (absolute). Defaults to the current capture directory.
    """
    if path:
        target = Path(path)
    else:
        target = _capture_dir

    # Resolve and normalise
    try:
        target = target.resolve()
    except Exception:
        target = _capture_dir.resolve()

    # Walk up to a real directory if the path doesn't exist yet
    while not target.is_dir() and target != target.parent:
        target = target.parent

    # Parent for the "go up" button (None at filesystem root)
    parent = str(target.parent) if target != target.parent else None

    dirs = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: e.name.lower()):
            if entry.is_dir() and not entry.name.startswith('.'):
                dirs.append(entry.name)
    except PermissionError:
        pass

    return {
        "path":   str(target),
        "parent": parent,
        "dirs":   dirs,
    }


@app.get("/api/gallery/thumb/{filename}")
def get_gallery_thumb(filename: str):
    result = _get_jpegs(filename)
    if result is None:
        return Response(status_code=404)
    return Response(content=result[0], media_type="image/jpeg")


@app.get("/api/gallery/full/{filename}")
def get_gallery_full(filename: str):
    result = _get_jpegs(filename)
    if result is None:
        return Response(status_code=404)
    return Response(content=result[1], media_type="image/jpeg")


# ---------------------------------------------------------------------------
# Gallery HTML page
# ---------------------------------------------------------------------------

_GALLERY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ALTAIR V2 — Image Gallery</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg:      #0d1117;
    --surface: #161b22;
    --border:  #1e2d3d;
    --accent:  #00e5ff;
    --green:   #00ff88;
    --muted:   #607080;
    --text:    #c9d1d9;
    --font:    'Courier New', Courier, monospace;
    --header-h: 40px;
  }
  body {
    background: var(--bg); color: var(--text);
    font-family: var(--font); font-size: 13px;
    height: 100vh; display: flex; flex-direction: column; overflow: hidden;
  }

  /* ── Header ── */
  header {
    flex-shrink: 0; height: var(--header-h);
    display: flex; align-items: center; gap: 12px;
    padding: 0 16px; background: var(--surface);
    border-bottom: 1px solid var(--border);
  }
  header h1 { font-size: 12px; letter-spacing: 2px; color: var(--accent); text-transform: uppercase; white-space: nowrap; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); flex-shrink: 0; }
  .dot.off { background: var(--muted); }
  input[type=text] {
    background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
    color: var(--text); font-family: var(--font); font-size: 11px;
    padding: 3px 8px; width: 260px;
  }
  button {
    background: transparent; border: 1px solid var(--accent); border-radius: 4px;
    color: var(--accent); font-family: var(--font); font-size: 11px;
    padding: 3px 10px; cursor: pointer; white-space: nowrap;
  }
  button:hover { background: rgba(0,229,255,0.08); }
  .spacer { flex: 1; }
  #count-label { color: var(--muted); font-size: 11px; white-space: nowrap; }

  /* ── Main split layout ── */
  #main {
    flex: 1; display: flex; flex-direction: column; overflow: hidden;
  }

  /* ── Top: image viewer (60% height) ── */
  #viewer {
    flex: 0 0 60%; display: flex; align-items: center; justify-content: center;
    background: #000; border-bottom: 2px solid var(--border); position: relative;
    overflow: hidden;
  }
  #viewer img {
    max-width: 100%; max-height: 100%; object-fit: contain; display: block;
  }
  #viewer-placeholder {
    color: var(--muted); font-size: 12px; text-align: center; line-height: 1.8;
  }
  #viewer-caption {
    position: absolute; bottom: 0; left: 0; right: 0;
    background: rgba(0,0,0,0.65); padding: 5px 12px;
    display: flex; justify-content: space-between; align-items: center;
    font-size: 11px;
  }
  #viewer-caption span:first-child { color: var(--accent); }
  #viewer-caption span:last-child  { color: var(--muted); }

  /* ── Bottom: image list (40% height, scrollable) ── */
  #list-pane {
    flex: 1; overflow-y: auto; background: var(--bg);
  }
  #list-pane table {
    width: 100%; border-collapse: collapse;
  }
  #list-pane thead th {
    position: sticky; top: 0; background: var(--surface);
    color: var(--muted); font-size: 10px; letter-spacing: 1px; text-transform: uppercase;
    padding: 6px 10px; border-bottom: 1px solid var(--border); text-align: left;
    font-weight: normal;
  }
  #list-pane tbody tr {
    border-bottom: 1px solid var(--border); cursor: pointer;
    transition: background 0.1s;
  }
  #list-pane tbody tr:hover   { background: var(--surface); }
  #list-pane tbody tr.active  { background: #0e2030; border-left: 2px solid var(--accent); }
  #list-pane tbody tr.new-row { border-left: 2px solid var(--green); }
  #list-pane td { padding: 5px 10px; vertical-align: middle; }
  .td-thumb { width: 56px; }
  .td-thumb img { width: 48px; height: 36px; object-fit: contain; background: #000; display: block; border-radius: 2px; }
  .td-name  { color: var(--accent); font-size: 11px; }
  .td-time  { color: var(--muted);  font-size: 11px; white-space: nowrap; }
  .td-size  { color: var(--muted);  font-size: 11px; white-space: nowrap; text-align: right; }

  #empty-row td { color: var(--muted); padding: 30px; text-align: center; cursor: default; }
</style>
</head>
<body>

<header>
  <h1>ALTAIR V2 &mdash; Gallery</h1>
  <div class="dot off" id="refresh-dot"></div>
  <div class="spacer"></div>
  <span id="count-label">0 images</span>
  <label style="color:var(--muted);font-size:11px">Dir:</label>
  <input type="text" id="dir-input" placeholder="captures/">
  <button onclick="setDir()">Set</button>
</header>

<div id="main">
  <!-- Top: viewer -->
  <div id="viewer">
    <div id="viewer-placeholder">Select an image from the list below.</div>
    <img id="viewer-img" src="" alt="" style="display:none">
    <div id="viewer-caption" style="display:none">
      <span id="cap-name"></span>
      <span id="cap-info"></span>
    </div>
  </div>

  <!-- Bottom: list -->
  <div id="list-pane">
    <table>
      <thead>
        <tr>
          <th class="td-thumb"></th>
          <th>Filename</th>
          <th>Time</th>
          <th style="text-align:right">Size</th>
        </tr>
      </thead>
      <tbody id="list-body">
        <tr id="empty-row"><td colspan="4">No images captured yet.</td></tr>
      </tbody>
    </table>
  </div>
</div>

<script>
const REFRESH_MS = 5000
let knownFiles = new Set()
let newCount = 0
let activeFile = null
const baseTitle = 'ALTAIR V2 \u2014 Image Gallery'

function fmt(mtime) {
  return new Date(mtime * 1000).toLocaleTimeString()
}

function selectImage(img) {
  activeFile = img.filename
  const viewImg = document.getElementById('viewer-img')
  const placeholder = document.getElementById('viewer-placeholder')
  const caption = document.getElementById('viewer-caption')
  viewImg.src = img.full_url
  viewImg.style.display = 'block'
  placeholder.style.display = 'none'
  caption.style.display = 'flex'
  document.getElementById('cap-name').textContent = img.filename
  document.getElementById('cap-info').textContent = fmt(img.mtime) + '  \u2022  ' + img.size_kb + ' KB'
  // Update active row highlight
  document.querySelectorAll('#list-body tr').forEach(r => r.classList.remove('active'))
  const row = document.getElementById('row-' + CSS.escape(img.filename))
  if (row) {
    row.classList.add('active')
    row.scrollIntoView({block: 'nearest'})
  }
}

async function loadImages() {
  const dot = document.getElementById('refresh-dot')
  dot.classList.remove('off')
  try {
    const res = await fetch('/api/gallery/images')
    const images = await res.json()
    const tbody = document.getElementById('list-body')
    const countLabel = document.getElementById('count-label')

    countLabel.textContent = images.length + ' image' + (images.length !== 1 ? 's' : '')

    if (images.length === 0) {
      tbody.innerHTML = '<tr id="empty-row"><td colspan="4">No images captured yet.</td></tr>'
      dot.classList.add('off')
      return
    }

    // Detect new arrivals
    const incoming = new Set(images.map(i => i.filename))
    const fresh = new Set([...incoming].filter(f => !knownFiles.has(f)))
    if (knownFiles.size > 0 && fresh.size > 0) {
      newCount += fresh.size
      if (document.hidden) updateTitle()
    }
    knownFiles = incoming

    tbody.innerHTML = images.map(img => `
      <tr id="row-${img.filename}"
          class="${fresh.has(img.filename) ? 'new-row' : ''}${activeFile === img.filename ? ' active' : ''}"
          onclick="selectImage(${JSON.stringify(img)})">
        <td class="td-thumb"><img src="${img.url}" loading="lazy" alt=""></td>
        <td class="td-name">${img.filename}</td>
        <td class="td-time">${fmt(img.mtime)}</td>
        <td class="td-size">${img.size_kb} KB</td>
      </tr>`).join('')

    // Auto-select newest if nothing selected yet
    if (!activeFile && images.length > 0) selectImage(images[0])

    // If the active image was just updated (re-captured), refresh the viewer
    if (activeFile) {
      const current = images.find(i => i.filename === activeFile)
      if (current) {
        const viewImg = document.getElementById('viewer-img')
        if (viewImg.src !== location.origin + current.full_url) {
          viewImg.src = current.full_url
        }
      }
    }
  } catch(e) {
    console.warn('Gallery fetch error:', e)
  }
  setTimeout(() => dot.classList.add('off'), 400)
}

function updateTitle() {
  document.title = newCount > 0 ? '(' + newCount + ' new) ' + baseTitle : baseTitle
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) { newCount = 0; updateTitle() }
})

async function loadConfig() {
  const res = await fetch('/api/gallery/config')
  const cfg = await res.json()
  document.getElementById('dir-input').value = cfg.capture_dir
}

async function setDir() {
  const val = document.getElementById('dir-input').value.trim()
  if (!val) return
  const res = await fetch('/api/gallery/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({capture_dir: val})
  })
  const data = await res.json()
  if (data.ok) {
    document.getElementById('dir-input').value = data.capture_dir
    knownFiles.clear()
    activeFile = null
    loadImages()
  }
}

loadConfig()
loadImages()
setInterval(loadImages, REFRESH_MS)
</script>
</body>
</html>"""


@app.get("/gallery", response_class=HTMLResponse)
def get_gallery():
    return HTMLResponse(content=_GALLERY_HTML)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    _server = uvicorn.Server(uvicorn.Config(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    ))

    # Prevent uvicorn from installing its own signal handlers — on Windows,
    # asyncio.run() blocks the main thread in the ProactorEventLoop's C-level
    # I/O completion port, so signal.signal() handlers never fire.  Instead we
    # run uvicorn's serve() coroutine on a daemon thread and spin the main
    # thread in a KeyboardInterrupt-sensitive sleep loop.
    _server.install_signal_handlers = lambda: None

    _serve_thread = threading.Thread(
        target=_server.run,
        daemon=True,
        name="uvicorn-serve",
    )
    _serve_thread.start()

    try:
        while _serve_thread.is_alive():
            _serve_thread.join(timeout=0.5)
    except KeyboardInterrupt:
        logger.info("Ctrl+C received — shutting down")
        _server.should_exit = True
        _serve_thread.join(timeout=10)
