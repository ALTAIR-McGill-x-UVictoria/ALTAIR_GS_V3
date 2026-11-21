"""
GS-to-GS telemetry relay over a private network (e.g. ZeroTier).

One ground station — the *host*, the one with the LR-900p radio (or the FC
tunnel) — streams every decoded telemetry packet it receives to any number of
*peer* ground stations running this same software. Each peer feeds the
relayed packets through its own normal pipeline (CSV logging, alarm engine,
event detection, telescope tracking, UI), exactly as if they had come off a
local radio. Peers only ever receive; nothing they do is sent back to the host
or the flight computer.

    Host GS  ── /ws/relay (WebSocket, JSON) ──►  Peer GS backend  ──►  Peer UI
    (radio)                                      (source = "peer")

Host side:  RelayHub      — fan-out to subscribers, one bounded queue per peer
                            so a slow ZeroTier link can never stall radio I/O.
Peer side:  PeerReader    — WebSocket client with reconnect/backoff, enabled by
                            ALTAIR_PEER_HOST or at runtime from the Settings tab.

Wire protocol (host → peer, JSON text frames):
    {"type": "relay_hello",  "protocol": 1, "station_id": ...}
    {"type": "relay_status", "radio_connected", "port", "telemetry_source", "emulating"}
    {"type": "packet", ...}   — identical to the /ws packet message
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import websockets
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("gs.relay")

RELAY_PROTOCOL = 1
RELAY_PATH = "/ws/relay"
DEFAULT_RELAY_PORT = 8000

# Per-peer backlog. At typical telemetry rates this is many seconds of data;
# if a peer falls further behind than that, the oldest messages are dropped.
_PEER_QUEUE_SIZE = 1000

# Random per-process ID, so a station pointed at its own address (easy to do
# by typing its own ZeroTier IP) is refused instead of subscribing to itself.
INSTANCE_ID = secrets.token_hex(8)


# ---------------------------------------------------------------------------
# Host side
# ---------------------------------------------------------------------------

@dataclass(eq=False)     # identity hashing — instances live in a set
class _Subscriber:
    ws: WebSocket
    station_id: str
    addr: str
    connected_at: float = field(default_factory=time.time)
    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(_PEER_QUEUE_SIZE))
    dropped: int = 0


class RelayHub:
    """Fan-out of host telemetry to peer ground stations."""

    def __init__(self, on_change: Callable[[], Awaitable[None]] | None = None) -> None:
        self._subs: set[_Subscriber] = set()
        self._on_change = on_change

    def set_on_change(self, cb: Callable[[], Awaitable[None]]) -> None:
        self._on_change = cb

    @property
    def peers(self) -> list[dict]:
        return [
            {"station_id": s.station_id, "addr": s.addr,
             "connected_at": s.connected_at, "dropped": s.dropped}
            for s in self._subs
        ]

    def publish(self, msg: dict[str, Any]) -> None:
        """Queue msg for every peer. Never blocks, never raises."""
        if not self._subs:
            return
        try:
            data = json.dumps(msg)
        except (TypeError, ValueError):
            logger.debug("Relay: message not JSON-serialisable, skipped", exc_info=True)
            return
        for sub in self._subs:
            if sub.queue.full():
                try:
                    sub.queue.get_nowait()
                    sub.dropped += 1
                except asyncio.QueueEmpty:
                    pass
            sub.queue.put_nowait(data)

    async def serve(self, ws: WebSocket, initial: list[dict[str, Any]]) -> None:
        """Run one peer subscription until it disconnects."""
        await ws.accept()
        if ws.query_params.get("instance") == INSTANCE_ID:
            await ws.close(code=4000, reason="cannot subscribe to own relay")
            return
        client = ws.client
        sub = _Subscriber(
            ws=ws,
            station_id=(ws.query_params.get("station") or "unknown")[:64],
            addr=f"{client.host}:{client.port}" if client else "?",
        )
        for msg in initial:
            sub.queue.put_nowait(json.dumps(msg))
        self._subs.add(sub)
        logger.info("Relay: peer %s (%s) connected — %d peer(s)",
                    sub.station_id, sub.addr, len(self._subs))
        await self._notify()

        async def _send() -> None:
            while True:
                await ws.send_text(await sub.queue.get())

        async def _recv() -> None:
            # Peers never send anything meaningful; this only detects close.
            while True:
                await ws.receive_text()

        tasks = [asyncio.create_task(_send()), asyncio.create_task(_recv())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except Exception:
            pass
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._subs.discard(sub)
            logger.info("Relay: peer %s (%s) disconnected — %d peer(s) remaining",
                        sub.station_id, sub.addr, len(self._subs))
            await self._notify()

    async def _notify(self) -> None:
        if self._on_change:
            try:
                await self._on_change()
            except Exception:
                logger.debug("Relay: on_change callback failed", exc_info=True)


# ---------------------------------------------------------------------------
# Peer side
# ---------------------------------------------------------------------------

def parse_host(value: str, default_port: int = DEFAULT_RELAY_PORT) -> tuple[str, int]:
    """'10.1.2.3' → ('10.1.2.3', 8000); '10.1.2.3:9000' → ('10.1.2.3', 9000)."""
    value = value.strip()
    for prefix in ("ws://", "wss://", "http://", "https://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    value = value.split("/", 1)[0]
    if value.startswith("["):                         # [ipv6]:port
        host, _, rest = value[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else default_port
        return host, port
    if value.count(":") == 1:
        host, port_s = value.split(":")
        return host, int(port_s)
    return value, default_port


class PeerReader:
    """
    Subscribes to a host GS's /ws/relay and hands each packet to on_packet.
    Reconnects with backoff forever while enabled.
    """

    def __init__(
        self,
        on_packet: Callable[[dict], Awaitable[None]],
        on_status: Callable[[dict], Awaitable[None]],
        station_id: str,
    ) -> None:
        self._on_packet = on_packet
        self._on_status = on_status
        self._station_id = station_id
        self._task: asyncio.Task | None = None
        self.host = ""
        self.port = DEFAULT_RELAY_PORT
        self.enabled = False
        self.connected = False
        self.error = ""
        self.remote_station = ""
        self.remote_status: dict[str, Any] = {}
        self.packets_received = 0
        self.last_packet_wall = 0.0

    def status_dict(self) -> dict[str, Any]:
        return {
            "type": "peer_status",
            "enabled": self.enabled,
            "connected": self.connected,
            "host": self.host,
            "port": self.port,
            "error": self.error,
            "remote_station": self.remote_station,
            "remote_status": self.remote_status,
            "packets_received": self.packets_received,
            "last_packet_wall": self.last_packet_wall,
        }

    async def start(self, host: str, port: int = DEFAULT_RELAY_PORT) -> None:
        await self.stop()
        self.host, self.port = host, port
        self.enabled = True
        self.error = ""
        self._task = asyncio.create_task(self._connect_loop())
        logger.info("Peer relay: will subscribe to %s:%d", host, port)
        await self._emit()

    async def stop(self) -> None:
        was_enabled = self.enabled
        self.enabled = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self.connected = False
        self.remote_status = {}
        if was_enabled:
            logger.info("Peer relay: stopped")
            await self._emit()

    async def _emit(self) -> None:
        try:
            await self._on_status(self.status_dict())
        except Exception:
            logger.debug("Peer relay: status callback failed", exc_info=True)

    async def _connect_loop(self) -> None:
        delay = 1.0
        uri = f"ws://{self.host}:{self.port}{RELAY_PATH}?station={quote(self._station_id)}&instance={INSTANCE_ID}"
        while True:
            try:
                async with websockets.connect(
                    uri,
                    open_timeout=5,
                    ping_interval=5,
                    ping_timeout=10,
                    close_timeout=2,
                    max_size=2 ** 22,
                ) as ws:
                    logger.info("Peer relay: connected to %s:%d", self.host, self.port)
                    self.connected = True
                    self.error = ""
                    delay = 1.0
                    await self._emit()
                    async for raw in ws:
                        await self._handle(raw)
                    self.error = ws.close_reason or "host closed the connection"
            except asyncio.CancelledError:
                raise
            except Exception as e:     # OSError, InvalidURI, InvalidHandshake, ConnectionClosed, …
                self.error = str(e) or type(e).__name__
                logger.info("Peer relay: %s:%d unavailable (%s) — retrying in %.0fs",
                            self.host, self.port, self.error, delay)
            if self.connected:
                self.connected = False
                self.remote_status = {}
            await self._emit()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)

    async def _handle(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        kind = msg.get("type")
        if kind == "packet":
            self.packets_received += 1
            self.last_packet_wall = time.time()
            await self._on_packet(msg)
        elif kind == "relay_hello":
            if msg.get("protocol") != RELAY_PROTOCOL:
                logger.warning("Peer relay: host speaks protocol %s, we speak %d — "
                               "update both stations to the same GS version",
                               msg.get("protocol"), RELAY_PROTOCOL)
            self.remote_station = str(msg.get("station_id", ""))
            await self._emit()
        elif kind == "relay_status":
            self.remote_status = {k: v for k, v in msg.items() if k != "type"}
            await self._emit()
