# lib/

Runtime binaries required by the ground station.

## ASICamera2.dll

ZWO ASI camera SDK DLL (x64). Required by the `zwoasi` Python package.

**How to get it:**
1. Download the ZWO ASI SDK from https://astronomy-imaging-camera.com/software-drivers
2. Extract `ASICamera2.dll` from `lib/x64/`
3. Place it in this directory: `ALTAIR_GS_V3/lib/ASICamera2.dll`

The file is not committed to the repository because it is a binary
redistributable. Each developer must obtain it from ZWO directly.
