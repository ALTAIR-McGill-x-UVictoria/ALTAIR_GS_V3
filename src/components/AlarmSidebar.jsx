import { useState, useEffect, useRef, useMemo } from 'react'

// ─── Constants ────────────────────────────────────────────────────────────────

const ACCENT      = '#00d4ff'
const CMD_ARM       = 0xC0
const CMD_LAUNCH_OK = 0xC1
const CMD_PING      = 0xC2
const CMD_LABELS    = { [CMD_ARM]: 'ARM', [CMD_LAUNCH_OK]: 'LAUNCH OK', [CMD_PING]: 'PING' }
const CMD_LOG_MAX   = 20

// ─── Event log helpers ────────────────────────────────────────────────────────


function formatEventTime(wall_ms) {
  const d = new Date(wall_ms)
  const h = String(d.getHours()).padStart(2, '0')
  const m = String(d.getMinutes()).padStart(2, '0')
  const s = String(d.getSeconds()).padStart(2, '0')
  return `${h}:${m}:${s}`
}

function FlightStageStrip({ stage, stageNames }) {
  const stages = Object.entries(stageNames).map(([k, v]) => ({ id: Number(k), name: v }))
  if (stages.length === 0) return null
  return (
    <div style={{ display:'flex', flexWrap:'wrap', gap:2, padding:'4px 6px' }}>
      {stages.map(({ id, name }) => {
        const active = id === stage
        const past   = id < stage
        return (
          <div key={id} style={{
            fontSize:     8,
            fontFamily:   'var(--font-mono)',
            padding:      '1px 5px',
            borderRadius: 3,
            letterSpacing: 0.5,
            background: active ? ACCENT : past ? 'rgba(0,212,255,0.1)' : 'transparent',
            color:      active ? '#000' : past ? ACCENT : '#607080',
            border:     active ? `1px solid ${ACCENT}`
                      : past   ? '1px solid rgba(0,212,255,0.25)'
                               : '1px solid #1e2d3d',
          }}>
            {name}
          </div>
        )
      })}
    </div>
  )
}

function EventLog({ events, stageNames }) {
  const bottomRef = useRef(null)
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [events.length])

  return (
    <div style={{ flex:1, minHeight:0, display:'flex', flexDirection:'column', overflow:'hidden' }}>
      {events.length === 0 ? (
        <div style={{ padding:'12px 8px', color:'#607080', fontSize:10,
                      fontFamily:'var(--font-mono)', textAlign:'center' }}>
          Waiting for events…
        </div>
      ) : (
        <div style={{ flex:1, overflowY:'auto', display:'flex',
                      flexDirection:'column', gap:1, padding:'2px 4px' }}>
          {[...events].reverse().map((ev, i) => {
            const isStage = ev.field === 'flight_stage'
            return (
              <div key={i} style={{
                display: 'flex', alignItems: 'flex-start', gap: 5,
                padding: '4px 5px', borderRadius: 3,
                background: isStage ? 'rgba(0,212,255,0.07)' : 'rgba(255,255,255,0.02)',
                borderLeft: isStage ? `2px solid ${ACCENT}` : '2px solid rgba(255,255,255,0.06)',
              }}>
                <span style={{ color:'#607080', fontSize:9, fontFamily:'var(--font-mono)',
                               flexShrink:0, paddingTop:1, minWidth:48 }}>
                  {formatEventTime(ev.wall_ms)}
                </span>
                <span style={{
                  color: isStage ? ACCENT : '#c9d1d9',
                  fontSize: 10,
                  fontFamily: 'var(--font-mono)',
                  fontWeight: isStage ? 700 : 400,
                  lineHeight: 1.4,
                }}>
                  {ev.message}
                </span>
              </div>
            )
          })}
          <div ref={bottomRef} />
        </div>
      )}
    </div>
  )
}

// ─── Alarm severity map ───────────────────────────────────────────────────────

