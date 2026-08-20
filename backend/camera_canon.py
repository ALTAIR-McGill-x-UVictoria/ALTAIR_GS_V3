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

                          Every fixed shutter speed the camera offers,
                          whole/decimal seconds included, is reliably
                          settable remotely — see _set_shutterspeed's
                          docstring on the Windows/digiCamControl backend
                          for a digiCamControl bug that used to make
                          whole/decimal-second values silently fail (worked
                          around there; not a camera or EDSDK limitation).

Captures are shot RAW (CR2) — image format is forced at connect time — and
stored as the *un-demosaiced* 16-bit Bayer mosaic: a 2-D (H, W) uint16
array handed to the same _write_fits() the ASI controller uses (with
self._bayer_pattern set first so the FITS gets a BAYERPAT header, matching
how camera.py's CameraController now always requests ASI_IMG_RAW16 for the
ASI camera — see that module's docstring for why undemosaiced is preferred
over a demosaiced-but-bit-depth-reduced image). This is what plate solvers
want (single channel, full bit depth, no interpolation); debayering to RGB
would interpolate away real detail and triple the size for no astrometric
benefit. Decoding needs `rawpy` (LibRaw bindings).

The Bayer pattern travels in the FITS header as the standard BAYERPAT card,
so external tools (Siril, ASTAP, PixInsight) debayer correctly on load, and
the gallery debayers for *preview* only — see _fits_to_pil in main.py. The
stored pixels are never modified.

