import { useEffect, useRef, useState } from 'react'
import { apiFetch } from '../api'

const WS_URL = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/ws/telescope`

/**
 * Telescope hook.
 *
 * Manages the WebSocket connection to /api/ws/telescope and exposes REST
 * helpers for mount / camera control.
 *
 * Returns:
 *   wsReady         — bool
 *   tracking        — latest { azimuth, elevation, ra_hours, dec_deg, distance_m, slant_m, ... } | null
 *   mountStatus     — { connected, mount_type, port, position } | null
 *   cameraStatus    — { connected, camera_type, camera_name, gain, exposure_ms } | null
 *   trackingEnabled    — bool
 *   autoCaptureEnabled — bool
 *   actions         — { connectMount, disconnectMount, gotoMount,
 *                       setTracking,
 *                       connectCamera, disconnectCamera,
 *                       setCameraSettings, refreshCameraSettings, captureFrame, setAutoCapture,
 *                       solveFrame(filename, signal?) — pass an
 *                       AbortController's signal to allow cancelling the
 *                       wait; the astrometry.net job itself may still run
 *                       to completion server-side regardless.
 *                       getCaptureDir, setCaptureDir(dir) — read/set the
 *                       directory captures are saved to.
 *                       setGpsSource('mavlink'|'local') — pick which GPS
 *                       fix (Pixhawk-fused vs. onboard module) tracking
 *                       uses. }
 */
export function useTelescope() {
  const ws             = useRef(null)
  const reconnectTimer = useRef(null)

  const [wsReady,            setWsReady]            = useState(false)
  const [tracking,           setTracking]           = useState(null)
  const [mountStatus,        setMountStatus]        = useState(null)
  const [cameraStatus,       setCameraStatus]       = useState(null)
  const [trackingEnabled,    setTrackingEnabled]    = useState(false)
  const [autoCaptureEnabled, setAutoCaptureEnabled] = useState(false)
  const [gpsSource,          setGpsSourceState]     = useState('mavlink')

  // Stable ref — avoids useCallback dependency cycle
  const connectRef = useRef(null)
  connectRef.current = function connect() {
    if (ws.current) {
      ws.current.onclose = null
      ws.current.close()
    }

    const socket = new WebSocket(WS_URL)
    ws.current = socket

    socket.onopen = () => setWsReady(true)

    socket.onclose = () => {
      setWsReady(false)
      clearTimeout(reconnectTimer.current)
      reconnectTimer.current = setTimeout(() => connectRef.current(), 3000)
    }

    socket.onerror = () => socket.close()

    socket.onmessage = (evt) => {
      const msg = JSON.parse(evt.data)
      if (msg.type === 'tracking') {
        const { type, ...params } = msg
        setTracking(params)
      } else if (msg.type === 'telescope_status') {
        if (msg.mount               !== undefined) setMountStatus(msg.mount)
        if (msg.camera              !== undefined) setCameraStatus(msg.camera)
        if (msg.tracking_enabled    !== undefined) setTrackingEnabled(msg.tracking_enabled)
        if (msg.auto_capture_enabled !== undefined) setAutoCaptureEnabled(msg.auto_capture_enabled)
        if (msg.gps_source          !== undefined) setGpsSourceState(msg.gps_source)
      }
    }
  }

  useEffect(() => {
    connectRef.current()
    return () => {
      clearTimeout(reconnectTimer.current)
      if (ws.current) {
        ws.current.onclose = null
        ws.current.close()
      }
    }
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  // ------------------------------------------------------------------
  // REST helpers
  // ------------------------------------------------------------------

  async function post(path, body = {}, signal = undefined) {
    const res = await apiFetch(path, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
      signal,
    })
    return res.json()
  }

  const actions = {
    connectMount:      (mountType, port, progid) => post('/api/telescope/mount/connect',
                         { mount_type: mountType, port: port || '', progid: progid || '' }),
    disconnectMount:   ()          => post('/api/telescope/mount/disconnect'),
    gotoMount:         (coords)    => post('/api/telescope/mount/goto', coords),
    setTracking:       (enabled)   => post('/api/telescope/tracking', { enabled }),
    connectCamera:     (cameraType) => post('/api/telescope/camera/connect',
                         cameraType ? { camera_type: cameraType } : {}),
    disconnectCamera:  ()          => post('/api/telescope/camera/disconnect'),
    setCameraSettings: (settings)  => post('/api/telescope/camera/settings', settings),
    refreshCameraSettings: ()      => post('/api/telescope/camera/refresh_settings'),
    captureFrame:      (filename)   => post('/api/telescope/camera/capture', { filename }),
    setAutoCapture:    (enabled)   => post('/api/telescope/camera/auto_capture', { enabled }),
    solveFrame:        (filename, signal) => post('/api/telescope/solve', { filename }, signal),
    applySolveCorrection: (raHours, decDeg) => post('/api/telescope/solve/apply',
                         { ra_hours: raHours, dec_deg: decDeg }),
    getCaptureDir:     ()          => apiFetch('/api/gallery/config').then(r => r.json()),
    setCaptureDir:     (dir)       => post('/api/gallery/config', { capture_dir: dir }),
    setGpsSource:      (source)    => post('/api/telescope/gps_source', { source }),
  }

  return { wsReady, tracking, mountStatus, cameraStatus, trackingEnabled, autoCaptureEnabled, gpsSource, actions }
}
