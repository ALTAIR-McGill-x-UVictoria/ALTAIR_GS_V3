"""
Telescope mount controllers.

Three concrete implementations behind a common async interface:

  NexStarController  — Celestron NexStar (Alt/Az) via the `nexstar` library.
                        Commanded with azimuth / elevation degrees.

  AM5Controller      — ZWO AM5 equatorial mount via ASCOM (win32com).
                        Commanded with RA (decimal hours) / Dec (degrees).
                        Requires the ZWO ASCOM driver installed on Windows.

  IndiMountController — ZWO AM3/AM5 (or any INDI telescope) via the INDI
                        protocol (backend/indi_client.py), e.g. a local
                        `indiserver` running `indi_lx200am5`. Linux-friendly
                        alternative to AM5Controller — no ASCOM/Windows.
                        Commanded with RA (decimal hours) / Dec (degrees).

All run blocking hardware calls in a single-threaded ThreadPoolExecutor
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
        """Return a short identifier: 'nexstar', 'am5', or 'indi'."""

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

    async def sync(self, ra_hours: float, dec_deg: float) -> None:
        """
        Tell the mount "you are actually pointed here" (ASCOM SyncToCoordinates)
        without physically moving it — used by the plate-solve "Apply
        Correction" button (POST /api/telescope/solve/apply in main.py) to
        fix the mount's internal alignment model from a solved RA/Dec.

        Deliberately NOT part of every mount's interface: only AM5Controller
        overrides this. NexStar's controller here exposes no sync primitive,
        and INDI sync semantics vary enough by driver that it wasn't judged
        safe to wire up without per-driver verification. The base
        implementation raises so callers get an explicit, actionable error
        instead of a silent no-op.
        """
        raise NotImplementedError(
            f"{self.mount_type} does not support sync via this backend."
        )

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
        try:
            import nexstar as ns
        except ImportError as e:
            raise RuntimeError(
                "NexStar control requires the `nexstar` package. "
                "Install with `pip install nexstar`."
            ) from e
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

    def _require_hc(self):
        """
        Guard against _hc having gone None between a caller's `if not
        self.connected` check and this method actually running in the
        executor — e.g. a disconnect() queued just ahead of a goto() /
        get_position() call in the same single-worker executor. Without
        this, the dereference below raises a bare AttributeError instead of
        a clear, catchable "disconnected mid-call" error.
        """
        hc = self._hc
        if hc is None:
            raise RuntimeError("NexStar disconnected while this call was queued — aborting")
        return hc

    async def get_position(self) -> dict:
        pos = await self._run(self._do_get_position)
        self._last_position = pos
        return pos

    def _do_get_position(self) -> dict:
        import nexstar as ns
        hc = self._require_hc()
        az, el = hc.getPosition(coordinateMode=ns.AZM_ALT, highPrecisionFlag=True)
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
        hc = self._require_hc()
        hc.gotoPosition(
            firstCoordinate=azimuth,
            secondCoordinate=elevation,
            coordinateMode=ns.AZM_ALT,
            highPrecisionFlag=True,
        )

    def _do_wait_goto(self) -> None:
        hc = self._require_hc()
        while True:
            if not hc.getGotoInProgress():
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

    The ASCOM driver ProgID defaults to "ASCOM.ASIMount.Telescope" — current
    ZWO ASCOM driver releases (confirmed on driver v6.5.36 against ASCOM
    Platform 7 Update 2) register the AM5 under that ProgID, not
    "ASCOM.ZWO.Telescope" as older docs/builds suggested; the latter isn't
    registered at all by this driver version and Dispatch() on it fails with
    "Invalid class string" (HRESULT 0x80040154, REGDB_E_CLASSNOTREG). Pass a
    different progid to connect() for other ASCOM telescope drivers, or to
    match a differently-versioned ZWO driver — check the actual registered
    ProgID with ASCOM's own Profile object if in doubt:
        (New-Object -ComObject ASCOM.Utilities.Profile).RegisteredDevices("Telescope")

    goto() accepts ra_hours and dec_deg (equatorial J2000 coordinates).
    get_position() returns {"azimuth", "elevation"} by converting the
    mount's reported RA/Dec back to topocentric Az/El using the same math
    as tracking.py, so the UI compass rose stays consistent.
    """

    _DEFAULT_PROGID = "ASCOM.ASIMount.Telescope"

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
        try:
            import win32com.client as win32
        except ImportError as e:
            raise RuntimeError(
                "AM5 control requires the ASCOM platform + pywin32, which are "
                "Windows-only. Install with `pip install pywin32` on a Windows "
                "host, and install the ASCOM Platform + ZWO ASCOM telescope "
                "driver. See backend/lib/README.md."
            ) from e
        tel = win32.Dispatch(progid)
        tel.Connected = True
        if not tel.Connected:
            raise RuntimeError(f"ASCOM driver {progid!r} refused connection")
        logger.info("AM5 connected via ASCOM ProgID=%s — %s", progid, tel.Description)

        # The driver computes its own horizon check from SiteLatitude /
        # SiteLongitude / SiteElevation, set independently in its own setup
        # dialog — NOT from backend.tracking.GS_LAT/GS_LON. If those go
        # stale or were never set to this ground station's location, the
        # driver derives a different (sometimes negative) elevation for the
        # same RA/Dec we send and rejects the slew as "below horizon" even
        # though our own tracking math says it's above. Push our GS
        # position into the driver on every connect so both sides agree.
        try:
            from backend.tracking import GS_LAT, GS_LON, GS_ALT
            tel.SiteLatitude  = GS_LAT
            tel.SiteLongitude = GS_LON
            tel.SiteElevation = GS_ALT
            logger.info("AM5 site set to lat=%.4f lon=%.4f alt=%.1f", GS_LAT, GS_LON, GS_ALT)
        except Exception as e:
            logger.warning("AM5: could not set site lat/lon/elevation on driver: %s", e)

        # There's no park/unpark control in this app — parking only ever
        # happens via the mount's own hand controller or a previous ASCOM
        # session. AtPark blocks SlewToCoordinates with a driver-level
        # error, so unpark unconditionally on connect to keep the mount
        # slew-ready.
        try:
            if getattr(tel, "AtPark", False):
                tel.Unpark()
                logger.info("AM5 was parked — unparked on connect")
        except Exception as e:
            logger.warning("AM5: could not unpark on connect: %s", e)

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

    def _require_telescope(self):
        """
        Guard against _telescope having gone None between a caller's
        `if not self.connected` check and this method actually running in
        the executor — e.g. a disconnect() queued just ahead of a goto() /
        get_position() / sync() call in the same single-worker executor.
        Without this, the dereference below raises a bare AttributeError
        instead of a clear, catchable "disconnected mid-call" error.
        """
        tel = self._telescope
        if tel is None:
            raise RuntimeError("AM5 disconnected while this call was queued — aborting")
        return tel

    async def get_position(self) -> dict:
        pos = await self._run(self._do_get_position)
        self._last_position = pos
        return pos

    def _do_get_position(self) -> dict:
        tel = self._require_telescope()
        # ASCOM reports RA (hours) and Dec (degrees)
        ra_h  = tel.RightAscension   # decimal hours
        dec_d = tel.Declination      # degrees
        # Convert to Az/El for UI consistency using tracking math
        from backend.tracking import radec_to_azalt
        az, el = radec_to_azalt(ra_h, dec_d)
        return {
            "azimuth":   az,
            "elevation": el,
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
        tel = self._require_telescope()
        # Re-push site position before every slew: GS_LAT/GS_LON can update
        # after connect (e.g. GS GPS fix arrives later), and the driver's
        # horizon check uses whatever it was last told, not our live value.
        try:
            from backend.tracking import GS_LAT, GS_LON, GS_ALT
            tel.SiteLatitude  = GS_LAT
            tel.SiteLongitude = GS_LON
            tel.SiteElevation = GS_ALT
        except Exception as e:
            logger.warning("AM5: could not refresh site lat/lon/elevation before goto: %s", e)
        tel.Tracking = True
        tel.SlewToCoordinates(ra_hours, dec_deg)

    def _do_wait_slew(self) -> None:
        tel = self._require_telescope()
        while tel.Slewing:
            time.sleep(0.2)

    async def sync(self, ra_hours: float, dec_deg: float) -> None:
        """
        ASCOM SyncToCoordinates — tells the driver's alignment model "you are
        actually pointed at this RA/Dec" without physically slewing. Used by
        the plate-solve Apply Correction button to fix pointing from a
        solved image rather than blind dead-reckoning.
        """
        if not self.connected:
            raise RuntimeError("AM5 sync called while disconnected")
        await self._run(self._do_sync, ra_hours, dec_deg)
        logger.info("AM5 synced to RA=%.4fh Dec=%.4f° (no slew)", ra_hours, dec_deg)

    def _do_sync(self, ra_hours: float, dec_deg: float) -> None:
        tel = self._require_telescope()
        if not getattr(tel, "CanSync", True):
            raise RuntimeError(
                "This ASCOM driver reports CanSync=False — it does not "
                "support sync. Check the driver's capabilities in its "
                "setup dialog."
            )
        tel.SyncToCoordinates(ra_hours, dec_deg)


# ---------------------------------------------------------------------------
# ZWO AM3/AM5 via INDI (direct — indi_lx200am5, no ASCOM/Windows required)
# ---------------------------------------------------------------------------

class IndiMountController(BaseMountController):
    """
    A mount driven over the INDI protocol instead of ASCOM — in practice a
    ZWO AM3/AM5 connected over USB to a local `indiserver` running the
    `indi_lx200am5` driver (part of the `indi-full`/`indi-bin` packages,
    libindi >= 1.9.4), so goto/tracking works on Linux without the
    Windows-only ASCOM driver AM5Controller depends on.

    Commanded with RA (decimal hours) / Dec (degrees), same as AM5Controller
    — INDI mounts are driven through the standard EQUATORIAL_EOD_COORD
    property. get_position() converts to Az/El for UI consistency using the
    same tracking math as AM5Controller.

    connect() reuses the BaseMountController `port` kwarg to carry the INDI
    server's host/IP instead of a serial port — pass "localhost" (indiserver
    running on this machine) or "192.168.1.50:7624" for a remote one. INDI
    port defaults to 7624 if omitted.

    NOTE: written against documented INDI standard properties. The AM5's
    indi_lx200am5 driver was written against the LX200 command set before
    any developer had real AM5/AM3 hardware to test against, and is reported
    as not fully mature — validate goto/tracking/guide behavior against real
    hardware before relying on it. See backend/indi_client.py's module
    docstring and backend/lib/README.md.
    """

    _ON_COORD_SET_SLEW = "SLEW"

    # ZWO AM3/AM5's own USB-serial identity (confirmed against real hardware:
    # enumerates as "ZWO Device / ZWO CDC Device"), used to auto-detect its
    # serial port so it can be handed to indi_lx200am5 via DEVICE_PORT before
    # connecting — the driver's own default (typically /dev/ttyUSB0) is not
    # reliably the mount and, on this ground station, is actually the radio
    # modem, which caused CONNECTION to never report connected.
    _AM5_VID = 0x03C3
    _AM5_PID = 0x4001

    @property
    def mount_type(self) -> str:
        return "indi"

    def __init__(self) -> None:
        super().__init__()
        self._indi = None
        self._device = None

    async def connect(self, port: str = "", **_) -> None:
        await self._run(self._do_connect, port)

    @classmethod
    def _find_mount_port(cls) -> str | None:
        """Only meaningful when indiserver runs on this same machine — a
        remote INDI host's serial devices aren't visible to us locally."""
        import serial.tools.list_ports
        for p in serial.tools.list_ports.comports():
            if p.vid == cls._AM5_VID and p.pid == cls._AM5_PID:
                return p.device
        return None

    def _do_connect(self, host_spec: str) -> None:
        if not host_spec:
            raise RuntimeError("INDI server host required (e.g. localhost or 192.168.1.50:7624)")
        from backend.indi_client import IndiConnection, DEFAULT_INDI_PORT
        if ":" in host_spec:
            host, port_s = host_spec.split(":", 1)
            indi_port = int(port_s)
        else:
            host, indi_port = host_spec, DEFAULT_INDI_PORT

        indi = IndiConnection(host, indi_port)
        indi.connect()
        device = indi.find_telescope()

        mount_port = self._find_mount_port() if host in ("localhost", "127.0.0.1") else None
        if mount_port:
            # The AM5 driver defaults to CONNECTION_MODE=CONNECTION_TCP (its
            # onboard WiFi AP, 192.168.4.1:4030) and only defines DEVICE_PORT
            # once switched to CONNECTION_SERIAL — confirmed against the live
            # driver's property dump, which has no DEVICE_PORT until this
            # switch happens.
            indi.send_switch(device, "CONNECTION_MODE", "CONNECTION_SERIAL")
            indi.wait_property(device, "DEVICE_PORT", timeout=5.0)
            indi.send_text(device, "DEVICE_PORT", {"PORT": mount_port})
            time.sleep(0.5)   # let the driver pick up the new port before CONNECT

        indi.connect_device(device)

        self._indi = indi
        self._device = device
        self.connected = True
        self.port_name = host_spec
        logger.info("INDI mount connected at %s (serial %s)", host_spec, mount_port or "driver default")

    async def disconnect(self) -> None:
        await self._run(self._do_disconnect)

    def _do_disconnect(self) -> None:
        if self._indi is not None:
            if self._device is not None:
                self._indi.disconnect_device(self._device)
            self._indi.disconnect()
            self._indi = None
            self._device = None
        self.connected = False
        self.port_name = ""
        logger.info("INDI mount disconnected")

    def _require_indi(self):
        """
        Guard against _indi/_device having gone None between a caller's `if
        not self.connected` check and this method actually running in the
        executor — e.g. a disconnect() queued just ahead of a goto() /
        get_position() call in the same single-worker executor. Without
        this, the dereference below raises a bare AttributeError instead of
        a clear, catchable "disconnected mid-call" error.
        """
        indi, device = self._indi, self._device
        if indi is None or device is None:
            raise RuntimeError("INDI mount disconnected while this call was queued — aborting")
        return indi, device

    async def get_position(self) -> dict:
        pos = await self._run(self._do_get_position)
        self._last_position = pos
        return pos

    def _do_get_position(self) -> dict:
        indi, device = self._require_indi()
        coords = indi.get_number(device, "EQUATORIAL_EOD_COORD")
        ra_h  = coords.get("RA", 0.0)
        dec_d = coords.get("DEC", 0.0)
        from backend.tracking import radec_to_azalt
        az, el = radec_to_azalt(ra_h, dec_d)
        return {"azimuth": az, "elevation": el, "ra_hours": ra_h, "dec_deg": dec_d}

    async def goto(self, ra_hours: float = 0.0, dec_deg: float = 0.0, **_) -> None:
        if not self.connected:
            logger.warning("INDI mount goto called while disconnected — ignoring")
            return
        await self._run(self._do_goto, ra_hours, dec_deg)
        logger.info("INDI mount slewed to RA=%.4fh Dec=%.4f°", ra_hours, dec_deg)

    def _do_goto(self, ra_hours: float, dec_deg: float) -> None:
        indi, device = self._require_indi()
        try:
            indi.send_switch(device, "ON_COORD_SET", self._ON_COORD_SET_SLEW)
        except Exception:
            pass  # some drivers default to SLEW already / don't expose this switch
        indi.send_number_and_wait(device, "EQUATORIAL_EOD_COORD",
                                   {"RA": ra_hours, "DEC": dec_deg}, timeout=120.0)


# ---------------------------------------------------------------------------
# Emulated mount (ALTAIR_DEBUG) — no hardware, no serial/ASCOM link
# ---------------------------------------------------------------------------

class EmulatedMountController(BaseMountController):
    """
    Software-only stand-in for NexStarController / AM5Controller, active when
    ALTAIR_DEBUG=1. Mimics the interface of whichever real mount_type it is
    asked to impersonate (Az/El for 'nexstar', RA/Dec for 'am5') so the
    Telescope tab, tracking loop, and capture pipeline can be exercised
    end-to-end without a physical mount attached.

    Slews are simulated at SLEW_RATE_DEG_S, capped at MAX_SLEW_S, so the UI
    still shows a brief in-flight state rather than teleporting instantly.
    """

    SLEW_RATE_DEG_S = 5.0
    MAX_SLEW_S = 3.0

    # Mount types commanded in RA/Dec rather than Az/El
    _RADEC_TYPES = ("am5", "indi")

    def __init__(self, emulated_type: str = "nexstar") -> None:
        if emulated_type not in ("nexstar",) + self._RADEC_TYPES:
            raise ValueError(f"Unknown mount type {emulated_type!r}. Choose 'nexstar', 'am5', or 'indi'.")
        super().__init__()
        self._emulated_type = emulated_type
        self._az = 180.0
        self._el = 45.0

    @property
    def mount_type(self) -> str:
        return self._emulated_type

    async def connect(self, port: str = "", progid: str = "", **_) -> None:
        self.connected = True
        self.port_name = f"EMULATED ({port or progid or 'debug'})"
        self._last_position = await self.get_position()
        logger.info("Emulated %s mount connected", self._emulated_type)

    async def disconnect(self) -> None:
        self.connected = False
        self.port_name = ""
        logger.info("Emulated mount disconnected")

    async def get_position(self) -> dict:
        pos = {"azimuth": self._az, "elevation": self._el}
        if self._emulated_type in self._RADEC_TYPES:
            from backend.tracking import azalt_to_radec
            ra_h, dec_d = azalt_to_radec(self._az, self._el)
            pos["ra_hours"] = ra_h
            pos["dec_deg"]  = dec_d
        self._last_position = pos
        return pos

    async def goto(self, azimuth: float | None = None, elevation: float | None = None,
                    ra_hours: float | None = None, dec_deg: float | None = None, **_) -> None:
        if not self.connected:
            logger.warning("Emulated mount goto called while disconnected — ignoring")
            return

        if self._emulated_type in self._RADEC_TYPES:
            if ra_hours is None or dec_deg is None:
                return
            from backend.tracking import radec_to_azalt
            target_az, target_el = radec_to_azalt(ra_hours, dec_deg)
        else:
            if azimuth is None or elevation is None:
                return
            target_az, target_el = azimuth, elevation

        duration = min(max(abs(target_az - self._az), abs(target_el - self._el)) / self.SLEW_RATE_DEG_S,
                        self.MAX_SLEW_S)
        if duration > 0:
            await asyncio.sleep(duration)

        self._az, self._el = target_az, target_el
        self._last_position = await self.get_position()
        logger.info("Emulated %s slewed to Az=%.2f° El=%.2f°", self._emulated_type, target_az, target_el)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_mount(mount_type: str) -> BaseMountController:
    """
    Return the appropriate controller for the requested mount type.

    mount_type: 'nexstar' | 'am5' | 'indi'

    Under ALTAIR_DEBUG=1, always returns an EmulatedMountController that
    impersonates the requested type instead of talking to real hardware.
    """
    import os
    if os.getenv("ALTAIR_DEBUG", "0") == "1":
        return EmulatedMountController(mount_type)
    if mount_type == "nexstar":
        return NexStarController()
    if mount_type == "am5":
        return AM5Controller()
    if mount_type == "indi":
        return IndiMountController()
    raise ValueError(f"Unknown mount type {mount_type!r}. Choose 'nexstar', 'am5', or 'indi'.")
