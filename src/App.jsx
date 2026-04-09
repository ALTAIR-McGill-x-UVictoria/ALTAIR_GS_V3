import { useState } from 'react'
import { useTelemetry } from './hooks/useTelemetry'
import { useTelescope } from './hooks/useTelescope'
import ConnectionBar from './components/ConnectionBar'
import PacketPanel from './components/PacketPanel'
import AlarmSidebar from './components/AlarmSidebar'
import MapView from './components/MapView'
import TelescopeView from './components/TelescopeView'
import DashboardView from './components/DashboardView'

const TABS = ['Dashboard', 'Telemetry', 'Map', 'Telescope']

export default function App() {
  const { status, packets, history, freshness, wsReady, alarms, alarmRules, events, stageNames, lastAck } = useTelemetry()
  const { tracking } = useTelescope()
  const [activeTab, setActiveTab] = useState('Telemetry')
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
      <ConnectionBar status={status} wsReady={wsReady} freshness={freshness} />

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
          {activeTab === 'Dashboard' && (
            <DashboardView packets={packets} events={events} stageNames={stageNames} lastAck={lastAck} />
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

          {activeTab === 'Map' && (
            <MapView
              gpsPacket={Object.entries(packets).find(([k]) => k.toLowerCase() === 'gps')?.[1]}
              envPacket={Object.entries(packets).find(([k]) => k.toLowerCase() === 'environment')?.[1]}
              evtPacket={Object.entries(packets).find(([k]) => k.toLowerCase() === 'event')?.[1]}
              history={Object.entries(history).find(([k]) => k.toLowerCase() === 'gps')?.[1]}
              tracking={tracking}
              stageNames={stageNames}
            />
          )}

          {activeTab === 'Telescope' && (
            <TelescopeView />
          )}
        </div>

        <AlarmSidebar
          alarms={visibleAlarms}
          onDismissAll={dismissAll}
          onDismissOne={dismissOne}
          events={events}
          stageNames={stageNames}
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
