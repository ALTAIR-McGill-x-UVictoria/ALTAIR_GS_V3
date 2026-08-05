import { useEffect, useState } from 'react'
import { useSerial } from '../hooks/useSerial'
import { apiFetch, getAdminToken, setAdminToken } from '../api'

// Square hardware indicator — visually distinct from the round packet dots
function HwIndicator({ label, state, detail }) {
  // state: 'connected' | 'receiving' | 'partial' | 'disconnected'
  const COLOR = {
    connected:    '#22c55e',
    receiving:    '#38bdf8',
    partial:      '#fbbf24',
    disconnected: '#374151',
  }
  const color = COLOR[state] ?? COLOR.disconnected
  return (
    <span style={hw.badge} title={detail ?? label}>
      <span style={{ ...hw.square, background: color, boxShadow: (state === 'connected' || state === 'receiving') ? `0 0 5px ${color}66` : 'none' }} />
      <span style={{ color: state === 'disconnected' ? 'var(--muted)' : 'var(--text)', fontSize: 11 }}>{label}</span>
    </span>
  )
}

const hw = {
  badge: {
    display:    'flex',
    alignItems: 'center',
    gap:        5,
    fontFamily: 'var(--font-mono)',
  },
  square: {
    width:        8,
    height:       8,
    borderRadius: 2,
    flexShrink:   0,
    display:      'inline-block',
  },
}

const BAUD_RATES = [9600, 19200, 38400, 57600, 115200]

const FRESHNESS_STYLE = {
  ok:      { color: 'var(--ok)',   label: '●' },
  stale:   { color: 'var(--warn)', label: '●' },
  lost:    { color: 'var(--error)', label: '●' },
  waiting: { color: 'var(--muted)', label: '○' },
}

