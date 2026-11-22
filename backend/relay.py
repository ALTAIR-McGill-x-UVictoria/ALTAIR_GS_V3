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

Telescope captures are mirrored too. The WebSocket only *announces* each new
image; the peer then pulls the file over plain HTTP from IMAGE_PATH, one at a
time, newest first, into its own capture directory (prefixed with the host's
station ID). Keeping multi-megabyte FITS files off the WebSocket means a big
download never delays live telemetry, and newest-first keeps the peer's
gallery current even when its link can't keep up with auto-capture.

Discovery: instead of typing the host's IP, a peer can scan every address on
its ZeroTier subnet(s) for a GS answering on DISCOVERY_PATH (discover()). In
*auto* mode (ALTAIR_PEER_HOST=auto, or AUTO in Settings) PeerReader does this
by itself: it subscribes to the best station that actually has live telemetry
(radio > tunnel > emulator), rescans whenever that link drops, and switches
if its current host goes quiet while another station is live.

Wire protocol (host → peer, JSON text frames):
    {"type": "relay_hello",  "protocol": 1, "station_id": ...}
    {"type": "relay_status", "radio_connected", "port", "telemetry_source", "emulating"}
    {"type": "packet", ...}   — identical to the /ws packet message
    {"type": "image", "name", "size", "mtime"}          — a capture just finished
    {"type": "image_index", "images": [{name, size, mtime}, ...]}
                              — every capture this host session has made; sent
                                on connect and every _INDEX_RESEND_S so a peer
                                that dropped an announcement still catches up
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import websockets
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("gs.relay")

RELAY_PROTOCOL = 1
RELAY_PATH = "/ws/relay"
IMAGE_PATH = "/api/relay/image"
DISCOVERY_PATH = "/api/relay/info"
DEFAULT_RELAY_PORT = 8000

# Captures announced per host session (the index peers catch up from).
_MAX_INDEXED_IMAGES = 500
_INDEX_RESEND_S = 60.0
# Peer download: per-socket-read timeout, and attempts before giving up.
_DOWNLOAD_TIMEOUT_S = 30.0
_DOWNLOAD_MAX_ATTEMPTS = 5

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
        # name → (path, meta) for every capture this session, oldest first
        self._images: OrderedDict[str, tuple[Path, dict]] = OrderedDict()

    # -- captured images ----------------------------------------------------

    def add_image(self, path: Path) -> None:
        """Announce a finished capture to all peers and make it downloadable."""
        try:
            st = path.stat()
        except OSError:
            logger.warning("Relay: captured image %s not found, not relayed", path)
            return
        meta = {"name": path.name, "size": st.st_size, "mtime": st.st_mtime}
        self._images[path.name] = (path, meta)
        self._images.move_to_end(path.name)
        while len(self._images) > _MAX_INDEXED_IMAGES:
            self._images.popitem(last=False)
        self.publish({"type": "image", **meta})

    def image_path(self, name: str) -> Path | None:
        """Only files this hub announced are served — never arbitrary paths."""
        entry = self._images.get(name)
        return entry[0] if entry else None

    def image_index_msg(self) -> dict[str, Any]:
        return {"type": "image_index", "images": [m for _, m in self._images.values()]}

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
        for msg in [*initial, self.image_index_msg()]:
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

        async def _resend_index() -> None:
            # Image announcements share the drop-oldest queue with packets;
            # resending the index means a dropped one is only ever delayed.
            while True:
                await asyncio.sleep(_INDEX_RESEND_S)
                if self._images and not sub.queue.full():
                    sub.queue.put_nowait(json.dumps(self.image_index_msg()))

        tasks = [asyncio.create_task(_send()), asyncio.create_task(_recv()),
                 asyncio.create_task(_resend_index())]
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


# ---------------------------------------------------------------------------
# Discovery — scan the ZeroTier subnet(s) for other ground stations
# ---------------------------------------------------------------------------

_SCAN_CONCURRENCY = 256
_SCAN_TIMEOUT_S = 2.0          # ZeroTier's first packet to a new peer can be slow
_MAX_SCAN_HOSTS = 4094         # a /20; bigger networks scan the /22 around us
_AUTO_RESCAN_S = 15.0          # auto mode: wait between scans that found nothing
_AUTO_IDLE_S = 30.0            # auto mode: host silent this long → look for a better one


def _is_zerotier_iface(name: str) -> bool:
    n = name.lower()
    # Linux/BSD "zt…", Windows "ZeroTier One [nwid]", macOS "feth…"
    return n.startswith("zt") or "zerotier" in n or n.startswith("feth")


