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
    captureCalibration: (filename, marginPreS, marginPostS) => post('/api/calibration/capture', {
      filename,
      ...(marginPreS  !== undefined ? { margin_pre_s:  marginPreS }  : {}),
      ...(marginPostS !== undefined ? { margin_post_s: marginPostS } : {}),
    }),
  }

  return { actions }
}
