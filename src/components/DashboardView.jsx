import { useMemo, useEffect, useRef, useState } from 'react'
import {
  fv, findPacket, formatUptime, formatDuration, formatEta,
  C, S, ArtificialHorizon, Compass, ArcGauge, VerticalTape,
  ClimbRateBar, StatRow, StatusPill, Card, LinearBar,
  TimerDisplay, CheckItem, FlightStageStrip, EventLog,
} from './instruments/FlightInstruments'

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

  const launchEvent  = useMemo(() => [...events].reverse().find(e => e.field === 'flight_stage' && e.new_val === 2), [events])
  const descentEvent = useMemo(() => [...events].reverse().find(e => e.field === 'flight_stage' && e.new_val === 6), [events])
  const landingEvent = useMemo(() => [...events].reverse().find(e => e.field === 'flight_stage' && e.new_val === 7), [events])

  const launchElapsed = launchEvent
    ? (landingEvent ? landingEvent.wall_ms - launchEvent.wall_ms : now - launchEvent.wall_ms)
    : null
  const ascentDurMs   = descentEvent && launchEvent ? descentEvent.wall_ms - launchEvent.wall_ms : null
  const landingEtaMs  = descentEvent && ascentDurMs
    ? (descentEvent.wall_ms + ascentDurMs * 3) - now
    : null
  const inDescent     = flightStage != null && flightStage === 6

  // ── ETA Termination / Burst ───────────────────────────────────────────────
  const TERM_ALT  = 25000
  const BURST_ALT = 30000

  const cutdownFired     = fv(evpkt, 'cutdown_fired',     0) === 1
  const terminationFired = fv(evpkt, 'termination_fired', 0) === 1
  const burstDetected    = fv(evpkt, 'burst_detected',    0) === 1
  const isRecovery       = flightStage != null && flightStage >= 8

  const MIN_CLIMB_MS = 0.5

  function etaToAlt(targetAlt, requireAscent = true) {
    if (isRecovery) return null
    if (baroAlt == null || climb == null) return null
    if (requireAscent && climb < MIN_CLIMB_MS) return null
    if (climb < MIN_CLIMB_MS) return baroAlt >= targetAlt ? 0 : null
    const secondsToAlt = (targetAlt - baroAlt) / climb
    return secondsToAlt * 1000
  }

  const etaTermMs  = etaToAlt(TERM_ALT, true)
  const etaBurstMs = etaToAlt(BURST_ALT, false)

  // ── Link latency ─────────────────────────────────────────────────────────
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
  const gpsHdop       = fv(gps, 'eph', null)

  // ── Command log ───────────────────────────────────────────────────────────
  const [cmdLog, setCmdLog] = useState([])
  const cmdSeqRef = useRef(0)

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
        const updated = [...prev]
        updated[idx] = { ...updated[idx], status: ackStatus, rtt_ms: ack_wall_ms - updated[idx].wall_ms }
        return updated
      }
      const entry = { seq: -1, wall_ms: ack_wall_ms, label: CMD_LABELS[cmd_id] ?? `0x${cmd_id.toString(16)}`, cmd_id, status: ackStatus, rtt_ms: null }
      return [entry, ...prev].slice(0, CMD_LOG_MAX)
    })
  }

  useEffect(() => {
    if (!lastAck) return
    logCommandAck(lastAck.cmd_id, lastAck.status, lastAck.wall_ms)
  }, [lastAck])

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

        <Card title="ATTITUDE" style={{ flex:'1 1 0' }}>
          <div style={{ flex:1, display:'flex', alignItems:'center', justifyContent:'center', minHeight:0 }}>
            <ArtificialHorizon roll={roll} pitch={pitch} />
          </div>
        </Card>

        <Card title="HEADING" style={{ flex:'1 1 0' }}>
          <div style={{ flex:1, display:'flex', alignItems:'center', justifyContent:'center', minHeight:0 }}>
            <Compass
              gpsDeg={hdg}
              yawDeg={(yaw * 180 / Math.PI + 360) % 360}
            />
          </div>
        </Card>

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

        <Card title="POWER" style={{ flex:'2 1 0' }}>
          <div style={{ flex:1, display:'grid', gridTemplateColumns:'1fr 1fr', gridTemplateRows:'1fr 1fr', gap:4, alignItems:'center', justifyItems:'center' }}>
            <ArcGauge label="Voltage" value={voltage} unit="V" min={9} max={14} warnLo={10.9} warnHi={12.9} size={100} />
            <ArcGauge label="Current" value={current} unit="A" min={0} max={15} warnHi={12} size={100} />
            <ArcGauge label="Temp"    value={pwrTemp} unit="°C" min={0} max={80} warnHi={60} size={100} />
            <ArcGauge
              label="Power"
              value={voltage != null && current != null ? voltage * current : null}
              unit="W" min={0} max={200} warnHi={150} size={100}
            />
          </div>
        </Card>

        <Card title="VESC / MOTOR" style={{ flex:'2 1 0' }}>
          <div style={{ flex:1, display:'flex', flexDirection:'column', justifyContent:'center', gap:4 }}>
            <LinearBar label="RPM"         value={rpm}         min={0} max={5000} warn={4500} unit="rpm" />
            <LinearBar label="Motor Curr." value={motorCur}    min={0} max={60}   warn={50}   unit="A"   color="#a78bfa" />
            <LinearBar label="Voltage"     value={vescVoltage} min={9} max={14}   warn={12.9} unit="V"   color="#22c55e" />
            <StatRow   label="MOSFET Temp" value={motorTemp}   unit="°C" warn={75} crit={85} />
          </div>
        </Card>

        <Card title="ENVIRONMENT" style={{ flex:'1 1 0' }}>
          <div style={{ flex:1, display:'flex', flexDirection:'column', justifyContent:'center', gap:6 }}>
            <StatRow label="Temperature"  value={envTemp}     unit="°C" />
            <StatRow label="Baro Alt"     value={baroAlt}     unit="m"  />
            <StatRow label="Airspeed"     value={airspeed}    unit="m/s" />
            <StatRow label="Groundspeed"  value={groundspeed} unit="m/s" />
          </div>
        </Card>

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

            <div style={{ display:'flex', gap:8, borderTop:`1px solid ${C.border}`, paddingTop:6 }}>
              <div style={{ flex:1, display:'flex', flexDirection:'column', gap:1 }}>
                <span style={{ color:C.muted, fontSize:9, fontFamily:'monospace', letterSpacing:1 }}>
                  ETA TERMINATION
                </span>
                <span style={{
                  fontFamily: 'monospace', fontSize:15, fontWeight:700,
                  color: terminationFired                        ? C.ok
                       : cutdownFired && burstDetected           ? C.crit
                       : !cutdownFired && burstDetected          ? C.crit
                       : cutdownFired && !terminationFired       ? C.warn
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

        <Card title="PRE-FLIGHT CHECKLIST" style={{ flex:'1 1 0' }}>
          <div style={{ flex:1, display:'flex', flexDirection:'column', justifyContent:'center' }}>
            <CheckItem label="GPS Fix" ok={gps ? gpsFix : null} detail={gpsFix && gpsHdop != null ? `HDOP ${gpsHdop.toFixed(1)}` : null} />
            <CheckItem label="Pixhawk Connected" ok={hb ? pixhawkOk : null} />
            <CheckItem label="VESC Connected"    ok={hb ? vescOk : null} />
            <CheckItem label="Power Board"       ok={hb ? powerOk : null} />
            <CheckItem label="Photodiode Board"  ok={hb ? photodiodeOk : null} />
            <CheckItem label="Data Logging Active" ok={evpkt ? dataLogging : null} />
            <CheckItem label="Arm State" ok={evpkt ? armState : null} detail={armState ? 'ARMED' : evpkt ? 'DISARMED' : null} />
          </div>
        </Card>

        {(() => {
          const allOk    = gpsFix && pixhawkOk && vescOk && powerOk && photodiodeOk && dataLogging
          const canArm   = allOk
          const canLaunch = allOk && armState
          return (
            <Card title="COMMANDS" style={{ flex:'1 1 0' }}>
              <div style={{ flex:1, display:'flex', flexDirection:'row', gap:12, overflow:'hidden' }}>
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
                  >ARM</button>
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
                  >LAUNCH OK</button>
                  <button
                    style={{
                      fontFamily:'monospace', fontSize:12, fontWeight:700,
                      letterSpacing:1, padding:'8px 16px', borderRadius:4,
                      border:`1px solid ${C.accent}`,
                      background:'rgba(0,212,255,0.07)',
                      color:C.accent, cursor:'pointer', transition:'all 0.15s',
                    }}
                    onClick={async () => {
                      logCommandSent(CMD_PING)
                      const r = await fetch('/api/fc/command/ping', { method:'POST' })
                      const j = await r.json()
                      if (!j.ok) console.error('PING failed:', j.error)
                    }}
                  >PING</button>
                </div>

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