export default function ConnectionBar({ status, tunnelStatus = {}, wsReady, freshness = {}, gsGps, gsGpsStatus, mountStatus, cameraStatus }) {
  const { ports, loading, error, refreshPorts, connectPort, disconnectPort } = useSerial()
  const [selectedPort, setSelectedPort] = useState('')
  const [selectedBaud, setSelectedBaud] = useState(57600)
  const [chronyStatus, setChronyStatus] = useState(null)
  const [unlocked, setUnlocked] = useState(() => !!getAdminToken())
  const [tokenInput, setTokenInput] = useState('')

  // Refresh port list on mount and pre-select the LR-900p if found
  useEffect(() => {
    refreshPorts().then(() => {})
  }, [])

  // Poll chrony sync status — changes slowly (chrony's own poll interval is
  // typically 1+ minutes once synced), so a lightweight interval is enough.
  useEffect(() => {
    let cancelled = false
    async function poll() {
      try {
        const res = await fetch('/api/system/chrony')
        const data = await res.json()
        if (!cancelled) setChronyStatus(data)
      } catch {
        if (!cancelled) setChronyStatus(null)
      }
    }
    poll()
    const id = setInterval(poll, 10000)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  useEffect(() => {
    const lr = ports.find(p => p.is_lr900p)
    if (lr && !selectedPort) setSelectedPort(lr.device)
  }, [ports])

  function handleUnlock(e) {
    e.preventDefault()
    if (!tokenInput.trim()) return
    setAdminToken(tokenInput.trim())
    setUnlocked(true)
    setTokenInput('')
  }

  function handleLock() {
    setAdminToken('')
    setUnlocked(false)
  }

  const dotColor = status.emulating
    ? '#a78bfa'
    : status.connected ? 'var(--ok)' : wsReady ? 'var(--warn)' : 'var(--error)'
  const dotLabel = status.emulating
    ? 'Emulator active'
    : status.connected
      ? `Connected — ${status.port}`
      : wsReady ? 'No serial link' : 'Backend offline'

  return (
    <header style={styles.bar}>
      <span style={styles.title}>ALTAIR Ground Station</span>

      <div style={styles.indicator}>
        <span style={{ ...styles.dot, background: dotColor }} />
        <span style={{ color: dotColor }}>{dotLabel}</span>
        {status.emulating && (
          <span style={styles.emulatorBadge}>EMULATOR</span>
        )}
        {status.telemetry_source === 'tunnel' && (
          <span style={styles.tunnelBadge} title={`Radio quiet — live via tunnel${tunnelStatus.host ? ` (${tunnelStatus.host})` : ''}`}>
            TUNNEL ACTIVE
          </span>
        )}
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

        <button
          style={{ ...styles.btn, background: '#1e2d3d', color: 'var(--warn)', border: '1px solid var(--warn)' }}
          onClick={() => apiFetch('/api/system/restart', { method: 'POST' })}
          title="Restart backend process (uvicorn reload)"
        >
          Restart Backend
        </button>
      </div>

      <div style={styles.lockBox}>
        {unlocked ? (
          <>
            <span style={{ color: 'var(--ok)', fontSize: 11 }}>🔓 Controls unlocked</span>
            <button style={{ ...styles.btn, background: 'var(--border)' }} onClick={handleLock}>Lock</button>
          </>
        ) : (
          <form style={{ display: 'flex', gap: 6 }} onSubmit={handleUnlock}>
            <input
              type="password"
              style={styles.select}
              placeholder="Operator token"
              value={tokenInput}
              onChange={e => setTokenInput(e.target.value)}
            />
            <button type="submit" style={{ ...styles.btn, background: 'var(--accent2)' }}>Unlock</button>
          </form>
        )}
      </div>

      <div style={styles.packetStatus}>
        {Object.entries(freshness).map(([label, state]) => {
          const { color, label: dot } = FRESHNESS_STYLE[state] ?? FRESHNESS_STYLE.waiting
          return (
            <span key={label} style={styles.packetBadge} title={`${label}: ${state}`}>
              <span style={{ color }}>{dot}</span>
              <span style={{ color: state === 'ok' ? 'var(--text)' : color }}>{label}</span>
            </span>
          )
        })}
      </div>

      {/* Hardware peripheral indicators — square badges, separated from packet dots */}
      <div style={styles.hwStatus}>
        <span style={styles.hwLabel}>HW</span>

        {/* Radio: connected = serial port open */}
        <HwIndicator
          label="Radio"
          state={status.emulating ? 'partial' : status.connected ? 'connected' : 'disconnected'}
          detail={
            status.emulating ? 'Radio — emulator active' :
            status.connected ? `Radio — ${status.port}` :
            'Radio — not connected'
          }
        />

        {/* Tunnel: only shown when the backend has ALTAIR_TUNNEL_HOST configured.
            partial = configured but not currently connected, connected = link up
            (does not by itself mean it's the active telemetry source — see the
            TUNNEL ACTIVE badge above for that). */}
        {tunnelStatus.enabled && (
          <HwIndicator
            label="Tunnel"
            state={tunnelStatus.connected ? 'connected' : 'partial'}
            detail={
              tunnelStatus.connected
                ? `Tunnel — connected to ${tunnelStatus.host}:${tunnelStatus.port}`
                : `Tunnel — configured (${tunnelStatus.host}:${tunnelStatus.port}), not currently connected`
            }
          />
        )}

        {/* GS GPS (XIAO ESP32-C3 UART passthrough):
            disconnected = not found, partial = port open but silent (wiring/power issue),
            receiving = valid NMEA streaming but no fix yet, connected = has live fix */}
        <HwIndicator
          label="GS GPS"
          state={
            gsGpsStatus.has_fix   ? 'connected' :
            gsGpsStatus.receiving ? 'receiving' :
            gsGpsStatus.connected ? 'partial'   :
            'disconnected'
          }
          detail={
            gsGpsStatus.has_fix   ? `GS GPS — fix (${gsGps?.sats ?? '?'} sats, HDOP ${gsGps?.hdop?.toFixed(1) ?? '?'}) on ${gsGpsStatus.port}` :
            gsGpsStatus.receiving ? `GS GPS — wired, receiving UART/NMEA data on ${gsGpsStatus.port}, waiting for fix` :
            gsGpsStatus.connected ? `GS GPS — port open on ${gsGpsStatus.port}, no data received yet (check wiring)` :
            'GS GPS — passthrough not found'
          }
        />

        {/* Mount: connected = mount controller reports connected */}
        <HwIndicator
          label="Mount"
          state={mountStatus?.connected ? 'connected' : 'disconnected'}
          detail={
            mountStatus?.connected
              ? `Mount — ${mountStatus.mount_type} on ${mountStatus.port}`
              : 'Mount — not connected'
          }
        />

        {/* Camera: connected = camera controller reports connected */}
        <HwIndicator
          label="Camera"
          state={cameraStatus?.connected ? 'connected' : 'disconnected'}
          detail={
            cameraStatus?.connected
              ? `Camera — ${cameraStatus.camera_name ?? 'connected'}`
              : 'Camera — not connected'
          }
        />

        {/* Chrony: disconnected = chronyd not reachable, partial = running but
            not synced, connected = system clock synchronized (any source —
            GPS vs NTP pool distinguished in the tooltip, not the dot color) */}
        <HwIndicator
          label="Chrony"
          state={
            !chronyStatus?.available ? 'disconnected' :
            chronyStatus.synced      ? 'connected'    :
            'partial'
          }
          detail={
            !chronyStatus?.available
              ? 'Chrony — not running / unreachable'
              : chronyStatus.synced
                ? `Chrony — synced via ${chronyStatus.using_gps ? 'GPS' : chronyStatus.ref_name} `
                  + `(stratum ${chronyStatus.stratum}, offset ${(chronyStatus.system_offset_s * 1000).toFixed(1)}ms)`
                  + (chronyStatus.gps_present && !chronyStatus.using_gps
                      ? chronyStatus.gps_reachable ? ' — GPS available, not yet selected' : ' — GPS present, no fix yet'
                      : '')
                : 'Chrony — running, not yet synchronized'
          }
        />
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
  lockBox: {
    display: 'flex',
    gap: 6,
    alignItems: 'center',
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
  hwStatus: {
    display:     'flex',
    gap:         12,
    alignItems:  'center',
    borderLeft:  '1px solid var(--border)',
    paddingLeft: 12,
  },
  hwLabel: {
    fontSize:      9,
    letterSpacing: 1.5,
    color:         'var(--muted)',
    fontFamily:    'var(--font-mono)',
    marginRight:   2,
  },
  error: {
    color: 'var(--error)',
    fontSize: 11,
    width: '100%',
  },
  emulatorBadge: {
    background:   '#4c1d95',
    color:        '#a78bfa',
    border:       '1px solid #7c3aed',
    borderRadius: 3,
    fontSize:     9,
    fontWeight:   700,
    padding:      '1px 6px',
    letterSpacing: 1,
    fontFamily:   'var(--font-mono)',
  },
  tunnelBadge: {
    background:   '#1e2d3d',
    color:        '#38bdf8',
    border:       '1px solid #0ea5e9',
    borderRadius: 3,
    fontSize:     9,
    fontWeight:   700,
    padding:      '1px 6px',
    letterSpacing: 1,
    fontFamily:   'var(--font-mono)',
  },
}
