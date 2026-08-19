"""
Canon EOS Rebel T3i (600D) camera controller — DSLR alternative to the ZWO
ASI585MC, to test whether the T3i's much larger APS-C sensor (22.3 x 14.9mm
vs the ASI585MC's 1/1.2" 11.2 x 6.3mm) is worth the tradeoffs (no cooling,
no direct raw-frame capture over USB in the same way, mechanical shutter).

Talks to the camera over USB via libgphoto2 (the standard Linux/macOS/
Windows tool for tethered DSLR control) through the `gphoto2` Python
bindings — NOT the unrelated `zwoasi` API the base CameraController uses.
See backend/lib/README.md section 5 for install steps (libgphoto2 +
`pip install gphoto2`, plus the gvfs-auto-mount workaround needed on most
Linux desktops).

Subclasses CameraController and overrides only the hardware-facing bits
(_do_connect / _do_disconnect / _do_set_gain / _do_set_exposure /
_do_get_all_controls / _do_capture_and_tag). The async connect() /
disconnect() / set_gain() / set_exposure_ms() / capture() wrappers, and the
FITS-writing pipeline (_write_fits / _apply_metadata_headers /
_build_description), are inherited unchanged from CameraController — this
mirrors how EmulatedCameraController is already structured in camera.py.

Unit mapping, since a DSLR has no "gain" or millisecond-exposure control:
    gain (int)        -> ISO speed (e.g. 100, 200, 400, ... 6400)
    exposure_ms (int)  -> shutter speed, matched to the nearest speed the
                          camera actually offers (T3i offers discrete
                          fractions like 1/4000s ... 30s; there is no
                          continuous exposure setting outside bulb mode).
                          Requests longer than the slowest non-bulb speed
                          (usually 30s) are clamped to that speed rather
                          than driving bulb mode — true bulb (mirror-lock,
                          manual timed release) is not implemented here.

Captures are downloaded as CR2 RAW (imageformat is forced to the camera's
plain "RAW" choice at connect time, when that config option exists) rather
than JPEG, to get the sensor's native bit depth (14-bit on the T3i/600D)
instead of an in-camera-processed 8-bit JPEG. The CR2 is decoded with
`rawpy` (wraps libraw — `pip install rawpy`) to the raw, undemosaiced
single-channel Bayer mosaic (`raw_image_visible`), matching how camera.py's
CameraController now always requests ASI_IMG_RAW16 for the ASI camera —
see that module's docstring for why undemosaiced is preferred over a
demosaiced-but-bit-depth-reduced image. The mosaic is handed to the same
_write_fits() the ASI controller uses (with self._bayer_pattern set first
so the FITS gets a BAYERPAT header), so gallery preview (which debayers for
display), raw-pixel export, and metadata headers all work identically
regardless of which camera produced the frame.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend.camera import CameraController

logger = logging.getLogger("gs.camera.canon")

# PTP config widget names libgphoto2 exposes for Canon EOS bodies. A couple
# of alternates are tried because widget naming has drifted slightly across
# libgphoto2 versions / camera generations.
_ISO_NAMES      = ["iso"]
_SHUTTER_NAMES  = ["shutterspeed", "shutterspeed2"]
_APERTURE_NAMES = ["aperture", "f-number"]
_WB_NAMES       = ["whitebalance"]
_FORMAT_NAMES   = ["imageformat", "imagequality"]


def _parse_iso(choice: str) -> float | None:
    try:
        return float(choice)
    except ValueError:
        return None   # e.g. "Auto"


def _parse_shutter_seconds(choice: str) -> float | None:
    c = choice.strip().lower()
    if c in ("bulb", "auto"):
        return None
    if "/" in c:
        num, _, den = c.partition("/")
        try:
            return float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(c)
    except ValueError:
        return None


def _nearest_choice(choices: list[str], target: float, parse_fn) -> str | None:
    """Return the choice string whose parsed value is closest to target."""
    parsed = [(parse_fn(c), c) for c in choices]
    parsed = [(v, c) for v, c in parsed if v is not None]
    if not parsed:
        return None
    return min(parsed, key=lambda vc: abs(vc[0] - target))[1]


class CanonCameraController(CameraController):
    """Thread-safe async wrapper around a Canon EOS DSLR via libgphoto2."""

    def __init__(self) -> None:
        self._camera        = None
        self._executor       = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camera-canon")
        self.connected        = False
        self._gain            = 100     # ISO
        self._exposure_ms     = 1000    # shutter time; matched to nearest supported speed
        self._camera_name     = ""
        self._is_color        = True    # every Canon EOS body has a Bayer color sensor
        self._bayer_pattern: str | None = None   # set per-capture from rawpy, see _do_capture_and_tag
        self._iso_choices: list[str]     = []
        self._shutter_choices: list[str] = []

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _do_connect(self) -> None:
        try:
            import gphoto2 as gp
        except ImportError as e:
            raise RuntimeError(
                "Canon camera control requires libgphoto2 + the `gphoto2` "
                "Python package. Install libgphoto2 (e.g. `apt install "
                "libgphoto2-dev` on Linux, or `brew install libgphoto2` on "
                "macOS) then `pip install gphoto2`. See backend/lib/README.md."
            ) from e

        try:
            camera = gp.Camera()
            camera.init()
        except gp.GPhoto2Error as e:
            raise RuntimeError(self._connect_error_message(e)) from e

        try:
            self._camera_name = camera.get_summary().text.splitlines()[0].strip()
        except Exception:
            self._camera_name = "Canon EOS"

        config = camera.get_config()

        # Force plain RAW output (not "RAW + JPEG", which would double the
        # per-shot transfer and leave an orphan JPEG on the card) so capture
        # gets the sensor's native bit depth instead of an in-camera 8-bit
        # JPEG. Preference order: a choice that says "raw" but not "+" (i.e.
        # RAW-only, not a RAW+JPEG combo) first, then any "raw" choice as a
        # fallback if that's all the body offers.
        fmt_widget = self._get_child(config, _FORMAT_NAMES)
        if fmt_widget is not None:
            try:
                choices = list(fmt_widget.get_choices())
                raw_choice = next(
                    (c for c in choices if "raw" in c.lower() and "+" not in c),
                    next((c for c in choices if "raw" in c.lower()), None),
                )
                if raw_choice is not None:
                    fmt_widget.set_value(raw_choice)
                else:
                    logger.warning("Canon: no RAW image format choice found among %r — "
                                    "captures will stay in the camera's current format", choices)
            except Exception:
                logger.debug("Canon: could not force RAW image format", exc_info=True)

        iso_widget     = self._get_child(config, _ISO_NAMES)
        shutter_widget = self._get_child(config, _SHUTTER_NAMES)
        self._iso_choices     = list(iso_widget.get_choices()) if iso_widget is not None else []
        self._shutter_choices = list(shutter_widget.get_choices()) if shutter_widget is not None else []

        if iso_widget is not None:
            pick = _nearest_choice(self._iso_choices, float(self._gain), _parse_iso)
            if pick is not None:
                iso_widget.set_value(pick)
        if shutter_widget is not None:
            pick = _nearest_choice(self._shutter_choices, self._exposure_ms / 1000.0, _parse_shutter_seconds)
            if pick is not None:
                shutter_widget.set_value(pick)

        camera.set_config(config)

        self._camera = camera
        self.connected = True
        logger.info("Canon camera connected: %s", self._camera_name)

    def _do_disconnect(self) -> None:
        if self._camera is not None:
            try:
                self._camera.exit()
            except Exception:
                pass
            self._camera = None
        self.connected = False
        logger.info("Canon camera disconnected")

    @staticmethod
    def _connect_error_message(e) -> str:
        """
        Turn a gp.GPhoto2Error from camera.init() into an actionable message.
        e.code is one of libgphoto2's GP_ERROR_* constants — branching on it
        avoids dumping every possible cause on the user regardless of which
        one actually applies.
        """
        import gphoto2 as gp

        code = getattr(e, "code", None)
        if code == gp.GP_ERROR_MODEL_NOT_FOUND:
            # libgphoto2's auto-detect probe found zero cameras it
            # recognizes as a distinct PTP device — this is NOT "found the
            # wrong model", it's "found nothing at all". Verify independent
            # of our Python bindings with the system `gphoto2` CLI:
            #   gphoto2 --auto-detect
            # An empty/"no device" result there confirms it's a
            # connection/mode/gvfs problem, not a bug in this module.
            return (
                "No camera detected on USB (libgphoto2 auto-detect found "
                "nothing) [-105 Unknown model]. Check, in order: (1) the "
                "camera is powered on, awake, and its USB cable is firmly "
                "seated at both ends; (2) the camera's Communication/USB "
                "setting is 'PTP' ('PC Connection' on the T3i), not Mass "
                "Storage or Auto — Mass Storage mode makes it invisible to "
                "libgphoto2 entirely; (3) on Linux, gvfs's auto-mount may "
                "have already claimed it — run `killall gvfsd-gphoto2 "
                "gvfs-gphoto2-volume-monitor` then retry; (4) confirm "
                "independently of this backend with the system `gphoto2` "
                "CLI: `gphoto2 --auto-detect` should list the camera by "
                "name — if it doesn't either, the problem is the USB/camera "
                "setup, not this code."
            )
        if code in (gp.GP_ERROR_IO_USB_CLAIM, gp.GP_ERROR_IO_USB_FIND):
            return (
                "Found the camera on USB but could not claim/open it "
                f"({e}). Something else already has it open — most often "
                "gvfs's auto-mount on Linux: run `killall gvfsd-gphoto2 "
                "gvfs-gphoto2-volume-monitor` and retry. Also check no "
                "other gphoto2/tethering app (e.g. a photo import dialog) "
                "is running."
            )
        if code == gp.GP_ERROR_CAMERA_BUSY:
            return (
                f"Camera reported busy ({e}). Power-cycle the camera and "
                "retry — this usually means a previous session left it in "
                "a stuck tethering state."
            )
        return (
            f"Could not open the Canon camera over USB ({e}). Common "
            "causes: the camera is off/asleep, gvfs auto-mount grabbed it "
            "first (Linux: `killall gvfsd-gphoto2 "
            "gvfs-gphoto2-volume-monitor`), or it's in a USB mode other "
            "than PTP. Run `gphoto2 --auto-detect` (system CLI) to check "
            "independent of this backend."
        )

    @staticmethod
    def _get_child(config, names: list[str]):
        for name in names:
            try:
                return config.get_child_by_name(name)
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _do_set_gain(self, gain: int) -> None:
        """gain is treated as an ISO value, snapped to the nearest offered ISO."""
        config = self._camera.get_config()
        widget = self._get_child(config, _ISO_NAMES)
        if widget is None:
            logger.warning("Canon: camera has no ISO control")
            return
        choices = list(widget.get_choices())
        self._iso_choices = choices
        pick = _nearest_choice(choices, float(gain), _parse_iso)
        if pick is None:
            logger.warning("Canon: no numeric ISO choice available near %d", gain)
            return
        if pick != str(gain):
            logger.info("Canon: requested ISO %d -> nearest available %s", gain, pick)
        widget.set_value(pick)
        self._camera.set_config(config)

    def _do_set_exposure(self, ms: int) -> None:
        """ms is matched to the nearest shutter speed the camera actually offers."""
        config = self._camera.get_config()
        widget = self._get_child(config, _SHUTTER_NAMES)
        if widget is None:
            logger.warning("Canon: camera has no shutter speed control")
            return
        choices = list(widget.get_choices())
        self._shutter_choices = choices
        target_s = ms / 1000.0
        pick = _nearest_choice(choices, target_s, _parse_shutter_seconds)
        if pick is None:
            logger.warning("Canon: no fixed shutter speed choice available near %d ms (bulb mode not implemented)", ms)
            return
        picked_s = _parse_shutter_seconds(pick)
        if picked_s is not None and abs(picked_s - target_s) > 0.05 * max(target_s, picked_s):
            logger.info("Canon: requested %d ms exposure -> nearest available %s (%.3fs)", ms, pick, picked_s)
        widget.set_value(pick)
        self._camera.set_config(config)

    # ------------------------------------------------------------------
    # Sensor control readback
    # ------------------------------------------------------------------

    def _do_get_all_controls(self) -> dict:
        config = self._camera.get_config()
        values: dict = {}
        for label, names in [
            ("Iso",           _ISO_NAMES),
            ("Shutterspeed",  _SHUTTER_NAMES),
            ("Aperture",      _APERTURE_NAMES),
            ("Whitebalance",  _WB_NAMES),
            ("Imageformat",   _FORMAT_NAMES),
        ]:
            widget = self._get_child(config, names)
            if widget is None:
                continue
            try:
                values[label] = widget.get_value()
            except Exception:
                pass

        try:
            abilities = self._camera.get_abilities()
            values["Model"] = abilities.model
        except Exception:
            pass

        return values

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _do_capture_and_tag(self, output_path: Path, metadata: dict) -> str:
        import io as _io

        import gphoto2 as gp
        import rawpy

        file_path = self._camera.capture(gp.GP_CAPTURE_IMAGE)
        cam_file = self._camera.file_get(file_path.folder, file_path.name, gp.GP_FILE_TYPE_NORMAL)
        raw_bytes = bytes(memoryview(cam_file.get_data_and_size()))
        logger.info("Canon: captured frame → %s", output_path)

        # Free the card slot — we've already pulled the bytes over USB.
        try:
            self._camera.file_delete(file_path.folder, file_path.name)
        except Exception:
            logger.debug("Canon: could not delete frame from camera storage", exc_info=True)

        # Decode the CR2 with rawpy/libraw to the raw, undemosaiced Bayer
        # mosaic at the sensor's native bit depth (uint16-packed; 14-bit on
        # the T3i/600D) — NOT rawpy's postprocess()/dcraw path, which would
        # demosaic and tone-map down to an 8-bit-per-channel RGB image and
        # throw away exactly the bit depth we're capturing RAW to keep.
        # raw_image_visible excludes the sensor's masked/optical-black
        # border pixels that raw_image includes.
        with rawpy.imread(_io.BytesIO(raw_bytes)) as raw:
            arr = raw.raw_image_visible.copy()                  # (H, W) uint16 Bayer mosaic
            self._bayer_pattern = self._rawpy_bayer_pattern(raw)
            bit_depth = int(raw.white_level).bit_length()

        sensor_controls = self._do_get_all_controls()
        sensor_controls["Bit Depth"] = bit_depth

        self._write_fits(output_path, arr, metadata, sensor_controls)

        return str(output_path)

    @staticmethod
    def _rawpy_bayer_pattern(raw) -> str | None:
        """
        Build a 4-character FITS BAYERPAT string (e.g. "RGGB") from rawpy's
        2x2 raw_pattern index grid + color_desc name table, matching the
        convention camera.py uses for the ASI camera's BayerPattern.
        """
        try:
            return "".join(
                chr(raw.color_desc[raw.raw_pattern[row, col]])
                for row in range(2) for col in range(2)
            )
        except Exception:
            logger.debug("Canon: could not determine Bayer pattern from rawpy", exc_info=True)
            return None