const SEV = {
  warning:  { rowBg: '#1e1800', border: '#ffd600', fg: '#ffd600', badgeBg: '#ffd600', badgeFg: '#000' },
  critical: { rowBg: '#1e0000', border: '#ff4444', fg: '#ff4444', badgeBg: '#ff4444', badgeFg: '#fff' },
}

const RULE_ICON = { threshold: '▲', rate: '⚡', state: '◈' }

const SIDEBAR_W = 300

function useNow(ms = 1000) {
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms)
    return () => clearInterval(id)
  }, [ms])
  return now
}

function fmtAge(wall_ms, now) {
  const s = Math.floor((now - wall_ms) / 1000)
  if (s < 60)   return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  return `${Math.floor(s / 3600)}h`
}

function fmtValue(v) {
  if (typeof v !== 'number') return String(v)
  if (Math.abs(v) >= 1000) return v.toFixed(0)
  if (Math.abs(v) >= 10)   return v.toFixed(2)
  return v.toFixed(3)
}

export default function AlarmSidebar({ alarms, onDismissAll, onDismissOne, events = [], stageNames = {}, currentStage = -1, lastAck = null, packets = {} }) {
  const now = useNow(1000)
  const [collapsed, setCollapsed] = useState(false)

  // ── Command state ──────────────────────────────────────────────────────────
  const [cmdLog, setCmdLog]   = useState([])
  const cmdSeqRef             = useRef(0)

  const fv = (pkt, name, fallback = null) =>
    pkt?.fields?.find(f => f.name === name)?.value ?? fallback
  const findPkt = (label) =>
    Object.entries(packets).find(([k]) => k.toLowerCase() === label.toLowerCase())?.[1] ?? null

  const hb    = findPkt('heartbeat')
  const evpkt = findPkt('event')
  // Prefer MavlinkGps when lat/lon non-zero, else fall back to LocalGps
  const _mavGps = findPkt('mavlinkgps')
  const _mavLat = fv(_mavGps, 'lat', 0)
  const _mavLon = fv(_mavGps, 'lon', 0)
  const gps = (_mavGps && (_mavLat !== 0 || _mavLon !== 0)) ? _mavGps : findPkt('localgps')

  const gpsFix      = gps != null && fv(gps, 'lat', 0) !== 0
  const pixhawkOk   = fv(hb, 'pixhawk_connected',    null) != null && fv(hb, 'pixhawk_connected',    0) > 0.5
  const vescOk      = fv(hb, 'vesc_connected',        null) != null && fv(hb, 'vesc_connected',        0) > 0.5
  const powerOk     = fv(hb, 'power_connected',       null) != null && fv(hb, 'power_connected',       0) > 0.5
  const photodiodeOk= fv(hb, 'photodiode_connected',  null) != null && fv(hb, 'photodiode_connected',  0) > 0.5
  const dataLogging = fv(evpkt, 'data_logging_active', 0) === 1
  const armState    = fv(evpkt, 'arm_state', 0) === 1

  const allOk     = gpsFix && pixhawkOk && vescOk && powerOk && photodiodeOk && dataLogging
  const canArm    = allOk
  const canLaunch = allOk && armState

  const logCommandSent = (cmd_id) => {
    const seq   = cmdSeqRef.current++
    const entry = { seq, wall_ms: Date.now(), label: CMD_LABELS[cmd_id] ?? `0x${cmd_id.toString(16)}`, cmd_id, status: 'sent', rtt_ms: null }
    setCmdLog(prev => [entry, ...prev].slice(0, CMD_LOG_MAX))
  }

  const logCommandAck = (cmd_id, status, ack_wall_ms) => {
    setCmdLog(prev => {
      const idx = prev.findIndex(e => e.cmd_id === cmd_id && e.status === 'sent')
      const ackStatus = status === 0 ? 'ack' : 'nack'
      if (idx !== -1) {
        const updated = [...prev]
        updated[idx]  = { ...updated[idx], status: ackStatus, rtt_ms: ack_wall_ms - updated[idx].wall_ms }
        return updated
      }
      return [{ seq: -1, wall_ms: ack_wall_ms, label: CMD_LABELS[cmd_id] ?? `0x${cmd_id.toString(16)}`, cmd_id, status: ackStatus, rtt_ms: null }, ...prev].slice(0, CMD_LOG_MAX)
    })
  }

  useEffect(() => {
    if (!lastAck) return
    logCommandAck(lastAck.cmd_id, lastAck.status, lastAck.wall_ms)
  }, [lastAck])

  // ── Alarm derived state ────────────────────────────────────────────────────
  const active    = alarms.filter(a => a.severity !== 'ok')
  const hasCrit   = active.some(a => a.severity === 'critical')
  const hasWarn   = active.some(a => a.severity === 'warning')
  const critCount = active.filter(a => a.severity === 'critical').length
  const warnCount = active.filter(a => a.severity === 'warning').length

  const accentColor = hasCrit ? '#ff4444' : hasWarn ? '#ffd600' : '#1e2d3d'
  const headerBg    = hasCrit ? '#2a0000' : hasWarn ? '#2a2200' : '#0d1117'

  return (
    <div style={{
      ...styles.sidebar,
      width:    collapsed ? 32 : SIDEBAR_W,
      minWidth: collapsed ? 32 : SIDEBAR_W,
    }}>

      {/* ── Header / collapse toggle ── */}
      <div style={{ ...styles.header, background: headerBg }}>
        {!collapsed && (
          <span style={{ ...styles.title, color: accentColor }}>
            ALARMS
          </span>
        )}

        {!collapsed && active.length > 0 && (
          <div style={styles.countRow}>
            {critCount > 0 && (
              <span style={{ ...styles.pill, background: '#ff4444', color: '#fff' }}>
                {critCount} CRIT
              </span>
            )}
            {warnCount > 0 && (
              <span style={{ ...styles.pill, background: '#ffd600', color: '#000' }}>
                {warnCount} WARN
              </span>
            )}
          </div>
        )}

        {collapsed && active.length > 0 && (
          <div style={{ ...styles.collapsedDot, background: accentColor }} title={`${active.length} alarm(s)`} />
        )}

        <button style={styles.collapseBtn} onClick={() => setCollapsed(v => !v)}
          title={collapsed ? 'Show sidebar' : 'Collapse sidebar'}>
          {collapsed ? '◀' : '▶'}
        </button>
      </div>

      {/* ── Content (hidden when collapsed) ── */}
      {!collapsed && (
        <>
          {/* Toolbar */}
          {active.length > 0 && (
            <div style={styles.toolbar}>
              <button style={styles.toolBtn} onClick={onDismissAll}>
                ✕ clear all
              </button>
            </div>
          )}

          {/* Alarm list */}
          <div style={styles.list}>
            {active.length === 0
              ? <div style={styles.empty}>No active alarms</div>
              : active.map(a => {
                  const s = SEV[a.severity]
                  return (
                    <div
                      key={`${a.label}.${a.field}`}
                      style={{ ...styles.card, background: s.rowBg, borderLeft: `3px solid ${s.border}` }}
                    >
                      <div style={styles.cardTop}>
                        <span style={{ ...styles.sevBadge, background: s.badgeBg, color: s.badgeFg }}>
                          {a.severity.toUpperCase()}
                        </span>
                        <span style={{ fontSize: 9, color: s.fg, marginLeft: 4 }} title={a.rule_type}>
                          {RULE_ICON[a.rule_type] ?? '?'}
                        </span>
                        <span style={styles.cardAge}>{fmtAge(a.wall_ms, now)}</span>
                        <button style={styles.dismissBtn} onClick={() => onDismissOne(a.label, a.field)} title="Dismiss">✕</button>
                      </div>
                      <div style={styles.cardSource}>
                        <span style={{ color: '#c9d1d9' }}>{a.label}</span>
                        <span style={{ color: '#607080' }}>.</span>
                        <span style={{ color: s.fg }}>{a.field}</span>
                        <span style={{ color: s.fg, marginLeft: 'auto', fontWeight: 700 }}>{fmtValue(a.value)}</span>
                      </div>
                      <div style={styles.cardMsg}>{a.message}</div>
                    </div>
                  )
                })
            }
          </div>

          {/* ── Commands ── */}
          <div style={styles.commandsSection}>
            <div style={styles.sectionHeader}>COMMANDS</div>
            <div style={{ display: 'flex', gap: 8, padding: '6px 8px', flex: 1, minHeight: 0, overflow: 'hidden' }}>
              {/* Buttons */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: 5, justifyContent: 'center', flexShrink: 0 }}>
                <button
                  disabled={!canArm}
                  style={{
                    fontFamily: 'var(--font-mono)', fontSize: 11, fontWeight: 700, letterSpacing: 1,
                    padding: '5px 10px', borderRadius: 4,
                    border: `1px solid ${canArm ? '#eab308' : '#1e2d3d'}`,
                    background: canArm ? 'rgba(234,179,8,0.12)' : 'rgba(255,255,255,0.03)',
                    color: canArm ? '#eab308' : '#607080',
                    cursor: canArm ? 'pointer' : 'not-allowed',
                  }}
                  onClick={async () => {
                    logCommandSent(CMD_ARM)
                    await fetch('/api/fc/command/arm', { method: 'POST' })
                  }}
                >ARM</button>
                <button
                  disabled={!canLaunch}
                  style={{
                    fontFamily: 'var(--font-mono)', fontSize: 11, fontWeight: 700, letterSpacing: 1,
                    padding: '5px 10px', borderRadius: 4,
                    border: `1px solid ${canLaunch ? '#22c55e' : '#1e2d3d'}`,
                    background: canLaunch ? 'rgba(34,197,94,0.12)' : 'rgba(255,255,255,0.03)',
                    color: canLaunch ? '#22c55e' : '#607080',
                    cursor: canLaunch ? 'pointer' : 'not-allowed',
                  }}
                  onClick={async () => {
                    logCommandSent(CMD_LAUNCH_OK)
                    await fetch('/api/fc/command/launch_ok', { method: 'POST' })
                  }}
                >LAUNCH OK</button>
                <button
                  style={{
                    fontFamily: 'var(--font-mono)', fontSize: 11, fontWeight: 700, letterSpacing: 1,
                    padding: '5px 10px', borderRadius: 4,
                    border: `1px solid ${ACCENT}`,
                    background: 'rgba(0,212,255,0.07)',
                    color: ACCENT, cursor: 'pointer',
                  }}
                  onClick={async () => {
                    logCommandSent(CMD_PING)
                    await fetch('/api/fc/command/ping', { method: 'POST' })
                  }}
                >PING</button>
              </div>

              {/* Command log */}
              <div style={{
                flex: 1, borderLeft: '1px solid #1e2d3d', paddingLeft: 8,
                overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 2,
              }}>
                {cmdLog.length === 0
                  ? <div style={{ fontSize: 10, color: '#607080', fontFamily: 'var(--font-mono)' }}>—</div>
                  : cmdLog.map((e, i) => {
                      const t  = new Date(e.wall_ms)
                      const ts = `${String(t.getHours()).padStart(2,'0')}:${String(t.getMinutes()).padStart(2,'0')}:${String(t.getSeconds()).padStart(2,'0')}`
                      const statusColor = e.status === 'ack' ? '#22c55e' : e.status === 'nack' ? '#ff4444' : '#607080'
                      const statusLabel = e.status === 'ack' ? '✓ ACK' : e.status === 'nack' ? '✗ NACK' : '···'
                      return (
                        <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 5, fontFamily: 'var(--font-mono)', fontSize: 10 }}>
                          <span style={{ color: '#607080', flexShrink: 0 }}>{ts}</span>
                          <span style={{ color: '#c9d1d9', flexShrink: 0 }}>{e.label}</span>
                          <span style={{ color: statusColor, flexShrink: 0 }}>{statusLabel}</span>
                          {e.rtt_ms != null && <span style={{ color: '#607080' }}>{e.rtt_ms}ms</span>}
                        </div>
                      )
                    })
                }
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  )
}