def zerotier_interfaces() -> tuple[list[ipaddress.IPv4Interface], str]:
    """
    This machine's ZeroTier IPv4 addresses (with netmask). Override with
    ALTAIR_PEER_SCAN_SUBNETS="10.96.204.0/24,…" if auto-detection misses one.
    Returns (interfaces, error message or "").
    """
    override = os.getenv("ALTAIR_PEER_SCAN_SUBNETS", "").strip()
    if override:
        try:
            return [ipaddress.IPv4Interface(x.strip()) for x in override.split(",") if x.strip()], ""
        except ValueError as e:
            return [], f"ALTAIR_PEER_SCAN_SUBNETS invalid: {e}"
    try:
        import psutil
    except ImportError:
        return [], "psutil not installed (pip install -r requirements.txt) — or set ALTAIR_PEER_SCAN_SUBNETS"
    stats = psutil.net_if_stats()
    found: list[ipaddress.IPv4Interface] = []
    for name, addrs in psutil.net_if_addrs().items():
        if not _is_zerotier_iface(name) or (name in stats and not stats[name].isup):
            continue
        for a in addrs:
            if a.family == socket.AF_INET and a.netmask:
                found.append(ipaddress.IPv4Interface(f"{a.address}/{a.netmask}"))
    return found, "" if found else "no ZeroTier network interface found on this PC"


