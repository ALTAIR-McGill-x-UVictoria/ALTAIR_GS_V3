# backend/lib/ — hardware binaries

This directory holds vendor-supplied shared libraries that can't be `pip
install`ed. They're git-ignored (`lib/*.dll` in `.gitignore`) — each machine
running real telescope/camera hardware needs to place its own copy here.

None of this is needed to run the ground station in emulator mode
(`ALTAIR_DEBUG=1`) — the mount and camera controllers are replaced by
`backend.mount.EmulatedMountController` / `backend.camera.EmulatedCameraController`
automatically, and the whole Telescope tab (compass rose, tracking, capture,
gallery) works without any of the downloads below.

## 1. ZWO ASI camera SDK (required for real camera capture)

`backend/camera.py` loads the ASI SDK shared library via the `zwoasi` Python
package (already in `requirements.txt`: `pip install zwoasi`) — this is the
**only** Python-facing camera API used in this codebase, on every platform,
so it stays identical on Windows and Linux. The SDK shared library itself
(`libASICamera2.so`/`.dll`/`.dylib`) is not on PyPI and needs to be obtained
separately, one of two ways:

**Option A — manual copy (all platforms, required on Windows/macOS):**
download it from ZWO's developer software page at **zwoastro.com** (look for
"ASI SDK" / "Developers" under Software Downloads) and drop the file
matching your OS into this directory:

| OS      | Expected filename in `backend/lib/` |
|---------|--------------------------------------|
| Windows | `ASICamera2.dll`                     |
| Linux   | `libASICamera2.so`                   |
| macOS   | `libASICamera2.dylib`                |

If you'd rather keep the SDK somewhere else, set:

```bash
export ALTAIR_ASI_LIB_PATH=/path/to/libASICamera2.so
```

**Option B — system-wide install via apt (Linux only):**
[seeing-things/zwo](https://github.com/seeing-things/zwo) publishes the SDK
as a PPA package, `libasicamera2`, which registers the library with
`ldconfig` instead of requiring a manual copy:

```bash
sudo bash -c 'echo "deb [trusted=yes] https://apt.fury.io/jgottula/ /" > /etc/apt/sources.list.d/jgottula.list'
sudo apt update
sudo apt install libasicamera2
```

**Known issue: this PPA build can be too old for newer camera models, and
installing it doesn't refresh the linker cache.** As tested against the
package (version `1.18-4`, library dated April 2021): `ASIGetNumOfConnectedCameras()`
silently returns 0 for an ASI585MC Pro that `lsusb`/full USB descriptor
enumeration can see fine — the SDK binary's embedded model table only goes
up to the ASI294/ASI462 generation, nothing from `libASICamera2.so.1.18`
recognizes the 585 series (or presumably any camera released after ~2021).
If `get_num_cameras()` returns 0 despite the camera showing up in `lsusb`,
this is almost certainly the cause — grab the current SDK from Option A
instead and drop it in this directory, which `backend/camera.py` prefers
over the system-wide copy automatically (see precedence order above).

Separately, on at least one system the package install didn't trigger an
`ldconfig` cache refresh (likely because it's packaged as `Architecture: all`
rather than a normal native shared-library package, so dpkg's usual
post-install trigger never fired) — `zwoasi.init(None)` failed until a
manual `sudo ldconfig` was run. If the SDK loads but nothing is found and
you haven't done this yet, try it before assuming a version mismatch.

**Note:** that repo *also* ships its own from-scratch Python bindings (a
`asi` package under `python/`, SWIG-generated, with a totally different
low-level API from `zwoasi` — free functions, explicit `CameraID`, tuple
returns). This project does **not** use that package — only the SDK/library
half of that repo. `backend/camera.py` doesn't require anything special when
the SDK is installed this way: if no manual copy is found in `backend/lib/`
and `ALTAIR_ASI_LIB_PATH` isn't set, it calls `zwoasi.init(None)`, which
falls back to `ctypes.util.find_library()` and finds the apt-installed copy
automatically.

On Linux you also need a udev rule so the camera is accessible without root
— ZWO ships one (usually `asi.rules`) in the official SDK zip, to be copied
to `/etc/udev/rules.d/` followed by `udevadm control --reload-rules`. Their
README doesn't document whether the `libasicamera2` PPA package installs
this rule for you; if the camera isn't detected after installing via apt,
check `/etc/udev/rules.d/` for an ASI/USB rule before assuming the SDK
itself is broken.

## 2. NexStar hand controller (required for real NexStar mount control)

No binary download needed — `backend/mount.py` talks to the hand controller
over a plain serial port via the `nexstar` PyPI package
(`pip install nexstar`, already in `requirements.txt`). You only need a
USB-to-serial driver for whatever cable you're using (many NexStar
hand-control cables use an FTDI or Prolific chipset — install that vendor's
driver if the port doesn't show up). Pick the COM/tty port from the dropdown
in the Telescope tab (same `/api/ports` listing used for the radio modem).

## 3. ZWO AM5 mount via ASCOM (required for real AM5 mount control — Windows only)

The AM5 controller (`backend/mount.py: AM5Controller`) goes through the
ASCOM platform via `pywin32`, which only exists on Windows
(`pip install pywin32`, already conditioned on `sys_platform == "win32"` in
`requirements.txt`). You additionally need, on the Windows host:

1. **ASCOM Platform** — from **ascom-standards.org**. This installs the
   COM infrastructure `win32com.client.Dispatch(...)` relies on.
2. **ZWO's ASCOM telescope driver for the AM5** — from **zwoastro.com**
   (Software Downloads → AM5 / ASCOM driver). This registers the
   `ASCOM.ZWO.Telescope` ProgID that `AM5Controller` connects to by default.
   Pass a different `progid` to `POST /api/telescope/mount/connect` if
   you're using a different ASCOM-compatible driver.

There is no Linux/macOS path for AM5 control today — ASCOM (as used here) is
Windows-only. If you need cross-platform AM5 control, that would mean
switching to ASCOM Alpaca (a network protocol ASCOM also supports) instead
of `win32com`, which is a separate implementation, not just a missing file.

## 4. ZWO AM3/AM5 mount via INDI (Linux-friendly alternative, no ASCOM)

`backend/mount.py: IndiMountController` drives the mount directly over the
INDI protocol instead of ASCOM, sidestepping the Windows-only requirement in
section 3. ZWO added an official INDI driver for the AM5/AM3 in libindi
1.9.4, exposed as the `indi_lx200am5` binary (LX200-command-set based),
included in the standard `indi-full`/`indi-bin` driver packages on
Debian/Ubuntu (`apt install indi-full`, or build libindi from source).

**Install:**
1. `apt install indi-full` (or equivalent) on the Linux box the mount's USB
   cable is plugged into — this is a system package, not a pip dependency.
2. Run `indiserver -v indi_lx200am5` on that box. It listens on **7624** by
   default.
3. `backend/indi_client.py` needs nothing beyond the Python standard
   library — deliberately **not** the official `pyindi-client` package,
   whose pre-generated SWIG wrapper is broken against any current libindi
   (`INDI::WidgetView` became a template class between libindi v1.9.3 and
   v1.9.9, and the wrapper was never regenerated — open upstream, no fix:
   [indilib/pyindi-client#63](https://github.com/indilib/pyindi-client/issues/63),
   [#44](https://github.com/indilib/pyindi-client/issues/44)). Talking to
   the documented XML wire protocol directly over a plain socket sidesteps
   that permanently and avoids a libindi-dev/swig/compiler dependency.

**Connecting:** in the Telescope tab, pick "AM3/AM5 (INDI)" as the mount
type and enter the INDI server's host — `localhost` if `indiserver` is
running on the same machine as the ground station backend, or a remote
IP:port (e.g. `192.168.1.50:7624`) if it's running elsewhere on the network.

**Caveat.** The AM5 INDI driver was written against the LX200 command set
before any INDI developer had real AM5/AM3 hardware to test against, and
community reports describe it as not fully mature (some LX200 guide/move
commands reportedly don't behave as expected). Validate goto, tracking, and
guide behavior against real hardware before relying on it in the field.
`backend/indi_client.py` logs every device discovered at INFO level on
connect, and every message at DEBUG — check that log first if a goto
misbehaves. You can also inspect the driver's property tree ahead of time
with:

```bash
indi_getprop -h <indiserver-host> -p 7624
```

If a property name doesn't match what `IndiMountController` expects, adjust
`EQUATORIAL_EOD_COORD`/`ON_COORD_SET` in `backend/mount.py`.

## 5. Canon EOS Rebel T3i (test alternative to the ZWO camera)

`backend/camera_canon.py` (`CanonCameraController`) is a test path for
using a Canon Rebel T3i/600D DSLR as the telescope sensor instead of the
ASI585MC, mainly to compare the T3i's much larger APS-C sensor against the
ASI585MC's small-format one. It picks one of two backends automatically by
`platform.system()` — the public interface and FITS output are identical
either way, so nothing else in the backend needs to know which one is active.

**Select it (either platform):** set `ALTAIR_CAMERA_TYPE=canon` before
starting the backend (default is `zwo`):

```bash
ALTAIR_CAMERA_TYPE=canon python -m backend.main
```

### Linux / macOS backend — libgphoto2

Talks to the camera over USB via [libgphoto2](http://www.gphoto.org/) —
the standard tethered-DSLR-control library — through the `gphoto2` PyPI
package (Python bindings around libgphoto2, **not** related to the
`zwoasi` package used for the ASI camera).

**Install:**

```bash
# Linux (Debian/Ubuntu)
sudo apt install libgphoto2-dev
pip install gphoto2

# macOS
brew install libgphoto2
pip install gphoto2
```

**Known Linux gotcha — gvfs auto-mount steals the USB connection.** Most
Linux desktops auto-mount a connected camera as a media device the moment
it's plugged in, which holds the USB connection libgphoto2 needs exclusive
access to. If `connect()` fails with a "could not claim the USB device" /
"Unknown model" style error, kill the auto-mount service first:

```bash
killall gvfsd-gphoto2 gvfs-gphoto2-volume-monitor
```

(or disable it persistently — search your desktop environment's docs for
"disable gvfs gphoto2 automount"). Also make sure the camera's own
Communication/USB setting is **PTP** ("PC Connection" on the T3i), not Mass
Storage — libgphoto2 needs PTP mode.

### Windows backend — digiCamControl

There is no first-class Windows build of libgphoto2, so on Windows
(`platform.system() == "Windows"`) this module instead drives
[digiCamControl](http://digicamcontrol.com/) — a free, open-source Windows
DSLR tethering application — over its built-in local HTTP remote-control
server, using only the Python standard library (`urllib`). No extra pip
package is required.

**Install:**

1. Download and install digiCamControl from
   [digicamcontrol.com](http://digicamcontrol.com/) (or its GitHub
   releases page).
2. Connect the T3i via USB. On the camera, set Communication/USB to
   **PTP** ("PC Connection"), not Mass Storage.
3. Launch digiCamControl and confirm the T3i appears in its camera list.
4. In digiCamControl: **Settings → Web Server → Enable web server**
   (default `http://127.0.0.1:5513`). Leave digiCamControl running in the
   background — this backend talks to that server, it does not launch or
   manage the digiCamControl process itself.

If your web server listens on a non-default host/port, override it with
`ALTAIR_DIGICAMCONTROL_URL` (e.g. `http://127.0.0.1:5513`) before starting
the backend.

**Troubleshooting:** if `connect()` reports it can't reach the server,
confirm digiCamControl is running and the web server is enabled. If it
reaches the server but reports no camera, check digiCamControl's own UI
for the camera — a USB mode other than PTP, a loose cable, or another app
(including a second digiCamControl instance) already holding the USB
connection are the usual causes.

### What's implemented / not (both backends)

**RAW capture.** Both backends force the camera into RAW (CR2) mode at
connect time and store the **un-demosaiced 16-bit Bayer mosaic** as a 2-D
FITS image. This is what plate solvers want: single-channel, full bit
depth, no interpolation. Decoding uses `rawpy` (`pip install rawpy`,
prebuilt wheels on all platforms).

The Bayer pattern is recorded in the FITS header as the standard
`BAYERPAT` card (plus a `HIERARCH ALTAIR SENSOR BAYER PATTERN` copy), so
Siril / ASTAP / PixInsight will debayer the file correctly on load. The
gallery debayers **for preview only** (2x2 binned to half resolution) —
the stored pixels are never modified, and `/api/gallery/rawpixels` still
serves the true mosaic.

Note the mosaic is colour-filtered, so a star's flux is split across the
RGGB quad. Solvers handle this fine, but the data is not photometric.

**Not implemented:** RAW+JPEG simultaneous capture (RAW only, since the
gallery regenerates previews from the FITS), and **bulb mode** — exposures
are capped at the camera's slowest fixed shutter speed, usually 30s.

ISO and shutter speed are snapped to the nearest value the camera actually
offers (`gain`/`exposure_ms` in the shared `CameraController` API map to
ISO / shutter speed respectively — see the module docstring in
`camera_canon.py`). Captured frames go through the same FITS-writing
pipeline as the ASI camera, so metadata headers work identically
regardless of camera or platform.

**digiCamControl caveat (Windows).** Because its HTTP interface serves a
decoded preview rather than the original raw bytes, the Windows backend
reads the CR2 **from digiCamControl's session folder on disk**. That
folder must be readable by the backend process. If digiCamControl is
configured to save JPEG, capture fails with an explicit error rather than
silently storing unsolvable data.

## Quick reference: what needs installing where

| Component        | pip package                  | External binary/driver                             | Platform |
|-------------------|-------------------------------|------------------------------------------------------|----------|
| ZWO camera        | `zwoasi`, `astropy`, `Pillow` | ASI SDK shared library (this dir, or `apt install libasicamera2` on Linux) | any      |
| Canon T3i (test)  | `gphoto2`                     | `libgphoto2-dev` (system package)                    | Linux/macOS |
| Canon T3i (test)  | none (stdlib `urllib`)        | [digiCamControl](http://digicamcontrol.com/) running with its web server enabled | Windows |
| NexStar mount     | `nexstar`                     | USB-serial driver for your cable, if needed          | any      |
| AM5 mount (ASCOM) | `pywin32`                     | ASCOM Platform + ZWO ASCOM driver                    | Windows  |
| AM3/AM5 mount (INDI) | none (stdlib `socket`)     | `indi-full`/`indi-bin` (`indi_lx200am5`) + `indiserver` | Linux    |
