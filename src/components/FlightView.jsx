import { useMemo, useEffect, useRef, useState } from 'react'
import MapView from './MapView'
import {
  fv, findPacket, formatUptime, formatDuration, formatEta,
  C, S, ArtificialHorizon, Compass, ArcGauge, VerticalTape,
  ClimbRateBar, StatRow, StatusPill, Card, LinearBar,
  TimerDisplay, CheckItem,
} from './instruments/FlightInstruments'

export default function FlightView({
  packets, events = [], stageNames = {},
  gpsPacket, envPacket, evtPacket, history, tracking, gsGps,
}) {
  const att   = findPacket(packets, 'attitude')
  const gps   = findPacket(packets, 'gps')
  const pwr   = findPacket(packets, 'power')
  const vesc  = findPacket(packets, 'vesc')
  const hb    = findPacket(packets, 'heartbeat')
  const env   = findPacket(packets, 'environment')
  const evpkt = findPacket(packets, 'event')

  const roll    = fv(att, 'roll',  0)
  const pitch   = fv(att, 'pitch', 0)
  const yaw     = fv(att, 'yaw',   0)

  const altMSL  = fv(gps, 'alt',          null)
  const altAGL  = fv(gps, 'relative_alt', null)
  const hdg     = fv(gps, 'hdg',          0)

  const climb       = fv(env, 'climb',       null)
  const airspeed    = fv(env, 'airspeed',    null)
  const groundspeed = fv(env, 'groundspeed', null)
  const baroAlt     = fv(env, 'baro_alt',    null)
  const envTemp     = fv(env, 'temperature', null)

  const voltage = fv(pwr, 'voltage_bus',   null)
  const current = fv(pwr, 'current_total', null)
  const pwrTemp = fv(pwr, 'temperature',   null)

  const rpm         = fv(vesc, 'rpm',             null)
  const motorTemp   = fv(vesc, 'temperature_mos', null)
  const vescVoltage = fv(vesc, 'input_voltage',   null)
  const motorCur    = fv(vesc, 'motor_current',   null)

  const cpu            = fv(hb, 'cpu_load_pct',         null)
  const mem            = fv(hb, 'mem_used_pct',          null)
  const uptime         = fv(hb, 'uptime_s',              null)
  const pixConn        = fv(hb, 'pixhawk_connected',     null)
  const vescConn       = fv(hb, 'vesc_connected',        null)
  const powerConn      = fv(hb, 'power_connected',       null)
  const photodiodeConn = fv(hb, 'photodiode_connected',  null)

  const flightStage = fv(evpkt, 'flight_stage', null)

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
  const ascentDurMs  = descentEvent && launchEvent ? descentEvent.wall_ms - launchEvent.wall_ms : null
  const landingEtaMs = descentEvent && ascentDurMs ? (descentEvent.wall_ms + ascentDurMs * 3) - now : null
  const inDescent    = flightStage != null && flightStage === 6

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
    return ((targetAlt - baroAlt) / climb) * 1000
  }

  const etaTermMs  = etaToAlt(TERM_ALT, true)
  const etaBurstMs = etaToAlt(BURST_ALT, false)

  // ── Link latency ─────────────────────────────────────────────────────────
  const fcTimeUnix   = fv(hb, 'time_unix', null)
  const _latencyBuf  = useRef([])
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
  const gpsFix       = gps != null && fv(gps, 'lat', 0) !== 0
  const pixhawkOk    = pixConn        != null && pixConn        > 0.5
  const vescOk       = vescConn       != null && vescConn       > 0.5
  const powerOk      = powerConn      != null && powerConn      > 0.5
  const photodiodeOk = photodiodeConn != null && photodiodeConn > 0.5
  const dataLogging  = fv(evpkt, 'data_logging_active', 0) === 1
  const armState     = fv(evpkt, 'arm_state', 0) === 1
  const gpsHdop      = fv(gps, 'eph', null)


  return (
    <div style={FL.root}>

      {/* ── Main 3-column body ──────────────────────────────────────────── */}
      <div style={FL.body}>

        {/* LEFT: orientation instruments — flex column, each card gets a share */}
        <div style={FL.leftPanel}>

          {/* Attitude */}
          <div style={{ ...FL.leftCard, flex: '0 0 auto' }}>
            <div style={S.cardTitle}>ATTITUDE</div>
            <ArtificialHorizon roll={roll} pitch={pitch} />
          </div>

          {/* Heading compass */}
          <div style={{ ...FL.leftCard, flex: '0 0 auto' }}>
            <div style={S.cardTitle}>HEADING</div>
            <Compass
              gpsDeg={hdg}
              yawDeg={(yaw * 180 / Math.PI + 360) % 360}
            />
          </div>

          {/* Alt / Climb */}
          <div style={{ ...FL.leftCard, flex: '0 0 auto' }}>
            <div style={S.cardTitle}>ALT / CLIMB</div>
            <div style={{ display:'flex', gap:4, alignItems:'flex-end', justifyContent:'center' }}>
              <VerticalTape label="MSL" value={altMSL ?? baroAlt} unit="m" min={0} max={35000} warnMax={30000} />
              <VerticalTape label="AGL" value={altAGL}            unit="m" min={0} max={35000} warnMax={30000} color="#22c55e" />
              <ClimbRateBar value={climb} />
            </div>
          </div>

          {/* Speed — compact, fixed-ish */}
          <div style={{ ...FL.leftCard, flex: '0 0 auto' }}>
            <div style={S.cardTitle}>SPEED</div>
            <StatRow label="Airspeed"    value={airspeed}    unit="m/s" />
            <StatRow label="Groundspeed" value={groundspeed} unit="m/s" />
          </div>

        </div>

        {/* CENTER: map fills remaining space */}
        <div style={FL.mapCol}>
          <MapView
            gpsPacket={gpsPacket}
            envPacket={envPacket}
            evtPacket={evtPacket}
            history={history}
            tracking={tracking}
            stageNames={stageNames}
            gsGps={gsGps}
          />
        </div>

        {/* RIGHT: systems panel */}
        <div style={FL.rightPanel}>

          <Card title="POWER" style={{ flexShrink: 0 }}>
            <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:2, alignItems:'center', justifyItems:'center' }}>
              <ArcGauge label="Voltage" value={voltage} unit="V"  min={9} max={14} warnLo={10.9} warnHi={12.9} size={80} />
              <ArcGauge label="Current" value={current} unit="A"  min={0} max={15} warnHi={12}               size={80} />
              <ArcGauge label="Temp"    value={pwrTemp} unit="°C" min={0} max={80} warnHi={60}               size={80} />
              <ArcGauge
                label="Power"
                value={voltage != null && current != null ? voltage * current : null}
                unit="W" min={0} max={200} warnHi={150} size={80}
              />
            </div>
          </Card>

          <Card title="VESC / MOTOR" style={{ flexShrink: 0 }}>
            <LinearBar label="RPM"         value={rpm}         min={0} max={5000} warn={4500} unit="rpm" />
            <LinearBar label="Motor Curr." value={motorCur}    min={0} max={60}   warn={50}   unit="A"   color="#a78bfa" />
            <LinearBar label="Voltage"     value={vescVoltage} min={9} max={14}   warn={12.9} unit="V"   color="#22c55e" />
            <StatRow   label="MOSFET Temp" value={motorTemp}   unit="°C" warn={75} crit={85} />
          </Card>

          <Card title="ENVIRONMENT" style={{ flexShrink: 0 }}>
            <StatRow label="Temperature"  value={envTemp}     unit="°C" />
            <StatRow label="Baro Alt"     value={baroAlt}     unit="m"  />
            <StatRow label="Airspeed"     value={airspeed}    unit="m/s" />
            <StatRow label="Groundspeed"  value={groundspeed} unit="m/s" />
          </Card>

          <Card title="SYSTEM HEALTH" style={{ flexShrink: 0 }}>
            <LinearBar label="CPU Load" value={cpu} min={0} max={100} warn={80} unit="%" color={C.accent} />
            <LinearBar label="Memory"   value={mem} min={0} max={100} warn={80} unit="%" color="#a78bfa" />
            <StatusPill label="Pixhawk Link" ok={pixConn > 0.5} trueLabel="CONNECTED" falseLabel="NO LINK" />
            <div style={S.statRow}>
              <span style={{ color:C.muted, fontSize:11, minWidth:90 }}>Uptime</span>
              <span style={{ color:C.text, fontWeight:700, fontFamily:'monospace', fontSize:12 }}>{formatUptime(uptime)}</span>
            </div>
          </Card>

        </div>
      </div>

      {/* ── Bottom strip ─────────────────────────────────────────────────── */}
      <div style={FL.bottomStrip}>

        {/* Mission Timers */}
        <div style={FL.bottomCell}>
          <div style={S.cardTitle}>MISSION TIMERS</div>
          {/* Uniform 2-row × 3-col grid so all six cells are the same height */}
          <div style={{ flex:1, display:'grid', gridTemplateColumns:'1fr 1fr 1fr', gridTemplateRows:'1fr 1fr', minHeight:0 }}>
            {/* Row 1 */}
            <div style={FL.timerCell}>
              <div style={FL.timerLabel}>LAUNCH T+</div>
              <div style={{ ...FL.timerValue, color: launchEvent ? C.accent : C.muted }}>
                {launchEvent ? formatDuration(launchElapsed) : '—'}
              </div>
            </div>
            <div style={{ ...FL.timerCell, borderLeft:`1px solid ${C.border}` }}>
              <div style={FL.timerLabel}>STAGE</div>
              <div style={{ ...FL.timerValue, color: C.accent }}>
                {flightStage != null ? (stageNames[flightStage] ?? `${flightStage}`) : '—'}
              </div>
            </div>
            <div style={{ ...FL.timerCell, borderLeft:`1px solid ${C.border}` }}>
              <div style={FL.timerLabel}>LATENCY</div>
              <div style={{
                ...FL.timerValue,
                color: linkLatencyMs == null ? C.muted
                     : linkLatencyMs > 2000 ? C.crit
                     : linkLatencyMs > 500  ? C.warn : C.ok,
              }}>
                {linkLatencyMs == null ? '—' : `${linkLatencyMs} ms`}
              </div>
            </div>
            {/* Row 2 */}
            <div style={{ ...FL.timerCell, borderTop:`1px solid ${C.border}` }}>
              <div style={FL.timerLabel}>ETA TERM</div>
              <div style={{
                ...FL.timerValue,
                color: terminationFired ? C.ok
                     : (cutdownFired && burstDetected) || (!cutdownFired && burstDetected) ? C.crit
                     : cutdownFired && !terminationFired ? C.warn
                     : flightStage >= 4 || etaTermMs == null ? C.muted
                     : etaTermMs < 0 ? C.warn : C.accent,
              }}>
                {terminationFired ? 'FIRED'
                  : cutdownFired && burstDetected ? 'FAILED'
                  : !cutdownFired && burstDetected ? 'FAIL'
                  : cutdownFired && !terminationFired ? 'ENGAGED'
                  : flightStage >= 4 ? '--:--'
                  : etaTermMs == null ? '—' : formatEta(etaTermMs)}
              </div>
            </div>
            <div style={{ ...FL.timerCell, borderTop:`1px solid ${C.border}`, borderLeft:`1px solid ${C.border}` }}>
              <div style={FL.timerLabel}>ETA BURST</div>
              <div style={{
                ...FL.timerValue,
                color: isRecovery ? C.muted : burstDetected ? C.ok
                     : etaBurstMs == null ? C.muted : etaBurstMs < 0 ? C.crit : C.accent,
              }}>
                {isRecovery ? '--:--' : etaBurstMs == null ? '—' : formatEta(etaBurstMs)}
              </div>
            </div>
            <div style={{ ...FL.timerCell, borderTop:`1px solid ${C.border}`, borderLeft:`1px solid ${C.border}` }}>
              <div style={FL.timerLabel}>{inDescent ? 'ETA LAND' : ''}</div>
              <div style={{ ...FL.timerValue, color: C.warn }}>
                {inDescent && landingEtaMs != null && landingEtaMs > 0 ? formatDuration(landingEtaMs) : '—'}
              </div>
            </div>
          </div>
        </div>

        {/* Pre-flight checklist */}
        <div style={FL.bottomCell}>
          <div style={S.cardTitle}>PRE-FLIGHT</div>
          <div style={{ flex:1, display:'flex', flexDirection:'column', justifyContent:'space-evenly', overflow:'hidden' }}>
            <CheckItem label="GPS Fix"             ok={gps   ? gpsFix    : null} detail={gpsFix && gpsHdop != null ? `HDOP ${gpsHdop.toFixed(1)}` : null} />
            <CheckItem label="Pixhawk Connected"   ok={hb    ? pixhawkOk : null} />
            <CheckItem label="VESC Connected"      ok={hb    ? vescOk    : null} />
            <CheckItem label="Power Board"         ok={hb    ? powerOk   : null} />
            <CheckItem label="Photodiode Board"    ok={hb    ? photodiodeOk : null} />
            <CheckItem label="Data Logging Active" ok={evpkt ? dataLogging  : null} />
            <CheckItem label="Arm State"           ok={evpkt ? armState     : null} detail={armState ? 'ARMED' : evpkt ? 'DISARMED' : null} />
          </div>
        </div>

        {/* Flight Events */}
        <div style={{ ...FL.bottomCell, borderRight: 'none' }}>
          <div style={S.cardTitle}>FLIGHT EVENTS</div>
          {/* Stage strip */}
          <div style={{ display:'flex', flexWrap:'wrap', gap:2, paddingBottom:4, flexShrink:0 }}>
            {Object.entries(stageNames).map(([k, name]) => {
              const id     = Number(k)
              const stage  = flightStage != null ? Math.round(flightStage) : -1
              const active = id === stage
              const past   = id < stage
              return (
                <div key={id} style={{
                  fontSize:9, fontFamily:'monospace', padding:'1px 5px', borderRadius:3, letterSpacing:0.5,
                  background: active ? C.accent : past ? 'rgba(0,212,255,0.1)' : 'transparent',
                  color:      active ? '#000' : past ? C.accent : C.muted,
                  border:     active ? `1px solid ${C.accent}` : past ? '1px solid rgba(0,212,255,0.25)' : `1px solid ${C.border}`,
                }}>
                  {name}
                </div>
              )
            })}
          </div>
          {/* Event list */}
          <div style={{ flex:1, overflowY:'auto', display:'flex', flexDirection:'column', gap:1 }}>
            {events.length === 0
              ? <div style={{ fontSize:10, color:C.muted, fontFamily:'monospace' }}>Waiting for events…</div>
              : [...events].reverse().map((ev, i) => {
                  const isStage = ev.field === 'flight_stage'
                  const d = new Date(ev.wall_ms)
                  const ts = `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`
                  return (
                    <div key={i} style={{
                      display:'flex', alignItems:'flex-start', gap:5, padding:'3px 4px', borderRadius:3,
                      background: isStage ? 'rgba(0,212,255,0.07)' : 'rgba(255,255,255,0.02)',
                      borderLeft: isStage ? `2px solid ${C.accent}` : `2px solid ${C.border}`,
                    }}>
                      <span style={{ color:C.muted, fontSize:9, fontFamily:'monospace', flexShrink:0, paddingTop:1, minWidth:48 }}>{ts}</span>
                      <span style={{ color: isStage ? C.accent : C.text, fontSize:10, fontFamily:'monospace', fontWeight: isStage ? 700 : 400, lineHeight:1.4 }}>
                        {ev.message}
                      </span>
                    </div>
                  )
                })
            }
          </div>
        </div>

      </div>
    </div>
  )
}

