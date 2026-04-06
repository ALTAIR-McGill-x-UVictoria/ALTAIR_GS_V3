import { useState } from 'react'
import { useTelemetry } from './hooks/useTelemetry'
import ConnectionBar from './components/ConnectionBar'
import PacketPanel from './components/PacketPanel'
import MapView from './components/MapView'
import TelescopeView from './components/TelescopeView'

const PANEL_ORDER = ['Attitude', 'Power', 'VESC', 'Photodiode', 'GPS']
const TABS = ['Telemetry', 'Map', 'Telescope']

export default function App() {
  const { status, packets, history, freshness, wsReady } = useTelemetry()
  const [activeTab, setActiveTab] = useState('Telemetry')

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
          </button>
        ))}
      </nav>

      {activeTab === 'Telemetry' && (
        <main style={styles.grid}>
          {PANEL_ORDER.map(label => (
            <PacketPanel
              key={label}
              label={label}
              packet={packets[label]}
              history={history[label]}
            />
          ))}
        </main>
      )}

      {activeTab === 'Map' && (
        <div style={styles.mapWrapper}>
          <MapView
            gpsPacket={packets['GPS']}
            history={history['GPS']}
          />
        </div>
      )}

      {activeTab === 'Telescope' && (
        <div style={styles.telescopeWrapper}>
          <TelescopeView />
        </div>
      )}

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
    display: 'flex',
    flexDirection: 'column',
    height: '100vh',
    overflow: 'hidden',
  },
  tabBar: {
    display: 'flex',
    gap: 0,
    background: 'var(--surface)',
    borderBottom: '1px solid var(--border)',
    padding: '0 16px',
  },
  tab: {
    background: 'none',
    border: 'none',
    borderBottom: '2px solid transparent',
    color: 'var(--muted)',
    fontFamily: 'var(--font-mono)',
    fontSize: 12,
    padding: '8px 16px',
    cursor: 'pointer',
    letterSpacing: 1,
  },
  tabActive: {
    color: 'var(--accent)',
    borderBottomColor: 'var(--accent)',
  },
  grid: {
    flex: 1,
    overflowY: 'auto',
    padding: 16,
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fill, minmax(420px, 1fr))',
    gridAutoRows: 'min-content',
    gap: 12,
    alignContent: 'start',
  },
  mapWrapper: {
    flex: 1,
    overflow: 'hidden',
    display: 'flex',
    flexDirection: 'column',
  },
  telescopeWrapper: {
    flex: 1,
    overflowY: 'auto',
    display: 'flex',
    flexDirection: 'column',
  },
  footer: {
    display: 'flex',
    justifyContent: 'space-between',
    padding: '4px 16px',
    background: 'var(--surface)',
    borderTop: '1px solid var(--border)',
    fontSize: 10,
    color: 'var(--muted)',
  },
}
