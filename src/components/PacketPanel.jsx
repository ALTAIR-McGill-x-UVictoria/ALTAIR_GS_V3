import { useState } from 'react'
import { LineChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, YAxis } from 'recharts'

const SPARKLINE_COLORS = [
  '#00d4ff', '#7c3aed', '#22c55e', '#f59e0b', '#ef4444', '#ec4899',
]

const SEV = {
  warning:  { border: '#ffd600', bg: 'rgba(255,214,0,0.07)',   valueFg: '#ffd600', badgeBg: '#ffd600', badgeFg: '#000' },
  critical: { border: '#ff4444', bg: 'rgba(255,68,68,0.08)',   valueFg: '#ff4444', badgeBg: '#ff4444', badgeFg: '#fff' },
}

const RULE_ICON = { threshold: '▲', rate: '⚡', state: '◈' }

// ---------------------------------------------------------------------------
// Threshold gauge — horizontal bar showing value position between min and max
// ---------------------------------------------------------------------------
function ThresholdGauge({ value, rule }) {
  if (rule.type !== 'threshold') return null
  const lo = rule.min ?? (value - Math.abs(value) * 0.5)
  const hi = rule.max ?? (value + Math.abs(value) * 0.5)
  if (hi === lo) return null

  const span      = hi - lo
  const margin    = rule.margin ?? 0.10
  const warnLo    = rule.min != null ? lo + span * margin : null
  const warnHi    = rule.max != null ? hi - span * margin : null
  const pct       = Math.max(0, Math.min(1, (value - lo) / span))

  // Zone color: red outside hard limits, yellow in margin, green in safe zone
  let fillColor = '#00ff88'
  if (value < lo || value > hi)         fillColor = '#ff4444'
  else if (warnLo != null && value < warnLo) fillColor = '#ffd600'
  else if (warnHi != null && value > warnHi) fillColor = '#ffd600'

  return (
    <div style={{ position: 'relative', height: 4, background: '#1e2d3d', borderRadius: 2, marginTop: 3, overflow: 'visible' }}>
      {/* Warning zone bands */}
      {warnLo != null && (
        <div style={{
          position: 'absolute', left: 0,
          width: `${((warnLo - lo) / span) * 100}%`,
          height: '100%', background: 'rgba(255,214,0,0.25)', borderRadius: '2px 0 0 2px',
        }} />
      )}
      {warnHi != null && (
        <div style={{
          position: 'absolute', right: 0,
          width: `${((hi - warnHi) / span) * 100}%`,
          height: '100%', background: 'rgba(255,214,0,0.25)', borderRadius: '0 2px 2px 0',
        }} />
      )}
      {/* Value needle */}
      <div style={{
        position:  'absolute',
        left:      `calc(${pct * 100}% - 1px)`,
        top:       -2,
        width:     2,
        height:    8,
        background: fillColor,
        borderRadius: 1,
      }} />
      {/* Min/max labels */}
      <div style={{ position: 'absolute', top: 6, left: 0,  fontSize: 8, color: '#607080', fontFamily: 'monospace' }}>
        {rule.min ?? ''}
      </div>
      <div style={{ position: 'absolute', top: 6, right: 0, fontSize: 8, color: '#607080', fontFamily: 'monospace' }}>
        {rule.max ?? ''}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------
export default function PacketPanel({ label, packet, history, alarms = [], rules = [] }) {
  const [collapsed, setCollapsed] = useState(false)

  // Map field name -> alarm entry and rule def
  const alarmMap = {}
  for (const a of alarms) alarmMap[a.field] = a

  const ruleMap = {}
  for (const r of rules) {
    if (!ruleMap[r.field]) ruleMap[r.field] = []
    ruleMap[r.field].push(r)
  }

  if (!packet) {
    return (
      <div style={styles.panel}>
        <div style={styles.header}>
          <span style={styles.label}>{label}</span>
          <span style={{ ...styles.badge, background: 'var(--border)', color: 'var(--muted)' }}>
            waiting…
          </span>
        </div>
        <div style={{ color: 'var(--muted)', padding: '12px 0', fontSize: 11 }}>
          No data received yet.
        </div>
      </div>
    )
  }

  const { fields, seq, timestamp, wall_ms, dropped, hz } = packet

  // Worst severity across all alarming fields
  const panelSev = alarms.find(a => a.severity === 'critical')?.severity
    ?? alarms.find(a => a.severity === 'warning')?.severity

  return (
    <div style={{
      ...styles.panel,
      ...(panelSev ? { border: `1px solid ${SEV[panelSev].border}` } : {}),
    }}>

      {/* Header */}
      <div style={{ ...styles.header, cursor: 'pointer' }} onClick={() => setCollapsed(v => !v)}>
        <span style={{ ...styles.collapseArrow, transform: collapsed ? 'rotate(-90deg)' : 'rotate(0deg)' }}>▾</span>
        <span style={styles.label}>{label}</span>
        <span style={styles.meta}>seq {seq}</span>
        {wall_ms != null && (
          <span style={styles.meta}>{formatWallTime(wall_ms)}</span>
        )}
        {dropped > 0 && (
          <span style={{ ...styles.badge, background: 'var(--error)' }}>
            -{dropped} dropped
          </span>
        )}
        {/* Alarm summary badge in header */}
        {panelSev && (
          <span style={{
            ...styles.badge,
            background: SEV[panelSev].badgeBg,
            color:      SEV[panelSev].badgeFg,
            fontWeight: 700,
            letterSpacing: 0.5,
          }}>
            {RULE_ICON[alarms.find(a => a.severity === panelSev)?.rule_type] ?? '⚠'}{' '}
            {panelSev.toUpperCase()}
          </span>
        )}
        <span style={{ ...styles.badge, background: 'var(--border)', color: 'var(--muted)', marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 3 }}>
          <span style={{ display: 'inline-block', width: 36, textAlign: 'right' }}>
            {hz !== null && hz !== undefined ? String(hz) : '—'}
          </span>
          <span>Hz</span>
        </span>
      </div>

      {/* Fields — one horizontal row, each field is a column */}
      {!collapsed && (
        <div style={styles.fields}>
          {fields.map((f, i) => {
            const sparkData  = history?.[f.name] ?? []
            const baseColor  = SPARKLINE_COLORS[i % SPARKLINE_COLORS.length]
            const alarm      = alarmMap[f.name]
            const fieldRules = ruleMap[f.name] ?? []
            const sev        = alarm?.severity
            const valueColor = sev ? SEV[sev].valueFg : baseColor
            const sparkColor = sev ? SEV[sev].valueFg : baseColor

            const threshRule = fieldRules.find(r => r.type === 'threshold')
            const refLines = []
            if (threshRule) {
              if (threshRule.min != null) refLines.push({ y: threshRule.min, color: '#ff4444' })
              if (threshRule.max != null) refLines.push({ y: threshRule.max, color: '#ff4444' })
              if (threshRule.min != null && threshRule.margin != null) {
                const span = (threshRule.max ?? threshRule.min * 2) - threshRule.min
                refLines.push({ y: threshRule.min + span * threshRule.margin, color: '#ffd60066' })
              }
              if (threshRule.max != null && threshRule.margin != null) {
                const span = threshRule.max - (threshRule.min ?? 0)
                refLines.push({ y: threshRule.max - span * threshRule.margin, color: '#ffd60066' })
              }
            }

            return (
              <div key={f.name} style={{
                ...styles.fieldCol,
                ...(i === 0 ? { borderLeft: 'none' } : {}),
                ...(sev ? { background: SEV[sev].bg, borderRadius: 4 } : {}),
              }}>
                {/* Label + rule icons */}
                <div style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                  <span style={{ color: 'var(--muted)', fontSize: 10, whiteSpace: 'nowrap' }}>{f.label}</span>
                  {fieldRules.map((r, ri) => (
                    <span key={ri} title={`${r.type} rule`}
                      style={{ fontSize: 8, color: sev ? SEV[sev].border : '#607080', lineHeight: 1 }}>
                      {RULE_ICON[r.type]}
                    </span>
                  ))}
                </div>

                {/* Value + unit */}
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 3 }}>
                  <span style={{ color: valueColor, fontWeight: 700, fontSize: 13, fontFamily: 'var(--font-mono)' }}>
                    {formatValue(f.value)}
                  </span>
                  <span style={{ color: 'var(--muted)', fontSize: 9 }}>{f.unit}</span>
                </div>

                {/* Threshold gauge */}
                {threshRule && (
                  <div style={{ marginTop: 2, marginBottom: 4 }}>
                    <ThresholdGauge value={f.value} rule={threshRule} />
                  </div>
                )}

                {/* Sparkline */}
                {sparkData.length > 2 && (
                  <div style={styles.sparkline}>
                    <ResponsiveContainer width="100%" height={32}>
                      <LineChart data={sparkData}>
                        <YAxis domain={['auto', 'auto']} hide />
                        <Tooltip
                          contentStyle={{ background: 'var(--surface)', border: '1px solid var(--border)', fontSize: 10 }}
                          formatter={(v) => [formatValue(v), f.label]}
                          labelFormatter={(t) => `t=${Number(t).toFixed(2)}s`}
                        />
                        {refLines.map((rl, ri) => (
                          <ReferenceLine key={ri} y={rl.y} stroke={rl.color} strokeDasharray="3 2" strokeWidth={1} />
                        ))}
                        <Line
                          type="monotone"
                          dataKey="v"
                          stroke={sparkColor}
                          strokeWidth={1.5}
                          dot={false}
                          isAnimationActive={false}
                        />
                      </LineChart>
                    </ResponsiveContainer>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function formatWallTime(ms) {
  const d = new Date(ms)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  const ms3 = String(d.getMilliseconds()).padStart(3, '0')
  return `${hh}:${mm}:${ss}.${ms3}`
}

function formatValue(v) {
  if (typeof v !== 'number') return String(v)
  if (Math.abs(v) >= 1000) return v.toFixed(0)
  if (Math.abs(v) >= 10)   return v.toFixed(2)
  return v.toFixed(4)
}

const styles = {
  panel: {
    background:    'var(--surface)',
    border:        '1px solid var(--border)',
    borderRadius:  8,
    padding:       '8px 12px',
    display:       'flex',
    flexDirection: 'column',
    gap:           6,
    transition:    'border-color 0.3s ease',
  },
  header: {
    display:       'flex',
    alignItems:    'center',
    gap:           8,
    borderBottom:  '1px solid var(--border)',
    paddingBottom: 6,
    userSelect:    'none',
  },
  collapseArrow: {
    fontSize:   11,
    color:      'var(--muted)',
    transition: 'transform 0.15s ease',
    flexShrink: 0,
    lineHeight: 1,
  },
  label: {
    fontWeight:    700,
    fontSize:      13,
    color:         'var(--accent)',
    letterSpacing: 1,
    textTransform: 'uppercase',
  },
  meta: {
    fontSize: 10,
    color:    'var(--muted)',
  },
  badge: {
    fontSize:     10,
    borderRadius: 4,
    padding:      '1px 6px',
    color:        'var(--text)',
    fontFamily:   'var(--font-mono)',
  },
  fields: {
    display:   'flex',
    flexWrap:  'wrap',
    gap:       1,
  },
  fieldCol: {
    display:        'flex',
    flexDirection:  'column',
    gap:            2,
    padding:        '4px 10px 4px 10px',
    borderLeft:     '1px solid var(--border)',
    minWidth:       100,
    flex:           '1 1 0',
  },
  sparkline: {
    width:  '100%',
    height: 32,
  },
}
