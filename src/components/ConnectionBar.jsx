import { useEffect, useState } from 'react'
import { useSerial } from '../hooks/useSerial'

const BAUD_RATES = [9600, 19200, 38400, 57600, 115200]

const FRESHNESS_STYLE = {
  ok:      { color: 'var(--ok)',   label: '●' },
  stale:   { color: 'var(--warn)', label: '●' },
  lost:    { color: 'var(--error)', label: '●' },
  waiting: { color: 'var(--muted)', label: '○' },
}

export default function ConnectionBar({ status, wsReady, freshness = {} }) {
  const { ports, loading, error, refreshPorts, connectPort, disconnectPort } = useSerial()
  const [selectedPort, setSelectedPort] = useState('')
  const [selectedBaud, setSelectedBaud] = useState(57600)

  // Refresh port list on mount and pre-select the LR-900p if found
  useEffect(() => {
    refreshPorts().then(() => {})
  }, [])

  useEffect(() => {
    const lr = ports.find(p => p.is_lr900p)
    if (lr && !selectedPort) setSelectedPort(lr.device)
  }, [ports])

  const dotColor = status.connected ? 'var(--ok)' : wsReady ? 'var(--warn)' : 'var(--error)'
  const dotLabel = status.connected
    ? `Connected — ${status.port}`
    : wsReady ? 'No serial link' : 'Backend offline'

  return (
    <header style={styles.bar}>
      <span style={styles.title}>ALTAIR V2 GS</span>

      <div style={styles.indicator}>
        <span style={{ ...styles.dot, background: dotColor }} />
        <span style={{ color: dotColor }}>{dotLabel}</span>
      </div>

      <div style={styles.controls}>
        <select
          style={styles.select}
          value={selectedPort}
          onChange={e => setSelectedPort(e.target.value)}
          onFocus={refreshPorts}
        >
          <option value="">— select port —</option>
          {ports.map(p => (
            <option key={p.device} value={p.device}>
              {p.device}{p.is_lr900p ? ' ★ LR-900p' : ''} — {p.description}
            </option>
          ))}
        </select>

        <select
          style={styles.select}
          value={selectedBaud}
          onChange={e => setSelectedBaud(Number(e.target.value))}
        >
          {BAUD_RATES.map(b => (
            <option key={b} value={b}>{b}</option>
          ))}
        </select>

        {status.connected ? (
          <button style={{ ...styles.btn, background: 'var(--error)' }} onClick={disconnectPort}>
            Disconnect
          </button>
        ) : (
          <button
            style={{ ...styles.btn, background: 'var(--accent2)', opacity: loading ? 0.6 : 1 }}
            onClick={() => connectPort(selectedPort, selectedBaud)}
            disabled={loading || !selectedPort}
          >
            Connect
          </button>
        )}

        <button style={{ ...styles.btn, background: 'var(--border)' }} onClick={refreshPorts}>
          ↻
        </button>
      </div>

      <div style={styles.packetStatus}>
        {['Attitude', 'Power', 'VESC', 'Photodiode', 'GPS'].map(label => {
          const state = freshness[label] ?? 'waiting'
          const { color, label: dot } = FRESHNESS_STYLE[state]
          return (
            <span key={label} style={styles.packetBadge} title={`${label}: ${state}`}>
              <span style={{ color }}>{dot}</span>
              <span style={{ color: state === 'ok' ? 'var(--text)' : color }}>{label}</span>
            </span>
          )
        })}
      </div>

      {error && <span style={styles.error}>{error}</span>}
    </header>
  )
}

const styles = {
  bar: {
    display: 'flex',
    alignItems: 'center',
    gap: 16,
    padding: '8px 16px',
    background: 'var(--surface)',
    borderBottom: '1px solid var(--border)',
    flexWrap: 'wrap',
  },
  title: {
    fontWeight: 700,
    fontSize: 15,
    color: 'var(--accent)',
    letterSpacing: 2,
    marginRight: 8,
  },
  indicator: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    fontSize: 12,
    flex: 1,
  },
  dot: {
    width: 8,
    height: 8,
    borderRadius: '50%',
    display: 'inline-block',
    flexShrink: 0,
  },
  controls: {
    display: 'flex',
    gap: 6,
    alignItems: 'center',
  },
  select: {
    background: 'var(--bg)',
    color: 'var(--text)',
    border: '1px solid var(--border)',
    borderRadius: 4,
    padding: '4px 8px',
    fontFamily: 'inherit',
    fontSize: 12,
  },
  btn: {
    border: 'none',
    borderRadius: 4,
    padding: '4px 12px',
    color: 'var(--text)',
    fontFamily: 'inherit',
    fontSize: 12,
    cursor: 'pointer',
  },
  packetStatus: {
    display: 'flex',
    gap: 12,
    alignItems: 'center',
    marginLeft: 8,
    borderLeft: '1px solid var(--border)',
    paddingLeft: 12,
  },
  packetBadge: {
    display: 'flex',
    alignItems: 'center',
    gap: 4,
    fontSize: 11,
  },
  error: {
    color: 'var(--error)',
    fontSize: 11,
    width: '100%',
  },
}
