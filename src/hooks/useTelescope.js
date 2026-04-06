import { useEffect, useRef, useState } from 'react'

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
 *   cameraStatus    — { connected, gain, exposure_ms } | null
 *   trackingEnabled — bool
 *   actions         — { connectMount, disconnectMount, gotoMount,
 *                       setTracking,
 *                       connectCamera, disconnectCamera,
 *                       setCameraSettings, captureFrame }
 */
export function useTelescope() {
  const ws             = useRef(null)
  const reconnectTimer = useRef(null)

  const [wsReady,         setWsReady]         = useState(false)
  const [tracking,        setTracking]         = useState(null)
  const [mountStatus,     setMountStatus]      = useState(null)
  const [cameraStatus,    setCameraStatus]     = useState(null)
  const [trackingEnabled, setTrackingEnabled]  = useState(false)

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
        if (msg.mount            !== undefined) setMountStatus(msg.mount)
        if (msg.camera           !== undefined) setCameraStatus(msg.camera)
        if (msg.tracking_enabled !== undefined) setTrackingEnabled(msg.tracking_enabled)
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

  async function post(path, body = {}) {
    const res = await fetch(path, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    })
    return res.json()
  }

  const actions = {
    connectMount:      (mountType, port, progid) => post('/api/telescope/mount/connect',
                         { mount_type: mountType, port: port || '', progid: progid || '' }),
    disconnectMount:   ()          => post('/api/telescope/mount/disconnect'),
    gotoMount:         (coords)    => post('/api/telescope/mount/goto', coords),
    setTracking:       (enabled)   => post('/api/telescope/tracking', { enabled }),
    connectCamera:     ()          => post('/api/telescope/camera/connect'),
    disconnectCamera:  ()          => post('/api/telescope/camera/disconnect'),
    setCameraSettings: (settings)  => post('/api/telescope/camera/settings', settings),
    captureFrame:      (filename)   => post('/api/telescope/camera/capture', { filename }),
  }

  return { wsReady, tracking, mountStatus, cameraStatus, trackingEnabled, actions }
}
