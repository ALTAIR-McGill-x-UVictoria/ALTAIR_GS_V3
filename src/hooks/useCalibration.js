import { apiFetch } from '../api'

/**
 * Calibration tab REST actions.
 *
 * Live sphere/PDRO telemetry (Lighting, PhotodiodeSignal packets) and camera
 * status already flow through useTelemetry()/useTelescope() — this hook only
 * wraps the calibration-specific endpoints backend/main.py exposes under
 * /api/calibration/*, following the same post() + actions-object shape as
 * useTelescope.js.
 *
 * Camera connect/disconnect/settings reuse the existing /api/telescope/camera/*
 * endpoints (same camera_controller instance backend-side) — call those via
 * useTelescope()'s actions rather than duplicating them here.
 */
export function useCalibration() {
  async function post(path, body = {}) {
    const res = await apiFetch(path, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    })
    return res.json()
  }

  const actions = {
    getStatus:       ()        => apiFetch('/api/calibration/status').then(r => r.json()),
    setBrightness:   (targetA) => post('/api/calibration/sphere/set_brightness', { target_a: targetA }),
    // label becomes the calibration_logs/<UTC timestamp>_<label>/ subdirectory
    // name (sanitized) that backend/main.py's post_calibration_capture creates
    // for capture.fits/.txt/.csv — sent as "filename" to match the request
    // body field the endpoint actually reads.
    captureCalibration: (label, marginPreS, marginPostS) => post('/api/calibration/capture', {
      filename: label,
      ...(marginPreS  !== undefined ? { margin_pre_s:  marginPreS }  : {}),
      ...(marginPostS !== undefined ? { margin_post_s: marginPostS } : {}),
    }),
  }

  return { actions }
}
