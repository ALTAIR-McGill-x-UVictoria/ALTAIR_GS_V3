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
  const ws             = useRef(null)
  const reconnectTimer = useRef(null)

  const [wsReady,     setWsReady]     = useState(false)
  const [status,      setStatus]      = useState({ connected: false, port: '' })
  const [packets,     setPackets]     = useState({})
  const [history,     setHistory]     = useState({})
  const [alarms,      setAlarms]      = useState([])
  const [alarmRules,  setAlarmRules]  = useState([])
  const [events,      setEvents]      = useState([])   // [{wall_ms, field, message, new_val, stage}, ...]
  const [stageNames,  setStageNames]  = useState({})   // {0: "Pre-flight", ...}
  const [lastAck,     setLastAck]     = useState(null) // {cmd_id, cmd_seq, status, wall_ms}
  const [gsGps,       setGsGps]       = useState(null) // {lat, lon, alt, utc_unix, sats, hdop, fix_quality}
  const [gsGpsStatus, setGsGpsStatus] = useState({ connected: false, has_fix: false, port: '' })
  const lastSeen     = useRef({})
  const arrivalTimes = useRef({})
  const [freshness, setFreshness] = useState({})

  const RATE_WINDOW = 10   // use last N arrivals to compute Hz

  // Stable ref so connect() can schedule itself without being a dependency
  const connectRef = useRef(null)
  connectRef.current = function connect() {
    if (ws.current) {
      ws.current.onclose = null  // prevent reconnect loop from old socket
      ws.current.close()
    }

    const socket = new WebSocket(WS_URL)
    ws.current = socket

    socket.onopen  = () => setWsReady(true)
    socket.onclose = () => { setWsReady(false); scheduleReconnect() }
    socket.onerror = () => socket.close()

    socket.onmessage = (evt) => {
      const msg = JSON.parse(evt.data)

      if (msg.type === 'registry') {
        // Pre-populate panels with null so placeholders appear before first packet
        setPackets(prev => {
          const next = { ...prev }
          for (const label of msg.labels) {
            if (!(label in next)) next[label] = null
          }
          return next
        })
        return
      }

      if (msg.type === 'alarm_rules') {
        setAlarmRules(msg.rules)
        return
      }

      if (msg.type === 'alarm') {
        setAlarms(prev => {
          // Keep newest 50 alarms; replace existing entry for same label+field if severity changed
          const entry = {
            severity:  msg.severity,
            label:     msg.label,
            field:     msg.field,
            value:     msg.value,
            message:   msg.message,
            rule_type: msg.rule_type,
            timestamp: msg.timestamp,
            wall_ms:   Date.now(),
          }
          const filtered = prev.filter(a => !(a.label === msg.label && a.field === msg.field))
          const next = [entry, ...filtered]
          return next.length > 50 ? next.slice(0, 50) : next
        })
        return
      }

      if (msg.type === 'status') {
        setStatus({ connected: msg.connected, port: msg.port, emulating: msg.emulating ?? false })
        return
      }

      if (msg.type === 'event_meta') {
        setStageNames(msg.stage_names ?? {})
        return
      }

      if (msg.type === 'event') {
        setEvents(prev => {
          const entry = {
            wall_ms: msg.wall_time * 1000,
            field:   msg.field,
            new_val: msg.new_val,
            old_val: msg.old_val,
            message: msg.message,
            stage:   msg.stage,
          }
          const next = [entry, ...prev]
          return next.length > 200 ? next.slice(0, 200) : next
        })
        return
      }

      if (msg.type === 'ack') {
        setLastAck({ cmd_id: msg.cmd_id, cmd_seq: msg.cmd_seq, status: msg.status, wall_ms: Date.now() })
        return
      }

      if (msg.type === 'gs_gps_status') {
        setGsGpsStatus({ connected: msg.connected, has_fix: msg.has_fix, port: msg.port })
        return
      }

      if (msg.type === 'gs_gps') {
        setGsGps({
          lat:         msg.lat,
          lon:         msg.lon,
          alt:         msg.alt,
          utc_unix:    msg.utc_unix,
          sats:        msg.sats,
          hdop:        msg.hdop,
          fix_quality: msg.fix_quality,
          wall_ms:     Date.now(),
        })
        return
      }

      if (msg.type === 'snapshot_ready') {
        fetch('/api/state/snapshot')
          .then(r => r.json())
          .then(snap => {
            // Pre-populate packets only for labels not yet seen live
            if (snap.packets && Object.keys(snap.packets).length) {
              setPackets(prev => {
                const next = { ...prev }
                for (const [label, pkt] of Object.entries(snap.packets)) {
                  if (!next[label]) next[label] = { ...pkt, _restored: true }
                }
                return next
              })
            }
            // Pre-populate alarms (overwrite — server list is authoritative)
            if (snap.alarms?.length) setAlarms(snap.alarms)
            // Pre-populate events (overwrite — server list is authoritative)
            if (snap.events?.length) {
              setEvents(snap.events.map(ev => ({
                wall_ms: ev.wall_time * 1000,
                field:   ev.field,
                new_val: ev.new_val,
                old_val: ev.old_val,
                message: ev.message,
                stage:   ev.stage,
              })))
            }
          })
          .catch(() => {})
        return
      }

      if (msg.type === 'packet') {
        const { label, seq, timestamp, fields } = msg

        const now = Date.now()
        lastSeen.current[label] = now

        // Rolling arrival window → update Hz
        const buf = arrivalTimes.current[label] ?? []
        buf.push(now)
        if (buf.length > RATE_WINDOW) buf.shift()
        arrivalTimes.current[label] = buf

        let hz = null
        if (buf.length >= 2) {
          const span = buf[buf.length - 1] - buf[0]   // ms across (N-1) intervals
          hz = Math.round(((buf.length - 1) / span) * 1000 * 10) / 10  // 1 decimal
        }

        setPackets(prev => ({
          ...prev,
          [label]: { fields, seq, timestamp, wall_ms: msg.wall_ms ?? null, dropped: msg.dropped ?? 0, hz },
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

  function scheduleReconnect() {
    clearTimeout(reconnectTimer.current)
    reconnectTimer.current = setTimeout(() => connectRef.current(), 2000)
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
  }, [])

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

  return { status, packets, history, freshness, wsReady, alarms, alarmRules, events, stageNames, lastAck, gsGps, gsGpsStatus }
}
