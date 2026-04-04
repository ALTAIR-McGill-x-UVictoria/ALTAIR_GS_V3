import { useEffect, useRef, useState, useCallback } from 'react'

const WS_URL = 'ws://localhost:5173/ws'
const HISTORY_LEN = 200   // data points kept per field for sparklines

// Staleness thresholds in milliseconds
const STALE_WARN_MS = 2000   // yellow  — no packet for 2 s
const STALE_LOST_MS = 5000   // red     — no packet for 5 s

/**
 * Central telemetry hook.
 *
 * Returns:
 *   status     — { connected: bool, port: string }
 *   packets    — { [label]: { fields: [{name, label, unit, value}], seq, timestamp } }
 *   history    — { [label]: { [fieldName]: [{ t, v }, ...] } }
 *   freshness  — { [label]: 'ok' | 'stale' | 'lost' | 'waiting' }
 *   wsReady    — bool
 */
export function useTelemetry() {
  const ws = useRef(null)
  const [wsReady,    setWsReady]    = useState(false)
  const [status,     setStatus]     = useState({ connected: false, port: '' })
  const [packets,    setPackets]    = useState({})
  const [history,    setHistory]    = useState({})
  // lastSeen: { [label]: Date.now() } — updated on every packet received
  const lastSeen = useRef({})
  const [freshness,  setFreshness]  = useState({})

  const connect = useCallback(() => {
    if (ws.current) ws.current.close()

    const socket = new WebSocket(WS_URL)
    ws.current = socket

    socket.onopen  = () => setWsReady(true)
    socket.onclose = () => { setWsReady(false); scheduleReconnect() }
    socket.onerror = () => socket.close()

    socket.onmessage = (evt) => {
      const msg = JSON.parse(evt.data)

      if (msg.type === 'status') {
        setStatus({ connected: msg.connected, port: msg.port })
        return
      }

      if (msg.type === 'packet') {
        const { label, seq, timestamp, fields } = msg

        // Record wall-clock time of receipt for freshness tracking
        lastSeen.current = { ...lastSeen.current, [label]: Date.now() }

        setPackets(prev => ({
          ...prev,
          [label]: { fields, seq, timestamp, dropped: msg.dropped ?? 0 },
        }))

        setHistory(prev => {
          const labelHist = prev[label] ?? {}
          const updated = { ...labelHist }
          for (const f of fields) {
            const existing = updated[f.name] ?? []
            const next = [...existing, { t: timestamp, v: f.value }]
            updated[f.name] = next.length > HISTORY_LEN
              ? next.slice(next.length - HISTORY_LEN)
              : next
          }
          return { ...prev, [label]: updated }
        })
      }
    }
  }, [])

  const reconnectTimer = useRef(null)
  const scheduleReconnect = useCallback(() => {
    clearTimeout(reconnectTimer.current)
    reconnectTimer.current = setTimeout(connect, 2000)
  }, [connect])

  useEffect(() => {
    connect()
    return () => {
      clearTimeout(reconnectTimer.current)
      ws.current?.close()
    }
  }, [connect])

  // Poll freshness every 500 ms
  useEffect(() => {
    const id = setInterval(() => {
      const now = Date.now()
      const next = {}
      for (const [label, ts] of Object.entries(lastSeen.current)) {
        const age = now - ts
        if      (age > STALE_LOST_MS) next[label] = 'lost'
        else if (age > STALE_WARN_MS) next[label] = 'stale'
        else                          next[label] = 'ok'
      }
      setFreshness(next)
    }, 500)
    return () => clearInterval(id)
  }, [])

  return { status, packets, history, freshness, wsReady }
}
