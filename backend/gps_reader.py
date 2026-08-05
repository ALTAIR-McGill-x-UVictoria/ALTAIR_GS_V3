"""
Ground station GPS reader — a Seeed XIAO ESP32-C3 wired to a GPS module and
flashed as a dumb UART passthrough (GPS module UART -> XIAO -> native USB
CDC, no processing in between). The USB identity on the wire is therefore
the XIAO's own Espressif VID/PID (0x303A / 0x1001, the default
"USB JTAG/serial debug unit" descriptor), not any GPS chip's — whatever GPS
module is wired to the XIAO shows up as plain NMEA on that port.

Prefers connecting via gpsd's client socket (localhost:2947) over opening
the serial port directly, and falls back to direct serial only if gpsd
isn't reachable. This matters because gpsd (once set up so chrony can
discipline the system clock from the same GPS — see
/etc/chrony/conf.d/gps-nmea.conf) takes an exclusive lock on the serial
device, so a second process opening it directly gets EBUSY. gpsd exists
specifically to multiplex one physical GPS to many consumers — chrony via
SHM, and this reader via gpsd's client protocol — instead of everyone
fighting over the raw port. Requesting `nmea:true, json:false` in the WATCH
command makes gpsd hand back plain `$GPxxx` sentences (mixed with a few
JSON preamble lines on connect), so the existing NMEA parsing below is
reused unchanged regardless of which transport is active.

Opens the passthrough (via gpsd or, as fallback, the raw serial port),
reads NMEA sentences, and updates the shared ground-station position in
backend.tracking. Also provides UTC time sync via an optional callback.

Usage (called from main.py lifespan):

    from backend.gps_reader import GsGpsReader
    gs_gps = GsGpsReader(on_fix=my_callback)
    await gs_gps.start()   # non-blocking — spawns background task
    ...
    await gs_gps.stop()
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time as _time
from typing import Callable

import serial
import serial.tools.list_ports

import backend.tracking as tracking

logger = logging.getLogger("gs.gps")

# gpsd's client protocol port (default, unchanged since gpsd's inception)
_GPSD_HOST = "localhost"
_GPSD_PORT = 2947
_GPSD_CONNECT_TIMEOUT_S = 2.0
_GPSD_WATCH_CMD = b'?WATCH={"enable":true,"nmea":true,"json":false};\n'


class _TransportError(Exception):
    """Raised (from either transport) when the GPS connection is lost."""

# Espressif VID is fixed across all their dev boards; 0x1001 is the default
# "USB JTAG/serial debug unit" PID exposed over native USB CDC (confirmed
# against the actual XIAO ESP32-C3 passthrough board — VID=0x303A PID=0x1001).
# Note this isn't unique to the GPS passthrough — any ESP32-C3/S3 board using
# the same default USB descriptor would also match, so don't rely on this if
# other Espressif dev boards are ever plugged into the same ground station.
_GPS_VID  = 0x303A
_GPS_PIDS = {
    0x1001,  # ESP32-C3/S3 default native-USB "JTAG/serial debug unit" descriptor
}

# NMEA baud rate (confirmed against the real passthrough board)
_BAUD = 9600

# Minimum satellite count to accept a fix
_MIN_SATS = 3

# How often to retry auto-detection if the dongle wasn't found at startup (seconds)
_RETRY_INTERVAL_S = 10


def find_gps_passthrough() -> str | None:
    """Return the first COM port that looks like the XIAO ESP32-C3 GPS passthrough, or None."""
    for p in serial.tools.list_ports.comports():
        if p.vid == _GPS_VID and p.pid in _GPS_PIDS:
            logger.debug("GPS passthrough candidate: %s VID=%04x PID=%04x desc=%s",
                         p.device, p.vid, p.pid, p.description)
            return p.device
    all_ports = [(p.device, p.vid, p.pid) for p in serial.tools.list_ports.comports()]
    logger.debug("find_gps_passthrough: no match. Ports seen: %s", all_ports)
    return None


class GsGpsReader:
    """
    Background task that reads NMEA from the XIAO ESP32-C3 GPS passthrough
    and keeps backend.tracking.GS_LAT / GS_LON / GS_ALT up to date.

    on_fix — optional coroutine or regular callable; called with a dict:
        { lat, lon, alt, utc_unix, sats, hdop, fix_quality }
    whenever a new valid fix arrives.
    """

    def __init__(self, on_fix: Callable | None = None, on_status: Callable | None = None) -> None:
        self._on_fix    = on_fix
        self._on_status = on_status   # called with (connected: bool, has_fix: bool, port: str, receiving: bool)
        self._task: asyncio.Task | None = None
        self._retry_task: asyncio.Task | None = None
        self._port: serial.Serial | None = None
        self._sock: socket.socket | None = None
        self._sock_file = None   # socket.makefile("r") — gives readline() over the socket
        self._transport: str = ""   # "gpsd" | "serial"
        self.port_name: str = ""
        self.connected: bool = False

        # True once at least one $-prefixed NMEA sentence has been seen on
        # this connection — proves the passthrough is wired and streaming,
        # independent of whether a GPS fix has been acquired yet.
        self.receiving: bool = False

        # Latest fix — None until first valid sentence
        self.fix: dict | None = None

        # Ring buffer of last 10 raw NMEA sentences for debug inspection
        self._raw_buf: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, port: str | None = None) -> bool:
        """
        Connect to the GPS passthrough and start reading — preferring gpsd
        (see module docstring for why) and falling back to opening the
        serial port directly if gpsd isn't reachable. `port` forces the
        direct-serial path (skips the gpsd attempt) when given explicitly.
        If neither is available, a background retry task polls every
        _RETRY_INTERVAL_S seconds until one appears.
        Returns True if connected immediately, False if deferred to retry loop.
        """
        if self.connected:
            await self.stop()

        if port is None and await self._try_open_gpsd():
            return True

        port = port or find_gps_passthrough()
        if port is None:
            logger.warning(
                "GsGpsReader: GPS passthrough not found (gpsd unreachable, no "
                "matching serial port) — will retry every %ds. GS position "
                "remains hardcoded until a fix is acquired.",
                _RETRY_INTERVAL_S,
            )
            if self._retry_task is None or self._retry_task.done():
                self._retry_task = asyncio.create_task(self._retry_loop())
            return False

        return await self._open(port)

    async def _try_open_gpsd(self) -> bool:
        """Attempt to connect via gpsd's client socket. Returns True on success."""
        loop = asyncio.get_event_loop()
        try:
            sock = await loop.run_in_executor(None, self._connect_gpsd)
        except OSError as e:
            logger.debug("GsGpsReader: gpsd not reachable at %s:%d (%s)",
                         _GPSD_HOST, _GPSD_PORT, e)
            return False

        self._sock = sock
        self._sock_file = sock.makefile("r", encoding="ascii", errors="replace")
        self._transport = "gpsd"
        self.port_name = f"gpsd://{_GPSD_HOST}:{_GPSD_PORT}"
        self.connected = True
        self._task = asyncio.create_task(self._read_loop())
        logger.info("GS GPS connected via gpsd at %s", self.port_name)
        await self._emit_status()
        return True

    def _connect_gpsd(self) -> socket.socket:
        """Blocking gpsd connect + WATCH handshake — runs in executor."""
        sock = socket.create_connection((_GPSD_HOST, _GPSD_PORT), timeout=_GPSD_CONNECT_TIMEOUT_S)
        sock.settimeout(1.0)   # matches serial timeout=1 semantics used below
        sock.sendall(_GPSD_WATCH_CMD)
        return sock

    async def _open(self, port: str) -> bool:
        """Open the serial port directly and start the read loop. Returns True on success."""
        try:
            self._port = serial.Serial(port, _BAUD, timeout=1)
            self._transport = "serial"
            self.port_name = port
            self.connected = True
            self._task = asyncio.create_task(self._read_loop())
            logger.info("GS GPS opened on %s @ %d baud", port, _BAUD)
            await self._emit_status()
            return True
        except serial.SerialException as e:
            logger.error("GsGpsReader: cannot open %s: %s", port, e)
            return False

    async def _retry_loop(self) -> None:
        """Poll for gpsd or a directly-attached GPS passthrough, then connect."""
        while not self.connected:
            await asyncio.sleep(_RETRY_INTERVAL_S)
            if await self._try_open_gpsd():
                return
            port = find_gps_passthrough()
            if port:
                logger.info("GsGpsReader: GPS passthrough found on retry — opening %s", port)
                await self._open(port)
                return
            logger.debug("GsGpsReader: retry — gpsd and direct passthrough both still unavailable")

    async def stop(self) -> None:
        if self._retry_task and not self._retry_task.done():
            self._retry_task.cancel()
            try:
                await self._retry_task
            except asyncio.CancelledError:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._close_transport()
        self.connected = False
        self.receiving = False
        self.port_name = ""
        logger.info("GS GPS connection closed")
        await self._emit_status()

    def _close_transport(self) -> None:
        if self._port and self._port.is_open:
            self._port.close()
        self._port = None
        if self._sock_file is not None:
            try:
                self._sock_file.close()
            except OSError:
                pass
            self._sock_file = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._transport = ""

    def status_dict(self) -> dict:
        return {
            "connected":  self.connected,
            "receiving":  self.receiving,
            "has_fix":    self.fix is not None,
            "port":       self.port_name,
            "fix":        self.fix,
            "raw_nmea":   list(self._raw_buf),
        }

    async def _emit_status(self) -> None:
        if self._on_status is None:
            return
        try:
            result = self._on_status(self.connected, self.fix is not None, self.port_name, self.receiving)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.warning("GsGpsReader on_status callback error: %s", e)

    # ------------------------------------------------------------------
    # Background read loop
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            try:
                line = await loop.run_in_executor(None, self._read_line)
                if line:
                    await self._handle_sentence(line.strip())
            except _TransportError as e:
                logger.error("GS GPS read error: %s — will retry every %ds",
                             e, _RETRY_INTERVAL_S)
                await self._handle_disconnect()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("GS GPS parse error: %s", e)

    async def _handle_disconnect(self) -> None:
        """Clean up transport state after a read error and start reconnecting."""
        self._close_transport()
        self.connected = False
        self.receiving = False
        self.port_name = ""
        await self._emit_status()
        if self._retry_task is None or self._retry_task.done():
            self._retry_task = asyncio.create_task(self._retry_loop())

    def _read_line(self) -> str:
        """Blocking readline — runs in executor thread."""
        if self._transport == "gpsd":
            return self._read_line_gpsd()
        return self._read_line_serial()

    def _read_line_serial(self) -> str:
        if self._port and self._port.is_open:
            try:
                return self._port.readline().decode("ascii", errors="replace")
            except serial.SerialException as e:
                raise _TransportError(str(e)) from e
        return ""

    def _read_line_gpsd(self) -> str:
        if self._sock_file is None:
            return ""
        try:
            return self._sock_file.readline()
        except (socket.timeout, TimeoutError):
            return ""   # matches serial's timeout=1 behavior — just means "no line yet"
        except OSError as e:
            raise _TransportError(f"gpsd connection lost: {e}") from e

    # ------------------------------------------------------------------
    # NMEA parsing
    # ------------------------------------------------------------------

    async def _handle_sentence(self, sentence: str) -> None:
        if not sentence.startswith("$"):
            return

        # Keep last 10 raw sentences for debug inspection
        self._raw_buf.append(sentence)
        if len(self._raw_buf) > 10:
            self._raw_buf.pop(0)

        if not self.receiving:
            self.receiving = True
            logger.info("GS GPS: NMEA data received on %s — passthrough is wired and streaming", self.port_name)
            await self._emit_status()

        try:
            import pynmea2
            msg = pynmea2.parse(sentence)
        except Exception:
            return

        sentence_type = msg.sentence_type

        if sentence_type == "GGA":
            await self._handle_gga(msg)
        elif sentence_type == "RMC":
            await self._handle_rmc(msg)

    async def _handle_gga(self, msg) -> None:
        try:
            fix_quality = int(msg.gps_qual) if msg.gps_qual else 0
            sats        = int(msg.num_sats)  if msg.num_sats  else 0
            hdop        = float(msg.horizontal_dil) if msg.horizontal_dil else 99.9
        except (ValueError, AttributeError):
            return

        if fix_quality == 0 or sats < _MIN_SATS:
            logger.debug("GGA: no fix yet (quality=%d sats=%d hdop=%.1f)", fix_quality, sats, hdop)
            return

        try:
            lat = float(msg.latitude)
            lon = float(msg.longitude)
            alt = float(msg.altitude) if msg.altitude else 0.0
        except (ValueError, AttributeError, TypeError):
            return

        await self._update_fix(lat, lon, alt, sats=sats, hdop=hdop,
                                fix_quality=fix_quality)

    async def _handle_rmc(self, msg) -> None:
        try:
            status = msg.status  # 'A' = active, 'V' = void
        except AttributeError:
            return
        if status != "A":
            return

        try:
            # pynmea2 .latitude / .longitude are already signed decimal degrees
            lat = float(msg.latitude)
            lon = float(msg.longitude)
        except (ValueError, AttributeError):
            return

        # RMC carries UTC datetime — use it to nudge system time reference
        utc_unix: float | None = None
        try:
            from datetime import datetime, timezone
            dt = datetime.combine(msg.datestamp, msg.timestamp,
                                  tzinfo=timezone.utc)
            utc_unix = dt.timestamp()
        except Exception:
            pass

        # RMC has no altitude — only update lat/lon if we already have a fix
        if self.fix is not None:
            await self._update_fix(lat, lon, self.fix["alt"],
                                   utc_unix=utc_unix,
                                   sats=self.fix.get("sats", 0),
                                   hdop=self.fix.get("hdop", 99.9),
                                   fix_quality=self.fix.get("fix_quality", 1))
        elif utc_unix is not None:
            # No altitude yet but we have UTC time — still notify caller
            pass

        # Emit UTC sync even before we have a full fix
        if utc_unix is not None and self._on_fix is not None:
            await self._emit_fix_event({"utc_unix": utc_unix,
                                        "lat": lat, "lon": lon,
                                        "alt": self.fix["alt"] if self.fix else 0.0,
                                        "sats": self.fix.get("sats", 0) if self.fix else 0,
                                        "hdop": self.fix.get("hdop", 99.9) if self.fix else 99.9,
                                        "fix_quality": self.fix.get("fix_quality", 0) if self.fix else 0})

    async def _update_fix(
        self,
        lat: float, lon: float, alt: float,
        utc_unix: float | None = None,
        sats: int = 0,
        hdop: float = 99.9,
        fix_quality: int = 1,
    ) -> None:
        fix = {
            "lat":         round(lat, 7),
            "lon":         round(lon, 7),
            "alt":         round(alt, 2),
            "utc_unix":    utc_unix if utc_unix is not None else _time.time(),
            "sats":        sats,
            "hdop":        hdop,
            "fix_quality": fix_quality,
        }

        first_fix = (self.fix is None)

        # Suppress broadcast when position hasn't meaningfully changed (RMC fires
        # every second and duplicates GGA with the same coordinates)
        if not first_fix and not self._position_changed(fix):
            self.fix = fix  # still update alt/utc/hdop quietly
            tracking.GS_ALT = fix["alt"]
            return

        self.fix = fix

        # Push live position into tracking module
        tracking.GS_LAT = fix["lat"]
        tracking.GS_LON = fix["lon"]
        tracking.GS_ALT = fix["alt"]

        logger.info("GS fix: lat=%.6f lon=%.6f alt=%.1fm sats=%d hdop=%.1f",
                    lat, lon, alt, sats, hdop)

        if first_fix:
            await self._emit_status()
        await self._emit_fix_event(fix)

    def _position_changed(self, new: dict) -> bool:
        """True if position changed meaningfully or sats/quality changed."""
        old = self.fix
        if abs(new["lat"] - old["lat"]) > 1e-4:  # ~10 m
            return True
        if abs(new["lon"] - old["lon"]) > 1e-4:
            return True
        if new["sats"] != old["sats"] or new["fix_quality"] != old["fix_quality"]:
            return True
        # Always broadcast at least once per minute so the frontend stays fresh
        if (_time.time() - old.get("utc_unix", 0)) > 60:
            return True
        return False

    async def _emit_fix_event(self, fix: dict) -> None:
        if self._on_fix is None:
            return
        try:
            result = self._on_fix(fix)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.warning("GsGpsReader on_fix callback error: %s", e)
