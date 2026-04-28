"""
Ground station GPS reader — u-blox 7 (USB, VID=0x1546 PID=0x01A7).

Opens the dongle's virtual COM port, reads NMEA sentences, and updates
the shared ground-station position in backend.tracking.  Also provides
UTC time sync via an optional callback.

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
import time as _time
from typing import Callable

import serial
import serial.tools.list_ports

import backend.tracking as tracking

logger = logging.getLogger("gs.gps")

# u-blox 7 USB VID / PID
_UBLOX7_VID = 0x1546
_UBLOX7_PID = 0x01A7

# NMEA baud rate (u-blox default)
_BAUD = 9600

# Minimum satellite count to accept a fix
_MIN_SATS = 4


def find_ublox7() -> str | None:
    """Return the first COM port that looks like a u-blox 7 dongle, or None."""
    for p in serial.tools.list_ports.comports():
        if p.vid == _UBLOX7_VID and p.pid == _UBLOX7_PID:
            return p.device
    return None


class GsGpsReader:
    """
    Background task that reads NMEA from the u-blox 7 and keeps
    backend.tracking.GS_LAT / GS_LON / GS_ALT up to date.

    on_fix — optional coroutine or regular callable; called with a dict:
        { lat, lon, alt, utc_unix, sats, hdop, fix_quality }
    whenever a new valid fix arrives.
    """

    def __init__(self, on_fix: Callable | None = None) -> None:
        self._on_fix = on_fix
        self._task: asyncio.Task | None = None
        self._port: serial.Serial | None = None
        self.port_name: str = ""
        self.connected: bool = False

        # Latest fix — None until first valid sentence
        self.fix: dict | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, port: str | None = None) -> bool:
        """
        Open the GPS serial port and start reading.
        Auto-detects the u-blox 7 if port is omitted.
        Returns True if started successfully.
        """
        if self.connected:
            await self.stop()

        port = port or find_ublox7()
        if port is None:
            logger.warning("GsGpsReader: u-blox 7 not found — GS position remains hardcoded")
            return False

        try:
            self._port = serial.Serial(port, _BAUD, timeout=1)
            self.port_name = port
            self.connected = True
            self._task = asyncio.create_task(self._read_loop())
            logger.info("GS GPS opened on %s @ %d baud", port, _BAUD)
            return True
        except serial.SerialException as e:
            logger.error("GsGpsReader: cannot open %s: %s", port, e)
            return False

    async def stop(self) -> None:
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
        logger.info("GS GPS port closed")

    def status_dict(self) -> dict:
        return {
            "connected": self.connected,
            "port":      self.port_name,
            "fix":       self.fix,
        }

    # ------------------------------------------------------------------
    # Background read loop
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        loop = asyncio.get_event_loop()
        buf = b""
        while True:
            try:
                line = await loop.run_in_executor(None, self._read_line)
                if line:
                    await self._handle_sentence(line.strip())
            except serial.SerialException as e:
                logger.error("GS GPS read error: %s", e)
                self.connected = False
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("GS GPS parse error: %s", e)

    def _read_line(self) -> str:
        """Blocking readline — runs in executor thread."""
        if self._port and self._port.is_open:
            try:
                return self._port.readline().decode("ascii", errors="replace")
            except serial.SerialException:
                raise
        return ""

    # ------------------------------------------------------------------
    # NMEA parsing
    # ------------------------------------------------------------------

    async def _handle_sentence(self, sentence: str) -> None:
        if not sentence.startswith("$"):
            return

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
        self.fix = fix

        # Push live position into tracking module
        tracking.GS_LAT = fix["lat"]
        tracking.GS_LON = fix["lon"]
        tracking.GS_ALT = fix["alt"]

        logger.debug("GS fix: lat=%.6f lon=%.6f alt=%.1fm sats=%d hdop=%.1f",
                     lat, lon, alt, sats, hdop)

        await self._emit_fix_event(fix)

    async def _emit_fix_event(self, fix: dict) -> None:
        if self._on_fix is None:
            return
        try:
            result = self._on_fix(fix)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.warning("GsGpsReader on_fix callback error: %s", e)
