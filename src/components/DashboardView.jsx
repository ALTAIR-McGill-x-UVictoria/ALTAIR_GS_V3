/**
 * DashboardView — high-level flight dashboard
 *
 * Displays the most operationally relevant telemetry using visual instruments:
 *   • Artificial horizon (roll + pitch)
 *   • Compass rose (heading)
 *   • Altitude tape + climb rate bar
 *   • Airspeed / ground speed
 *   • Power gauges (voltage arc, current, temperature)
 *   • VESC motor status
 *   • System health (CPU, memory, Pixhawk link, uptime)
 *
 * All values are derived directly from the `packets` and `alarms` props passed
 * in from App.jsx — no extra data fetching.
 */

import { useMemo, useEffect, useRef, useState } from 'react'

// ─── helpers ────────────────────────────────────────────────────────────────

/** Extract a named field value from a packet object (as returned by useTelemetry) */
function fv(packet, fieldName, fallback = null) {
  if (!packet) return fallback
  return packet.fields.find(f => f.name === fieldName)?.value ?? fallback
}

/** Find a packet by case-insensitive label match */
function findPacket(packets, labelLC) {
  const entry = Object.entries(packets).find(([k]) => k.toLowerCase() === labelLC)
  return entry?.[1] ?? null
}

/** Format seconds → h mm ss */
function formatUptime(s) {
  if (s == null) return '—'
  const h  = Math.floor(s / 3600)
  const m  = Math.floor((s % 3600) / 60)
  const ss = Math.floor(s % 60)
  if (h > 0) return `${h}h ${String(m).padStart(2,'0')}m`
  return `${String(m).padStart(2,'0')}m ${String(ss).padStart(2,'0')}s`
}

// ─── colour palette ─────────────────────────────────────────────────────────
const C = {
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

// ─────────────────────────────────────────────────────────────────────────────
// Artificial Horizon
// ─────────────────────────────────────────────────────────────────────────────
function ArtificialHorizon({ roll = 0, pitch = 0 }) {
  const W = 200, H = 200, R = 96
  // pitch offset: 1° ≈ 2px
  const pitchPx = pitch * (180 / Math.PI) * 2
  const rollDeg = roll * (180 / Math.PI)

  const clipId = 'ah-clip'
  const rollTicks = [-60,-45,-30,-20,-10,0,10,20,30,45,60]

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ display:'block', width:'100%', maxWidth:200, maxHeight:200, margin:'0 auto' }}>
      <defs>
        <clipPath id={clipId}>
          <circle cx={W/2} cy={H/2} r={R} />
        </clipPath>
      </defs>

      {/* Rotating sky+earth group */}
      <g clipPath={`url(#${clipId})`}
         transform={`rotate(${-rollDeg} ${W/2} ${H/2})`}>
        {/* Sky */}
        <rect x={0} y={0} width={W} height={H/2 + pitchPx} fill={C.sky} />
        {/* Earth */}
        <rect x={0} y={H/2 + pitchPx} width={W} height={H} fill={C.earth} />
        {/* Horizon line */}
        <line
          x1={0} y1={H/2 + pitchPx}
          x2={W} y2={H/2 + pitchPx}
          stroke={C.horizon} strokeWidth={1.5}
        />
        {/* Pitch ladder — lines every 5° */}
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

      {/* Fixed roll arc + ticks */}
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

      {/* Fixed aircraft symbol */}
      <g stroke={C.warn} strokeWidth={2} fill="none">
        {/* Left wing */}
        <line x1={W/2 - 40} y1={H/2} x2={W/2 - 12} y2={H/2} />
        <line x1={W/2 - 12} y1={H/2} x2={W/2 - 12} y2={H/2 + 6} />
        {/* Right wing */}
        <line x1={W/2 + 12} y1={H/2} x2={W/2 + 40} y2={H/2} />
        <line x1={W/2 + 12} y1={H/2} x2={W/2 + 12} y2={H/2 + 6} />
        {/* Centre dot */}
        <circle cx={W/2} cy={H/2} r={3} fill={C.warn} stroke="none" />
      </g>

      {/* Roll pointer triangle (rotates with roll) */}
      <g transform={`rotate(${-rollDeg} ${W/2} ${H/2})`}>
        <polygon
          points={`${W/2},${H/2-R+2} ${W/2-5},${H/2-R+11} ${W/2+5},${H/2-R+11}`}
          fill={C.accent} stroke="none"
        />
      </g>

      {/* Labels */}
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
// needle: fixed pointer from centre toward a given absolute bearing (degrees)
function CompassNeedle({ cx, cy, bearing, length, color, width = 1.5, tailLength = 0 }) {
  const rad = (bearing - 90) * Math.PI / 180
  const tx = cx + length * Math.cos(rad)
  const ty = cy + length * Math.sin(rad)
  const bx = cx - tailLength * Math.cos(rad)
  const by = cy - tailLength * Math.sin(rad)
  return <line x1={bx} y1={by} x2={tx} y2={ty} stroke={color} strokeWidth={width} strokeLinecap="round" />
}

