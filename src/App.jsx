import { useState } from 'react'
import { useTelemetry } from './hooks/useTelemetry'
import { useTelescope } from './hooks/useTelescope'
import ConnectionBar from './components/ConnectionBar'
import PacketPanel from './components/PacketPanel'
import AlarmSidebar from './components/AlarmSidebar'
import TelescopeView from './components/TelescopeView'
import GraphsView from './components/GraphsView'
import FlightView from './components/FlightView'
import SettingsView from './components/SettingsView'

const TABS = ['Flight', 'Telemetry', 'Graphs', 'Telescope', 'Settings']

/**
 * Resolve the best available GPS packet, normalizing field names to the
 * MavlinkGps schema (lat, lon, alt, relative_alt, hdg) so all consumers
 * can use the same field names regardless of which source is active.
 *
 * Selects the source with the better fix, scored as:
 *   fix_type (LocalGps field, 0–5; MavlinkGps treated as 3 when lat/lon non-zero)
 *   then num_sv as tiebreaker.
 *
 * LocalGps field remapping applied when selected:
 *   alt_msl      → alt
 *   heading_deg  → hdg
 *   relative_alt → 0 (not provided by local GPS)
 */
function gpsScore(pkt, isMavlink) {
  if (!pkt) return { fixType: 0, numSv: 0 }
  const fv = (name, def) => pkt.fields.find(f => f.name === name)?.value ?? def
  const lat = fv('lat', 0), lon = fv('lon', 0)
  if (isMavlink) {
    const hasFix = lat !== 0 || lon !== 0
    return { fixType: hasFix ? 3 : 0, numSv: fv('num_sv', 0) }
  }
  return { fixType: fv('fix_type', 0), numSv: fv('num_sv', 0) }
}

function remapLocalGps(local) {
  const remapped = local.fields.map(f => {
    if (f.name === 'alt_msl')     return { ...f, name: 'alt' }
    if (f.name === 'heading_deg') return { ...f, name: 'hdg' }
    return f
  })
  if (!remapped.find(f => f.name === 'relative_alt')) {
    remapped.push({ name: 'relative_alt', label: 'Altitude AGL', unit: 'm', value: 0 })
  }
  return { ...local, fields: remapped }
}

function resolveGpsPacket(packets) {
  const mavlink = Object.entries(packets).find(([k]) => k === 'MavlinkGps')?.[1] ?? null
  const local   = Object.entries(packets).find(([k]) => k === 'LocalGps')?.[1]   ?? null

  if (!mavlink && !local) return null
  if (!mavlink) return remapLocalGps(local)
  if (!local)   return mavlink

  const ms = gpsScore(mavlink, true)
  const ls = gpsScore(local,   false)
  const mavWins = ms.fixType > ls.fixType || (ms.fixType === ls.fixType && ms.numSv >= ls.numSv)
  return mavWins ? mavlink : remapLocalGps(local)
}

function resolveGpsHistory(history, packets) {
  const mavlink = Object.entries(packets).find(([k]) => k === 'MavlinkGps')?.[1] ?? null
  const local   = Object.entries(packets).find(([k]) => k === 'LocalGps')?.[1]   ?? null
  if (!mavlink && !local) return Object.entries(history).find(([k]) => k === 'LocalGps')?.[1]
  if (!local) return Object.entries(history).find(([k]) => k === 'MavlinkGps')?.[1]
  if (!mavlink) return Object.entries(history).find(([k]) => k === 'LocalGps')?.[1]
  const ms = gpsScore(mavlink, true)
  const ls = gpsScore(local,   false)
  const mavWins = ms.fixType > ls.fixType || (ms.fixType === ls.fixType && ms.numSv >= ls.numSv)
  return mavWins
    ? Object.entries(history).find(([k]) => k === 'MavlinkGps')?.[1]
    : Object.entries(history).find(([k]) => k === 'LocalGps')?.[1]
}