const styles = {
  sidebar: {
    display:       'flex',
    flexDirection: 'column',
    overflow:      'hidden',
    borderLeft:    '1px solid #1e2d3d',
    background:    '#0d1117',
    transition:    'width 0.2s ease, min-width 0.2s ease',
    flexShrink:    0,
    fontFamily:    'var(--font-mono)',
  },
  header: {
    display:    'flex',
    alignItems: 'center',
    gap:        6,
    padding:    '6px 8px',
    flexShrink: 0,
    borderBottom: '1px solid #1e2d3d',
    minHeight:  32,
  },
  title: {
    fontSize:      10,
    fontWeight:    700,
    letterSpacing: 2,
    flex:          1,
  },
  countRow: {
    display: 'flex',
    gap:     4,
    flex:    1,
  },
  pill: {
    fontSize:      9,
    fontWeight:    700,
    borderRadius:  3,
    padding:       '1px 5px',
    letterSpacing: 0.5,
  },
  collapsedDot: {
    width:        8,
    height:       8,
    borderRadius: '50%',
    margin:       '0 auto',
  },
  collapseBtn: {
    background: 'none',
    border:     'none',
    color:      '#607080',
    fontSize:   10,
    cursor:     'pointer',
    padding:    '2px 4px',
    lineHeight: 1,
    flexShrink: 0,
  },
  toolbar: {
    padding:      '4px 8px',
    borderBottom: '1px solid #1e2d3d',
    flexShrink:   0,
  },
  toolBtn: {
    background:   'none',
    border:       '1px solid #1e2d3d',
    borderRadius: 3,
    color:        '#ff6666',
    fontFamily:   'var(--font-mono)',
    fontSize:     10,
    cursor:       'pointer',
    padding:      '2px 8px',
    width:        '100%',
  },
  list: {
    flex:      1,
    overflowY: 'auto',
    display:   'flex',
    flexDirection: 'column',
    gap:       1,
    padding:   4,
  },
  empty: {
    color:     '#607080',
    fontSize:  11,
    padding:   '20px 8px',
    textAlign: 'center',
  },
  card: {
    borderRadius: 4,
    padding:      '6px 8px',
    display:      'flex',
    flexDirection:'column',
    gap:          3,
  },
  cardTop: {
    display:    'flex',
    alignItems: 'center',
    gap:        4,
  },
  sevBadge: {
    fontSize:      8,
    fontWeight:    700,
    borderRadius:  2,
    padding:       '1px 4px',
    letterSpacing: 0.5,
    flexShrink:    0,
  },
  cardAge: {
    fontSize:  9,
    color:     '#607080',
    marginLeft:'auto',
    flexShrink: 0,
  },
  dismissBtn: {
    background: 'none',
    border:     'none',
    color:      '#607080',
    fontSize:   10,
    cursor:     'pointer',
    padding:    '0 2px',
    lineHeight: 1,
    flexShrink: 0,
  },
  cardSource: {
    display:    'flex',
    alignItems: 'baseline',
    fontSize:   11,
    gap:        0,
  },
  cardMsg: {
    fontSize:  10,
    color:     '#8b949e',
    lineHeight: 1.3,
  },
  commandsSection: {
    display:       'flex',
    flexDirection: 'column',
    flexShrink:    0,
    borderTop:     '1px solid #1e2d3d',
    minHeight:     130,
  },
  sectionHeader: {
    fontSize:      9,
    fontWeight:    700,
    letterSpacing: 2,
    color:         '#607080',
    padding:       '6px 8px 4px',
    flexShrink:    0,
  },
}