Bulb mode is still not implemented, so exposures remain capped at the
camera's slowest fixed shutter speed (usually 30s).
"""
from __future__ import annotations

import asyncio
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


def _parse_fstop(choice: str) -> float | None:
    """Parse an f-stop choice like "f/8.0" or a bare "8.0" into a float."""
    c = choice.strip().lower().replace("ƒ", "f")  # normalize the "ƒ" glyph digiCamControl uses
    c = c.replace("f/", "").replace("f", "").strip()
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


def _build_camera_choices(iso_choices: list[str], shutter_choices: list[str], aperture_choices: list[str]) -> dict:
    """
    Build the {"iso": [...], "shutter": [...], "aperture": [...]} payload
    status_dict() sends the frontend for populating dropdowns, straight from
    each backend's own live-queried choice lists (already the camera's real,
    current offering — e.g. the aperture range varies with the attached lens,
    which also affects which shutter speeds sync with flash/etc.).

    Each shutter entry also carries "reliable": every fixed speed (anything
    _parse_shutter_seconds can parse) is remotely settable — see
    _set_shutterspeed's docstring for the digiCamControl bug this used to
    hit on whole/decimal-second values and how it's worked around. Only
    "Bulb" is flagged unreliable: it has no fixed duration and needs the
    separate timed-release machinery this module doesn't implement (see the
    module docstring). Aperture and ISO choices need no such flag — neither
    ever hit a format-dependent bug like the shutter one (aperture uses the
    same camera.fnumber workaround as a precaution, but every value tested
    applies correctly through it).
    """
    shutter = []
    for choice in shutter_choices:
        seconds = _parse_shutter_seconds(choice)
        shutter.append({
            "value":    choice,
            "seconds":  seconds,
            "reliable": seconds is not None,
        })
    return {
        "iso":      list(iso_choices),
        "shutter":  shutter,
        "aperture": list(aperture_choices),
    }


def _decode_raw_to_mono_bayer(raw_bytes: bytes) -> tuple["object", str, int]:
    """
    Decode Canon CR2 bytes to the un-demosaiced sensor mosaic.

    Returns (arr, bayer_pattern, bit_depth) where arr is a 2-D (H, W) uint16
    array of the raw Bayer data, bayer_pattern is e.g. "RGGB" describing its
    layout, and bit_depth is the sensor's native ADC depth (e.g. 14 on the
    T3i/600D) derived from rawpy's white_level, not the uint16 container size.

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

        bit_depth = int(raw.white_level).bit_length()

    return arr, pattern, bit_depth


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
        self._aperture         = ""      # e.g. "f/8"; "" until connect() populates it from the lens
        self._camera_name     = ""
        self._iso_choices: list[str]      = []
        self._shutter_choices: list[str]  = []
        self._aperture_choices: list[str] = []

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

        iso_widget      = self._get_child(config, _ISO_NAMES)
        shutter_widget  = self._get_child(config, _SHUTTER_NAMES)
        aperture_widget = self._get_child(config, _APERTURE_NAMES)
        self._iso_choices      = list(iso_widget.get_choices()) if iso_widget is not None else []
        self._shutter_choices  = list(shutter_widget.get_choices()) if shutter_widget is not None else []
        self._aperture_choices = list(aperture_widget.get_choices()) if aperture_widget is not None else []
        if aperture_widget is not None:
            try:
                self._aperture = aperture_widget.get_value()
            except Exception:
                self._aperture = ""

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
        # Persist what the camera actually ended up at — see the matching
        # comment on _DigiCamControlBackend._do_set_gain.
        picked_iso = _parse_iso(pick)
        if picked_iso is not None:
            self._gain = int(picked_iso)

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

        # Verify by reading the property back rather than trusting set_config()
        # to have taken — the Windows/digiCamControl backend was found to
        # have a real bug where its dedicated shutterspeed Set() handler
        # silently no-ops for whole/decimal-second values while reporting
        # success (worked around there via the camera.shutterspeed
        # reflection path — see _DigiCamControlBackend._set_shutterspeed).
        # Unverified whether libgphoto2 has any equivalent issue, but the
        # readback here is cheap insurance regardless.
        try:
            fresh_config = self._camera.get_config()
            fresh_widget = self._get_child(fresh_config, _SHUTTER_NAMES)
            actual = fresh_widget.get_value() if fresh_widget is not None else pick
        except Exception:
            actual = pick
        actual_s = _parse_shutter_seconds(actual)
        if actual_s is not None and picked_s is not None and abs(actual_s - picked_s) > 1e-6:
            logger.warning(
                "Canon: requested shutter speed %s but camera is still at %s "
                "after the write.", pick, actual,
            )
            self._exposure_ms = round(actual_s * 1000.0)
        elif picked_s is not None:
            self._exposure_ms = round(picked_s * 1000.0)

    def _do_set_aperture(self, value: str) -> None:
        """
        value is expected to be one of self._aperture_choices verbatim (the
        frontend dropdown only offers real choices) — no nearest-match
        snapping, unlike ISO/shutter.
        """
        config = self._camera.get_config()
        widget = self._get_child(config, _APERTURE_NAMES)
        if widget is None:
            logger.warning("Canon: camera has no aperture control")
            return
        choices = list(widget.get_choices())
        self._aperture_choices = choices
        if choices and value not in choices:
            logger.warning(
                "Canon: requested aperture %r is not in this lens's current "
                "choice list %r — attempting it anyway.", value, choices,
            )
        widget.set_value(value)
        self._camera.set_config(config)

        try:
            fresh_config = self._camera.get_config()
            fresh_widget = self._get_child(fresh_config, _APERTURE_NAMES)
            actual = fresh_widget.get_value() if fresh_widget is not None else value
        except Exception:
            actual = value
        requested_f = _parse_fstop(value)
        actual_f = _parse_fstop(actual)
        if requested_f is not None and actual_f is not None and abs(actual_f - requested_f) > 1e-6:
            logger.warning(
                "Canon: requested aperture %s but camera is still at %s "
                "after the write.", value, actual,
            )
        self._aperture = actual or value

    def _do_refresh_settings(self) -> None:
        """Pure read: re-query ISO/shutter/aperture current values and choice
        lists from the live camera. Never writes anything."""
        config = self._camera.get_config()

        iso_widget = self._get_child(config, _ISO_NAMES)
        if iso_widget is not None:
            try:
                self._iso_choices = list(iso_widget.get_choices())
                picked_iso = _parse_iso(iso_widget.get_value())
                if picked_iso is not None:
                    self._gain = int(picked_iso)
            except Exception:
                logger.warning("Canon: could not read current ISO", exc_info=True)

        shutter_widget = self._get_child(config, _SHUTTER_NAMES)
        if shutter_widget is not None:
            try:
                self._shutter_choices = list(shutter_widget.get_choices())
                picked_s = _parse_shutter_seconds(shutter_widget.get_value())
                if picked_s is not None:
                    self._exposure_ms = round(picked_s * 1000.0)
            except Exception:
                logger.warning("Canon: could not read current shutter speed", exc_info=True)

        aperture_widget = self._get_child(config, _APERTURE_NAMES)
        if aperture_widget is not None:
            try:
                self._aperture_choices = list(aperture_widget.get_choices())
                self._aperture = aperture_widget.get_value()
            except Exception:
                logger.warning("Canon: could not read current aperture", exc_info=True)

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
        Capture a RAW frame and return (arr, bayer_pattern, bit_depth,
        raw_bytes) — a 2-D (H, W) uint16 Bayer mosaic, its pattern string,
        the sensor's native ADC bit depth, and the original CR2 file bytes
        (kept so the caller can save the untouched RAW alongside the FITS;
        see _decode_raw_to_mono_bayer for why the decoded array stays
        un-demosaiced).
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

        arr, pattern, bit_depth = _decode_raw_to_mono_bayer(raw_bytes)
        return arr, pattern, bit_depth, raw_bytes


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

    Wire protocol, verified against a real T3i + digiCamControl 2.0.26 —
    NOT the `?CMD=<action>&PARAM=<value>` form some older third-party docs
    describe. That form is a fire-and-forget WPF window-command dispatch
    (WebServerModule.HandleRequest, line ~303 in dukus/digiCamControl's
    CameraControl.Core/Classes/WebServerModule.cs): it does trigger real
    actions (confirmed — it fires an actual capture) but the HTTP response
    body is always the SPA's index.html, never the value queried, so it is
    useless for Get/List/IsBusy-style calls. The read/write query API that
    actually returns plain text is a *different* query string:

        GET /?slc=list&param1=cameras            -> connected serials, one per line
        GET /?slc=get&param1=iso                  -> current value, e.g. "100"
        GET /?slc=list&param1=iso                 -> choices, one per line
        GET /?slc=set&param1=iso&param2=200       -> sets it, body "OK"
        GET /?slc=get&param1=lastcaptured          -> bare filename (not a path!)
        GET /?slc=capture                          -> blocks (device.WaitForCamera,
                                                       ~30s cap) until the exposure
                                                       and on-camera write finish;
                                                       there is no separate IsBusy
                                                       command in this dispatcher

    (dispatched by CameraControl.Core/Scripting/CommandLineProcessor.Pharse,
    switching on args[0] = slc into Get/List/Set/Capture — case-insensitive,
    property names lowercase.) `param1`/`param2` are always required when the
    sub-command needs them: omitting param1 null-refs server-side (caught
    and rendered as a page-not-command-body "Object reference not set..."
    message, distinguishable from a real "Unknow parameter ..." rejection).
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
        self._aperture           = ""   # e.g. "f/8.0"; "" until connect() populates it from the lens
        self._camera_name       = ""
        self._session_folder    = ""
        self._iso_choices: list[str]      = []
        self._shutter_choices: list[str]  = []
        self._aperture_choices: list[str] = []

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _request(self, params: dict, timeout: float | None = None) -> str:
        import urllib.error
        import urllib.parse
        import urllib.request

        url = f"{self._base_url}/?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=timeout or self._TIMEOUT_S) as resp:
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

    def _slc(self, cmd: str, param1: str = "", param2: str = "", timeout: float | None = None) -> str:
        """
        Call the `?slc=...` command/response API (see class docstring) and
        return its raw text body. Raises RuntimeError if digiCamControl
        rejected the command server-side (an "Unknow parameter ..." or
        "Wrong value ..." body — CommandLineProcessor catches its own
        exceptions and returns the message as 200 OK text rather than an
        HTTP error status, so this has to be checked explicitly).
        """
        params = {"slc": cmd}
        if param1:
            params["param1"] = param1
        if param2:
            params["param2"] = param2
        body = self._request(params, timeout=timeout).strip()
        if body.startswith("Unknow parameter") or body.startswith("Wrong value") or body.startswith("Object reference"):
            raise RuntimeError(f"digiCamControl rejected slc={cmd} param1={param1!r} param2={param2!r}: {body}")
        return body

    def _get_property(self, name: str) -> str:
        return self._slc("get", name)

    def _get_property_choices(self, name: str) -> list[str]:
        raw = self._slc("list", name)
        return [c for c in raw.splitlines() if c.strip()]

    def _set_property(self, name: str, value: str) -> None:
        self._slc("set", name, value)

    def _set_shutterspeed(self, value: str) -> None:
        """
        Set shutter speed via the `camera.shutterspeed` reflection-based
        property path, NOT the dedicated `shutterspeed` case in
        digiCamControl's Set() (CommandLineProcessor.cs) that
        _set_property("shutterspeed", ...) would otherwise use.

        CONFIRMED BUG, isolated on real hardware: the dedicated
        `shutterspeed` Set() case silently fails for every value tested
        that lacks a "/" (whole/decimal seconds: "1", "1.3", "2", "5", "10",
        ...) — it returns "OK" with no exception anywhere in the stack, but
        the camera doesn't move. Fractional values ("1/500", "1/60", ...)
        work fine through that same path. This isn't a camera/EDSDK
        limitation — going through the generic `camera.<property>` path
        instead (which reaches the same underlying ShutterSpeed property via
        a different code path — PropertyValue<T>.Value's setter via
        reflection, see CommandLineProcessor.Set's `camera.` branch) sets
        every value correctly, whole seconds included. Root cause in
        digiCamControl's own dedicated-property Set() branch not fully
        pinned down (something in how it validates/dispatches
        non-fractional strings before calling device.ShutterSpeed.SetValue),
        but the workaround is unconditional and reliable — see
        backend/lib/README.md.
        """
        self._slc("set", "camera.shutterspeed", value)

    def _set_aperture(self, value: str) -> None:
        """
        Set aperture (f-number) via `camera.fnumber`, the same
        reflection-based property path _set_shutterspeed uses — and for the
        same reason. The dedicated `aperture` case in digiCamControl's
        Set() has the identical bug: confirmed on real hardware that
        `slc=set&param1=aperture&param2=...` returns "OK" without changing
        the lens, while `slc=set&param1=camera.fnumber&param2=...` sets it
        correctly every time. `camera.fnumber` accepts either the exact
        "f/8.0"-style string from the choices list or a bare numeric string
        ("8.0") — either works, so callers don't need to add/strip the "f/"
        prefix. See _set_shutterspeed's docstring for the general pattern.
        """
        self._slc("set", "camera.fnumber", value)

    def _session_folder_path(self) -> str:
        """Read the active session's output folder from session.json (used to
        resolve the bare filename `get lastcaptured` returns into a real path)."""
        import json as _json
        import urllib.request

        url = f"{self._base_url}/session.json"
        with urllib.request.urlopen(url, timeout=self._TIMEOUT_S) as resp:
            data = _json.loads(resp.read().decode("utf-8", errors="replace"))
        return data.get("Folder", "")

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _do_connect(self) -> None:
        # "list cameras" confirms the server is reachable AND that it sees a
        # camera (an empty response means the server is up but no camera is
        # attached/selected in digiCamControl's own UI).
        try:
            cameras = self._get_property_choices("cameras")
        except RuntimeError as e:
            raise RuntimeError(
                "digiCamControl's remote server is reachable at "
                f"{self._base_url} but rejected the camera list query ({e}). "
                "Open digiCamControl itself and confirm the T3i appears in "
                "its camera list (USB mode must be PTP/'PC Connection', not "
                "Mass Storage) before retrying."
            ) from e
        if not cameras:
            raise RuntimeError(
                "digiCamControl's remote server is reachable at "
                f"{self._base_url} but reports no connected camera. Open "
                "digiCamControl itself and confirm the T3i appears in its "
                "camera list (USB mode must be PTP/'PC Connection', not "
                "Mass Storage) before retrying."
            )
        # The `slc=list&param1=cameras` command returns serial numbers, not a
        # human-readable model name — this dispatcher has no property for
        # that (session.json's own Name field is just "[Camera Name]",
        # a template placeholder, not the actual model). Good enough for a
        # status display; the serial still uniquely identifies the body.
        self._camera_name = f"Canon EOS (S/N {cameras[0].strip()})" if cameras[0].strip() else "Canon EOS"
        try:
            self._session_folder = self._session_folder_path()
        except Exception:
            logger.warning("Canon (digiCamControl): could not read session.json for the capture folder", exc_info=True)
            self._session_folder = ""

        try:
            self._iso_choices = self._get_property_choices("iso")
        except Exception:
            self._iso_choices = []
        try:
            self._shutter_choices = self._get_property_choices("shutterspeed")
        except Exception:
            self._shutter_choices = []
        try:
            self._aperture_choices = self._get_property_choices("aperture")
        except Exception:
            self._aperture_choices = []
        try:
            self._aperture = self._get_property("aperture")
        except Exception:
            self._aperture = ""

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
                self._set_shutterspeed(pick)

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
        # Persist what the camera actually ended up at, not the raw request —
        # CanonCameraController._do_set_gain reads this back into the
        # public-facing self._gain (see its comment) so status_dict() (and
        # therefore the frontend) reports the real applied ISO.
        picked_iso = _parse_iso(pick)
        if picked_iso is not None:
            self._gain = int(picked_iso)

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
        self._set_shutterspeed(pick)

        # Verify by reading the property back rather than trusting the "OK"
        # response as proof the write landed — cheap insurance in case a
        # future digiCamControl/firmware update reintroduces some variant of
        # the bug _set_shutterspeed works around (see its docstring).
        # Persist whatever the camera actually reports, not the request, so
        # status_dict() never lies about it either way.
        try:
            actual = self._get_property("shutterspeed")
        except RuntimeError:
            actual = pick  # readback failed; trust the write, nothing better to do
        actual_s = _parse_shutter_seconds(actual)
        if actual_s is not None and picked_s is not None and abs(actual_s - picked_s) > 1e-6:
            logger.warning(
                "Canon (digiCamControl): requested shutter speed %s but camera is "
                "still at %s after the write — see _set_shutterspeed's docstring "
                "for the known digiCamControl bug this works around; this warning "
                "firing means something unexpected happened beyond that.", pick, actual,
            )
            self._exposure_ms = round(actual_s * 1000.0)
        elif picked_s is not None:
            self._exposure_ms = round(picked_s * 1000.0)

    def _do_set_aperture(self, value: str) -> None:
        """
        value is expected to be one of self._aperture_choices verbatim (the
        frontend dropdown only offers real choices) — no nearest-match
        snapping like ISO/shutter get, since there's no numeric target to
        snap *to*, just a caller-picked exact choice.
        """
        choices = self._get_property_choices("aperture") or self._aperture_choices
        self._aperture_choices = choices
        if choices and value not in choices:
            logger.warning(
                "Canon (digiCamControl): requested aperture %r is not in this "
                "lens's current choice list %r — attempting it anyway.", value, choices,
            )
        self._set_aperture(value)

        # Verify by reading back — same rationale as _do_set_exposure's
        # readback (the dedicated `aperture` Set() case has the identical
        # digiCamControl bug _set_aperture works around; see its docstring).
        try:
            actual = self._get_property("aperture")
        except RuntimeError:
            actual = value
        requested_f = _parse_fstop(value)
        actual_f = _parse_fstop(actual)
        if requested_f is not None and actual_f is not None and abs(actual_f - requested_f) > 1e-6:
            logger.warning(
                "Canon (digiCamControl): requested aperture %s but camera is "
                "still at %s after the write.", value, actual,
            )
        self._aperture = actual or value

    def _do_refresh_settings(self) -> None:
        """Pure read: re-query ISO/shutter/aperture current values and choice
        lists from the live camera. Never writes anything."""
        try:
            self._iso_choices = self._get_property_choices("iso")
        except Exception:
            pass
        try:
            iso = self._get_property("iso")
            picked_iso = _parse_iso(iso)
            if picked_iso is not None:
                self._gain = int(picked_iso)
        except Exception:
            logger.warning("Canon (digiCamControl): could not read current ISO", exc_info=True)

        try:
            self._shutter_choices = self._get_property_choices("shutterspeed")
        except Exception:
            pass
        try:
            shutter = self._get_property("shutterspeed")
            picked_s = _parse_shutter_seconds(shutter)
            if picked_s is not None:
                self._exposure_ms = round(picked_s * 1000.0)
        except Exception:
            logger.warning("Canon (digiCamControl): could not read current shutter speed", exc_info=True)

        try:
            self._aperture_choices = self._get_property_choices("aperture")
        except Exception:
            pass
        try:
            self._aperture = self._get_property("aperture")
        except Exception:
            logger.warning("Canon (digiCamControl): could not read current aperture", exc_info=True)

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
            ("Imageformat",   "compressionsetting"),
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
        Trigger a RAW capture and return (arr, bayer_pattern, bit_depth,
        raw_bytes) — a 2-D (H, W) uint16 Bayer mosaic, its pattern string,
        the sensor's native ADC bit depth, and the original CR2 file bytes
        (kept so the caller can save the untouched RAW alongside the FITS).

        Unlike the gphoto2 backend (which pulls bytes straight off the camera
        over USB), digiCamControl downloads the file to its own session folder
        and we read the CR2 from disk. Its HTTP interface exposes a decoded
        preview rather than the original raw bytes, which would defeat the
        whole point of shooting RAW — so the file path is the transport here.

        Fires the capture via the `?CMD=Capture` window-command dispatch, NOT
        `?slc=capture` — verified against a real T3i that the latter throws a
        server-side NullReferenceException in CommandLineProcessor.Pharse
        (SelectedCameraDevice reads as null on that code path even with a
        camera actively connected and selected in the UI; never root-caused,
        may be a race between the HTTP worker thread and the WPF device
        selection). `?CMD=Capture` reliably fires a real capture (confirmed:
        DSC_NNNN.cr2 appears in the session folder each time) but is
        fire-and-forget with no completion signal of its own, so completion
        is detected by polling `slc=get&param1=lastcaptured` (the one part of
        the `slc` API that *is* reliable) until its filename changes from
        the pre-capture baseline — there is no IsBusy command in this
        dispatcher to poll instead.
        """
        import time as _time
        from pathlib import Path as _Path

        capture_timeout = max(self._TIMEOUT_S, self._exposure_ms / 1000.0 + 30.0)

        try:
            before = self._slc("get", "lastcaptured").strip()
        except RuntimeError:
            before = ""

        self._request({"CMD": "Capture"})

        # digiCamControl sets lastcaptured to the literal "-" the instant a
        # capture starts (CameraHelper.Capture: LastCapturedImage[cam] = "-")
        # and only replaces it with the real filename once the file has
        # actually landed — so "-" means "still in progress", not "done".
        last = before
        deadline = _time.monotonic() + capture_timeout
        while _time.monotonic() < deadline:
            _time.sleep(0.3)
            try:
                current = self._slc("get", "lastcaptured").strip()
            except RuntimeError:
                continue
            if current and current not in ("", "?", "-", before):
                last = current
                break
        else:
            raise RuntimeError(
                f"digiCamControl did not report a new captured file within "
                f"{capture_timeout:.0f}s of triggering capture. Check the "
                "camera is still connected and awake, and that its session "
                "folder is writable."
            )

        # `get lastcaptured` returns a bare filename, not a path (see class
        # docstring) — resolve it against the session's output folder.
        # Always re-read session.json rather than trusting self._session_folder
        # (only populated once, at connect time) — confirmed on real hardware
        # that changing digiCamControl's session folder in its own UI without
        # reconnecting through this backend left captures resolving against
        # the stale connect-time path and failing with a false "not readable
        # from this process" error even though the file existed at the new
        # location. This costs one extra HTTP round-trip per capture instead
        # of zero, which is negligible next to the capture itself.
        try:
            folder = self._session_folder_path()
        except Exception:
            folder = self._session_folder
        else:
            self._session_folder = folder
        path = _Path(folder) / last if folder else _Path(last)
        if not path.exists():
            raise RuntimeError(
                f"digiCamControl reported captured file {last!r} (resolved to "
                f"{path}) but it is not readable from this process. If "
                "digiCamControl saves to a different machine or a protected "
                "folder, point its session folder somewhere this backend can read."
            )
        if path.suffix.lower() in (".jpg", ".jpeg"):
            raise RuntimeError(
                f"digiCamControl saved a JPEG ({path.name}), not a RAW/CR2 file. "
                "RAW capture is required for plate solving — set the camera's "
                "image quality to RAW in digiCamControl (Settings, or the "
                "camera body itself) and retry."
            )

        # digiCamControl reporting the filename via lastcaptured doesn't
        # guarantee the file handle it (or, on a OneDrive-synced folder like
        # the default digiCamControl session path, OneDrive itself grabbing
        # the new file to start syncing it) is done with is actually closed
        # yet — confirmed on real hardware: a bare read_bytes() here can hit
        # PermissionError ([Errno 13]) on a file that exists and finishes
        # writing/unlocking a few hundred ms later. Retry briefly rather than
        # failing the whole capture over a race that resolves on its own.
        raw_bytes = None
        last_err: PermissionError | None = None
        for attempt in range(10):
            try:
                raw_bytes = path.read_bytes()
                break
            except PermissionError as e:
                last_err = e
                _time.sleep(0.3)
        if raw_bytes is None:
            raise RuntimeError(
                f"Captured file {path} still locked by another process "
                f"(likely digiCamControl or OneDrive syncing the session "
                f"folder) after retrying for 3s: {last_err}"
            )
        logger.info("Canon (digiCamControl): captured %s (%d bytes)", path.name, len(raw_bytes))

        arr, pattern, bit_depth = _decode_raw_to_mono_bayer(raw_bytes)
        return arr, pattern, bit_depth, raw_bytes


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

    camera_type = "canon"

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camera-canon")
        self._is_color  = True   # every Canon EOS body has a Bayer color sensor
        self._bayer_pattern: str | None = None   # set per-capture, see _do_capture_and_tag

        system = platform.system()
        if system == "Windows":
            self._backend = _DigiCamControlBackend()
        else:
            self._backend = _GphotoBackend()

        self.connected     = False
        self._gain          = self._backend._gain
        self._exposure_ms   = self._backend._exposure_ms
        self._aperture       = ""
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
        self._aperture = self._backend._aperture
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
        # _do_set_gain snaps to the nearest ISO the camera actually offers
        # and overwrites self._backend._gain with that real value (see the
        # backend implementations) — read it back so the base
        # CameraController.set_gain()/status_dict() (see camera.py) report
        # what the camera is really set to, not the raw requested gain.
        self._gain = self._backend._gain

    def _do_set_exposure(self, ms: int) -> None:
        self._backend._exposure_ms = ms
        self._backend._do_set_exposure(ms)
        self._exposure_ms = self._backend._exposure_ms

    async def set_aperture(self, value: str) -> None:
        """
        Set the lens aperture (f-number). Canon-only — ZWO's CameraController
        has no such method since an astronomy sensor has no lens iris to
        control, so callers (main.py) must check hasattr() before calling
        this rather than assuming every camera_controller has it.

        `value` should be one of the choices this camera reports in
        status_dict()["aperture"] (a "f/N.N"-style string, e.g. "f/8.0") —
        arbitrary values aren't snapped to nearest like gain/exposure_ms are,
        since the frontend dropdown only ever offers real choices in the
        first place.
        """
        self._aperture = value
        if self.connected:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(self._executor, self._do_set_aperture, value)

    def _do_set_aperture(self, value: str) -> None:
        self._backend._aperture = value
        self._backend._do_set_aperture(value)
        self._aperture = self._backend._aperture

    async def refresh_settings(self) -> None:
        """
        Re-query ISO/shutter/aperture (current values and choice lists) from
        the live camera and update cached state to match — a pure read, does
        NOT write anything to the camera (unlike connect(), which pushes our
        own remembered gain/exposure_ms onto it). Useful after changing a
        setting on the camera body's own controls directly, or any time the
        cached self._gain/_exposure_ms/_aperture might be stale — e.g. after
        the digiCamControl UI or another client changed something.
        """
        if not self.connected:
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._do_refresh_settings)

    def _do_refresh_settings(self) -> None:
        self._backend._do_refresh_settings()
        self._gain        = self._backend._gain
        self._exposure_ms = self._backend._exposure_ms
        self._aperture     = self._backend._aperture

    # ------------------------------------------------------------------
    # Sensor control readback
    # ------------------------------------------------------------------

    def _do_get_all_controls(self) -> dict:
        return self._backend._do_get_all_controls()

    def status_dict(self) -> dict:
        """
        Adds aperture (current f-number, ZWO has no such concept so this
        isn't in the base CameraController) plus iso/shutter/aperture_choices
        choice lists to the base fields, so the frontend can render
        dropdowns constrained to what this camera (with its currently
        attached lens) actually offers rather than free-typed numbers.
        "aperture" itself is the current value (mirrors "gain"/
        "exposure_ms"); the offered choices are under "aperture_choices" to
        avoid colliding with it. See _build_camera_choices.
        """
        base = super().status_dict()
        base["aperture"] = self._aperture
        choices = _build_camera_choices(
            self._backend._iso_choices, self._backend._shutter_choices, self._backend._aperture_choices)
        base["iso"]              = choices["iso"]
        base["shutter"]          = choices["shutter"]
        base["aperture_choices"] = choices["aperture"]
        return base

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _do_capture_and_tag(self, output_path: Path, metadata: dict) -> str:
        arr, bayer_pattern, bit_depth, raw_bytes = self._backend._capture_frame()
        sensor_controls = self._backend._do_get_all_controls()
        sensor_controls["Bit Depth"] = bit_depth
        # Consumed by CameraController._write_fits (camera.py) to tag the
        # FITS BAYERPAT header, and by the gallery preview path
        # (_fits_to_pil in main.py) to debayer for display. The stored
        # pixels stay un-demosaiced so plate solvers get the real mosaic —
        # see _decode_raw_to_mono_bayer.
        self._bayer_pattern = bayer_pattern
        self._write_fits(output_path, arr, metadata, sensor_controls)

        # Keep the camera's original CR2 too, next to the FITS — previously
        # decoded then discarded (file_delete'd off the card / left in
        # digiCamControl's session folder), losing the untouched raw file a
        # user might want for e.g. Canon's own DPP or Lightroom, distinct
        # from the FITS this app itself consumes for plate solving/gallery.
        raw_path = output_path.with_suffix(".cr2")
        raw_path.write_bytes(raw_bytes)
        logger.info("Canon: RAW written to %s", raw_path.name)

        return str(output_path)
