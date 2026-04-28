import { useEffect, useRef } from 'react'

// ─── helpers ────────────────────────────────────────────────────────────────

export function fv(packet, fieldName, fallback = null) {
  if (!packet) return fallback
  return packet.fields.find(f => f.name === fieldName)?.value ?? fallback
}

export function findPacket(packets, labelLC) {
  const entry = Object.entries(packets).find(([k]) => k.toLowerCase() === labelLC)
  return entry?.[1] ?? null
}

export function formatUptime(s) {
  if (s == null) return '—'
  const h  = Math.floor(s / 3600)
  const m  = Math.floor((s % 3600) / 60)
  const ss = Math.floor(s % 60)
  if (h > 0) return `${h}h ${String(m).padStart(2,'0')}m`
  return `${String(m).padStart(2,'0')}m ${String(ss).padStart(2,'0')}s`
}

export function formatDuration(ms) {
  if (ms == null || ms < 0) return '—'
  const s = Math.floor(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const ss = s % 60
  if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`
  return `${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`
}

export function formatEta(ms) {
  if (ms == null) return '—'
  const abs = Math.abs(ms)
  const s = Math.floor(abs / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const ss = s % 60
  const sign = ms < 0 ? '−' : ''
  if (h > 0) return `${sign}${h}:${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`
  return `${sign}${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`
}

export function formatEventTime(wall_ms) {
  const d = new Date(wall_ms)
  const h = String(d.getHours()).padStart(2, '0')
  const m = String(d.getMinutes()).padStart(2, '0')
  const s = String(d.getSeconds()).padStart(2, '0')
  return `${h}:${m}:${s}`
}

// ─── colour palette ─────────────────────────────────────────────────────────
export const C = {
  accent:   '#00d4ff',
  sky:      '#1a3a5c',
  earth:    '#5c3a1a',
  horizon:  '#00d4ff',
  warn:     '#ffd600',
  crit:     '#ff4444',
  ok:       '#22c55e',
  muted:    '#6b7280',
  surface:  '#0f1318',
  card:     '#141820',
  border:   '#1e2535',
  text:     '#e2e8f0',
}

export const EVENT_ICONS = {
  flight_stage:        '🚀',
  arm_state:           '🔒',
  launch_detected:     '🚀',
  ascent_active:       '↑',
  apogee_detected:     '⬆',
  descent_active:      '↓',
  landing_detected:    '⬇',
  cutdown_fired:       '✂',
  recovery_active:     '📡',
  data_logging_active: '💾',
}

// ─── shared styles ───────────────────────────────────────────────────────────
export const S = {
  root: {
    flex: 1,
    overflow: 'hidden',
    padding: 12,
    display: 'flex',
    flexDirection: 'column',
    gap: 10,
    background: 'var(--bg)',
    boxSizing: 'border-box',
  },
  row: {
    display: 'flex',
    gap: 10,
    flexWrap: 'nowrap',
    alignItems: 'stretch',
    flex: 1,
    minHeight: 0,
  },
  card: {
    background: C.card,
    border: `1px solid ${C.border}`,
    borderRadius: 8,
    padding: '10px 14px 14px',
    display: 'flex',
    flexDirection: 'column',
    minWidth: 0,
    overflow: 'hidden',
  },
  cardTitle: {
    fontSize: 10,
    fontFamily: 'var(--font-mono)',
    letterSpacing: 2,
    color: C.muted,
    marginBottom: 10,
    textTransform: 'uppercase',
    flexShrink: 0,
  },
  statRow: {
    display:'flex',
    alignItems:'center',
    gap:8,
    padding:'3px 0',
  },
}

// ─────────────────────────────────────────────────────────────────────────────
// Artificial Horizon
// ─────────────────────────────────────────────────────────────────────────────
export function ArtificialHorizon({ roll = 0, pitch = 0 }) {
  const W = 200, H = 200, R = 96
  const pitchPx = pitch * (180 / Math.PI) * 2
  const rollDeg = roll * (180 / Math.PI)

  const clipId = 'ah-clip'
  const rollTicks = [-60,-45,-30,-20,-10,0,10,20,30,45,60]

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ display:'block', width:'100%', height:'auto' }}>
      <defs>
        <clipPath id={clipId}>
          <circle cx={W/2} cy={H/2} r={R} />
        </clipPath>
      </defs>

      <g clipPath={`url(#${clipId})`}
         transform={`rotate(${-rollDeg} ${W/2} ${H/2})`}>
        <rect x={0} y={0} width={W} height={H/2 + pitchPx} fill={C.sky} />
        <rect x={0} y={H/2 + pitchPx} width={W} height={H} fill={C.earth} />
        <line
          x1={0} y1={H/2 + pitchPx}
          x2={W} y2={H/2 + pitchPx}
          stroke={C.horizon} strokeWidth={1.5}
        />
        {[-20,-15,-10,-5,5,10,15,20].map(deg => {
          const py = H/2 + pitchPx - deg * 2
          const len = deg % 10 === 0 ? 40 : 24
          return (
            <g key={deg}>
              <line
                x1={W/2 - len/2} y1={py}
                x2={W/2 + len/2} y2={py}
                stroke="rgba(255,255,255,0.45)" strokeWidth={1}
              />
              {deg % 10 === 0 && (
                <text
                  x={W/2 - len/2 - 4} y={py + 3}
                  fontSize={8} fill="rgba(255,255,255,0.55)"
                  textAnchor="end"
                >{Math.abs(deg)}</text>
              )}
            </g>
          )
        })}
      </g>

      <circle cx={W/2} cy={H/2} r={R} fill="none" stroke={C.border} strokeWidth={1.5} />
      {rollTicks.map(deg => {
        const rad = (deg - 90) * Math.PI / 180
        const inner = deg % 30 === 0 ? R - 10 : R - 6
        return (
          <line key={deg}
            x1={W/2 + R * Math.cos(rad)}
            y1={H/2 + R * Math.sin(rad)}
            x2={W/2 + inner * Math.cos(rad)}
            y2={H/2 + inner * Math.sin(rad)}
            stroke={deg === 0 ? C.accent : 'rgba(255,255,255,0.4)'}
            strokeWidth={deg % 30 === 0 ? 1.5 : 1}
          />
        )
      })}

      <g stroke={C.warn} strokeWidth={2} fill="none">
        <line x1={W/2 - 40} y1={H/2} x2={W/2 - 12} y2={H/2} />
        <line x1={W/2 - 12} y1={H/2} x2={W/2 - 12} y2={H/2 + 6} />
        <line x1={W/2 + 12} y1={H/2} x2={W/2 + 40} y2={H/2} />
        <line x1={W/2 + 12} y1={H/2} x2={W/2 + 12} y2={H/2 + 6} />
        <circle cx={W/2} cy={H/2} r={3} fill={C.warn} stroke="none" />
      </g>

      <g transform={`rotate(${-rollDeg} ${W/2} ${H/2})`}>
        <polygon
          points={`${W/2},${H/2-R+2} ${W/2-5},${H/2-R+11} ${W/2+5},${H/2-R+11}`}
          fill={C.accent} stroke="none"
        />
      </g>

      <text x={W/2} y={H-4} textAnchor="middle" fontSize={9}
            fill={C.muted} fontFamily="monospace">
        {`R ${(roll*(180/Math.PI)).toFixed(1)}°  P ${(pitch*(180/Math.PI)).toFixed(1)}°`}
      </text>
    </svg>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Compass Rose
// ─────────────────────────────────────────────────────────────────────────────
export function CompassNeedle({ cx, cy, bearing, length, color, width = 1.5, tailLength = 0 }) {
  const rad = (bearing - 90) * Math.PI / 180
  const tx = cx + length * Math.cos(rad)
  const ty = cy + length * Math.sin(rad)
  const bx = cx - tailLength * Math.cos(rad)
  const by = cy - tailLength * Math.sin(rad)
  return <line x1={bx} y1={by} x2={tx} y2={ty} stroke={color} strokeWidth={width} strokeLinecap="round" />
}

export function Compass({ yawDeg = null, gpsDeg = null }) {
  const W = 160, H = 160, R = 70, cx = W/2, cy = H/2
  const cardinals = ['N','NE','E','SE','S','SW','W','NW']

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ display:'block', width:'100%', height:'auto' }}>
      <circle cx={cx} cy={cy} r={R} fill={C.card} stroke={C.border} strokeWidth={1.5} />
      {Array.from({length:36},(_,i) => i*10).map(deg => {
        const rad = (deg - 90) * Math.PI / 180
        const major = deg % 90 === 0
        const mid   = deg % 45 === 0
        const inner = major ? R-14 : mid ? R-10 : R-6
        return (
          <line key={deg}
            x1={cx + R * Math.cos(rad)} y1={cy + R * Math.sin(rad)}
            x2={cx + inner * Math.cos(rad)} y2={cy + inner * Math.sin(rad)}
            stroke={major ? C.accent : 'rgba(255,255,255,0.25)'}
            strokeWidth={major ? 1.5 : 0.75}
          />
        )
      })}
      {cardinals.map((lbl, i) => {
        const deg = i * 45
        const rad = (deg - 90) * Math.PI / 180
        const lr  = R - 22
        return (
          <text key={lbl}
            x={cx + lr * Math.cos(rad)}
            y={cy + lr * Math.sin(rad) + 4}
            textAnchor="middle" fontSize={lbl.length === 1 ? 11 : 8}
            fontWeight={lbl === 'N' ? 700 : 400}
            fill={lbl === 'N' ? C.crit : 'rgba(255,255,255,0.65)'}
            fontFamily="monospace"
          >{lbl}</text>
        )
      })}

      {gpsDeg != null && (
        <CompassNeedle cx={cx} cy={cy} bearing={gpsDeg}  length={R-8} color={C.text}   width={2}   tailLength={12} />
      )}
      {yawDeg != null && (
        <CompassNeedle cx={cx} cy={cy} bearing={yawDeg} length={R-18} color={C.warn} width={1.5} tailLength={8} />
      )}

      <circle cx={cx} cy={cy} r={3} fill={C.card} stroke={C.border} strokeWidth={1} />

      <text x={cx} y={cy+6} textAnchor="middle"
            fontSize={15} fontWeight={700} fontFamily="monospace" fill={C.text}>
        {String(Math.round(gpsDeg ?? yawDeg ?? 0)).padStart(3,'0')}°
      </text>

      <text x={6} y={H-5} fontSize={7} fontFamily="monospace" fill={C.text}>━ GPS</text>
      <text x={6} y={H-14} fontSize={7} fontFamily="monospace" fill={C.warn}>━ YAW</text>
    </svg>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Arc gauge
// ─────────────────────────────────────────────────────────────────────────────
export function ArcGauge({ value, min, max, warnLo, warnHi, unit, label, size = 120 }) {
  const r = size * 0.38
  const cx = size / 2, cy = size / 2
  const START_DEG = 225, SWEEP = 270
  const pct = value == null ? 0 : Math.max(0, Math.min(1, (value - min) / (max - min)))

  function polarToXY(deg, radius) {
    const rad = (deg) * Math.PI / 180
    return { x: cx + radius * Math.cos(rad), y: cy + radius * Math.sin(rad) }
  }

  function arcPath(startDeg, endDeg, radius) {
    const s = polarToXY(startDeg, radius)
    const e = polarToXY(endDeg, radius)
    const large = Math.abs(endDeg - startDeg) > 180 ? 1 : 0
    const sweep = endDeg < startDeg ? 0 : 1
    return `M ${s.x} ${s.y} A ${radius} ${radius} 0 ${large} ${sweep} ${e.x} ${e.y}`
  }

  const bgPath = arcPath(180 + START_DEG - SWEEP, 180 + START_DEG - SWEEP + SWEEP, r)

  let fillColor = C.ok
  if (value != null) {
    if ((warnHi != null && value >= warnHi) || (warnLo != null && value <= warnLo)) fillColor = C.warn
    if ((max != null && value >= max)       || (min != null && value <= min))       fillColor = C.crit
  }

  const needleRad = (180 + START_DEG - pct * SWEEP) * Math.PI / 180
  const needleTip = { x: cx + (r - 4) * Math.cos(needleRad), y: cy + (r - 4) * Math.sin(needleRad) }

  return (
    <svg viewBox={`0 0 ${size} ${size * 0.82}`} style={{ display:'block', width:'100%', height:'auto' }}>
      <path d={bgPath} fill="none" stroke={C.border} strokeWidth={6} strokeLinecap="round" />
      <path
        d={arcPath(180 + START_DEG - SWEEP, 180 + START_DEG - pct * SWEEP + 0.01, r)}
        fill="none" stroke={fillColor} strokeWidth={6} strokeLinecap="round"
      />
      <line
        x1={cx} y1={cy}
        x2={needleTip.x} y2={needleTip.y}
        stroke={fillColor} strokeWidth={1.5}
      />
      <circle cx={cx} cy={cy} r={4} fill={C.card} stroke={fillColor} strokeWidth={1.5} />
      <text x={cx} y={cy + (size * 0.18)} textAnchor="middle"
            fontSize={size * 0.145} fontWeight={700} fontFamily="monospace"
            fill={fillColor}>
        {value != null ? value.toFixed(1) : '—'}
      </text>
      <text x={cx} y={cy + (size * 0.30)} textAnchor="middle"
            fontSize={size * 0.09} fontFamily="monospace" fill={C.muted}>
        {unit}
      </text>
      <text x={cx} y={size * 0.80} textAnchor="middle"
            fontSize={size * 0.09} fontFamily="monospace" fill={C.muted}>
        {label}
      </text>
      <text x={cx - r + 2} y={size * 0.78} textAnchor="middle"
            fontSize={size * 0.075} fontFamily="monospace" fill="rgba(255,255,255,0.2)">
        {min}
      </text>
      <text x={cx + r - 2} y={size * 0.78} textAnchor="middle"
            fontSize={size * 0.075} fontFamily="monospace" fill="rgba(255,255,255,0.2)">
        {max}
      </text>
    </svg>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Vertical tape
// ─────────────────────────────────────────────────────────────────────────────
export function VerticalTape({ value, unit, label, min = 0, max = 500, warnMax, color = C.accent }) {
  const W = 64, H = 160
  const pct = value == null ? 0 : Math.max(0, Math.min(1, (value - min) / (max - min)))
  const fillH = pct * (H - 20)
  const warnPct = warnMax != null ? (warnMax - min) / (max - min) : null

  return (
    <div style={{ display:'flex', flexDirection:'column', alignItems:'center', gap:2 }}>
      <span style={{ color:C.muted, fontSize:9, fontFamily:'monospace' }}>{label}</span>
      <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} style={{ display:'block' }}>
        <rect x={24} y={10} width={16} height={H-20} rx={3} fill={C.border} />
        {warnPct != null && (
          <line
            x1={22} y1={10 + (1-warnPct)*(H-20)}
            x2={42} y2={10 + (1-warnPct)*(H-20)}
            stroke={C.warn} strokeWidth={1} strokeDasharray="3,2"
          />
        )}
        <rect
          x={24} y={10 + (H-20) - fillH}
          width={16} height={fillH}
          rx={3} fill={color} opacity={0.85}
        />
        {[0,20,40,60,80,100].map(pctTick => {
          const v = min + (pctTick/100) * (max - min)
          const y = 10 + (H-20) * (1 - pctTick/100)
          return (
            <g key={pctTick}>
              <line x1={20} y1={y} x2={24} y2={y} stroke="rgba(255,255,255,0.3)" strokeWidth={1} />
              <text x={18} y={y+3} textAnchor="end" fontSize={7}
                    fontFamily="monospace" fill="rgba(255,255,255,0.3)">
                {Math.round(v)}
              </text>
            </g>
          )
        })}
        <polygon
          points={`${44},${10+(H-20)*(1-pct)} ${52},${10+(H-20)*(1-pct)-5} ${52},${10+(H-20)*(1-pct)+5}`}
          fill={color}
        />
      </svg>
      <span style={{
        fontFamily:'monospace', fontSize:12, fontWeight:700,
        color: (warnMax != null && value > warnMax) ? C.warn : color,
        textAlign:'center',
      }}>
        {value != null ? value.toFixed(0) : '—'}
      </span>
      <span style={{ color:C.muted, fontSize:9, fontFamily:'monospace' }}>{unit}</span>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Climb rate bar
// ─────────────────────────────────────────────────────────────────────────────
export function ClimbRateBar({ value }) {
  const H = 160, W = 36
  const MAX_RATE = 15
  const pct = value == null ? 0 : Math.max(-1, Math.min(1, value / MAX_RATE))
  const barH = Math.abs(pct) * ((H - 20) / 2)
  const barY = pct >= 0
    ? (H/2) - barH
    : (H/2)
  const color = value > 0 ? C.ok : value < 0 ? C.crit : C.muted

  return (
    <div style={{ display:'flex', flexDirection:'column', alignItems:'center', gap:2 }}>
      <span style={{ color:C.muted, fontSize:9, fontFamily:'monospace' }}>V/S</span>
      <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} style={{ display:'block' }}>
        <rect x={10} y={10} width={16} height={H-20} rx={3} fill={C.border} />
        <line x1={8} y1={H/2} x2={W-2} y2={H/2} stroke="rgba(255,255,255,0.3)" strokeWidth={1} />
        <rect x={10} y={barY} width={16} height={barH} rx={2} fill={color} opacity={0.85} />
        {[-10,-5,5,10].map(v => {
          const y = H/2 - (v/MAX_RATE) * ((H-20)/2)
          return (
            <g key={v}>
              <line x1={8} y1={y} x2={10} y2={y} stroke="rgba(255,255,255,0.3)" strokeWidth={1}/>
              <text x={7} y={y+3} textAnchor="end" fontSize={7}
                    fontFamily="monospace" fill="rgba(255,255,255,0.25)">{Math.abs(v)}</text>
            </g>
          )
        })}
      </svg>
      <span style={{
        fontFamily:'monospace', fontSize:12, fontWeight:700,
        color, textAlign:'center',
      }}>
        {value != null ? (value >= 0 ? '+' : '') + value.toFixed(1) : '—'}
      </span>
      <span style={{ color:C.muted, fontSize:9, fontFamily:'monospace' }}>m/s</span>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Stat row
// ─────────────────────────────────────────────────────────────────────────────
export function StatRow({ label, value, unit, warn, crit, decimals = 1 }) {
  const num = typeof value === 'number' ? value : null
  let color = C.text
  if (num != null && crit != null && num >= crit) color = C.crit
  else if (num != null && warn != null && num >= warn) color = C.warn
  return (
    <div style={S.statRow}>
      <span style={{ color:C.muted, fontSize:11, minWidth:90 }}>{label}</span>
      <span style={{ color, fontWeight:700, fontFamily:'monospace', fontSize:13 }}>
        {num != null ? num.toFixed(decimals) : '—'}
      </span>
      <span style={{ color:C.muted, fontSize:10 }}>{unit}</span>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Status pill
// ─────────────────────────────────────────────────────────────────────────────
export function StatusPill({ label, ok, trueLabel = 'OK', falseLabel = 'FAULT' }) {
  return (
    <div style={S.statRow}>
      <span style={{ color:C.muted, fontSize:11, minWidth:90 }}>{label}</span>
      <span style={{
        fontSize:10, fontWeight:700, fontFamily:'monospace',
        padding:'2px 8px', borderRadius:10,
        background: ok ? 'rgba(34,197,94,0.15)' : 'rgba(255,68,68,0.15)',
        color: ok ? C.ok : C.crit,
        border: `1px solid ${ok ? C.ok : C.crit}`,
      }}>
        {ok ? trueLabel : falseLabel}
      </span>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Card wrapper
// ─────────────────────────────────────────────────────────────────────────────
export function Card({ title, children, style }) {
  return (
    <div style={{ ...S.card, ...style }}>
      <div style={S.cardTitle}>{title}</div>
      {children}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Linear bar
// ─────────────────────────────────────────────────────────────────────────────
export function LinearBar({ value, min = 0, max, warn, label, unit, color = C.accent }) {
  const pct = value == null ? 0 : Math.max(0, Math.min(1, (value - min) / (max - min)))
  const warnPct = warn != null ? (warn - min) / (max - min) : null
  const isWarn = warn != null && value != null && value >= warn
  const barColor = isWarn ? C.warn : color
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display:'flex', justifyContent:'space-between', marginBottom:3 }}>
        <span style={{ color:C.muted, fontSize:11 }}>{label}</span>
        <span style={{ color:barColor, fontSize:12, fontWeight:700, fontFamily:'monospace' }}>
          {value != null ? value.toFixed(0) : '—'} <span style={{ color:C.muted, fontSize:9 }}>{unit}</span>
        </span>
      </div>
      <div style={{ position:'relative', height:6, borderRadius:3, background:C.border }}>
        <div style={{
          position:'absolute', left:0, top:0, bottom:0,
          width:`${pct*100}%`, borderRadius:3,
          background: barColor, transition:'width 0.3s',
        }} />
        {warnPct != null && (
          <div style={{
            position:'absolute', top:-2, bottom:-2,
            left:`${warnPct*100}%`, width:1,
            background: C.warn, opacity:0.6,
          }} />
        )}
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Timer display
// ─────────────────────────────────────────────────────────────────────────────
export function TimerDisplay({ label, value, color = C.accent, subtitle = null, fontSize = 22 }) {
  return (
    <div style={{ display:'flex', flexDirection:'column', alignItems:'center', gap:1 }}>
      <div style={{ color:C.muted, fontSize:9, letterSpacing:1.5, textTransform:'uppercase' }}>{label}</div>
      <div style={{ fontFamily:'monospace', fontSize, fontWeight:700, color, letterSpacing:1.5, lineHeight:1.1 }}>{value}</div>
      {subtitle && <div style={{ color:C.muted, fontSize:9 }}>{subtitle}</div>}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Checklist item
// ─────────────────────────────────────────────────────────────────────────────
export function CheckItem({ label, ok, detail = null }) {
  const color = ok === null ? C.muted : ok ? C.ok : C.crit
  const icon  = ok === null ? '○' : ok ? '●' : '●'
  return (
    <div style={{ display:'flex', alignItems:'center', gap:8, padding:'4px 0',
                  borderBottom:`1px solid ${C.border}` }}>
      <span style={{ color, fontSize:10, flexShrink:0 }}>{icon}</span>
      <span style={{ color: ok === null ? C.muted : C.text, fontSize:11,
                     fontFamily:'monospace', flex:1 }}>{label}</span>
      {detail != null && (
        <span style={{ color, fontSize:10, fontFamily:'monospace', flexShrink:0 }}>{detail}</span>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Flight stage strip
// ─────────────────────────────────────────────────────────────────────────────
export function FlightStageStrip({ stage, stageNames }) {
  const stages = Object.entries(stageNames).map(([k, v]) => ({ id: Number(k), name: v }))
  if (stages.length === 0) return null

  return (
    <div style={{ display:'flex', gap:2, alignItems:'center', flexWrap:'wrap' }}>
      {stages.map(({ id, name }) => {
        const active = id === stage
        const past   = id < stage
        return (
          <div key={id} style={{
            padding: '2px 8px',
            borderRadius: 10,
            fontSize: 9,
            fontFamily: 'monospace',
            fontWeight: active ? 700 : 400,
            letterSpacing: 0.5,
            background: active ? C.accent
                       : past   ? 'rgba(0,212,255,0.12)'
                                : 'transparent',
            color: active ? '#000'
                  : past   ? C.accent
                           : C.muted,
            border: active ? `1px solid ${C.accent}`
                   : past   ? `1px solid rgba(0,212,255,0.25)`
                            : `1px solid ${C.border}`,
            transition: 'all 0.3s',
          }}>
            {name}
          </div>
        )
      })}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Event log
// ─────────────────────────────────────────────────────────────────────────────
export function EventLog({ events, stageNames }) {
  const bottomRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [events.length])

  const isStageChange = (ev) => ev.field === 'flight_stage'

  return (
    <div style={{ flex:1, display:'flex', flexDirection:'column', minHeight:0, overflow:'hidden' }}>
      {events.length === 0 ? (
        <div style={{ flex:1, display:'flex', alignItems:'center', justifyContent:'center',
                      color:C.muted, fontSize:11, fontFamily:'monospace' }}>
          Waiting for events…
        </div>
      ) : (
        <div style={{
          flex: 1,
          overflowY: 'auto',
          display: 'flex',
          flexDirection: 'column',
          gap: 2,
          paddingRight: 2,
        }}>
          {[...events].reverse().map((ev, i) => (
            <div key={i} style={{
              display: 'flex',
              alignItems: 'flex-start',
              gap: 8,
              padding: '5px 6px',
              borderRadius: 4,
              background: isStageChange(ev)
                ? 'rgba(0,212,255,0.08)'
                : 'rgba(255,255,255,0.03)',
              borderLeft: isStageChange(ev)
                ? `2px solid ${C.accent}`
                : `2px solid rgba(255,255,255,0.08)`,
            }}>
              <span style={{
                color: C.muted, fontSize: 10, fontFamily: 'monospace',
                flexShrink: 0, paddingTop: 1, minWidth: 54,
              }}>
                {formatEventTime(ev.wall_ms)}
              </span>
              <span style={{ fontSize: 12, flexShrink: 0, paddingTop: 1 }}>
                {EVENT_ICONS[ev.field] ?? '●'}
              </span>
              <span style={{
                color: isStageChange(ev) ? C.accent : C.text,
                fontSize: 11,
                fontFamily: 'monospace',
                fontWeight: isStageChange(ev) ? 700 : 400,
                lineHeight: 1.4,
              }}>
                {ev.message}
              </span>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>
      )}
    </div>
  )
}