async def probe(ip: str, port: int = DEFAULT_RELAY_PORT,
                timeout: float = _SCAN_TIMEOUT_S) -> dict | None:
    """GET DISCOVERY_PATH from ip:port; the station's info dict, or None."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        writer.write(
            f"GET {DISCOVERY_PATH} HTTP/1.0\r\nHost: {ip}:{port}\r\n"
            f"Connection: close\r\n\r\n".encode()
        )
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), timeout)
        head, _, body = raw.partition(b"\r\n\r\n")
        if b" 200 " not in head.split(b"\r\n", 1)[0]:
            return None
        info = json.loads(body)
        if not isinstance(info, dict) or info.get("service") != "altair-gs":
            return None
        return {**info, "ip": ip, "port": port}
    except (OSError, asyncio.TimeoutError, ValueError):
        return None
    finally:
        writer.close()


def _scan_targets(ifaces: list[ipaddress.IPv4Interface]) -> tuple[list[str], list[str], str]:
    own = {str(i.ip) for i in ifaces}
    targets: list[str] = []
    subnets: list[str] = []
    note = ""
    for iface in ifaces:
        net = iface.network
        if net.num_addresses - 2 > _MAX_SCAN_HOSTS:
            net = ipaddress.IPv4Interface(f"{iface.ip}/22").network
            note = (f"{iface.network} is large — scanned only {net}; set "
                    f"ALTAIR_PEER_SCAN_SUBNETS to cover the rest")
        subnets.append(str(net))
        targets.extend(str(h) for h in net.hosts() if str(h) not in own)
    return list(dict.fromkeys(targets)), subnets, note


def _host_rank(h: dict) -> tuple:
    """Higher is better. Live telemetry beats a connected-but-quiet radio."""
    source_rank = {"radio": 3, "tunnel": 2}.get(h.get("live_source") or "", 0)
    return (bool(h.get("live_source")), source_rank,
            bool(h.get("radio_connected")), bool(h.get("tunnel_connected")))


def is_useful_host(h: dict) -> bool:
    """A station worth subscribing to — not just another viewer."""
    return bool(h.get("live_source") or h.get("radio_connected") or h.get("tunnel_connected"))


async def discover(port: int = DEFAULT_RELAY_PORT) -> dict[str, Any]:
    """Scan every address on our ZeroTier subnet(s) for ground stations."""
    t0 = time.monotonic()
    ifaces, error = zerotier_interfaces()
    targets, subnets, note = _scan_targets(ifaces)
    sem = asyncio.Semaphore(_SCAN_CONCURRENCY)

    async def _one(ip: str) -> dict | None:
        async with sem:
            return await probe(ip, port)

    results = await asyncio.gather(*(_one(ip) for ip in targets))
    hosts = [h for h in results if h and h.get("instance") != INSTANCE_ID]
    hosts.sort(key=_host_rank, reverse=True)
    logger.info("Peer discovery: scanned %d address(es) on %s in %.1fs — %d GS found%s",
                len(targets), ", ".join(subnets) or "nothing", time.monotonic() - t0,
                len(hosts), "".join(f"; {h['station_id']}@{h['ip']}" for h in hosts))
    return {
        "subnets": subnets,
        "scanned": len(targets),
        "hosts":   hosts,
        "error":   error,
        "note":    note,
        "duration_s": round(time.monotonic() - t0, 1),
        "time":    time.time(),
    }


# ---------------------------------------------------------------------------
# Peer side
# ---------------------------------------------------------------------------

_SAFE_PREFIX_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _download(url: str, dest: Path) -> int:
    """Blocking HTTP GET of url into dest; returns bytes written."""
    with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_S) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f, 1 << 20)
        return f.tell()


class PeerReader:
    """
    Subscribes to a host GS's /ws/relay and hands each packet to on_packet.
    Also mirrors the host's telescope captures into dest_dir(). Reconnects
    with backoff forever while enabled.
    """

    def __init__(
        self,
        on_packet: Callable[[dict], Awaitable[None]],
        on_status: Callable[[dict], Awaitable[None]],
        station_id: str,
        dest_dir: Callable[[], Path] | None = None,
    ) -> None:
        self._on_packet = on_packet
        self._on_status = on_status
        self._station_id = station_id
        self._dest_dir = dest_dir
        self._task: asyncio.Task | None = None
        self._img_task: asyncio.Task | None = None
        self._img_pending: dict[str, dict] = {}       # name → meta (+ "attempts")
        self._img_wake = asyncio.Event()
        self.images_received = 0
        self.image_current = ""
        self.image_error = ""
        self.host = ""
        self.port = DEFAULT_RELAY_PORT
        self.auto = False
        self.scanning = False
        self.last_scan: dict[str, Any] | None = None
        self._switch_to: dict | None = None      # auto mode: better host found while connected
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
            "auto": self.auto,
            "scanning": self.scanning,
            "last_scan": self.last_scan,
            "connected": self.connected,
            "host": self.host,
            "port": self.port,
            "error": self.error,
            "remote_station": self.remote_station,
            "remote_status": self.remote_status,
            "packets_received": self.packets_received,
            "last_packet_wall": self.last_packet_wall,
            "images": {
                "enabled":  self._dest_dir is not None,
                "received": self.images_received,
                "pending":  len(self._img_pending),
                "current":  self.image_current,
                "error":    self.image_error,
            },
        }

    async def start(self, host: str, port: int = DEFAULT_RELAY_PORT, auto: bool = False) -> None:
        """Subscribe to host:port, or with auto=True find the best host by scanning."""
        await self.stop()
        self.auto = auto
        self.host, self.port = ("", port) if auto else (host, port)
        self.remote_station = ""
        self.enabled = True
        self.error = ""
        self._task = asyncio.create_task(self._connect_loop())
        if self._dest_dir is not None:
            self._img_task = asyncio.create_task(self._image_loop())
        if auto:
            logger.info("Peer relay: auto mode — will scan the ZeroTier network for a host")
        else:
            logger.info("Peer relay: will subscribe to %s:%d", host, port)
        await self._emit()

    async def stop(self) -> None:
        was_enabled = self.enabled
        self.enabled = False
        for task in (self._task, self._img_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._task = self._img_task = None
        self._img_pending.clear()
        self.image_current = ""
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

    async def _scan(self) -> dict[str, Any]:
        self.scanning = True
        await self._emit()
        try:
            self.last_scan = await discover(self.port)
        finally:
            self.scanning = False
        return self.last_scan

    async def _pick_auto_target(self) -> bool:
        """Auto mode: point self.host at the best station found, if any."""
        best = self._switch_to
        self._switch_to = None
        if best is None:
            scan = await self._scan()
            useful = [h for h in scan["hosts"] if is_useful_host(h)]
            if not useful:
                others = ", ".join(f"{h['station_id']}@{h['ip']} (viewer)" for h in scan["hosts"])
                self.error = scan["error"] or (
                    f"no GS with live telemetry among {scan['scanned']} address(es) on "
                    f"{', '.join(scan['subnets'])}" + (f" — found {others}" if others else ""))
                self.host = ""
                await self._emit()
                return False
            best = useful[0]
        if best["ip"] != self.host:
            # Different host: images queued from the old one would only 404.
            self._img_pending.clear()
            self.remote_station = best.get("station_id", "")
        self.host, self.port = best["ip"], best["port"]
        logger.info("Peer relay: auto-selected %s at %s:%d", self.remote_station, self.host, self.port)
        return True

    async def _auto_watchdog(self, ws) -> None:
        """Auto mode: if our host goes quiet and another station is live, switch."""
        connected_at = time.time()
        while True:
            await asyncio.sleep(_AUTO_IDLE_S)
            last = max(self.last_packet_wall, connected_at)
            if time.time() - last < _AUTO_IDLE_S:
                continue
            scan = await self._scan()
            await self._emit()
            better = [h for h in scan["hosts"] if h.get("live_source") and h["ip"] != self.host]
            if better:
                self._switch_to = better[0]
                self.error = f"{self.remote_station or self.host} quiet — switching to {better[0]['station_id']}"
                await ws.close()
                return

    async def _connect_loop(self) -> None:
        delay = 1.0
        while True:
            if self.auto and not await self._pick_auto_target():
                await asyncio.sleep(_AUTO_RESCAN_S)
                continue
            uri = f"ws://{self.host}:{self.port}{RELAY_PATH}?station={quote(self._station_id)}&instance={INSTANCE_ID}"
            watchdog: asyncio.Task | None = None
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
                    if self.auto:
                        watchdog = asyncio.create_task(self._auto_watchdog(ws))
                    async for raw in ws:
                        await self._handle(raw)
                    if not self._switch_to:
                        self.error = ws.close_reason or "host closed the connection"
            except asyncio.CancelledError:
                raise
            except Exception as e:     # OSError, InvalidURI, InvalidHandshake, ConnectionClosed, …
                self.error = str(e) or type(e).__name__
                logger.info("Peer relay: %s:%d unavailable (%s) — retrying in %.0fs",
                            self.host, self.port, self.error, delay)
            finally:
                if watchdog:
                    watchdog.cancel()
            if self.connected:
                self.connected = False
                self.remote_status = {}
            await self._emit()
            if self._switch_to:
                continue                    # auto mode found a better host — go now
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
        elif kind == "image":
            self._offer_images([msg])
        elif kind == "image_index":
            self._offer_images(msg.get("images") or [])

    # -- image mirroring ----------------------------------------------------

    def _local_image_path(self, name: str) -> Path | None:
        if self._dest_dir is None:
            return None
        if not name or Path(name).name != name or name.startswith("."):
            return None                              # refuse anything path-like
        prefix = _SAFE_PREFIX_RE.sub("-", self.remote_station or self.host).strip("-") or "relay"
        return self._dest_dir() / f"{prefix}_{name}"

    def _offer_images(self, metas: list[dict]) -> None:
        added = False
        for meta in metas:
            name = str(meta.get("name", ""))
            dest = self._local_image_path(name)
            if dest is None or name in self._img_pending or name == self.image_current:
                continue
            try:
                if dest.stat().st_size == meta.get("size"):
                    continue                          # already mirrored
            except OSError:
                pass
            self._img_pending[name] = {"name": name, "size": meta.get("size"),
                                       "mtime": float(meta.get("mtime") or 0), "attempts": 0}
            added = True
        if added:
            self._img_wake.set()

    async def _image_loop(self) -> None:
        while True:
            if not self._img_pending:
                self._img_wake.clear()
                await self._img_wake.wait()
                continue
            # Newest first: the live view matters more than backlog.
            meta = max(self._img_pending.values(), key=lambda m: m["mtime"])
            name = meta["name"]
            dest = self._local_image_path(name)
            if dest is None:
                self._img_pending.pop(name, None)
                continue
            self.image_current = name
            await self._emit()
            tmp = dest.with_name(dest.name + ".part")   # not a gallery extension
            url = f"http://{self.host}:{self.port}{IMAGE_PATH}/{quote(name)}"
            retry_in = 0.0
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                t0 = time.monotonic()
                size = await asyncio.to_thread(_download, url, tmp)
                if meta["size"] is not None and size != meta["size"]:
                    raise IOError(f"got {size} of {meta['size']} bytes")
                tmp.replace(dest)
                self._img_pending.pop(name, None)
                self.images_received += 1
                self.image_error = ""
                logger.info("Peer relay: mirrored image %s (%.1f MB in %.1fs)",
                            dest.name, size / 1e6, time.monotonic() - t0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                tmp.unlink(missing_ok=True)
                if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                    # Aged out of the host's index (it keeps the last
                    # _MAX_INDEXED_IMAGES) or deleted there — expected when a
                    # slow link falls far behind auto-capture; not an error.
                    self._img_pending.pop(name, None)
                else:
                    self.image_error = f"{name}: {e}"
                    meta["attempts"] += 1
                    if meta["attempts"] >= _DOWNLOAD_MAX_ATTEMPTS:
                        logger.warning("Peer relay: giving up on image %s (%s)", name, e)
                        self._img_pending.pop(name, None)
                    else:
                        retry_in = min(2.0 ** meta["attempts"], 30.0)
                        logger.info("Peer relay: image %s failed (%s) — retrying in %.0fs",
                                    name, e, retry_in)
            finally:
                self.image_current = ""
            await self._emit()
            if retry_in:
                await asyncio.sleep(retry_in)