function Compass({ yawDeg = null, gpsDeg = null }) {
  const W = 160, H = 160, R = 70, cx = W/2, cy = H/2
  const cardinals = ['N','NE','E','SE','S','SW','W','NW']

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ display:'block', width:'100%', maxWidth:200, maxHeight:200, margin:'0 auto' }}>
      {/* Fixed disc */}
      <circle cx={cx} cy={cy} r={R} fill={C.card} stroke={C.border} strokeWidth={1.5} />
      {/* Degree ticks every 10° — fixed, N at top */}
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
      {/* Cardinal labels — fixed */}
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

      {/* GPS heading needle — white, longer */}
      {gpsDeg != null && (
        <CompassNeedle cx={cx} cy={cy} bearing={gpsDeg}  length={R-8} color={C.text}   width={2}   tailLength={12} />
      )}

      {/* Yaw heading needle — yellow, shorter */}
      {yawDeg != null && (
        <CompassNeedle cx={cx} cy={cy} bearing={yawDeg} length={R-18} color={C.warn} width={1.5} tailLength={8} />
      )}

      {/* Centre dot */}
      <circle cx={cx} cy={cy} r={3} fill={C.card} stroke={C.border} strokeWidth={1} />

      {/* Readout: GPS hdg left, yaw right */}
      <text x={cx} y={cy+6} textAnchor="middle"
            fontSize={15} fontWeight={700} fontFamily="monospace" fill={C.text}>
        {String(Math.round(gpsDeg ?? yawDeg ?? 0)).padStart(3,'0')}°
      </text>

      {/* Legend */}
      <text x={6} y={H-5} fontSize={7} fontFamily="monospace" fill={C.text}>━ GPS</text>
      <text x={6} y={H-14} fontSize={7} fontFamily="monospace" fill={C.warn}>━ YAW</text>
    </svg>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Arc gauge (voltage, temperature, etc.)
