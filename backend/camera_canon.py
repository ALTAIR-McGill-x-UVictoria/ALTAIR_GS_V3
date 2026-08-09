"""
Canon EOS Rebel T3i (600D) camera controller — DSLR alternative to the ZWO
ASI585MC, to test whether the T3i's much larger APS-C sensor (22.3 x 14.9mm
vs the ASI585MC's 1/1.2" 11.2 x 6.3mm) is worth the tradeoffs (no cooling,
no direct raw-frame capture over USB in the same way, mechanical shutter).

Two mutually-exclusive backends provide the same hardware-facing primitives,
selected once at connect time by platform.system():

  - Linux/macOS: libgphoto2 (the standard tethered-DSLR-control library)
    through the `gphoto2` Python bindings — NOT the unrelated `zwoasi` API
    the base CameraController uses. See backend/lib/README.md section 5 for
    install steps (libgphoto2 + `pip install gphoto2`, plus the
    gvfs-auto-mount workaround needed on most Linux desktops).

  - Windows: libgphoto2 has no first-class Windows build, so Windows instead
    drives digiCamControl (open-source Windows DSLR tethering app,
    http://digicamcontrol.com/) via its built-in local HTTP remote-control
    server (default http://127.0.0.1:5513). digiCamControl must already be
    running with the T3i connected — this module talks to it over loopback
    HTTP using only the stdlib (urllib), no extra pip package. See
    backend/lib/README.md section 5 for setup.

Subclasses CameraController and overrides only the hardware-facing bits
(_do_connect / _do_disconnect / _do_set_gain / _do_set_exposure /
_do_get_all_controls / _do_capture_and_tag). The async connect() /
disconnect() / set_gain() / set_exposure_ms() / capture() wrappers, and the
FITS-writing pipeline (_write_fits / _apply_metadata_headers /
_build_description), are inherited unchanged from CameraController — this
mirrors how EmulatedCameraController is already structured in camera.py.
CanonCameraController itself only picks a backend and delegates every
hardware call to it, so main.py/camera.py need no platform-specific code.

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

Captures are shot RAW (CR2) — image format is forced at connect time — and
stored as the *un-demosaiced* 16-bit Bayer mosaic: a 2-D (H, W) uint16
array handed to the same _write_fits() the ASI controller uses. This is
what plate solvers want (single channel, full bit depth, no interpolation);
debayering to RGB would interpolate away real detail and triple the size
for no astrometric benefit. Decoding needs `rawpy` (LibRaw bindings).

The Bayer pattern travels in the FITS header as the standard BAYERPAT card,
so external tools (Siril, ASTAP, PixInsight) debayer correctly on load, and
the gallery debayers for *preview* only — see _fits_to_pil in main.py. The
stored pixels are never modified.

Bulb mode is still not implemented, so exposures remain capped at the
camera's slowest fixed shutter speed (usually 30s).
"""
from __future__ import annotations

import logging
import os
import platform
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


def _pick_raw_choice(choices: list[str]) -> str | None:
    """
    Pick the RAW-only image-format choice from a camera's offered list.

    Canon bodies expose these as strings like "RAW", "RAW + Large Fine JPEG",
    "Large Fine JPEG", ... — we want RAW *alone*, not RAW+JPEG, since the
    combined mode doubles capture/transfer time for a JPEG we regenerate from
    the FITS anyway (see the gallery preview path in main.py). Matching is
    substring-based and case-insensitive because the exact wording drifts
    across libgphoto2 versions and camera generations.
    """
    lowered = [(c, c.lower()) for c in choices]
    # RAW without any JPEG mentioned alongside it.
    for original, low in lowered:
        if "raw" in low and "jpeg" not in low and "jpg" not in low:
            return original
    # Fall back to a combined RAW+JPEG mode — still gives us the CR2, just
    # with the extra JPEG we don't need.
    for original, low in lowered:
        if "raw" in low:
            return original
    return None


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


