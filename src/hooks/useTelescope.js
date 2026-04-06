import { useEffect, useRef, useState, useCallback } from 'react'

const WS_URL = 'ws://localhost:8000/ws/telescope'
const API    = 'http://localhost:8000'

/**
 * Telescope hook.
 *
 * Manages the WebSocket connection to /ws/telescope and exposes REST
 * helpers for mount / camera control.
 *
 * Returns:
 *   wsReady        — bool
 *   tracking       — latest { azimuth, elevation, distance_m, slant_m, gs_lat, gs_lon, gs_alt } | null
 *   mountStatus    — { connected, port, position } | null
 *   cameraStatus   — { connected, gain, exposure_ms } | null
 *   trackingEnabled — bool
 *   actions        — { connectMount, disconnectMount, gotoMount,
 *                      setTracking,
 *                      connectCamera, disconnectCamera,
 *                      setCameraSettings, captureFrame }
 */
export function useTelescope() {
  const ws = useRef(null)
  const reconnectTimer = useRef(null)

  const [wsReady,          setWsReady]          = useState(false)
  const [tracking,         setTracking]          = useState(null)
  const [mountStatus,      setMountStatus]       = useState(null)
  const [cameraStatus,     setCameraStatus]      = useState(null)
  const [trackingEnabled,  setTrackingEnabled]   = useState(false)

  const connect = useCallback(() => {
    if (ws.current) ws.current.close()
    const socket = new WebSocket(WS_URL)
    ws.current = socket

    socket.onopen  = () => setWsReady(true)
    socket.onclose = () => {
      setWsReady(false)
      clearTimeout(reconnectTimer.current)
      reconnectTimer.current = setTimeout(connect, 3000)
    }
    socket.onerror = () => socket.close()

    socket.onmessage = (evt) => {
      const msg = JSON.parse(evt.data)
      if (msg.type === 'tracking') {
        const { type, ...params } = msg
        setTracking(params)
      } else if (msg.type === 'telescope_status') {
        if (msg.mount   !== undefined) setMountStatus(msg.mount)
        if (msg.camera  !== undefined) setCameraStatus(msg.camera)
        if (msg.tracking_enabled !== undefined) setTrackingEnabled(msg.tracking_enabled)
      }
    }
  }, [])

  useEffect(() => {
    connect()
    return () => {
      clearTimeout(reconnectTimer.current)
      ws.current?.close()
    }
  }, [connect])

  // ------------------------------------------------------------------
  // REST helpers
  // ------------------------------------------------------------------

  const post = useCallback(async (path, body = {}) => {
    const res = await fetch(`${API}${path}`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    })
    return res.json()
  }, [])

  const actions = {
    connectMount:      (mountType, port, progid) => post('/api/telescope/mount/connect',
                         { mount_type: mountType, port: port || '', progid: progid || '' }),
    disconnectMount:   ()     => post('/api/telescope/mount/disconnect'),
    gotoMount:         (coords) => post('/api/telescope/mount/goto', coords),
    setTracking:       (enabled) => post('/api/telescope/tracking', { enabled }),
    connectCamera:     ()     => post('/api/telescope/camera/connect'),
    disconnectCamera:  ()     => post('/api/telescope/camera/disconnect'),
    setCameraSettings: (settings) => post('/api/telescope/camera/settings', settings),
    captureFrame:      (outputPath) => post('/api/telescope/camera/capture', { output_path: outputPath }),
  }

  return {
    wsReady,
    tracking,
    mountStatus,
    cameraStatus,
    trackingEnabled,
    actions,
  }
}
