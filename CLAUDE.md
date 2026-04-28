# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ALTAIR GS V3 is a full-stack ground station application for the ALTAIR high-altitude balloon project. It receives and decodes binary telemetry from the flight computer via an LR-900p radio modem, displays real-time data, evaluates alarms, logs sessions, and controls a telescope/camera system for tracking the balloon.

## Commands

### Backend (Python/FastAPI)
```bash
# Install dependencies
pip install -r requirements.txt

# Run the backend server (port 8000)
python -m backend.main

# Run with debug/emulator mode (no hardware required)
ALTAIR_DEBUG=1 python -m backend.main
```

### Frontend (React/Vite)
```bash
npm install
npm run dev      # Dev server on port 5173 (proxies /api and /ws to :8000)
npm run build    # Production build
npm run preview  # Preview production build
```

Run both backend and frontend concurrently during development. The Vite dev server proxies `/api` and `/ws` to `localhost:8000` (configured in [vite.config.js](vite.config.js)).

## Architecture

### Data Flow
```
Flight Computer → LR-900p modem (serial, 57600 baud)
  → SerialReader (backend/main.py)
  → Frame decoder (backend/packets.py)
  → AlarmEngine + CSV logger (backend/logging_manager.py)
  → WebSocket broadcast (/ws)
  → useTelemetry() hook (src/hooks/useTelemetry.js)
  → React UI (App.jsx + tab components)
```

Control commands flow the opposite direction: frontend REST calls → `backend/main.py` → serial write.

### Backend (`backend/`)

**[main.py](backend/main.py)** (~1284 lines) — The FastAPI application. Manages:
- `SerialReader` async task: scans for LR-900p modem (CP210x USB VID/PID), reads binary frames, broadcasts JSON to WebSocket clients at `/ws`
- REST endpoints for serial port management, flight computer commands, telescope control, and camera gallery
- A separate WebSocket at `/api/ws/telescope` for telescope status streaming
- Lifespan startup: opens log session, starts GS GPS reader, auto-detects modem

**[packets.py](backend/packets.py)** — Binary frame decoder. The wire format is:
`[SYNC:0xAA][ID:1B][SEQ:1B][TS:8B float64 LE][LEN:2B uint16 LE][DATA:N][CRC16:2B]`
Imports the flight computer's telemetry registry from `../Altairfc_V2/altairfc/telemetry/` at runtime via `importlib`. This external path must exist for packet decoding to work.

**[logging_manager.py](backend/logging_manager.py)** — `TelemetryLogger` writes per-packet-type CSV files under `logs/YYYY-MM-DD_HHMMSS/`. `AlarmEngine` evaluates three rule types against incoming field values:
- `threshold`: min/max limits with pre-warning margins
- `rate_of_change`: detects sudden anomalies (Δvalue/Δt)
- `state`: enumerated bands (e.g., VESC operational vs. error)

**[alarms.py](backend/alarms.py)** — Definitions of 50+ alarm rules across subsystems (Power, VESC, GPS, Attitude, etc.). Edit this file to add/modify alarm thresholds.

**[events.py](backend/events.py)** — 9-stage flight mission enum (Pre-flight → Recovery) and boolean event flags (arm, launch, ascent, termination, etc.). Events emit over WebSocket when state transitions occur.

**[tracking.py](backend/tracking.py)** — Haversine distance/bearing, elevation angle, and Az/El ↔ RA/Dec coordinate conversion (IAU 1982 GMST). Maintains global `GS_LAT/LON/ALT` updated by the u-blox GPS reader. Default GS position is Montreal area (45.5088°N, -73.5542°E).

**[mount.py](backend/mount.py)** — Abstract `BaseMountController` with two implementations: `NexStarController` (Celestron Alt/Az serial protocol) and `AM5Controller` (ZWO AM5 RA/Dec via ASCOM/pywin32).

**[camera.py](backend/camera.py)** — ZWO ASI camera wrapper. Captures TIFF images, converts to JPEG, and injects EXIF metadata (GPS coordinates, pointing angles, camera settings). Images saved to `captures/`.

**[gps_reader.py](backend/gps_reader.py)** — Reads NMEA sentences from u-blox 7 USB GPS dongle (VID `0x1546`, PID `0x01A7`). Updates `tracking.GS_LAT/LON/ALT` at runtime.

**[emulator.py](backend/emulator.py)** — Synthetic packet generator for development without hardware. Activated by `ALTAIR_DEBUG=1`. Generates sinusoidal signals, sweeps alarm thresholds, and traces a GPS circle around the GS position.

**[commands.py](backend/commands.py)** — Builds binary command frames (same wire format as telemetry, using the FC command registry) for sending to the flight computer.

### Frontend (`src/`)

**[App.jsx](src/App.jsx)** — Top-level shell. Manages 5 tabs (Dashboard, Telemetry, Graphs, Map, Telescope), renders the alarm sidebar and event log. Calls `useTelemetry()`, `useTelescope()`, and `useSerial()` hooks.

**Hooks (`src/hooks/`):**
- `useTelemetry.js` — Central WebSocket connection to `/ws`. Maintains packet state, 200-point rolling history per field, packet freshness (ok/stale at 2s/lost at 5s), alarm list, event log, and GS GPS state.
- `useTelescope.js` — WebSocket connection to `/api/ws/telescope`. Manages mount/camera status and tracking data.
- `useSerial.js` — REST wrapper for port listing, connect/disconnect, and command sending.

**Components (`src/components/`):**
- `PacketPanel.jsx` — Telemetry card with threshold gauge, sparkline (Recharts), and alarm indicators per field.
- `DashboardView.jsx` — Visual flight instruments (artificial horizon, compass rose, altitude tape, speed gauges, power arcs).
- `GraphsView.jsx` — Recharts time-series plots with zoom/pan.
- `MapView.jsx` — Leaflet map showing GS marker, payload position, and telescope pointing arrow.
- `TelescopeView.jsx` — Mount slew/park controls, camera capture, tracking enable/disable, and image gallery.
- `AlarmSidebar.jsx` — Real-time alarm list with dismiss, and flight event log.
- `ConnectionBar.jsx` — Port selector, connect/disconnect, hardware status indicators (GPS, Mount, Camera).

### WebSocket Message Types

All messages from `/ws` are JSON with a `type` field:
- `"packet"` — Telemetry data with `packet_id`, `label`, `seq`, `timestamp`, `fields[]`
- `"alarm"` — Alarm trigger with `field`, `severity`, `rule_type`, `value`, `message`
- `"event"` — Flight state transition with `field`, `old_val`, `new_val`, `message`, `stage`
- `"tracking"` — Telescope pointing data with `azimuth`, `elevation`, `ra_hours`, `dec_deg`, `distance_m`
- `"gs_gps"` — Ground station GPS fix with `lat`, `lon`, `alt`, `fix_quality`, `hdop`

### External Dependency: Flight Computer Registry

`backend/packets.py` imports telemetry and command struct definitions from `../Altairfc_V2/altairfc/telemetry/` relative to the repo root. This sibling repository must be checked out for packet decoding and command sending to work. Without it, the backend falls back gracefully but cannot decode any packets.
