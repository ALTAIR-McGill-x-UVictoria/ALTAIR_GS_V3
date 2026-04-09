import { useState, useEffect } from 'react'

const SEV = {
  warning:  { rowBg: '#2a2200', border: '#ffd600', fg: '#ffd600', badgeBg: '#ffd600', badgeFg: '#000' },
  critical: { rowBg: '#2a0000', border: '#ff4444', fg: '#ff4444', badgeBg: '#ff4444', badgeFg: '#fff' },
}

const RULE_LABEL = { threshold: 'THRESHOLD', rate: 'RATE', state: 'STATE' }
const RULE_ICON  = { threshold: '▲', rate: '⚡', state: '◈' }

function useNow(intervalMs = 1000) {
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(id)
  }, [intervalMs])
  return now
}

function fmtAge(wall_ms, now) {
  const s = Math.floor((now - wall_ms) / 1000)
  if (s < 60)   return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

function fmtValue(v) {
  if (typeof v !== 'number') return String(v)
  if (Math.abs(v) >= 1000) return v.toFixed(0)
  if (Math.abs(v) >= 10)   return v.toFixed(2)
  return v.toFixed(3)
}

export default function AlarmPanel({ alarms, onDismissAll, onDismissOne }) {
  const now       = useNow(1000)
  const [collapsed, setCollapsed] = useState(false)

  const active = alarms.filter(a => a.severity !== 'ok')
  if (active.length === 0) return null

  const hasCrit = active.some(a => a.severity === 'critical')
  const accentColor = hasCrit ? '#ff4444' : '#ffd600'

  return (
    <div style={{ ...styles.root, borderColor: accentColor }}>

      {/* ── Header bar ── */}
      <div style={{ ...styles.headerBar, borderBottomColor: hasCrit ? '#3a0000' : '#2a2200' }}>
        <span style={{
          ...styles.headerBadge,
          background: accentColor,
          color:      hasCrit ? '#fff' : '#000',
        }}>
          {hasCrit ? '⚠ CRITICAL' : '⚠ WARNING'}
        </span>

        <span style={styles.headerCount}>
          {active.length} active alarm{active.length !== 1 ? 's' : ''}
          {active.filter(a => a.severity === 'critical').length > 0 && active.filter(a => a.severity === 'warning').length > 0 && (
            <span style={{ color: '#607080', marginLeft: 6 }}>
              ({active.filter(a => a.severity === 'critical').length} crit,{' '}
               {active.filter(a => a.severity === 'warning').length} warn)
            </span>
          )}
        </span>

        <button style={styles.headerBtn} onClick={() => setCollapsed(v => !v)}>
          {collapsed ? '▼ show' : '▲ hide'}
        </button>
        <button style={{ ...styles.headerBtn, color: '#ff6666' }} onClick={onDismissAll}>
          ✕ clear all
        </button>
      </div>

      {/* ── Alarm rows ── */}
      {!collapsed && (
        <div style={styles.list}>
          {active.map(a => {
            const s = SEV[a.severity]
            return (
              <div key={`${a.label}.${a.field}`} style={{ ...styles.row, background: s.rowBg, borderLeft: `3px solid ${s.border}` }}>

                {/* Severity badge */}
                <span style={{ ...styles.sevBadge, background: s.badgeBg, color: s.badgeFg }}>
                  {a.severity.toUpperCase()}
                </span>

                {/* Rule type icon */}
                <span title={RULE_LABEL[a.rule_type] ?? a.rule_type}
                  style={{ fontSize: 10, color: s.fg, flexShrink: 0, width: 12, textAlign: 'center' }}>
                  {RULE_ICON[a.rule_type] ?? '?'}
                </span>

                {/* Source: Label.field */}
                <span style={styles.source}>
                  <span style={{ color: '#c9d1d9' }}>{a.label}</span>
                  <span style={{ color: '#607080' }}>.</span>
                  <span style={{ color: s.fg }}>{a.field}</span>
                </span>

                {/* Current value */}
                <span style={{ ...styles.valueCell, color: s.fg }}>
                  {fmtValue(a.value)}
                </span>

                {/* Message */}
                <span style={styles.message}>{a.message}</span>

                {/* Age — live-updating */}
                <span style={styles.age}>{fmtAge(a.wall_ms, now)}</span>

                {/* Per-alarm dismiss */}
                <button
                  style={styles.dismissBtn}
                  onClick={() => onDismissOne(a.label, a.field)}
                  title="Dismiss this alarm"
                >✕</button>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

const styles = {
  root: {
    border:       '1px solid',
    borderRadius: 6,
    overflow:     'hidden',
    flexShrink:   0,
    fontFamily:   'var(--font-mono)',
  },
  headerBar: {
    display:       'flex',
    alignItems:    'center',
    gap:           10,
    padding:       '5px 10px',
    background:    '#161b22',
    borderBottom:  '1px solid',
  },
  headerBadge: {
    fontSize:      10,
    fontWeight:    700,
    padding:       '2px 8px',
    borderRadius:  3,
    letterSpacing: 1,
    flexShrink:    0,
  },
  headerCount: {
    fontSize: 11,
    color:    '#607080',
    flex:     1,
  },
  headerBtn: {
    background:   'none',
    border:       '1px solid #1e2d3d',
    borderRadius: 3,
    color:        '#607080',
    fontFamily:   'var(--font-mono)',
    fontSize:     10,
    cursor:       'pointer',
    padding:      '2px 8px',
  },
  list: {
    display:       'flex',
    flexDirection: 'column',
    maxHeight:     220,
    overflowY:     'auto',
  },
  row: {
    display:    'flex',
    alignItems: 'center',
    gap:        8,
    padding:    '5px 10px',
    fontSize:   11,
    borderBottom: '1px solid #0d1117',
  },
  sevBadge: {
    fontSize:      9,
    padding:       '1px 5px',
    borderRadius:  2,
    flexShrink:    0,
    letterSpacing: 0.5,
    fontWeight:    700,
  },
  source: {
    minWidth:   160,
    flexShrink: 0,
  },
  valueCell: {
    minWidth:   72,
    flexShrink: 0,
    textAlign:  'right',
    fontWeight: 700,
  },
  message: {
    flex:         1,
    color:        '#c9d1d9',
    overflow:     'hidden',
    textOverflow: 'ellipsis',
    whiteSpace:   'nowrap',
  },
  age: {
    color:      '#607080',
    flexShrink: 0,
    fontSize:   10,
    minWidth:   52,
    textAlign:  'right',
  },
  dismissBtn: {
    background:   'none',
    border:       'none',
    color:        '#607080',
    fontSize:     11,
    cursor:       'pointer',
    padding:      '0 4px',
    flexShrink:   0,
    lineHeight:   1,
  },
}