def _decode_raw_to_mono_bayer(raw_bytes: bytes) -> tuple["object", str]:
    """
    Decode Canon CR2 bytes to the un-demosaiced sensor mosaic.

    Returns (arr, bayer_pattern) where arr is a 2-D (H, W) uint16 array of the
    raw Bayer data and bayer_pattern is e.g. "RGGB" describing its layout.

    Deliberately does NOT demosaic. Plate solvers want single-channel data,
    and debayering to RGB both interpolates away real detail and triples the
    data size for no benefit to astrometry. The Bayer pattern is recorded in
    the FITS header instead so the gallery can debayer at *preview* time
    (see _fits_to_pil in main.py) while the stored pixels stay solver-ready.

    Note the mosaic is colour-filtered: adjacent pixels sample different
    bands, so a star's flux is split across the RGGB quad. Solvers handle
    this fine (star centroids survive), but the data is not photometric.
    """
    import io as _io

    import numpy as np

    try:
        import rawpy
    except ImportError as e:
        raise RuntimeError(
            "Canon RAW capture requires the `rawpy` package (LibRaw bindings) "
            "to decode CR2 files. Install with `pip install rawpy`. "
            "See backend/lib/README.md."
        ) from e

    with rawpy.imread(_io.BytesIO(raw_bytes)) as raw:
        # raw_image_visible crops off the masked/optical-black border that
        # raw_image includes, so the array matches the real imaging area.
        arr = np.asarray(raw.raw_image_visible, dtype=np.uint16).copy()

        # raw_pattern is a 2x2 index grid into raw.color_desc (e.g. b"RGBG").
        try:
            desc = raw.color_desc.decode("ascii", "replace")
            pattern = "".join(desc[i] for i in raw.raw_pattern.flatten())
        except Exception:
            pattern = ""

    return arr, pattern


class _GphotoBackend:
    """
    Linux/macOS backend: talks to the camera directly via libgphoto2 through
    the `gphoto2` Python bindings. See the module docstring for install steps.
    """

    def __init__(self) -> None:
        self._camera        = None
        self.connected        = False
        self._gain            = 100     # ISO
        self._exposure_ms     = 1000    # shutter time; matched to nearest supported speed
        self._camera_name     = ""
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

        # Force RAW (CR2) output — we need the un-demosaiced Bayer mosaic at
        # full bit depth for plate solving; see _capture_frame.
        fmt_widget = self._get_child(config, _FORMAT_NAMES)
        if fmt_widget is not None:
            try:
                choices = list(fmt_widget.get_choices())
                raw_choice = _pick_raw_choice(choices)
                if raw_choice is not None:
                    fmt_widget.set_value(raw_choice)
                    logger.info("Canon: image format set to %r", raw_choice)
                else:
                    logger.warning(
                        "Canon: no RAW image-format choice found (offered: %s) — "
                        "captures will not be solvable-quality raw data", choices,
                    )
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

    def _capture_frame(self):
        """
        Capture a RAW frame and return (arr, bayer_pattern) — a 2-D (H, W)
        uint16 Bayer mosaic and its pattern string. See
        _decode_raw_to_mono_bayer for why this stays un-demosaiced.
        """
        import gphoto2 as gp

        file_path = self._camera.capture(gp.GP_CAPTURE_IMAGE)
        cam_file = self._camera.file_get(file_path.folder, file_path.name, gp.GP_FILE_TYPE_NORMAL)
        raw_bytes = bytes(memoryview(cam_file.get_data_and_size()))
        logger.info("Canon (gphoto2): captured %s (%d bytes)", file_path.name, len(raw_bytes))

        # Free the card slot — we've already pulled the bytes over USB.
        try:
            self._camera.file_delete(file_path.folder, file_path.name)
        except Exception:
            logger.debug("Canon: could not delete frame from camera storage", exc_info=True)

        return _decode_raw_to_mono_bayer(raw_bytes)


# ---------------------------------------------------------------------------
# Windows backend: digiCamControl's local HTTP remote-control server
# ---------------------------------------------------------------------------