// ─────────────────────────────────────────────────────────────────────────────
function ArcGauge({ value, min, max, warnLo, warnHi, unit, label, size = 120 }) {
  const r = size * 0.38
  const cx = size / 2, cy = size / 2
  // Arc spans from -225° to 45° (270° sweep), starting bottom-left
  const START_DEG = 225, SWEEP = 270
  const pct = value == null ? 0 : Math.max(0, Math.min(1, (value - min) / (max - min)))
  const valueDeg = START_DEG - pct * SWEEP

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

  // Background arc (full range)
  const bgPath = arcPath(180 + START_DEG - SWEEP, 180 + START_DEG - SWEEP + SWEEP, r)

  // Value fill color
  let fillColor = C.ok
  if (value != null) {
    if ((warnHi != null && value >= warnHi) || (warnLo != null && value <= warnLo)) fillColor = C.warn
    if ((max != null && value >= max)       || (min != null && value <= min))       fillColor = C.crit
  }

  // Needle tip
  const needleRad = (180 + START_DEG - pct * SWEEP) * Math.PI / 180
  const needleTip = { x: cx + (r - 4) * Math.cos(needleRad), y: cy + (r - 4) * Math.sin(needleRad) }

  return (
    <svg width={size} height={size * 0.82} style={{ display:'block', overflow:'visible' }}>
      {/* Track */}
      <path d={bgPath} fill="none" stroke={C.border} strokeWidth={6} strokeLinecap="round" />
      {/* Value arc */}
      <path
        d={arcPath(180 + START_DEG - SWEEP, 180 + START_DEG - pct * SWEEP + 0.01, r)}
        fill="none" stroke={fillColor} strokeWidth={6} strokeLinecap="round"
      />
      {/* Needle */}
      <line
        x1={cx} y1={cy}
        x2={needleTip.x} y2={needleTip.y}
        stroke={fillColor} strokeWidth={1.5}
      />
      <circle cx={cx} cy={cy} r={4} fill={C.card} stroke={fillColor} strokeWidth={1.5} />
      {/* Value text */}
      <text x={cx} y={cy + (size * 0.18)} textAnchor="middle"
            fontSize={size * 0.145} fontWeight={700} fontFamily="monospace"
            fill={fillColor}>
        {value != null ? value.toFixed(1) : '—'}
      </text>
      {/* Unit */}
      <text x={cx} y={cy + (size * 0.30)} textAnchor="middle"
            fontSize={size * 0.09} fontFamily="monospace" fill={C.muted}>
        {unit}
      </text>
      {/* Label */}
      <text x={cx} y={size * 0.80} textAnchor="middle"
            fontSize={size * 0.09} fontFamily="monospace" fill={C.muted}>
        {label}
      </text>
      {/* Min/max */}
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
// Vertical tape (altitude or airspeed)
// ─────────────────────────────────────────────────────────────────────────────
function VerticalTape({ value, unit, label, min = 0, max = 500, warnMax, color = C.accent }) {
  const W = 64, H = 160
  const pct = value == null ? 0 : Math.max(0, Math.min(1, (value - min) / (max - min)))
  const fillH = pct * (H - 20)
  const warnPct = warnMax != null ? (warnMax - min) / (max - min) : null

  return (
    <div style={{ display:'flex', flexDirection:'column', alignItems:'center', gap:4 }}>
      <span style={{ color:C.muted, fontSize:10, fontFamily:'monospace' }}>{label}</span>
      <svg width={W} height={H}>
        {/* Track */}
        <rect x={24} y={10} width={16} height={H-20} rx={3} fill={C.border} />
        {/* Warn line */}
        {warnPct != null && (
          <line
            x1={22} y1={10 + (1-warnPct)*(H-20)}
            x2={42} y2={10 + (1-warnPct)*(H-20)}
            stroke={C.warn} strokeWidth={1} strokeDasharray="3,2"
          />
        )}
        {/* Fill */}
        <rect
          x={24} y={10 + (H-20) - fillH}
          width={16} height={fillH}
          rx={3} fill={color} opacity={0.85}
        />
        {/* Tick marks every 20% */}
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
        {/* Current value pointer */}
        <polygon
          points={`${44},${10+(H-20)*(1-pct)} ${52},${10+(H-20)*(1-pct)-5} ${52},${10+(H-20)*(1-pct)+5}`}
          fill={color}
        />
      </svg>
      <span style={{
        fontFamily:'monospace', fontSize:15, fontWeight:700,
        color: (warnMax != null && value > warnMax) ? C.warn : color,
        minWidth: 56, textAlign:'center',
      }}>
        {value != null ? value.toFixed(1) : '—'}
      </span>
      <span style={{ color:C.muted, fontSize:9, fontFamily:'monospace' }}>{unit}</span>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Climb rate bar (vertical, centred, bidirectional)
// ─────────────────────────────────────────────────────────────────────────────
function ClimbRateBar({ value }) {
  const H = 160, W = 36
  const MAX_RATE = 15  // m/s display range
  const pct = value == null ? 0 : Math.max(-1, Math.min(1, value / MAX_RATE))
  const barH = Math.abs(pct) * ((H - 20) / 2)
  const barY = pct >= 0
    ? (H/2) - barH
    : (H/2)
  const color = value > 0 ? C.ok : value < 0 ? C.crit : C.muted

  return (
    <div style={{ display:'flex', flexDirection:'column', alignItems:'center', gap:4 }}>
      <span style={{ color:C.muted, fontSize:10, fontFamily:'monospace' }}>V/S</span>
      <svg width={W} height={H}>
        {/* Track */}
        <rect x={10} y={10} width={16} height={H-20} rx={3} fill={C.border} />
        {/* Zero line */}
        <line x1={8} y1={H/2} x2={W-2} y2={H/2} stroke="rgba(255,255,255,0.3)" strokeWidth={1} />
        {/* Fill */}
        <rect x={10} y={barY} width={16} height={barH} rx={2} fill={color} opacity={0.85} />
        {/* Tick ±5, ±10 */}
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
        fontFamily:'monospace', fontSize:13, fontWeight:700,
        color, minWidth:36, textAlign:'center',
      }}>
        {value != null ? (value >= 0 ? '+' : '') + value.toFixed(1) : '—'}
      </span>
      <span style={{ color:C.muted, fontSize:9, fontFamily:'monospace' }}>m/s</span>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Stat row — simple labelled value
// ─────────────────────────────────────────────────────────────────────────────
function StatRow({ label, value, unit, warn, crit, decimals = 1 }) {
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
function StatusPill({ label, ok, trueLabel = 'OK', falseLabel = 'FAULT' }) {
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
function Card({ title, children, style }) {
  return (
    <div style={{ ...S.card, ...style }}>
      <div style={S.cardTitle}>{title}</div>
      {children}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Linear bar (e.g. RPM, current)
// ─────────────────────────────────────────────────────────────────────────────
function LinearBar({ value, min = 0, max, warn, label, unit, color = C.accent }) {
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
// Main dashboard
// ─────────────────────────────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────────
// Event Log
// ─────────────────────────────────────────────────────────────────────────────

/** Icons for known event fields */
const EVENT_ICONS = {
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

function formatEventTime(wall_ms) {
  const d = new Date(wall_ms)
  const h = String(d.getHours()).padStart(2, '0')
  const m = String(d.getMinutes()).padStart(2, '0')
  const s = String(d.getSeconds()).padStart(2, '0')
  return `${h}:${m}:${s}`
}

function EventLog({ events, stageNames }) {
  const bottomRef = useRef(null)

  // Auto-scroll to bottom when new events arrive
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
          {/* Oldest at top, newest at bottom */}
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
              {/* Timestamp */}
              <span style={{
                color: C.muted, fontSize: 10, fontFamily: 'monospace',
                flexShrink: 0, paddingTop: 1, minWidth: 54,
              }}>
                {formatEventTime(ev.wall_ms)}
              </span>
              {/* Icon */}
              <span style={{ fontSize: 12, flexShrink: 0, paddingTop: 1 }}>
                {EVENT_ICONS[ev.field] ?? '●'}
              </span>
              {/* Message */}
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

// ─────────────────────────────────────────────────────────────────────────────
// Flight stage indicator strip
// ─────────────────────────────────────────────────────────────────────────────
function FlightStageStrip({ stage, stageNames }) {
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
// Timers card
// ─────────────────────────────────────────────────────────────────────────────
function formatDuration(ms) {
  if (ms == null || ms < 0) return '—'
  const s = Math.floor(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const ss = s % 60
  if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`
  return `${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`
}

function formatEta(ms) {
  // Like formatDuration but allows negative (overdue) values, prefixed with −
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

function TimerDisplay({ label, value, color = C.accent, subtitle = null }) {
  return (
    <div style={{ display:'flex', flexDirection:'column', alignItems:'center', gap:2 }}>
      <div style={{ color:C.muted, fontSize:9, letterSpacing:1.5, textTransform:'uppercase' }}>{label}</div>
      <div style={{ fontFamily:'monospace', fontSize:28, fontWeight:700, color, letterSpacing:2 }}>{value}</div>
      {subtitle && <div style={{ color:C.muted, fontSize:9 }}>{subtitle}</div>}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Pre-flight checklist card
// ─────────────────────────────────────────────────────────────────────────────
function CheckItem({ label, ok, detail = null }) {
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

// ACK command IDs (must match FC telemetry/command_registry IDs)
const CMD_ARM       = 0xC0
const CMD_LAUNCH_OK = 0xC1
const CMD_PING      = 0xC2

export default function DashboardView({ packets, events = [], stageNames = {}, lastAck = null }) {
  const att  = findPacket(packets, 'attitude')
  const gps  = findPacket(packets, 'gps')
  const pwr  = findPacket(packets, 'power')
  const vesc = findPacket(packets, 'vesc')
  const hb   = findPacket(packets, 'heartbeat')
  const env  = findPacket(packets, 'environment')
  const evpkt = findPacket(packets, 'event')

  const roll    = fv(att,  'roll',    0)
  const pitch   = fv(att,  'pitch',   0)
  const yaw     = fv(att,  'yaw',     0)

  const altMSL  = fv(gps,  'alt',         null)
  const altAGL  = fv(gps,  'relative_alt', null)
  const hdg     = fv(gps,  'hdg',          0)

  const climb       = fv(env, 'climb',       null)
  const airspeed    = fv(env, 'airspeed',    null)
  const groundspeed = fv(env, 'groundspeed', null)
  const baroAlt     = fv(env, 'baro_alt',    null)
  const envTemp     = fv(env, 'temperature', null)

  const voltage = fv(pwr,  'voltage_bus',   null)
  const current = fv(pwr,  'current_total', null)
  const pwrTemp = fv(pwr,  'temperature',   null)

  const rpm        = fv(vesc, 'rpm',             null)
  const motorTemp  = fv(vesc, 'temperature_mos', null)
  const vescVoltage= fv(vesc, 'input_voltage',   null)
  const motorCur   = fv(vesc, 'motor_current',   null)

  const cpu     = fv(hb,   'cpu_load_pct',      null)
  const mem     = fv(hb,   'mem_used_pct',       null)
  const uptime  = fv(hb,   'uptime_s',           null)
  const pixConn        = fv(hb, 'pixhawk_connected',    null)
  const vescConn       = fv(hb, 'vesc_connected',       null)
  const powerConn      = fv(hb, 'power_connected',      null)
  const photodiodeConn = fv(hb, 'photodiode_connected', null)

  const flightStage  = fv(evpkt, 'flight_stage', null)

  // ── Timers ────────────────────────────────────────────────────────────────
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  // Find the wall_ms of the launch event (flight_stage transition to stage 2)
  // events array is newest-first; use findLast-equivalent to get the earliest occurrence
  const launchEvent  = useMemo(() => [...events].reverse().find(e => e.field === 'flight_stage' && e.new_val === 2), [events])
  const descentEvent = useMemo(() => [...events].reverse().find(e => e.field === 'flight_stage' && e.new_val === 6), [events])
  const landingEvent = useMemo(() => [...events].reverse().find(e => e.field === 'flight_stage' && e.new_val === 7), [events])

  // Stop counting at landing; persist the final elapsed time after that
  const launchElapsed = launchEvent
    ? (landingEvent ? landingEvent.wall_ms - launchEvent.wall_ms : now - launchEvent.wall_ms)
    : null
  // ETA to landing: descent duration ≈ ascent duration × 3 (descends 3× slower)
  // Estimate landing as 3× the ascent duration from launch
  const ascentDurMs   = descentEvent && launchEvent ? descentEvent.wall_ms - launchEvent.wall_ms : null
  const landingEtaMs  = descentEvent && ascentDurMs
    ? (descentEvent.wall_ms + ascentDurMs * 3) - now
    : null
  const inDescent     = flightStage != null && flightStage === 6

  // ── ETA Termination / Burst ───────────────────────────────────────────────
  // Altitudes match FC defaults in settings.toml
  const TERM_ALT  = 25000   // m MSL
  const BURST_ALT = 30000   // m MSL

  const cutdownFired     = fv(evpkt, 'cutdown_fired',     0) === 1
  const terminationFired = fv(evpkt, 'termination_fired', 0) === 1
  const burstDetected    = fv(evpkt, 'burst_detected',    0) === 1
  const isRecovery       = flightStage != null && flightStage >= 8

  // ETA in ms from now to reaching an altitude at current climb rate.
  // Positive = future, negative = overdue (already past that altitude).
  // Returns null when we have no data or are already past recovery.
  // requireAscent: if true, return null when climb ≤ 0 (used for termination —
  //   no point counting down if not climbing). For burst we allow climb ≤ 0 so
  //   the counter goes negative through apogee instead of disappearing.
  // Minimum meaningful climb rate — below this the balloon isn't ascending
  const MIN_CLIMB_MS = 0.5  // m/s

  function etaToAlt(targetAlt, requireAscent = true) {
    if (isRecovery) return null
    if (baroAlt == null || climb == null) return null
    if (requireAscent && climb < MIN_CLIMB_MS) return null
    // Not climbing meaningfully: if already at/above target, overdue; else can't predict
    if (climb < MIN_CLIMB_MS) return baroAlt >= targetAlt ? 0 : null
    const secondsToAlt = (targetAlt - baroAlt) / climb
    return secondsToAlt * 1000  // ms, may be negative
  }

  const etaTermMs  = etaToAlt(TERM_ALT, true)
  const etaBurstMs = etaToAlt(BURST_ALT, false)

  // ── Link latency ─────────────────────────────────────────────────────────
  // FC embeds time_unix (wall-clock seconds) in the heartbeat payload.
  // hb.wall_ms is when the GS received that packet. Difference = RF + processing delay.
  // A rolling 10-sample average smooths out jitter.
  const fcTimeUnix = fv(hb, 'time_unix', null)
  const _latencyBuf = useRef([])
  const rawLatencyMs = (hb?.wall_ms != null && fcTimeUnix != null)
    ? Math.max(0, hb.wall_ms - fcTimeUnix * 1000)
    : null
  const linkLatencyMs = useMemo(() => {
    if (rawLatencyMs == null) return null
    const buf = _latencyBuf.current
    buf.push(rawLatencyMs)
    if (buf.length > 10) buf.shift()
    return Math.round(buf.reduce((a, b) => a + b, 0) / buf.length)
  }, [rawLatencyMs])

  // ── Pre-flight checklist ─────────────────────────────────────────────────
  const gpsFix        = gps != null && fv(gps, 'lat', 0) !== 0
  const pixhawkOk     = pixConn        != null && pixConn        > 0.5
  const vescOk        = vescConn       != null && vescConn       > 0.5
  const powerOk       = powerConn      != null && powerConn      > 0.5
  const photodiodeOk  = photodiodeConn != null && photodiodeConn > 0.5
  const dataLogging   = fv(evpkt, 'data_logging_active', 0) === 1
  const armState      = fv(evpkt, 'arm_state', 0) === 1
  const gpsHdop       = fv(gps, 'eph', null)  // horizontal dilution of precision

  // ── Command log ───────────────────────────────────────────────────────────
  // Each entry: { seq, wall_ms, label, cmd_id, status: 'sent'|'ack'|'nack', rtt_ms }
  const [cmdLog, setCmdLog] = useState([])
  const cmdSeqRef = useRef(0)  // monotonic counter to match sent→ack pairs

  const CMD_LABELS = { [CMD_ARM]: 'ARM', [CMD_LAUNCH_OK]: 'LAUNCH OK', [CMD_PING]: 'PING' }
  const CMD_LOG_MAX = 20

  const logCommandSent = (cmd_id) => {
    const seq = cmdSeqRef.current++
    const entry = { seq, wall_ms: Date.now(), label: CMD_LABELS[cmd_id] ?? `0x${cmd_id.toString(16)}`, cmd_id, status: 'sent', rtt_ms: null }
    setCmdLog(prev => [entry, ...prev].slice(0, CMD_LOG_MAX))
    return seq
  }

  const logCommandAck = (cmd_id, status, ack_wall_ms) => {
    setCmdLog(prev => {
      const idx = prev.findIndex(e => e.cmd_id === cmd_id && e.status === 'sent')
      const ackStatus = status === 0 ? 'ack' : 'nack'
      if (idx !== -1) {
        // Update the matching 'sent' entry in-place
        const updated = [...prev]
        updated[idx] = { ...updated[idx], status: ackStatus, rtt_ms: ack_wall_ms - updated[idx].wall_ms }
        return updated
      }
      // No matching sent entry (race condition) — append ACK as a standalone row
      const entry = { seq: -1, wall_ms: ack_wall_ms, label: CMD_LABELS[cmd_id] ?? `0x${cmd_id.toString(16)}`, cmd_id, status: ackStatus, rtt_ms: null }
      return [entry, ...prev].slice(0, CMD_LOG_MAX)
    })
  }

  useEffect(() => {
    if (!lastAck) return
    logCommandAck(lastAck.cmd_id, lastAck.status, lastAck.wall_ms)
  }, [lastAck])

  // No data at all?
  const hasAny = att || gps || pwr || vesc || hb || env

  if (!hasAny) {
    return (
      <div style={{ flex:1, display:'flex', alignItems:'center', justifyContent:'center',
                    color:C.muted, fontFamily:'monospace', fontSize:13 }}>
        Waiting for telemetry…
      </div>
    )
  }

  return (
    <div style={S.root}>

      {/* ── Row 1: Flight instruments ─────────────────────────────────── */}
      <div style={S.row}>

        {/* Attitude */}
        <Card title="ATTITUDE" style={{ flex:'1 1 0' }}>
          <div style={{ flex:1, display:'flex', alignItems:'center', justifyContent:'center', minHeight:0 }}>
            <ArtificialHorizon roll={roll} pitch={pitch} />
          </div>
        </Card>

        {/* Compass */}
        <Card title="HEADING" style={{ flex:'1 1 0' }}>
          <div style={{ flex:1, display:'flex', alignItems:'center', justifyContent:'center', minHeight:0 }}>
            <Compass
              gpsDeg={hdg}
              yawDeg={(yaw * 180 / Math.PI + 360) % 360}
            />
          </div>
        </Card>

        {/* Altitude + climb */}
        <Card title="ALTITUDE / CLIMB" style={{ flex:'1 1 0' }}>
          <div style={{ flex:1, display:'flex', gap:16, alignItems:'center', justifyContent:'center', minHeight:0 }}>
            <VerticalTape
              label="ALT MSL" value={altMSL ?? baroAlt} unit="m"
              min={0} max={35000} warnMax={30000}
            />
            <VerticalTape
              label="ALT AGL" value={altAGL} unit="m"
              min={0} max={35000} warnMax={30000}
              color="#22c55e"
            />
            <ClimbRateBar value={climb} />
          </div>
        </Card>

        {/* Speed */}
        <Card title="SPEED" style={{ flex:'1 1 0' }}>
          <div style={{ flex:1, display:'flex', flexDirection:'column', justifyContent:'center', gap:12, paddingTop:4 }}>
            <div style={{ textAlign:'center' }}>
              <div style={{ color:C.muted, fontSize:10, marginBottom:2 }}>AIRSPEED</div>
              <div style={{ fontFamily:'monospace', fontSize:36, fontWeight:700, color:C.accent }}>
                {airspeed != null ? airspeed.toFixed(1) : '—'}
              </div>
              <div style={{ color:C.muted, fontSize:10 }}>m/s</div>
            </div>
            <div style={{ borderTop:`1px solid ${C.border}`, paddingTop:10 }}>
              <StatRow label="Groundspeed" value={groundspeed} unit="m/s" decimals={1} />
            </div>
          </div>
        </Card>

      </div>

      {/* ── Row 2: Systems ───────────────────────────────────────────────── */}
      <div style={S.row}>

        {/* Power */}
        <Card title="POWER" style={{ flex:'2 1 0' }}>
          <div style={{ flex:1, display:'grid', gridTemplateColumns:'1fr 1fr', gridTemplateRows:'1fr 1fr', gap:4, alignItems:'center', justifyItems:'center' }}>
            <ArcGauge
              label="Voltage" value={voltage} unit="V"
              min={9} max={14} warnLo={10.9} warnHi={12.9} size={100}
            />
            <ArcGauge
              label="Current" value={current} unit="A"
              min={0} max={15} warnHi={12} size={100}
            />
            <ArcGauge
              label="Temp" value={pwrTemp} unit="°C"
              min={0} max={80} warnHi={60} size={100}
            />
            <ArcGauge
              label="Power"
              value={voltage != null && current != null ? voltage * current : null}
              unit="W"
              min={0} max={200} warnHi={150} size={100}
            />
          </div>
        </Card>

        {/* VESC */}
        <Card title="VESC / MOTOR" style={{ flex:'2 1 0' }}>
          <div style={{ flex:1, display:'flex', flexDirection:'column', justifyContent:'center', gap:4 }}>
            <LinearBar label="RPM"         value={rpm}         min={0} max={5000} warn={4500} unit="rpm" />
            <LinearBar label="Motor Curr." value={motorCur}    min={0} max={60}   warn={50}   unit="A"   color="#a78bfa" />
            <LinearBar label="Voltage"     value={vescVoltage} min={9} max={14}   warn={12.9} unit="V"   color="#22c55e" />
            <StatRow   label="MOSFET Temp" value={motorTemp}   unit="°C" warn={75} crit={85} />
          </div>
        </Card>

        {/* Environment */}
        <Card title="ENVIRONMENT" style={{ flex:'1 1 0' }}>
          <div style={{ flex:1, display:'flex', flexDirection:'column', justifyContent:'center', gap:6 }}>
            <StatRow label="Temperature"  value={envTemp}     unit="°C" />
            <StatRow label="Baro Alt"     value={baroAlt}     unit="m"  />
            <StatRow label="Airspeed"     value={airspeed}    unit="m/s" />
            <StatRow label="Groundspeed"  value={groundspeed} unit="m/s" />
          </div>
        </Card>

        {/* System health */}
        <Card title="SYSTEM HEALTH" style={{ flex:'2 1 0' }}>
          <div style={{ flex:1, display:'flex', flexDirection:'column', justifyContent:'center', gap:4 }}>
            <LinearBar label="CPU Load"  value={cpu} min={0} max={100} warn={80} unit="%" color={C.accent} />
            <LinearBar label="Memory"    value={mem} min={0} max={100} warn={80} unit="%" color="#a78bfa" />
            <div style={{ marginTop:8, display:'flex', flexDirection:'column', gap:8 }}>
              <StatusPill label="Pixhawk Link" ok={pixConn > 0.5} trueLabel="CONNECTED" falseLabel="NO LINK" />
              <div style={S.statRow}>
                <span style={{ color:C.muted, fontSize:11, minWidth:90 }}>Uptime</span>
                <span style={{ color:C.text, fontWeight:700, fontFamily:'monospace', fontSize:12 }}>
                  {formatUptime(uptime)}
                </span>
              </div>
            </div>
          </div>
        </Card>

      </div>

      {/* ── Row 3: Timers + Pre-flight checklist ──────────────────────── */}
      <div style={{ ...S.row, flex: '0 0 180px', minHeight: 0, overflow: 'hidden' }}>

        {/* Timers */}
        <Card title="MISSION TIMERS" style={{ flex:'1 1 0' }}>
          <div style={{ flex:1, display:'flex', flexDirection:'column', gap:8 }}>
            <div style={{ display:'flex', alignItems:'center', justifyContent:'space-evenly', gap:12, flexWrap:'wrap', flex:1 }}>
              <TimerDisplay
                label="Launch T+"
                value={launchEvent ? formatDuration(launchElapsed) : '—'}
                color={launchEvent ? C.accent : C.muted}
                subtitle={launchEvent ? null : 'waiting for launch'}
              />
              <TimerDisplay
                label="Flight Stage"
                value={flightStage != null ? (stageNames[flightStage] ?? `Stage ${flightStage}`) : '—'}
                color={C.accent}
              />
              {inDescent && (
                <TimerDisplay
                  label="ETA Landing"
                  value={landingEtaMs != null && landingEtaMs > 0 ? formatDuration(landingEtaMs) : '—'}
                  color={C.warn}
                  subtitle="estimated"
                />
              )}
            </div>

            {/* Termination / Burst ETA row */}
            <div style={{ display:'flex', gap:8, borderTop:`1px solid ${C.border}`, paddingTop:6 }}>
              {/* Termination ETA */}
              <div style={{ flex:1, display:'flex', flexDirection:'column', gap:1 }}>
                <span style={{ color:C.muted, fontSize:9, fontFamily:'monospace', letterSpacing:1 }}>
                  ETA TERMINATION
                </span>
                <span style={{
                  fontFamily: 'monospace', fontSize:15, fontWeight:700,
                  color: terminationFired                        ? C.ok
                       : cutdownFired && burstDetected           ? C.crit   // cutdown engaged but mechanism failed
                       : !cutdownFired && burstDetected          ? C.crit   // burst without cutdown attempt
                       : cutdownFired && !terminationFired       ? C.warn   // cutdown engaged, awaiting confirmation
                       : flightStage >= 4                        ? C.muted
                       : etaTermMs == null                       ? C.muted
                       : etaTermMs < 0                           ? C.warn
                       : C.accent,
                  letterSpacing: 1,
                }}>
                  {terminationFired
                    ? 'FIRED'
                    : cutdownFired && burstDetected
                      ? 'FAILED'
                    : !cutdownFired && burstDetected
                      ? 'FAIL'
                    : cutdownFired && !terminationFired
                      ? 'ENGAGED'
                    : flightStage >= 4
                      ? '--:--'
                      : etaTermMs == null ? '—' : formatEta(etaTermMs)}
                </span>
              </div>

              <div style={{ width:1, background:C.border, flexShrink:0 }} />

              {/* Burst ETA */}
              <div style={{ flex:1, display:'flex', flexDirection:'column', gap:1 }}>
                <span style={{ color:C.muted, fontSize:9, fontFamily:'monospace', letterSpacing:1 }}>
                  ETA BURST
                </span>
                <span style={{
                  fontFamily: 'monospace', fontSize:15, fontWeight:700,
                  color: isRecovery          ? C.muted
                       : burstDetected       ? C.ok
                       : etaBurstMs == null  ? C.muted
                       : etaBurstMs < 0      ? C.crit
                       : C.accent,
                  letterSpacing: 1,
                }}>
                  {isRecovery
                    ? '--:--'
                    : etaBurstMs == null ? '—' : formatEta(etaBurstMs)}
                </span>
              </div>
            </div>
            <div style={{ borderTop:`1px solid ${C.border}`, paddingTop:6, display:'flex', alignItems:'center', gap:8 }}>
              <span style={{ color:C.muted, fontSize:10, fontFamily:'monospace', flex:1 }}>Link latency</span>
              <span style={{
                fontFamily:'monospace', fontSize:13, fontWeight:700,
                color: linkLatencyMs == null ? C.muted
                     : linkLatencyMs > 2000  ? C.crit
                     : linkLatencyMs > 500   ? C.warn
                     : C.ok,
              }}>
                {linkLatencyMs == null ? '—' : `${linkLatencyMs} ms`}
              </span>
            </div>
          </div>
        </Card>

        {/* Pre-flight checklist */}
        <Card title="PRE-FLIGHT CHECKLIST" style={{ flex:'1 1 0' }}>
          <div style={{ flex:1, display:'flex', flexDirection:'column', justifyContent:'center' }}>
            <CheckItem
              label="GPS Fix"
              ok={gps ? gpsFix : null}
              detail={gpsFix && gpsHdop != null ? `HDOP ${gpsHdop.toFixed(1)}` : null}
            />
            <CheckItem
              label="Pixhawk Connected"
              ok={hb ? pixhawkOk : null}
            />
            <CheckItem
              label="VESC Connected"
              ok={hb ? vescOk : null}
            />
            <CheckItem
              label="Power Board"
              ok={hb ? powerOk : null}
            />
            <CheckItem
              label="Photodiode Board"
              ok={hb ? photodiodeOk : null}
            />
            <CheckItem
              label="Data Logging Active"
              ok={evpkt ? dataLogging : null}
            />
            <CheckItem
              label="Arm State"
              ok={evpkt ? armState : null}
              detail={armState ? 'ARMED' : evpkt ? 'DISARMED' : null}
            />
          </div>
        </Card>

        {/* Commands + log */}
        {(() => {
          const allOk    = gpsFix && pixhawkOk && vescOk && powerOk && photodiodeOk && dataLogging
          const canArm   = allOk
          const canLaunch = allOk && armState
          return (
            <Card title="COMMANDS" style={{ flex:'1 1 0' }}>
              <div style={{ flex:1, display:'flex', flexDirection:'row', gap:12, overflow:'hidden' }}>

                {/* Buttons */}
                <div style={{ display:'flex', flexDirection:'column', gap:8, justifyContent:'center' }}>
                  <button
                    disabled={!canArm}
                    style={{
                      fontFamily:'monospace', fontSize:12, fontWeight:700,
                      letterSpacing:1, padding:'8px 16px', borderRadius:4,
                      border:`1px solid ${canArm ? C.warn : C.border}`,
                      background: canArm ? 'rgba(234,179,8,0.12)' : 'rgba(255,255,255,0.03)',
                      color: canArm ? C.warn : C.muted,
                      cursor: canArm ? 'pointer' : 'not-allowed',
                      transition:'all 0.15s',
                    }}
                    onClick={async () => {
                      logCommandSent(CMD_ARM)
                      const r = await fetch('/api/fc/command/arm', { method:'POST' })
                      const j = await r.json()
                      if (!j.ok) console.error('ARM failed:', j.error)
                    }}
                  >
                    ARM
                  </button>
                  <button
                    disabled={!canLaunch}
                    style={{
                      fontFamily:'monospace', fontSize:12, fontWeight:700,
                      letterSpacing:1, padding:'8px 16px', borderRadius:4,
                      border:`1px solid ${canLaunch ? C.ok : C.border}`,
                      background: canLaunch ? 'rgba(34,197,94,0.12)' : 'rgba(255,255,255,0.03)',
                      color: canLaunch ? C.ok : C.muted,
                      cursor: canLaunch ? 'pointer' : 'not-allowed',
                      transition:'all 0.15s',
                    }}
                    onClick={async () => {
                      logCommandSent(CMD_LAUNCH_OK)
                      const r = await fetch('/api/fc/command/launch_ok', { method:'POST' })
                      const j = await r.json()
                      if (!j.ok) console.error('LAUNCH_OK failed:', j.error)
                    }}
                  >
                    LAUNCH OK
                  </button>
                  <button
                    style={{
                      fontFamily:'monospace', fontSize:12, fontWeight:700,
                      letterSpacing:1, padding:'8px 16px', borderRadius:4,
                      border:`1px solid ${C.accent}`,
                      background:'rgba(0,212,255,0.07)',
                      color:C.accent,
                      cursor:'pointer',
                      transition:'all 0.15s',
                    }}
                    onClick={async () => {
                      logCommandSent(CMD_PING)
                      const r = await fetch('/api/fc/command/ping', { method:'POST' })
                      const j = await r.json()
                      if (!j.ok) console.error('PING failed:', j.error)
                    }}
                  >
                    PING
                  </button>
                </div>

                {/* Command log */}
                <div style={{
                  flex:1, borderLeft:`1px solid ${C.border}`, paddingLeft:12,
                  display:'flex', flexDirection:'column', gap:4, overflow:'hidden',
                }}>
                  {cmdLog.length === 0
                    ? <div style={{ fontSize:10, color:C.muted, fontFamily:'monospace' }}>—</div>
                    : <div style={{ overflowY:'auto', display:'flex', flexDirection:'column', gap:3 }}>
                        {cmdLog.map((e, i) => {
                          const t  = new Date(e.wall_ms)
                          const ts = `${String(t.getHours()).padStart(2,'0')}:${String(t.getMinutes()).padStart(2,'0')}:${String(t.getSeconds()).padStart(2,'0')}`
                          const statusColor = e.status === 'ack' ? C.ok : e.status === 'nack' ? C.crit : C.muted
                          const statusLabel = e.status === 'ack' ? '✓ ACK' : e.status === 'nack' ? '✗ NACK' : '···'
                          return (
                            <div key={i} style={{ display:'flex', alignItems:'center', gap:6, fontFamily:'monospace', fontSize:10 }}>
                              <span style={{ color:C.muted,    flexShrink:0 }}>{ts}</span>
                              <span style={{ color:C.text,     flexShrink:0 }}>{e.label}</span>
                              <span style={{ color:statusColor,flexShrink:0 }}>{statusLabel}</span>
                              {e.rtt_ms != null && <span style={{ color:C.muted }}>{e.rtt_ms} ms</span>}
                            </div>
                          )
                        })}
                      </div>
                  }
                </div>

              </div>
            </Card>
          )
        })()}

      </div>

    </div>
  )
}

// ─── Styles ─────────────────────────────────────────────────────────────────
const S = {
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
