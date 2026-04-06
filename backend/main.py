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
import json
import logging
import sys
from contextlib import asynccontextmanager
from typing import Any

import serial
import serial.tools.list_ports
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from backend.packets import REGISTRY, HEADER_SIZE, CRC_SIZE, MIN_FRAME, SYNC_BYTE, decode_frame
from backend.packets import _HEADER, _CRC
from backend.tracking import calculate_tracking_params
from backend.mount import BaseMountController, create_mount
from backend.camera import CameraController

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

            await self._broadcast({"type": "packet", **result})

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        # Intercept GPS packets to keep _latest_gps up-to-date for telescope tracking
        if msg.get("type") == "packet" and msg.get("label") == "GPS":
            global _latest_gps
            fields = {f["name"]: f["value"] for f in msg.get("fields", [])}
            _latest_gps = fields  # keys: lat, lon, alt, relative_alt, hdg

        if not self._clients:
            return
        data = json.dumps(msg)
        dead: set[WebSocket] = set()
        for ws in self._clients:
            try:
                await ws.send_text(data)
            except Exception:
                dead.add(ws)
        self._clients -= dead


serial_reader = SerialReader()

# ---------------------------------------------------------------------------
# Telescope hardware controllers
# ---------------------------------------------------------------------------

mount_controller: BaseMountController | None = None
camera_controller = CameraController()

# Latest GPS data from telemetry — updated by _gps_forwarder, read by tracking poll
_latest_gps: dict | None = None

# Telescope WebSocket clients (separate from telemetry WS)
_telescope_clients: set[WebSocket] = set()

_tracking_task: asyncio.Task | None = None
_tracking_enabled = False


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
    _telescope_clients -= dead


async def _tracking_poll_loop(interval_s: float = 1.0) -> None:
    """
    Periodically compute tracking params from latest GPS and broadcast to
    telescope clients. Also commands the mount if tracking is enabled.
    """
    global _latest_gps, _tracking_enabled
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
    global _tracking_task
    # Auto-connect on startup if LR-900p is present
    port = find_lr900p()
    if port:
        try:
            await serial_reader.connect(port)
        except Exception:
            pass
    # Start telescope tracking poll loop
    _tracking_task = asyncio.create_task(_tracking_poll_loop())
    yield
    if _tracking_task:
        _tracking_task.cancel()
        try:
            await _tracking_task
        except asyncio.CancelledError:
            pass
    await serial_reader.disconnect()
    if mount_controller is not None and mount_controller.connected:
        await mount_controller.disconnect()
    if camera_controller.connected:
        await camera_controller.disconnect()


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


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    serial_reader.add_client(ws)
    logger.info("WebSocket client connected (%d total)", len(serial_reader._clients))
    # Send current connection status immediately on connect
    await ws.send_text(json.dumps({
        "type": "status",
        "connected": serial_reader.connected,
        "port": serial_reader.port_name,
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

@app.websocket("/ws/telescope")
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
    output_path = body.get("output_path", "captures/frame.tif")
    try:
        saved = await camera_controller.capture(output_path)
        return {"ok": True, "path": saved}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