class _DigiCamControlBackend:
    """
    Windows backend: drives digiCamControl (http://digicamcontrol.com/), a
    free/open-source DSLR tethering app, over its built-in local HTTP
    remote-control server instead of talking to libgphoto2 directly (which
    has no first-class Windows build).

    digiCamControl must be installed and already running with the T3i
    connected and recognized (its own UI will show the camera) before
    connect() is called here — this class does not launch the app. The
    remote server defaults to http://127.0.0.1:5513 and is enabled via
    digiCamControl's Settings > Web Server tab ("Enable web server").

    Only the stdlib (urllib) is used for the HTTP calls, so no extra pip
    package is required on top of what's already in requirements.txt.
    """

    _DEFAULT_BASE_URL = "http://127.0.0.1:5513"
    _TIMEOUT_S = 10.0

    def __init__(self, base_url: str = "") -> None:
        self._base_url         = (
            base_url
            or os.getenv("ALTAIR_DIGICAMCONTROL_URL")
            or self._DEFAULT_BASE_URL
        )
        self.connected          = False
        self._gain              = 100
        self._exposure_ms       = 1000
        self._camera_name       = ""
        self._iso_choices: list[str]     = []
        self._shutter_choices: list[str] = []

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _request(self, params: dict) -> str:
        import urllib.error
        import urllib.parse
        import urllib.request

        url = f"{self._base_url}/?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self._TIMEOUT_S) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as e:
            raise RuntimeError(
                "Could not reach digiCamControl's remote-control server at "
                f"{self._base_url} ({e}). Check: (1) digiCamControl is "
                "running with the T3i connected and shown in its camera "
                "list; (2) Settings > Web Server > 'Enable web server' is "
                "checked; (3) the port matches (default 5513) — override "
                "with the ALTAIR_DIGICAMCONTROL_URL env var if you changed "
                "it. See backend/lib/README.md."
            ) from e

    def _get_property(self, name: str) -> str:
        return self._request({"CMD": "Get", "param": name}).strip()

    def _get_property_choices(self, name: str) -> list[str]:
        raw = self._request({"CMD": "List", "param": name}).strip()
        return [c for c in raw.splitlines() if c.strip()]

    def _set_property(self, name: str, value: str) -> None:
        self._request({"CMD": "Set", "param": name, "value": value})

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _do_connect(self) -> None:
        # "list" confirms the server is reachable AND that it sees a camera
        # (an empty response means the server is up but no camera is
        # attached/selected in digiCamControl's own UI).
        cameras = self._request({"CMD": "list"}).strip()
        if not cameras:
            raise RuntimeError(
                "digiCamControl's remote server is reachable at "
                f"{self._base_url} but reports no connected camera. Open "
                "digiCamControl itself and confirm the T3i appears in its "
                "camera list (USB mode must be PTP/'PC Connection', not "
                "Mass Storage) before retrying."
            )
        self._camera_name = cameras.splitlines()[0].strip() or "Canon EOS"

        try:
            self._iso_choices = self._get_property_choices("iso")
        except Exception:
            self._iso_choices = []
        try:
            self._shutter_choices = self._get_property_choices("shutterspeed")
        except Exception:
            self._shutter_choices = []

        # Force RAW (CR2) output, same rationale as the gphoto2 backend — we
        # need the un-demosaiced Bayer mosaic for plate solving.
        try:
            choices = self._get_property_choices("compressionsetting")
            raw_choice = _pick_raw_choice(choices) if choices else None
            if raw_choice is not None:
                self._set_property("compressionsetting", raw_choice)
                logger.info("Canon (digiCamControl): image format set to %r", raw_choice)
            else:
                # Older digiCamControl builds name the property differently;
                # "RAW" is accepted verbatim by both.
                self._set_property("compressionsetting", "RAW")
                logger.info("Canon (digiCamControl): image format set to 'RAW' (no choice list returned)")
        except Exception:
            logger.warning(
                "Canon (digiCamControl): could not force RAW image format — "
                "set it manually in digiCamControl, or captures will not be "
                "solvable-quality raw data", exc_info=True,
            )

        if self._iso_choices:
            pick = _nearest_choice(self._iso_choices, float(self._gain), _parse_iso)
            if pick is not None:
                self._set_property("iso", pick)
        if self._shutter_choices:
            pick = _nearest_choice(self._shutter_choices, self._exposure_ms / 1000.0, _parse_shutter_seconds)
            if pick is not None:
                self._set_property("shutterspeed", pick)

        self.connected = True
        logger.info("Canon camera connected via digiCamControl: %s", self._camera_name)

    def _do_disconnect(self) -> None:
        # digiCamControl owns the camera session; we just stop talking to it.
        self.connected = False
        logger.info("Canon camera (digiCamControl) disconnected")

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _do_set_gain(self, gain: int) -> None:
        """gain is treated as an ISO value, snapped to the nearest offered ISO."""
        choices = self._get_property_choices("iso") or self._iso_choices
        self._iso_choices = choices
        pick = _nearest_choice(choices, float(gain), _parse_iso)
        if pick is None:
            logger.warning("Canon (digiCamControl): no numeric ISO choice available near %d", gain)
            return
        if pick != str(gain):
            logger.info("Canon (digiCamControl): requested ISO %d -> nearest available %s", gain, pick)
        self._set_property("iso", pick)

    def _do_set_exposure(self, ms: int) -> None:
        """ms is matched to the nearest shutter speed the camera actually offers."""
        choices = self._get_property_choices("shutterspeed") or self._shutter_choices
        self._shutter_choices = choices
        target_s = ms / 1000.0
        pick = _nearest_choice(choices, target_s, _parse_shutter_seconds)
        if pick is None:
            logger.warning(
                "Canon (digiCamControl): no fixed shutter speed choice available near %d ms "
                "(bulb mode not implemented)", ms,
            )
            return
        picked_s = _parse_shutter_seconds(pick)
        if picked_s is not None and abs(picked_s - target_s) > 0.05 * max(target_s, picked_s):
            logger.info("Canon (digiCamControl): requested %d ms exposure -> nearest available %s (%.3fs)", ms, pick, picked_s)
        self._set_property("shutterspeed", pick)

    # ------------------------------------------------------------------
    # Sensor control readback
    # ------------------------------------------------------------------

    def _do_get_all_controls(self) -> dict:
        values: dict = {}
        for label, name in [
            ("Iso",           "iso"),
            ("Shutterspeed",  "shutterspeed"),
            ("Aperture",      "aperture"),
            ("Whitebalance",  "whitebalance"),
            ("Imageformat",   "imagequality"),
        ]:
            try:
                values[label] = self._get_property(name)
            except Exception:
                pass
        if self._camera_name:
            values["Model"] = self._camera_name
        return values

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _capture_frame(self):
        """
        Trigger a RAW capture and return (arr, bayer_pattern) — a 2-D (H, W)
        uint16 Bayer mosaic and its pattern string.

        Unlike the gphoto2 backend (which pulls bytes straight off the camera
        over USB), digiCamControl downloads the file to its own session folder
        and we read the CR2 from disk. Its HTTP interface exposes a decoded
        preview rather than the original raw bytes, which would defeat the
        whole point of shooting RAW — so the file path is the transport here.
        """
        import time as _time
        from pathlib import Path as _Path

        # Capture timeout has to cover the exposure itself plus the CR2
        # transfer over USB2 (~25MB, several seconds), not just the command.
        capture_timeout = max(self._TIMEOUT_S, self._exposure_ms / 1000.0 + 30.0)

        self._request({"CMD": "Capture"})

        # digiCamControl's capture command returns immediately; poll until it
        # reports idle, meaning the exposure finished AND the file finished
        # transferring to the session folder.
        deadline = _time.monotonic() + capture_timeout
        while _time.monotonic() < deadline:
            try:
                busy = self._request({"CMD": "IsBusy"}).strip().lower()
            except Exception:
                busy = ""
            if busy in ("false", "0", "no", ""):
                break
            _time.sleep(0.2)
        else:
            raise RuntimeError(
                f"digiCamControl still busy {capture_timeout:.0f}s after capture "
                "was triggered — the exposure or file transfer did not complete. "
                "Check the camera is still connected and awake."
            )

        last = self._request({"CMD": "LastCapturedImageFileName"}).strip()
        if not last:
            raise RuntimeError(
                "digiCamControl did not report a captured file name. Confirm a "
                "session folder is configured in digiCamControl and that the "
                "capture actually saved (check its own image list)."
            )

        path = _Path(last)
        if not path.exists():
            raise RuntimeError(
                f"digiCamControl reported captured file {last!r} but it is not "
                "readable from this process. If digiCamControl saves to a "
                "different machine or a protected folder, point its session "
                "folder somewhere this backend can read."
            )
        if path.suffix.lower() in (".jpg", ".jpeg"):
            raise RuntimeError(
                f"digiCamControl saved a JPEG ({path.name}), not a RAW/CR2 file. "
                "RAW capture is required for plate solving — set the camera's "
                "image quality to RAW in digiCamControl (Settings, or the "
                "camera body itself) and retry."
            )

        raw_bytes = path.read_bytes()
        logger.info("Canon (digiCamControl): captured %s (%d bytes)", path.name, len(raw_bytes))

        return _decode_raw_to_mono_bayer(raw_bytes)


