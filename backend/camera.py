"""
ZWO ASI camera controller.

Wraps the `zwoasi` library (which in turn wraps ASICamera2.dll) to
provide async-safe camera capture. All blocking calls run in a
ThreadPoolExecutor so they never stall the FastAPI event loop.

The DLL path is resolved relative to this file by default; supply
`dll_path` explicitly if the DLL is elsewhere.

Usage:
    cc = CameraController()
    await cc.connect()
    await cc.set_gain(150)
    await cc.set_exposure_ms(500)
    path = await cc.capture("output/frame_001.tif")
    await cc.disconnect()
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger("gs.camera")

# Default DLL location — bundled alongside the codebase
_DEFAULT_DLL = (
    Path(__file__).parent.parent.parent
    / "Ground-Station"
    / "GUI 2.1"
    / "views"
    / "panels"
    / "ZWO_Trigger"
    / "ZWO_ASI_LIB"
    / "lib"
    / "x64"
    / "ASICamera2.dll"
)


class CameraController:
    """Thread-safe async wrapper around a ZWO ASI camera."""

    def __init__(self, dll_path: str | Path | None = None) -> None:
        self._dll_path = Path(dll_path) if dll_path else _DEFAULT_DLL
        self._camera = None      # zwoasi.Camera instance
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camera")
        self.connected = False
        self._gain        = 150
        self._exposure_ms = 1000
        self._image_type  = None  # set after zwoasi import

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
        cam.set_control_value(zwoasi.ASI_GAIN, self._gain)
        cam.set_control_value(zwoasi.ASI_EXPOSURE, self._exposure_ms * 1000)  # µs
        cam.set_image_type(zwoasi.ASI_IMG_RAW16)
        self._image_type = zwoasi.ASI_IMG_RAW16
        self._camera = cam
        self.connected = True
        logger.info("ZWO camera connected")

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
        if not self.connected:
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._do_set_gain, gain)

    def _do_set_gain(self, gain: int) -> None:
        import zwoasi
        self._camera.set_control_value(zwoasi.ASI_GAIN, gain)

    async def set_exposure_ms(self, ms: int) -> None:
        self._exposure_ms = ms
        if not self.connected:
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._do_set_exposure, ms)

    def _do_set_exposure(self, ms: int) -> None:
        import zwoasi
        self._camera.set_control_value(zwoasi.ASI_EXPOSURE, ms * 1000)  # convert to µs

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    async def capture(self, output_path: str | Path) -> str:
        """
        Capture a single frame and save it to *output_path*.

        Returns the absolute path of the saved file as a string.
        """
        if not self.connected or self._camera is None:
            raise RuntimeError("Camera not connected")
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_event_loop()
        saved_path = await loop.run_in_executor(self._executor, self._do_capture, out)
        return saved_path

    def _do_capture(self, output_path: Path) -> str:
        filename = str(output_path)
        self._camera.capture(filename=filename)
        logger.info("ZWO: captured frame → %s", filename)
        return filename

    # ------------------------------------------------------------------
    # Status snapshot for frontend
    # ------------------------------------------------------------------

    def status_dict(self) -> dict:
        return {
            "connected":    self.connected,
            "gain":         self._gain,
            "exposure_ms":  self._exposure_ms,
        }
