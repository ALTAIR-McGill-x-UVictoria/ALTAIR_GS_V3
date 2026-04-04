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
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-connect on startup if LR-900p is present
    port = find_lr900p()
    if port:
        try:
            await serial_reader.connect(port)
        except Exception:
            pass
    yield
    await serial_reader.disconnect()


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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
