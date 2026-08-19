"""
ZWO ASI camera controller.

Wraps the `zwoasi` library (which in turn wraps ASICamera2.dll) to
provide async-safe camera capture. All blocking calls run in a
ThreadPoolExecutor so they never stall the FastAPI event loop.

All sensors capture at their native maximum bit depth via ASI_IMG_RAW16 —
including color (Bayer-filtered) sensors, which means the saved data is the
raw undemosaiced single-channel mosaic (each pixel only R, G, or B) rather
than baked-in RGB. This preserves the sensor's full ADC range (e.g. 12-bit
on the ASI585MC Pro, padded into 16-bit words) instead of discarding it for
an in-driver demosaic to 8-bit/channel RGB24. The mosaic looks like a flat
grey speckle field to any viewer that doesn't know to demosaic it, even
though brightness still tracks real scene color correctly — the FITS header
records the Bayer pattern (BAYERPAT) so downstream tools (Siril, PixInsight,
DS9, or this repo's own gallery preview in backend/main.py) can debayer it.
See CameraController._do_connect.

Captures are saved as FITS (via astropy.io.fits) rather than TIFF+EXIF —
the standard format for astronomical/scientific imaging, and directly
loadable by DS9, Siril, PixInsight, etc. Mono frames are a plain 2-D
(H, W) primary HDU; color frames are a (3, H, W) RGB cube. Metadata that
EXIF would have carried is written three ways: a handful of standard FITS
header keywords (DATE-OBS, EXPTIME, GAIN, INSTRUME), one HIERARCH card per
individual value (mount RA/Dec/Az/El, tracking target, GPS fix, every
sensor control readback, ...) so each is independently queryable by FITS
tools, and a full human-readable key=value block of the same data stored
as COMMENT cards for quick eyeballing.

Metadata is passed in at capture time as a plain dict so this module
stays decoupled from main.py's global state.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import time as _time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger("gs.camera")

# ZWO ships the ASI SDK as a per-platform shared library. The filename (not
# just the extension) differs by OS — see backend/lib/README.md for where to
# download each one. Override with ALTAIR_ASI_LIB_PATH if yours lives elsewhere.
_LIB_NAMES = {
    "Windows": "ASICamera2.dll",
    "Linux":   "libASICamera2.so",
    "Darwin":  "libASICamera2.dylib",
}


def _default_lib_path() -> Path | None:
    """
    Resolve the ASI SDK shared library path.

    Precedence:
      1. ALTAIR_ASI_LIB_PATH env var, if set — must point to a real file.
      2. backend/lib/<platform filename>, if a copy was manually placed there.
      3. None — CameraController then falls back to zwoasi's own system-wide
         search (ctypes.util.find_library) at connect time, which resolves
         the library automatically when it was installed via a package
         manager instead of a manual copy — e.g. on Linux, `apt install
         libasicamera2` from the seeing-things/zwo PPA registers it with
         ldconfig (see backend/lib/README.md). Windows has no equivalent
         system-wide install path, so a manual copy (1 or 2) is required there.
    """
    override = os.getenv("ALTAIR_ASI_LIB_PATH")
    if override:
        return Path(override)
    name = _LIB_NAMES.get(platform.system(), _LIB_NAMES["Linux"])
    candidate = Path(__file__).parent / "lib" / name
    return candidate if candidate.exists() else None


_DEFAULT_DLL = _default_lib_path()

# zwoasi control IDs that we want to read back after capture.
# Queried dynamically — any control not present on a given sensor is skipped.
_CONTROL_NAMES = [
    "ASI_GAIN",
    "ASI_EXPOSURE",
    "ASI_GAMMA",
    "ASI_WB_R",
    "ASI_WB_B",
    "ASI_BRIGHTNESS",        # also called Offset on some models
    "ASI_BANDWIDTHOVERLOAD",
    "ASI_OVERCLOCK",
    "ASI_TEMPERATURE",
    "ASI_FLIP",
    "ASI_AUTO_MAX_GAIN",
    "ASI_AUTO_MAX_EXP",
    "ASI_AUTO_TARGET_BRIGHTNESS",
    "ASI_HARDWARE_BIN",
    "ASI_HIGH_SPEED_MODE",
    "ASI_COOLER_POWER_PERC",
    "ASI_TARGET_TEMP",
    "ASI_COOLER_ON",
    "ASI_MONO_BIN",
    "ASI_FAN_ON",
    "ASI_PATTERN_ADJUST",
    "ASI_ANTI_DEW_HEATER",
]

# zwoasi's get_camera_property()["BayerPattern"] is the ASI SDK's
# ASI_BAYER_PATTERN enum (ASI_BAYER_RG=0, BG=1, GR=2, GB=3), naming the
# color of the top-left 2x2 pixel pair. Mapped here to the 4-character FITS
# BAYERPAT convention (e.g. "RGGB") that debayering tools expect.
_BAYER_PATTERN_NAMES = {
    0: "RGGB",
    1: "BGGR",
    2: "GRBG",
    3: "GBRG",
}


class CameraController:
    """Thread-safe async wrapper around a ZWO ASI camera."""

    def __init__(self, dll_path: str | Path | None = None) -> None:
        self._dll_path   = Path(dll_path) if dll_path else _DEFAULT_DLL
        self._camera     = None
        self._executor   = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camera")
        self.connected   = False
        self._gain       = 150
        self._exposure_ms = 1000
        self._camera_name = ""
        self._is_color    = False
        self._bayer_pattern: str | None = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._do_connect)

    def _do_connect(self) -> None:
        try:
            import zwoasi
        except ImportError as e:
            raise RuntimeError(
                "ZWO camera control requires the `zwoasi` package. "
                "Install with `pip install zwoasi`."
            ) from e

        if self._dll_path is not None and not self._dll_path.exists():
            raise FileNotFoundError(
                f"ASI SDK library not found at {self._dll_path}. "
                "See backend/lib/README.md for where to download it."
            )
        try:
            # zwoasi.__init__.py calls init() unconditionally at import time
            # (`ctypes.util.find_library()`, silently succeeding whenever any
            # ASICamera2 lib is discoverable on the system search path), and
            # init() no-ops if zwolib is already set — so without resetting
            # it here, our explicit path below can silently lose to whatever
            # system-wide copy zwoasi already auto-loaded on import, even
            # when that's an older/incompatible SDK version than the one we
            # were explicitly given (confirmed: a stale system 1.18 install
            # auto-loaded ahead of a manually-placed 1.40 copy and failed to
            # recognize a real camera the 1.40 copy detects fine).
            zwoasi.zwolib = None
            # dll_path is None when no manual copy was found — zwoasi.init(None)
            # falls back to ctypes.util.find_library(), which resolves a
            # system-wide install (e.g. the seeing-things/zwo PPA on Linux).
            zwoasi.init(str(self._dll_path) if self._dll_path else None)
        except Exception as e:
            raise RuntimeError(
                "ASI SDK library not found. Place it in backend/lib/, set "
                "ALTAIR_ASI_LIB_PATH, or install it system-wide (e.g. `apt "
                "install libasicamera2` on Linux). See backend/lib/README.md."
            ) from e

        num = zwoasi.get_num_cameras()
        if num == 0:
            raise RuntimeError("No ZWO ASI cameras detected")
        logger.info("ZWO: %d camera(s) found — opening camera 0", num)

        cam = zwoasi.Camera(0)
        info = cam.get_camera_property()
        self._camera_name = info.get("Name", "ZWO ASI")
        cam.set_control_value(zwoasi.ASI_GAIN,     self._gain)
        cam.set_control_value(zwoasi.ASI_EXPOSURE, self._exposure_ms * 1000)
        # Always capture at the sensor's native max bit depth (RAW16, which
        # returns however many bits the ADC actually has — e.g. 12-bit on
        # the ASI585MC Pro — zero-padded into 16-bit words). For a color
        # (Bayer-filtered) sensor this is the raw undemosaiced single-channel
        # mosaic, not RGB — see the module docstring. We record which Bayer
        # pattern it is here so _write_fits can tag BAYERPAT in the header
        # for downstream debayering (Siril/PixInsight/DS9, or this repo's
        # gallery preview in backend/main.py:_fits_to_pil).
        self._is_color = bool(info.get("IsColorCam", False))
        cam.set_image_type(zwoasi.ASI_IMG_RAW16)
        if self._is_color:
            self._bayer_pattern = _BAYER_PATTERN_NAMES.get(info.get("BayerPattern"))
        self._camera = cam
        self.connected = True
        logger.info("ZWO camera connected: %s (%s)", self._camera_name,
                    f"color sensor, RAW16 mosaic ({self._bayer_pattern})" if self._is_color
                    else "mono sensor, RAW16")

    async def disconnect(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._do_disconnect)

    def _do_disconnect(self) -> None:
        if self._camera is not None:
            try:
                self._camera.close()
            except Exception:
                pass
            self._camera = None
        self.connected = False
        logger.info("ZWO camera disconnected")

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    async def set_gain(self, gain: int) -> None:
        self._gain = gain
        if self.connected:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(self._executor, self._do_set_gain, gain)

    def _do_set_gain(self, gain: int) -> None:
        import zwoasi
        self._camera.set_control_value(zwoasi.ASI_GAIN, gain)

    async def set_exposure_ms(self, ms: int) -> None:
        self._exposure_ms = ms
        if self.connected:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(self._executor, self._do_set_exposure, ms)

    def _do_set_exposure(self, ms: int) -> None:
        import zwoasi
        self._camera.set_control_value(zwoasi.ASI_EXPOSURE, ms * 1000)

    # ------------------------------------------------------------------
    # Sensor control readback
    # ------------------------------------------------------------------

    def _do_get_all_controls(self) -> dict:
        """
        Read every available control value from the sensor and return as a dict.
        Keys are human-readable names (e.g. "Gain", "Exposure_us").
        Missing or unreadable controls are silently skipped.
        """
        import zwoasi
        values = {}
        for name in _CONTROL_NAMES:
            const = getattr(zwoasi, name, None)
            if const is None:
                continue
            try:
                val, is_auto = self._camera.get_control_value(const)
                human = name.replace("ASI_", "").replace("_", " ").title()
                values[human] = val
                if is_auto:
                    values[human + " (auto)"] = True
            except Exception:
                pass

        # Camera property block (pixel size, resolution, bit depth, etc.)
        try:
            prop = self._camera.get_camera_property()
            values["Pixel Size um"]   = prop.get("PixelSize", "")
            values["Max Width px"]    = prop.get("MaxWidth",  "")
            values["Max Height px"]   = prop.get("MaxHeight", "")
            values["Bit Depth"]       = prop.get("BitDepth",  "")
            values["Is Color"]        = prop.get("IsColorCam", False)
            values["Has Cooler"]      = prop.get("IsCoolerCam", False)
            values["Has ST4 Port"]    = prop.get("ST4Port", False)
        except Exception:
            pass

        return values

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    async def capture(self, output_path: str | Path, metadata: dict | None = None) -> str:
        """
        Capture a single frame and save it to *output_path* as a FITS file
        with capture/pointing/GPS/sensor metadata in the header.

        metadata keys (all optional):
            capture_utc        float   Unix UTC timestamp of capture
            payload_lat        float   payload GPS latitude, degrees
            payload_lon        float   payload GPS longitude, degrees
            payload_alt_m      float   payload altitude MSL, metres
            payload_alt_rel_m  float   payload altitude above home, metres
            payload_hdg_deg    float   payload heading, degrees
            gs_lat             float   ground station latitude
            gs_lon             float   ground station longitude
            gs_alt             float   ground station altitude MSL
            azimuth            float   computed target azimuth, degrees
            elevation          float   computed target elevation, degrees
            ra_hours           float   J2000 RA of computed target, decimal hours
            dec_deg            float   J2000 Dec of computed target, degrees
            distance_m         float   horizontal distance to payload, metres
            slant_m            float   3-D slant range, metres
            mount_type         str     "nexstar" | "am5" | "indi"
            mount_port         str     serial port / ASCOM ProgID / INDI host
            mount_connected    bool    whether the mount was connected at capture time
            tracking_enabled   bool    whether closed-loop tracking was enabled
            mount_azimuth      float   mount's own reported azimuth, degrees
            mount_elevation    float   mount's own reported elevation, degrees
            mount_ra_hours     float   mount's own reported RA, decimal hours (am5/indi)
            mount_dec_deg      float   mount's own reported Dec, degrees (am5/indi)
        """
        if not self.connected or self._camera is None:
            raise RuntimeError("Camera not connected")
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_event_loop()
        saved_path = await loop.run_in_executor(
            self._executor, self._do_capture_and_tag, out, metadata or {}
        )
        return saved_path

    def _do_capture_and_tag(self, output_path: Path, metadata: dict) -> str:
        # Capture the raw frame array directly via zwoasi (no filename=
        # kwarg — that path round-trips through a PIL-saved file, which we
        # don't want since we're writing FITS instead). RAW16 mode always
        # hands back a single-channel (H, W) array, mono or Bayer mosaic.
        arr = self._camera.capture()
        logger.info("ZWO: captured frame → %s", output_path)

        # Read all sensor controls immediately after capture
        sensor_controls = self._do_get_all_controls()

        # Write FITS with metadata header — a write failure here means no
        # file was saved at all (unlike the old TIFF+EXIF flow, where a
        # failed EXIF injection still left a usable TIFF behind), so this
        # is allowed to propagate rather than being logged-and-swallowed.
        self._write_fits(output_path, arr, metadata, sensor_controls)

        return str(output_path)

    def _write_fits(self, path: Path, arr, meta: dict, sensor: dict) -> None:
        """
        Write `arr` — a 2-D (H, W) mono array or 3-D (3, H, W) RGB cube — to
        `path` as a FITS primary HDU. FITS has no EXIF equivalent, so
        metadata is written three ways: a few standard/searchable header
        keywords (DATE-OBS, EXPTIME, GAIN, INSTRUME), one HIERARCH card per
        individual value via _apply_metadata_headers() (GPS position,
        pointing/tracking, mount, sensor controls) so every value is
        independently queryable, and the same data as a human-readable
        key=value COMMENT block from _build_description() for quick
        eyeballing.
        """
        try:
            from astropy.io import fits
        except ImportError as e:
            raise RuntimeError(
                "FITS capture requires the `astropy` package. "
                "Install with `pip install astropy`."
            ) from e

        capture_utc = meta.get("capture_utc", _time.time())
        exp_us   = sensor.get("Exposure", self._exposure_ms * 1000)
        gain_val = sensor.get("Gain", self._gain)

        hdu = fits.PrimaryHDU(data=arr)
        hdr = hdu.header
        hdr["DATE-OBS"] = (
            _time.strftime("%Y-%m-%dT%H:%M:%S", _time.gmtime(capture_utc)),
            "UTC of exposure start",
        )
        hdr["EXPTIME"]  = (exp_us / 1_000_000, "Exposure time, seconds")
        hdr["GAIN"]     = (int(gain_val), "Camera gain")
        hdr["INSTRUME"] = (self._camera_name.encode("ascii", "replace").decode("ascii"), "Camera model")
        hdr["CREATOR"]  = "ALTAIR V2 Ground Station"
        if arr.ndim == 3:
            hdr["CTYPE3"] = ("RGB", "Axis 3 = color plane (R, G, B)")
        # BAYERPAT is the de-facto standard card for "this 2-D frame is an
        # un-demosaiced colour mosaic" — Siril, ASTAP, PixInsight et al. read
        # it to debayer on load. Written whenever the capture path reported a
        # pattern (ASI color sensors always; Canon RAW path via rawpy).
        if self._bayer_pattern:
            hdr["BAYERPAT"] = (self._bayer_pattern, "Bayer mosaic pattern; data is undemosaiced RAW")

        self._apply_metadata_headers(hdr, meta, sensor)

        for line in self._build_description(meta, sensor).splitlines():
            # FITS header values must be strict printable ASCII — astropy
            # raises ValueError on anything else (confirmed against real
            # hardware: the em dash in _build_description's own header line
            # triggered this). Any non-ASCII byte (em dash, degree sign, a
            # stray unicode char in a future metadata value, ...) is
            # replaced with '?' rather than dropping the whole line.
            hdr["COMMENT"] = line.encode("ascii", "replace").decode("ascii")

        hdu.writeto(path, overwrite=True)
        logger.debug("ZWO: FITS written to %s", path.name)

    def _apply_metadata_headers(self, hdr, meta: dict, sensor: dict) -> None:
        """
        Promote every capture/pointing/GPS/sensor value into its own FITS
        header card, using the HIERARCH convention for names over 8
        characters, so each value is independently queryable by FITS tools
        (fitsheader, DS9, astropy) rather than only living inside the
        COMMENT text block _build_description() writes below.
        """
        def put(key: str, value, comment: str = "") -> None:
            if value is None:
                return
            if isinstance(value, str):
                value = value.encode("ascii", "replace").decode("ascii")
            try:
                hdr[f"HIERARCH ALTAIR {key}"] = (value, comment)
            except Exception:
                logger.debug("FITS header: skipped unsupported value for %r", key)

        # Mount's own live-queried pointing at capture time
        put("MOUNT RA",        meta.get("mount_ra_hours"),  "Mount-reported RA, J2000 decimal hours")
        put("MOUNT DEC",       meta.get("mount_dec_deg"),   "Mount-reported Dec, J2000 degrees")
        put("MOUNT AZ",        meta.get("mount_azimuth"),   "Mount-reported azimuth, degrees")
        put("MOUNT ALT",       meta.get("mount_elevation"), "Mount-reported elevation, degrees")
        put("MOUNT TYPE",      meta.get("mount_type"),      "Mount controller type")
        put("MOUNT PORT",      meta.get("mount_port"),      "Mount port / ProgID / INDI host")
        put("MOUNT CONNECTED", meta.get("mount_connected"), "Mount connected at capture time")
        put("TRACKING",        meta.get("tracking_enabled"),"Closed-loop tracking enabled at capture time")

        # Tracking-computed target (derived from payload GPS) — distinct
        # from the mount's own reported position above
        put("TARGET RA",    meta.get("ra_hours"),   "Computed target RA, J2000 decimal hours")
        put("TARGET DEC",   meta.get("dec_deg"),    "Computed target Dec, J2000 degrees")
        put("TARGET AZ",    meta.get("azimuth"),    "Computed target azimuth, degrees")
        put("TARGET ALT",   meta.get("elevation"),  "Computed target elevation, degrees")
        put("TARGET DIST",  meta.get("distance_m"), "Horizontal distance to target, metres")
        put("TARGET SLANT", meta.get("slant_m"),    "3-D slant range to target, metres")

        # Ground station site
        put("SITE LAT", meta.get("gs_lat"), "Ground station latitude, degrees")
        put("SITE LON", meta.get("gs_lon"), "Ground station longitude, degrees")
        put("SITE ALT", meta.get("gs_alt"), "Ground station altitude MSL, metres")

        # Payload GPS
        put("PAYLOAD LAT",    meta.get("payload_lat"),       "Payload GPS latitude, degrees")
        put("PAYLOAD LON",    meta.get("payload_lon"),       "Payload GPS longitude, degrees")
        put("PAYLOAD ALT",    meta.get("payload_alt_m"),     "Payload GPS altitude MSL, metres")
        put("PAYLOAD ALTAGL", meta.get("payload_alt_rel_m"), "Payload altitude above home, metres")
        put("PAYLOAD HDG",    meta.get("payload_hdg_deg"),   "Payload heading, degrees")

        # Every sensor control read back after capture
        for key, value in sensor.items():
            safe = key.upper().replace("(", "").replace(")", "")
            put(f"SENSOR {safe}", value)

    def _build_description(self, meta: dict, sensor: dict) -> str:
        """
        Build a human-readable key=value text block, stored as COMMENT cards
        in the FITS header (see _write_fits).
        Machine-parseable (one KEY=VALUE per line) as well as human-readable.
        """
        lines = ["ALTAIR V2 — Telescope Capture"]

        # Timestamp
        utc = meta.get("capture_utc", _time.time())
        lines.append(f"CaptureUTC={_time.strftime('%Y-%m-%dT%H:%M:%S', _time.gmtime(utc))}")

        # Payload GPS
        for key, label in [
            ("payload_lat",       "PayloadLat_deg"),
            ("payload_lon",       "PayloadLon_deg"),
            ("payload_alt_m",     "PayloadAltMSL_m"),
            ("payload_alt_rel_m", "PayloadAltAGL_m"),
            ("payload_hdg_deg",   "PayloadHeading_deg"),
        ]:
            if key in meta:
                lines.append(f"{label}={meta[key]}")

        # Ground station
        for key, label in [
            ("gs_lat", "GS_Lat_deg"),
            ("gs_lon", "GS_Lon_deg"),
            ("gs_alt", "GS_Alt_m"),
        ]:
            if key in meta:
                lines.append(f"{label}={meta[key]}")

        # Pointing / tracking
        for key, label in [
            ("azimuth",    "Azimuth_deg"),
            ("elevation",  "Elevation_deg"),
            ("ra_hours",   "RA_hours"),
            ("dec_deg",    "Dec_deg"),
            ("distance_m", "HorizDist_m"),
            ("slant_m",    "SlantRange_m"),
        ]:
            if key in meta:
                lines.append(f"{label}={meta[key]}")

        # Mount
        if "mount_type" in meta:
            lines.append(f"MountType={meta['mount_type']}")
        if "mount_port" in meta:
            lines.append(f"MountPort={meta['mount_port']}")

        # Camera / sensor
        lines.append(f"CameraModel={self._camera_name}")
        for k, v in sensor.items():
            safe_key = k.replace(" ", "_").replace("(", "").replace(")", "")
            lines.append(f"Sensor_{safe_key}={v}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Status snapshot for frontend
    # ------------------------------------------------------------------

    def status_dict(self) -> dict:
        return {
            "connected":    self.connected,
            "camera_name":  self._camera_name,
            "gain":         self._gain,
            "exposure_ms":  self._exposure_ms,
        }


# ---------------------------------------------------------------------------
# Emulated camera (ALTAIR_DEBUG) — no hardware, no ASI SDK
# ---------------------------------------------------------------------------

class EmulatedCameraController(CameraController):
    """
    Software-only stand-in for CameraController, active when ALTAIR_DEBUG=1.

    Synthesizes a noise frame instead of talking to a ZWO ASI camera and the
    ASICamera2 SDK, but reuses the real EXIF-injection pipeline so captures
    still carry pointing/GPS/sensor metadata for end-to-end testing of the
    Telescope tab and gallery without hardware attached.
    """

    _SENSOR_CONTROLS = {
        "Pixel Size um":  3.75,
        "Max Width px":   1920,
        "Max Height px":  1080,
        "Bit Depth":      12,
        "Is Color":       True,
        "Has Cooler":     False,
        "Has ST4 Port":   False,
        "Temperature":    24.5,
    }

    async def connect(self, **_) -> None:
        self._camera_name = "Emulated ASI Camera"
        self._camera = "emulated"   # sentinel — truthy, never dereferenced as a real handle
        self.connected = True
        self._is_color = True
        # Fixed pattern (not queried from real hardware) — just needs to be
        # a valid BAYERPAT value so the gallery's debayer preview path
        # (backend/main.py:_fits_to_pil) gets exercised the same way real
        # ASI/Canon color captures do.
        self._bayer_pattern = "RGGB"
        logger.info("Emulated camera connected: %s", self._camera_name)

    async def disconnect(self) -> None:
        self._camera = None
        self.connected = False
        logger.info("Emulated camera disconnected")

    async def set_gain(self, gain: int) -> None:
        self._gain = gain

    async def set_exposure_ms(self, ms: int) -> None:
        self._exposure_ms = ms

    def _do_get_all_controls(self) -> dict:
        return {
            "Gain":     self._gain,
            "Exposure": self._exposure_ms * 1000,
            **self._SENSOR_CONTROLS,
        }

    def _do_capture_and_tag(self, output_path: Path, metadata: dict) -> str:
        import numpy as np
        # Synthetic 12-bit-in-uint16 Bayer mosaic — same shape/dtype/BAYERPAT
        # tagging a real color sensor in RAW16 mode would produce, so the
        # FITS-write and gallery debayer-preview paths get exercised the
        # same way as real hardware.
        arr = np.random.randint(0, 4096, size=(1080, 1920), dtype=np.uint16)
        logger.info("Emulated: synthesized frame -> %s", output_path)

        sensor_controls = self._do_get_all_controls()
        self._write_fits(output_path, arr, metadata, sensor_controls)

        return str(output_path)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_camera(camera_type: str = "zwo") -> CameraController:
    """
    Return the appropriate controller for the requested camera type.

    Under ALTAIR_DEBUG=1, always returns an EmulatedCameraController so the
    Telescope tab works without hardware.
    """
    if os.getenv("ALTAIR_DEBUG", "0") == "1":
        return EmulatedCameraController()
    if camera_type == "zwo":
        return CameraController()
    if camera_type == "canon":
        # Imported lazily so the `gphoto2` package is only required when
        # this camera type is actually selected — see backend/camera_canon.py.
        from backend.camera_canon import CanonCameraController
        return CanonCameraController()
    raise ValueError(f"Unknown camera type {camera_type!r}. Choose 'zwo' or 'canon'.")
