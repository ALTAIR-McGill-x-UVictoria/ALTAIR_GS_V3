"""
ALTAIR V2 Ground Station — Python Backend

Responsibilities:
  - Scan for / open the LR-900p serial port (CP210x auto-detect)
  - Decode binary telemetry frames (mirrors altairfc wire format)
  - Stream decoded packets to connected browser clients via WebSocket
  - Expose a REST API for port listing and connection control

Start with:
    uvicorn backend.main:app --reload --port 8000

Or via the helper script:
    python -m backend.main
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import signal
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import serial
import serial.tools.list_ports
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response

from backend.packets import REGISTRY, HEADER_SIZE, CRC_SIZE, MIN_FRAME, SYNC_BYTE, decode_frame
from backend.packets import _HEADER, _CRC
from backend.tracking import calculate_tracking_params
from backend.mount import BaseMountController, create_mount
from backend.camera import CameraController
from backend.logging_manager import TelemetryLogger
from backend.alarms import ALARM_RULES
from backend.emulator import PacketEmulator
from backend.events import EVENT_DEFS, FLIGHT_STAGE_NAMES, BOOLEAN_EVENT_FIELDS
from backend.gps_reader import GsGpsReader

logger = logging.getLogger("gs.backend")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
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
        self._buf  = bytearray()
        self._seq_prev: dict[int, int]   = {}
        self.connected = False
        self.port_name = ""
        self._clients: set[WebSocket]    = set()

    def add_client(self, ws: WebSocket)    -> None: self._clients.add(ws)
    def remove_client(self, ws: WebSocket) -> None: self._clients.discard(ws)

    async def connect(self, port: str, baud: int = 57600) -> None:
        if self.connected:
            await self.disconnect()
        try:
            self._port = serial.Serial(port, baud, timeout=0)
            self.connected = True
            self.port_name = port
            self._buf.clear()
            self._seq_prev.clear()
            self._task = asyncio.create_task(self._read_loop())
            logger.info("Serial port %s opened @ %d baud", port, baud)
            await self._broadcast({"type": "status", "connected": True, "port": port})
        except serial.SerialException as e:
            logger.error("Could not open %s: %s", port, e)
            raise

    async def disconnect(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._port and self._port.is_open:
            self._port.close()
        self.connected = False
        self.port_name = ""
        logger.info("Serial port closed")
        await self._broadcast({"type": "status", "connected": False, "port": ""})

    async def _read_loop(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            try:
                chunk = await loop.run_in_executor(None, self._read_chunk)
                if chunk:
                    self._buf.extend(chunk)
                    await self._process_buffer()
                else:
                    await asyncio.sleep(0.005)
            except serial.SerialException as e:
                logger.error("Serial read error: %s", e)
                await self._broadcast({"type": "status", "connected": False, "port": "", "error": str(e)})
                break

    def _read_chunk(self) -> bytes:
        if self._port and self._port.is_open and self._port.in_waiting:
            return self._port.read(self._port.in_waiting)
        return b""

    def send_command(self, frame: bytes) -> bool:
        """
        Write a pre-built command frame to the serial port.
        Blocking — call via run_in_executor from async route handlers.
        Returns True on success.
        """
        if self._port is None or not self._port.is_open:
            logger.warning("send_command: serial port not open")
            return False
        try:
            self._port.write(frame)
            logger.info(
                "send_command: sent %d bytes (CMD_ID=0x%02X)",
                len(frame), frame[1] if len(frame) > 1 else 0xFF,
            )
            return True
        except serial.SerialException as e:
            logger.error("send_command: write error: %s", e)
            return False

    async def _process_buffer(self) -> None:
        while len(self._buf) >= MIN_FRAME:
            sync_pos = self._buf.find(SYNC_BYTE)
            if sync_pos == -1:
                self._buf.clear()
                return
            if sync_pos > 0:
                del self._buf[:sync_pos]

            if len(self._buf) < 13:  # need full header
                return

            _, _, _, _, length = _HEADER.unpack_from(self._buf, 0)
            frame_size = HEADER_SIZE + length + CRC_SIZE

            if len(self._buf) < frame_size:
                return

            frame = bytes(self._buf[:frame_size])
            del self._buf[:frame_size]

            result = decode_frame(frame)
            if result is None:
                continue

            # Sequence gap detection
            pkt_id = result["packet_id"]
            seq    = result["seq"]
            prev   = self._seq_prev.get(pkt_id)
            if prev is not None:
                expected = (prev + 1) & 0xFF
                if seq != expected:
                    dropped = (seq - expected) & 0xFF
                    logger.warning("SEQ gap PKT_ID=0x%02X: expected %d got %d (%d dropped)",
                                   pkt_id, expected, seq, dropped)
                    result["dropped"] = dropped
            self._seq_prev[pkt_id] = seq

            result["wall_ms"] = round(time.time() * 1000)
            await self._broadcast({"type": "packet", **result})

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        # Intercept ACK packets — re-emit as a dedicated "ack" message type
        # so the frontend can react without treating it as a regular telemetry packet.
        if msg.get("type") == "packet" and msg.get("label", "").lower() == "ack":
            fields = {f["name"]: int(f["value"]) for f in msg.get("fields", [])}
            ack_msg = json.dumps({
                "type":    "ack",
                "cmd_id":  fields.get("cmd_id",  0),
                "cmd_seq": fields.get("cmd_seq", 0),
                "status":  fields.get("status",  0),  # 0=ok, 1=rejected
            })
            for ws in list(self._clients):
                try:
                    await ws.send_text(ack_msg)
                except Exception:
                    pass
            return  # do not broadcast ACK as a regular packet

        # Intercept GPS packets to keep _latest_gps up-to-date for telescope tracking
        if msg.get("type") == "packet" and msg.get("label", "").lower() == "gps":
            global _latest_gps
            fields = {f["name"]: f["value"] for f in msg.get("fields", [])}
            _latest_gps = fields  # keys: lat, lon, alt, relative_alt, hdg

        # Log packet to CSV and evaluate alarms
        if msg.get("type") == "packet":
            alarms = telem_logger.ingest(msg)
            for alarm in alarms:
                alarm_msg = json.dumps({"type": "alarm", **alarm})
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

            for ev in events_to_emit:
                ev_msg = json.dumps({
                    "type":      "event",
                    "wall_time": time.time(),
                    **ev,
                })
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
_emulator: PacketEmulator | None = None
_emulating = False

# Previous values of Event packet fields — used to detect transitions
_event_prev: dict[str, int] = {}

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
_tracking_enabled = False

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


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _tracking_task, _emulator, _emulating
    telem_logger.open_session()

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
    yield

    await gs_gps_reader.stop()

    if _tracking_task:
        _tracking_task.cancel()
        try:
            await _tracking_task
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


@app.get("/api/gs/gps")
def get_gs_gps():
    """Current ground station GPS fix from the u-blox 7 dongle."""
    return gs_gps_reader.status_dict()


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
    """Gracefully exit so uvicorn --reload restarts the process."""
    logger.info("Restart requested via API — sending SIGTERM")
    os.kill(os.getpid(), signal.SIGTERM)
    return {"restarting": True}


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
    ok = await asyncio.get_event_loop().run_in_executor(None, serial_reader.send_command, frame)
    return {"ok": ok, "error": None if ok else "Serial port not connected"}


@app.post("/api/fc/command/launch_ok")
async def post_fc_launch_ok():
    if _emulating:
        await _emulated_ack(0xC1)
        return {"ok": True, "emulated": True}
    from backend.commands import build_command_frame
    from telemetry.commands.launch_ok import LaunchOkCommandPacket
    frame = build_command_frame(LaunchOkCommandPacket(confirm=1))
    ok = await asyncio.get_event_loop().run_in_executor(None, serial_reader.send_command, frame)
    return {"ok": ok, "error": None if ok else "Serial port not connected"}


@app.post("/api/fc/command/ping")
async def post_fc_ping():
    if _emulating:
        await _emulated_ack(0xC2)
        return {"ok": True, "emulated": True}
    from backend.commands import build_command_frame
    from telemetry.commands.ping import PingCommandPacket
    frame = build_command_frame(PingCommandPacket(token=0))
    ok = await asyncio.get_event_loop().run_in_executor(None, serial_reader.send_command, frame)
    return {"ok": ok, "error": None if ok else "Serial port not connected"}


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
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
