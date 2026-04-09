"""
ZWO ASI camera controller.

Wraps the `zwoasi` library (which in turn wraps ASICamera2.dll) to
provide async-safe camera capture. All blocking calls run in a
ThreadPoolExecutor so they never stall the FastAPI event loop.

After zwoasi saves a raw TIFF, Pillow + piexif inject a comprehensive
EXIF block containing:
  - Capture timestamp (UTC, ISO 8601)
  - GPS position of the payload (lat, lon, alt MSL)
  - Ground station position
  - Pointing: azimuth, elevation, RA (J2000 hours), Dec (J2000 degrees)
  - Mount type and port
  - All available camera sensor controls (gain, exposure, white balance,
    gamma, brightness, flip, USB bandwidth, …)
  - Camera model name
  - Software tag identifying this system

Metadata is passed in at capture time as a plain dict so this module
stays decoupled from main.py's global state.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger("gs.camera")

_DEFAULT_DLL = Path(__file__).parent.parent / "lib" / "ASICamera2.dll"

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


def _deg_to_dms_rational(deg: float) -> tuple:
    """Convert decimal degrees to (degrees, minutes, seconds) as piexif rationals."""
    d = int(abs(deg))
    m_float = (abs(deg) - d) * 60
    m = int(m_float)
    s_float = (m_float - m) * 60
    # Represent seconds as a rational with 1000x precision
    s_num = int(round(s_float * 1000))
    return ((d, 1), (m, 1), (s_num, 1000))


def _rational(value: float, precision: int = 1000) -> tuple:
    """Express a float as a (numerator, denominator) rational for piexif."""
    return (int(round(value * precision)), precision)


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

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._do_connect)

    def _do_connect(self) -> None:
        import zwoasi
        if not self._dll_path.exists():
            raise FileNotFoundError(f"ASICamera2.dll not found at {self._dll_path}")
        zwoasi.init(str(self._dll_path))

        num = zwoasi.get_num_cameras()
        if num == 0:
            raise RuntimeError("No ZWO ASI cameras detected")
        logger.info("ZWO: %d camera(s) found — opening camera 0", num)

        cam = zwoasi.Camera(0)
        info = cam.get_camera_property()
        self._camera_name = info.get("Name", "ZWO ASI")
        cam.set_control_value(zwoasi.ASI_GAIN,     self._gain)
        cam.set_control_value(zwoasi.ASI_EXPOSURE, self._exposure_ms * 1000)
        cam.set_image_type(zwoasi.ASI_IMG_RAW16)
        self._camera = cam
        self.connected = True
        logger.info("ZWO camera connected: %s", self._camera_name)

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
        Capture a single frame, save to *output_path*, and inject EXIF metadata.

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
            azimuth            float   telescope azimuth to target, degrees
            elevation          float   telescope elevation to target, degrees
            ra_hours           float   J2000 RA of target, decimal hours
            dec_deg            float   J2000 Dec of target, degrees
            distance_m         float   horizontal distance to payload, metres
            slant_m            float   3-D slant range, metres
            mount_type         str     "nexstar" | "am5"
            mount_port         str     serial port / ASCOM ProgID
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
        # 1. Capture raw TIFF via zwoasi
        self._camera.capture(filename=str(output_path))
        logger.info("ZWO: captured frame → %s", output_path)

        # 2. Read all sensor controls immediately after capture
        sensor_controls = self._do_get_all_controls()

        # 3. Inject EXIF
        try:
            self._inject_exif(output_path, metadata, sensor_controls)
        except Exception as e:
            logger.warning("ZWO: EXIF injection failed for %s: %s", output_path.name, e)

        return str(output_path)

    def _inject_exif(self, path: Path, meta: dict, sensor: dict) -> None:
        import piexif
        from PIL import Image

        capture_utc = meta.get("capture_utc", _time.time())
        dt_str = _time.strftime("%Y:%m:%d %H:%M:%S", _time.gmtime(capture_utc))

        # ── ImageIFD ──────────────────────────────────────────────────
        image_ifd = {
            piexif.ImageIFD.Make:             b"ZWO",
            piexif.ImageIFD.Model:            self._camera_name.encode(),
            piexif.ImageIFD.Software:         b"ALTAIR V2 Ground Station",
            piexif.ImageIFD.DateTime:         dt_str.encode(),
            piexif.ImageIFD.ImageDescription: self._build_description(meta, sensor).encode(),
        }

        # ── ExifIFD ───────────────────────────────────────────────────
        # Exposure: stored in seconds as a rational
        exp_us  = sensor.get("Exposure", self._exposure_ms * 1000)
        exp_sec = exp_us / 1_000_000
        gain_val = sensor.get("Gain", self._gain)

        exif_ifd = {
            piexif.ExifIFD.DateTimeOriginal:  dt_str.encode(),
            piexif.ExifIFD.DateTimeDigitized: dt_str.encode(),
            piexif.ExifIFD.ExposureTime:      _rational(exp_sec, 1_000_000),
            piexif.ExifIFD.ISOSpeedRatings:   int(gain_val),
            piexif.ExifIFD.ExposureProgram:   1,   # 1 = manual
        }

        # ── GPS IFD ───────────────────────────────────────────────────
        gps_ifd = {}
        lat = meta.get("payload_lat")
        lon = meta.get("payload_lon")
        alt = meta.get("payload_alt_m")

        if lat is not None and lon is not None:
            gps_ifd[piexif.GPSIFD.GPSLatitudeRef]  = b"N" if lat >= 0 else b"S"
            gps_ifd[piexif.GPSIFD.GPSLatitude]     = _deg_to_dms_rational(lat)
            gps_ifd[piexif.GPSIFD.GPSLongitudeRef] = b"E" if lon >= 0 else b"W"
            gps_ifd[piexif.GPSIFD.GPSLongitude]    = _deg_to_dms_rational(lon)
            gps_ifd[piexif.GPSIFD.GPSMeasureMode]  = b"3"   # 3-D fix
            gps_ifd[piexif.GPSIFD.GPSDateStamp]    = _time.strftime("%Y:%m:%d", _time.gmtime(capture_utc)).encode()

        if alt is not None:
            gps_ifd[piexif.GPSIFD.GPSAltitudeRef] = 0   # 0 = above sea level
            gps_ifd[piexif.GPSIFD.GPSAltitude]    = _rational(max(0.0, alt))

        hdg = meta.get("payload_hdg_deg")
        if hdg is not None:
            gps_ifd[piexif.GPSIFD.GPSImgDirectionRef] = b"T"   # True north
            gps_ifd[piexif.GPSIFD.GPSImgDirection]    = _rational(hdg)

        # ── Assemble & write ──────────────────────────────────────────
        exif_dict = {
            "0th":  image_ifd,
            "Exif": exif_ifd,
            "GPS":  gps_ifd,
        }
        exif_bytes = piexif.dump(exif_dict)

        img = Image.open(path)
        img.save(path, exif=exif_bytes)
        logger.debug("ZWO: EXIF injected into %s", path.name)

    def _build_description(self, meta: dict, sensor: dict) -> str:
        """
        Build a human-readable text block embedded in ImageDescription.
        This is what most image viewers show in their 'description' field,
        and is also machine-parseable as key=value lines.
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