export default function App() {
  const { status, packets, history, freshness, wsReady, alarms, alarmRules, events, stageNames, lastAck, gsGps, gsGpsStatus } = useTelemetry()
  const { tracking, mountStatus, cameraStatus } = useTelescope()
  const [activeTab, setActiveTab] = useState('Flight')
  const [dismissed, setDismissed] = useState(new Set())

  const visibleAlarms = alarms.filter(
    a => a.severity !== 'ok' && !dismissed.has(`${a.label}.${a.field}`)
  )

  const dismissOne = (label, field) =>
    setDismissed(prev => new Set([...prev, `${label}.${field}`]))

  const dismissAll = () =>
    setDismissed(new Set(visibleAlarms.map(a => `${a.label}.${a.field}`)))

  const critCount = visibleAlarms.filter(a => a.severity === 'critical').length
  const warnCount = visibleAlarms.filter(a => a.severity === 'warning').length

  return (
    <div style={styles.root}>
      <ConnectionBar
        status={status}
        wsReady={wsReady}
        freshness={freshness}
        gsGps={gsGps}
        gsGpsStatus={gsGpsStatus}
        mountStatus={mountStatus}
        cameraStatus={cameraStatus}
      />

      <nav style={styles.tabBar}>
        {TABS.map(tab => (
          <button
            key={tab}
            style={{ ...styles.tab, ...(activeTab === tab ? styles.tabActive : {}) }}
            onClick={() => setActiveTab(tab)}
          >
            {tab}
            {tab === 'Telemetry' && critCount > 0 && (
              <span style={styles.critBadge}>{critCount}</span>
            )}
            {tab === 'Telemetry' && critCount === 0 && warnCount > 0 && (
              <span style={styles.warnBadge}>{warnCount}</span>
            )}
          </button>
        ))}
      </nav>

      {/* Main content + persistent alarm sidebar */}
      <div style={styles.body}>

        <div style={styles.tabContent}>
          {activeTab === 'Flight' && (
            <FlightView
              packets={packets}
              events={events}
              stageNames={stageNames}
              gpsPacket={resolveGpsPacket(packets)}
              envPacket={Object.entries(packets).find(([k]) => k.toLowerCase() === 'environment')?.[1]}
              evtPacket={Object.entries(packets).find(([k]) => k.toLowerCase() === 'event')?.[1]}
              history={resolveGpsHistory(history, packets)}
              tracking={tracking}
              gsGps={gsGps}
            />
          )}

          {activeTab === 'Telemetry' && (
            <main style={styles.grid}>
              {Object.keys(packets).filter(label => label.toLowerCase() !== 'ack').map(label => (
                <PacketPanel
                  key={label}
                  label={label}
                  packet={packets[label]}
                  history={history[label]}
                  alarms={visibleAlarms.filter(a => a.label === label)}
                  rules={alarmRules.filter(r => r.label === label)}
                />
              ))}
            </main>
          )}

          {activeTab === 'Graphs' && (
            <GraphsView packets={packets} history={history} />
          )}

          {activeTab === 'Telescope' && (
            <TelescopeView />
          )}

          {activeTab === 'Settings' && (
            <SettingsView packets={packets} lastAck={lastAck} />
          )}
        </div>

        <AlarmSidebar
          alarms={visibleAlarms}
          onDismissAll={dismissAll}
          onDismissOne={dismissOne}
          events={events}
          stageNames={stageNames}
          lastAck={lastAck}
          packets={packets}
          currentStage={(() => {
            const evtPkt = Object.entries(packets).find(([k]) => k.toLowerCase() === 'event')?.[1]
            const v = evtPkt?.fields?.find(f => f.name === 'flight_stage')?.value
            return v != null ? Math.round(v) : -1
          })()}
        />
      </div>

      <footer style={styles.footer}>
        <span>ALTAIR V2 Ground Station</span>
        <span style={{ color: 'var(--muted)' }}>
          WS {wsReady ? '●' : '○'}  ·  {Object.keys(packets).length} active packet type(s)
        </span>
      </footer>
    </div>
  )
}

const styles = {
  root: {
    display:       'flex',
    flexDirection: 'column',
    height:        '100vh',
    overflow:      'hidden',
  },
  tabBar: {
    display:      'flex',
    gap:          0,
    background:   'var(--surface)',
    borderBottom: '1px solid var(--border)',
    padding:      '0 16px',
    flexShrink:   0,
  },
  tab: {
    background:    'none',
    border:        'none',
    borderBottom:  '2px solid transparent',
    color:         'var(--muted)',
    fontFamily:    'var(--font-mono)',
    fontSize:      12,
    padding:       '8px 16px',
    cursor:        'pointer',
    letterSpacing: 1,
    display:       'flex',
    alignItems:    'center',
    gap:           6,
  },
  tabActive: {
    color:           'var(--accent)',
    borderBottomColor: 'var(--accent)',
  },
  critBadge: {
    background:   '#ff4444',
    color:        '#fff',
    fontSize:     9,
    fontWeight:   700,
    borderRadius: 8,
    padding:      '1px 5px',
    lineHeight:   1.4,
  },
  warnBadge: {
    background:   '#ffd600',
    color:        '#000',
    fontSize:     9,
    fontWeight:   700,
    borderRadius: 8,
    padding:      '1px 5px',
    lineHeight:   1.4,
  },
  body: {
    flex:     1,
    display:  'flex',
    overflow: 'hidden',
  },
  tabContent: {
    flex:     1,
    display:  'flex',
    overflow: 'hidden',
  },
  grid: {
    flex:                1,
    overflowY:           'auto',
    padding:             16,
    display:             'grid',
    gridTemplateColumns: '1fr',
    gridAutoRows:        'min-content',
    gap:                 12,
    alignContent:        'start',
  },
  footer: {
    display:         'flex',
    justifyContent:  'space-between',
    padding:         '4px 16px',
    background:      'var(--surface)',
    borderTop:       '1px solid var(--border)',
    fontSize:        10,
    color:           'var(--muted)',
    flexShrink:      0,
  },
}
