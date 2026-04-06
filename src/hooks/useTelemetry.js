import { useEffect, useRef, useState } from 'react'

const WS_URL = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/ws`
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
  const ws             = useRef(null)
  const reconnectTimer = useRef(null)

  const [wsReady,   setWsReady]   = useState(false)
  const [status,    setStatus]    = useState({ connected: false, port: '' })
  const [packets,   setPackets]   = useState({})
  const [history,   setHistory]   = useState({})
  const lastSeen = useRef({})
  const [freshness, setFreshness] = useState({})

  // Stable ref so connect() can schedule itself without being a dependency
  const connectRef = useRef(null)
  connectRef.current = function connect() {
    if (ws.current) {
      ws.current.onclose = null  // prevent reconnect loop from old socket
      ws.current.close()
    }

    const socket = new WebSocket(WS_URL)
    ws.current = socket

    socket.onopen = () => setWsReady(true)

    socket.onclose = () => {
      setWsReady(false)
      clearTimeout(reconnectTimer.current)
      reconnectTimer.current = setTimeout(() => connectRef.current(), 2000)
    }

    socket.onerror = () => socket.close()

    socket.onmessage = (evt) => {
      const msg = JSON.parse(evt.data)

      if (msg.type === 'status') {
        setStatus({ connected: msg.connected, port: msg.port })
        return
      }

      if (msg.type === 'packet') {
        const { label, seq, timestamp, fields } = msg

        lastSeen.current[label] = Date.now()

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
  }

  // Mount once — no dependency array churn
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
