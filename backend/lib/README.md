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
package (already in `requirements.txt`: `pip install zwoasi`). The library
itself is not on PyPI — download it from ZWO's developer software page at
**zwoastro.com** (look for "ASI SDK" / "Developers" under Software Downloads)
and drop the file matching your OS into this directory:

| OS      | Expected filename in `backend/lib/` |
|---------|--------------------------------------|
| Windows | `ASICamera2.dll`                     |
| Linux   | `libASICamera2.so`                   |
| macOS   | `libASICamera2.dylib`                |

`backend/camera.py` picks the right filename automatically based on
`platform.system()`. If you'd rather keep the SDK somewhere else, set:

```bash
export ALTAIR_ASI_LIB_PATH=/path/to/libASICamera2.so
```

On Linux you also need the udev rule ZWO ships in the SDK zip (usually
`asi.rules`) copied to `/etc/udev/rules.d/` and `udevadm control --reload-rules`
so the camera is accessible without root.

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

## Quick reference: what needs installing where

| Component        | pip package                    | External binary/driver                          | Platform |
|-------------------|--------------------------------|---------------------------------------------------|----------|
| ZWO camera        | `zwoasi`, `piexif`, `Pillow`   | ASI SDK shared library (this dir)                 | any      |
| NexStar mount     | `nexstar`                      | USB-serial driver for your cable, if needed       | any      |
| AM5 mount         | `pywin32`                      | ASCOM Platform + ZWO ASCOM driver                 | Windows  |