const FL = {
  root: {
    flex: 1,
    display: 'flex',
    flexDirection: 'column',
    overflow: 'hidden',
    background: 'var(--bg)',
  },
  body: {
    flex: 1,
    display: 'flex',
    overflow: 'hidden',
    minHeight: 0,
  },
  // Left panel: fixed width, flex column with overflow scroll as safety net
  leftPanel: {
    width: 200,
    flexShrink: 0,
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
    padding: '6px 5px 6px 6px',
    borderRight: '1px solid var(--border)',
    overflowY: 'auto',
  },
  // Individual card in the left panel — no inner padding from Card, we do it here
  leftCard: {
    background: '#141820',
    border: '1px solid #1e2535',
    borderRadius: 6,
    padding: '6px 8px 8px',
    display: 'flex',
    flexDirection: 'column',
    minHeight: 0,
    overflow: 'hidden',
  },
  mapCol: {
    flex: 1,
    display: 'flex',
    overflow: 'hidden',
  },
  // Right panel: fixed width, scrollable
  rightPanel: {
    width: 195,
    flexShrink: 0,
    overflowY: 'auto',
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
    padding: '6px 6px 6px 5px',
    borderLeft: '1px solid var(--border)',
  },
  bottomStrip: {
    height: 160,
    flexShrink: 0,
    display: 'flex',
    borderTop: '1px solid var(--border)',
    overflow: 'hidden',
  },
  bottomCell: {
    flex: 1,
    borderRight: '1px solid var(--border)',
    padding: '6px 10px',
    display: 'flex',
    flexDirection: 'column',
    overflow: 'hidden',
  },
  timerCell: {
    display: 'flex',
    flexDirection: 'column',
    justifyContent: 'center',
    padding: '0 8px',
  },
  timerLabel: {
    color: 'var(--muted)',
    fontSize: 9,
    fontFamily: 'monospace',
    letterSpacing: 1,
    marginBottom: 2,
  },
  timerValue: {
    fontFamily: 'monospace',
    fontSize: 15,
    fontWeight: 700,
    letterSpacing: 0.5,
  },
}