# ---------------------------------------------------------------------------
# Public controller — picks a backend by platform, delegates everything
# ---------------------------------------------------------------------------

class CanonCameraController(CameraController):
    """
    Thread-safe async wrapper around a Canon EOS DSLR.

    Picks the appropriate backend by platform.system() at construction time:
    _GphotoBackend (Linux/macOS, via libgphoto2) or _DigiCamControlBackend
    (Windows, via digiCamControl's HTTP remote server). Both backends expose
    the same _do_connect/_do_disconnect/_do_set_gain/_do_set_exposure/
    _do_get_all_controls/_capture_frame surface, so this class only ever
    delegates — no platform branching happens anywhere else in the codebase.
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camera-canon")
        self._is_color  = True   # every Canon EOS body has a Bayer color sensor

        system = platform.system()
        if system == "Windows":
            self._backend = _DigiCamControlBackend()
        else:
            self._backend = _GphotoBackend()

        self.connected     = False
        self._gain          = self._backend._gain
        self._exposure_ms   = self._backend._exposure_ms
        self._camera_name   = ""

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _do_connect(self) -> None:
        self._backend._gain = self._gain
        self._backend._exposure_ms = self._exposure_ms
        self._backend._do_connect()
        self.connected = self._backend.connected
        self._camera_name = self._backend._camera_name
        # Base CameraController.capture() gates on self._camera is not None
        # (see camera.py) — the backend object itself is a fine sentinel
        # since it's truthy and never dereferenced as a real camera handle.
        self._camera = self._backend

    def _do_disconnect(self) -> None:
        self._backend._do_disconnect()
        self.connected = self._backend.connected
        self._camera = None

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _do_set_gain(self, gain: int) -> None:
        self._backend._gain = gain
        self._backend._do_set_gain(gain)

    def _do_set_exposure(self, ms: int) -> None:
        self._backend._exposure_ms = ms
        self._backend._do_set_exposure(ms)

    # ------------------------------------------------------------------
    # Sensor control readback
    # ------------------------------------------------------------------

    def _do_get_all_controls(self) -> dict:
        return self._backend._do_get_all_controls()

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _do_capture_and_tag(self, output_path: Path, metadata: dict) -> str:
        arr, bayer_pattern = self._backend._capture_frame()
        sensor_controls = self._backend._do_get_all_controls()
        if bayer_pattern:
            # Consumed by the gallery preview path (_fits_to_pil in main.py)
            # to debayer for display. The stored pixels stay un-demosaiced so
            # plate solvers get the real mosaic — see
            # _decode_raw_to_mono_bayer.
            sensor_controls["Bayer Pattern"] = bayer_pattern
        self._write_fits(output_path, arr, metadata, sensor_controls)
        return str(output_path)
