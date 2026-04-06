"""
Telescope mount controllers.

Two concrete implementations behind a common async interface:

  NexStarController  — Celestron NexStar (Alt/Az) via the `nexstar` library.
                        Commanded with azimuth / elevation degrees.

  AM5Controller      — ZWO AM5 equatorial mount via ASCOM (win32com).
                        Commanded with RA (decimal hours) / Dec (degrees).
                        Requires the ZWO ASCOM driver installed on Windows.

Both run all blocking hardware calls in a single-threaded ThreadPoolExecutor
so they never stall the FastAPI event loop.

Smart positioning (NexStar only):
  Moves larger than LARGE_MOVE_DEG are split into a midpoint slew + final slew
  to reduce mechanical stress and improve tracking responsiveness.

Usage:
    # NexStar
    mc = NexStarController()
    await mc.connect("COM10")
    await mc.goto(azimuth=180.0, elevation=45.0)

    # AM5 (RA/Dec supplied by tracking.azalt_to_radec)
    mc = AM5Controller()
    await mc.connect()        # ASCOM chooser; or pass progid="ASCOM.ZWO.Telescope"
    await mc.goto(ra_hours=12.5, dec_deg=30.0)

    pos = await mc.get_position()   # always {"azimuth", "elevation"} for UI consistency
    await mc.disconnect()
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("gs.mount")

# Smart-slew thresholds (degrees) — used by NexStar only
LARGE_MOVE_DEG = 15.0


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseMountController(ABC):
    """
    Common interface for all mount types.

    All public methods are async-safe.  The `mount_type` property identifies
    which hardware is in use so the frontend and tracking loop can adapt.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mount")
        self.connected = False
        self.port_name = ""
        self._last_position: dict | None = None

    @property
    @abstractmethod
    def mount_type(self) -> str:
        """Return a short identifier: 'nexstar' or 'am5'."""

    @abstractmethod
    async def connect(self, port: str = "") -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def goto(self, **kwargs) -> None:
        """
        Slew to target.
        NexStar: goto(azimuth=float, elevation=float)
        AM5:     goto(ra_hours=float, dec_deg=float)
        """

    @abstractmethod
    async def get_position(self) -> dict:
        """
        Return current position.
        Always returns {"azimuth": float, "elevation": float} for UI consistency.
        The AM5 implementation converts from RA/Dec → Az/El internally.
        """

    def status_dict(self) -> dict:
        return {
            "connected":  self.connected,
            "mount_type": self.mount_type,
            "port":       self.port_name,
            "position":   self._last_position,
        }

    # Convenience wrapper
    async def _run(self, fn, *args):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, fn, *args)


# ---------------------------------------------------------------------------
# NexStar (Alt/Az)
# ---------------------------------------------------------------------------

class NexStarController(BaseMountController):
    """Celestron NexStar hand-controller over serial via the `nexstar` library."""

    @property
    def mount_type(self) -> str:
        return "nexstar"

    def __init__(self) -> None:
        super().__init__()
        self._hc = None

    async def connect(self, port: str = "") -> None:
        await self._run(self._do_connect, port)

    def _do_connect(self, port: str) -> None:
        import nexstar as ns
        hc = ns.NexstarHandController(port)
        model = hc.getModel()
        logger.info("NexStar connected on %s — model: %s", port, model)
        self._hc = hc
        self.connected = True
        self.port_name = port

    async def disconnect(self) -> None:
        await self._run(self._do_disconnect)

    def _do_disconnect(self) -> None:
        if self._hc is not None:
            try:
                self._hc.close()
            except Exception:
                pass
            self._hc = None
        self.connected = False
        self.port_name = ""
        logger.info("NexStar disconnected")

    async def get_position(self) -> dict:
        pos = await self._run(self._do_get_position)
        self._last_position = pos
        return pos

    def _do_get_position(self) -> dict:
        import nexstar as ns
        az, el = self._hc.getPosition(coordinateMode=ns.AZM_ALT, highPrecisionFlag=True)
        return {"azimuth": az, "elevation": el}

    async def goto(self, azimuth: float = 0.0, elevation: float = 0.0, **_) -> None:
        if not self.connected:
            logger.warning("NexStar goto called while disconnected — ignoring")
            return

        # Large-move intermediate step
        if self._last_position is not None:
            az0 = self._last_position["azimuth"]
            el0 = self._last_position["elevation"]
            if max(abs(azimuth - az0), abs(elevation - el0)) > LARGE_MOVE_DEG:
                mid_az = (az0 + azimuth) / 2.0
                mid_el = (el0 + elevation) / 2.0
                logger.debug("NexStar large move: midpoint (%.1f°, %.1f°)", mid_az, mid_el)
                await self._run(self._do_goto, mid_az, mid_el)
                await self._run(self._do_wait_goto)

        await self._run(self._do_goto, azimuth, elevation)
        await self._run(self._do_wait_goto)
        self._last_position = {"azimuth": azimuth, "elevation": elevation}
        logger.info("NexStar slewed to Az=%.2f° El=%.2f°", azimuth, elevation)

    def _do_goto(self, azimuth: float, elevation: float) -> None:
        import nexstar as ns
        self._hc.gotoPosition(
            firstCoordinate=azimuth,
            secondCoordinate=elevation,
            coordinateMode=ns.AZM_ALT,
            highPrecisionFlag=True,
        )

    def _do_wait_goto(self) -> None:
        while True:
            if not self._hc.getGotoInProgress():
                break
            time.sleep(0.2)


# ---------------------------------------------------------------------------
# ZWO AM5 (RA/Dec via ASCOM)
# ---------------------------------------------------------------------------

class AM5Controller(BaseMountController):
    """
    ZWO AM5 equatorial mount via the ASCOM platform (Windows only).

    Requires:
      - ASCOM Platform 6.x installed
      - ZWO ASCOM telescope driver installed
      - pywin32 package  (`pip install pywin32`)

    The ASCOM driver ProgID defaults to "ASCOM.ZWO.Telescope".
    Pass a different progid to connect() for other ASCOM telescope drivers.

    goto() accepts ra_hours and dec_deg (equatorial J2000 coordinates).
    get_position() returns {"azimuth", "elevation"} by converting the
    mount's reported RA/Dec back to topocentric Az/El using the same math
    as tracking.py, so the UI compass rose stays consistent.
    """

    _DEFAULT_PROGID = "ASCOM.ZWO.Telescope"

    @property
    def mount_type(self) -> str:
        return "am5"

    def __init__(self) -> None:
        super().__init__()
        self._telescope = None   # win32com ASCOM telescope object

    async def connect(self, port: str = "", progid: str = "") -> None:
        """
        Connect to the AM5 via ASCOM.

        port   : ignored (ASCOM manages the COM port internally via the driver)
        progid : ASCOM ProgID, e.g. "ASCOM.ZWO.Telescope"
                 If empty, uses the default ZWO ProgID.
        """
        _progid = progid or self._DEFAULT_PROGID
        await self._run(self._do_connect, _progid)

    def _do_connect(self, progid: str) -> None:
        import win32com.client as win32
        tel = win32.Dispatch(progid)
        tel.Connected = True
        if not tel.Connected:
            raise RuntimeError(f"ASCOM driver {progid!r} refused connection")
        logger.info("AM5 connected via ASCOM ProgID=%s — %s", progid, tel.Description)
        self._telescope = tel
        self.connected = True
        self.port_name = progid   # repurpose port_name to hold the ProgID for display

    async def disconnect(self) -> None:
        await self._run(self._do_disconnect)

    def _do_disconnect(self) -> None:
        if self._telescope is not None:
            try:
                self._telescope.Connected = False
            except Exception:
                pass
            self._telescope = None
        self.connected = False
        self.port_name = ""
        logger.info("AM5 disconnected")

    async def get_position(self) -> dict:
        pos = await self._run(self._do_get_position)
        self._last_position = pos
        return pos

    def _do_get_position(self) -> dict:
        # ASCOM reports RA (hours) and Dec (degrees)
        ra_h  = self._telescope.RightAscension   # decimal hours
        dec_d = self._telescope.Declination      # degrees
        # Convert to Az/El for UI consistency using tracking math
        from backend.tracking import GS_LAT, GS_LON, _gmst_deg, _julian_date, _DEG2RAD, _RAD2DEG
        import math
        import time as t
        jd  = _julian_date(t.time())
        lst = (_gmst_deg(jd) + GS_LON) % 360.0
        ha_deg = lst - ra_h * 15.0
        ha_r   = ha_deg  * _DEG2RAD
        dec_r  = dec_d   * _DEG2RAD
        lat_r  = GS_LAT  * _DEG2RAD
        sin_el = (math.sin(dec_r) * math.sin(lat_r)
                  + math.cos(dec_r) * math.cos(lat_r) * math.cos(ha_r))
        el_r   = math.asin(max(-1.0, min(1.0, sin_el)))
        cos_az = (math.sin(dec_r) - math.sin(lat_r) * sin_el) / (math.cos(lat_r) * math.cos(el_r) + 1e-12)
        az_r   = math.acos(max(-1.0, min(1.0, cos_az)))
        if math.sin(ha_r) > 0:
            az_r = 2 * math.pi - az_r
        return {
            "azimuth":   az_r  * _RAD2DEG,
            "elevation": el_r  * _RAD2DEG,
            "ra_hours":  ra_h,
            "dec_deg":   dec_d,
        }

    async def goto(self, ra_hours: float = 0.0, dec_deg: float = 0.0, **_) -> None:
        if not self.connected:
            logger.warning("AM5 goto called while disconnected — ignoring")
            return
        await self._run(self._do_goto, ra_hours, dec_deg)
        await self._run(self._do_wait_slew)
        logger.info("AM5 slewed to RA=%.4fh Dec=%.4f°", ra_hours, dec_deg)

    def _do_goto(self, ra_hours: float, dec_deg: float) -> None:
        self._telescope.Tracking = True
        self._telescope.SlewToCoordinates(ra_hours, dec_deg)

    def _do_wait_slew(self) -> None:
        while self._telescope.Slewing:
            time.sleep(0.2)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_mount(mount_type: str) -> BaseMountController:
    """
    Return the appropriate controller for the requested mount type.

    mount_type: 'nexstar' | 'am5'
    """
    if mount_type == "nexstar":
        return NexStarController()
    if mount_type == "am5":
        return AM5Controller()
    raise ValueError(f"Unknown mount type {mount_type!r}. Choose 'nexstar' or 'am5'.")
