import { useState, useEffect } from 'react'
import { useTelescope } from '../hooks/useTelescope'

const C = {
  accent:  '#00e5ff',
  green:   '#00ff88',
  yellow:  '#ffd600',
  red:     '#ff4444',
  muted:   '#607080',
  surface: '#0d1117',
  border:  '#1e2d3d',
  text:    '#c9d1d9',
}

const SIDEBAR_W = '420px'

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function StatusDot({ ok }) {
  return (
    <span style={{
      display:      'inline-block',
      width:        8,
      height:       8,
      borderRadius: '50%',
      background:   ok ? C.green : C.red,
      marginRight:  6,
      flexShrink:   0,
    }} />
  )
}

function Section({ title, children }) {
  return (
    <div style={styles.section}>
      <div style={styles.sectionTitle}>{title}</div>
      {children}
    </div>
  )
}

function Row({ label, value, unit }) {
  return (
    <div style={styles.row}>
      <span style={styles.rowLabel}>{label}</span>
      <span style={styles.rowValue}>
        {value !== undefined && value !== null ? String(value) : '—'}
        {unit && <span style={{ color: C.muted, marginLeft: 4 }}>{unit}</span>}
      </span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Compass rose
// ---------------------------------------------------------------------------

function CompassRose({ azimuth, elevation }) {
  const az  = azimuth   ?? 0
  const el  = elevation ?? 0
  const r   = 60
  const cx  = 80
  const cy  = 80
  const rad = (az - 90) * Math.PI / 180
  const tx  = cx + r * Math.cos(rad)
  const ty  = cy + r * Math.sin(rad)

  return (
    <svg width={160} height={160} style={{ display: 'block', margin: '0 auto' }}>
      <circle cx={cx} cy={cy} r={r + 10} fill="none" stroke={C.border} strokeWidth={1} />
      {[['N', 0, -1], ['E', 1, 0], ['S', 0, 1], ['W', -1, 0]].map(([lbl, dx, dy]) => (
        <text key={lbl} x={cx + dx * (r + 16)} y={cy + dy * (r + 16) + 4}
          textAnchor="middle" fill={C.muted} fontSize={10} fontFamily="monospace">
          {lbl}
        </text>
      ))}
      <line x1={cx} y1={cy} x2={tx} y2={ty}
        stroke={C.accent} strokeWidth={2} strokeLinecap="round" />
      <circle cx={tx} cy={ty} r={4} fill={C.accent} />
      <text x={cx} y={cy + r + 28} textAnchor="middle"
        fill={C.yellow} fontSize={10} fontFamily="monospace">
        El {el.toFixed(1)}°
      </text>
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function TelescopeView() {
  const {
    wsReady,
    tracking,
    mountStatus,
    cameraStatus,
    trackingEnabled,
    actions,
  } = useTelescope()

  const [mountPort,    setMountPort]   = useState('COM10')
  const [mountType,    setMountType]   = useState('nexstar')
  const [gain,         setGain]        = useState(150)
  const [exposureMs,   setExposureMs]  = useState(1000)
  const [manualAz,     setManualAz]    = useState('')
  const [manualEl,     setManualEl]    = useState('')
  const [manualRa,     setManualRa]    = useState('')
  const [manualDec,    setManualDec]   = useState('')
  const [capturePath,  setCapturePath] = useState('')
  const [lastCapture,  setLastCapture] = useState(null)
  const [busy,         setBusy]        = useState(false)
  const [images,       setImages]      = useState([])
  const [activeIndex,  setActiveIndex] = useState(0)
  const [sidebarOpen,  setSidebarOpen] = useState(true)

  const run = async (fn) => {
    setBusy(true)
    try { await fn() } finally { setBusy(false) }
  }

  async function fetchImages(selectFilename = null) {
    try {
      const res  = await fetch('/api/gallery/images')
      const list = await res.json()
      setImages(list)
      if (selectFilename) {
        const idx = list.findIndex(i => i.filename === selectFilename)
        setActiveIndex(idx !== -1 ? idx : 0)
      }
    } catch (_) { /* backend not ready */ }
  }

  useEffect(() => {
    fetchImages()
    const id = setInterval(fetchImages, 5000)
    return () => clearInterval(id)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const mountConnected  = mountStatus?.connected  ?? false
  const activeMountType = mountStatus?.mount_type ?? mountType
  const cameraConnected = cameraStatus?.connected ?? false
  const activeImg       = images[activeIndex]

  return (
    <div style={styles.root}>

      {/* ============================================================
          Left column — controls (always visible, scrollable)
      ============================================================ */}
      <div style={styles.leftCol}>

        {/* Toggle button row */}
        <div style={styles.toggleRow}>
          <button
            style={sidebarOpen ? styles.btnActive : styles.btn}
            onClick={() => setSidebarOpen(v => !v)}
          >
            {sidebarOpen ? '▶ Images' : '◀ Images'}
          </button>
          <span style={{ fontSize: 10, color: C.muted }}>
            {images.length} capture{images.length !== 1 ? 's' : ''}
          </span>
        </div>

        <Section title="Mount">
          <div style={styles.rowFlex}>
            <StatusDot ok={mountConnected} />
            <span style={{ color: C.text, fontSize: 12 }}>
              {mountConnected
                ? `Connected — ${mountStatus.mount_type?.toUpperCase()} — ${mountStatus.port}`
                : 'Disconnected'}
            </span>
          </div>
          <div style={{ ...styles.inputRow, marginTop: 8 }}>
            <select
              style={{ ...styles.input, flex: 0, minWidth: 90 }}
              value={mountType}
              onChange={e => setMountType(e.target.value)}
              disabled={mountConnected}
            >
              <option value="nexstar">NexStar</option>
              <option value="am5">ZWO AM5</option>
            </select>
            {mountType === 'nexstar' && (
              <input
                style={styles.input}
                value={mountPort}
                onChange={e => setMountPort(e.target.value)}
                placeholder="COM port"
                disabled={mountConnected}
              />
            )}
            {mountConnected
              ? <button style={styles.btnDanger} disabled={busy}
                  onClick={() => run(actions.disconnectMount)}>Disconnect</button>
              : <button style={styles.btn} disabled={busy}
                  onClick={() => run(() => actions.connectMount(mountType, mountPort))}>Connect</button>
            }
          </div>

          {activeMountType === 'am5'
            ? (
              <div style={{ ...styles.inputRow, marginTop: 8 }}>
                <input style={{ ...styles.input, width: 90 }} value={manualRa}
                  onChange={e => setManualRa(e.target.value)} placeholder="RA (h)"
                  disabled={!mountConnected} />
                <input style={{ ...styles.input, width: 90 }} value={manualDec}
                  onChange={e => setManualDec(e.target.value)} placeholder="Dec (°)"
                  disabled={!mountConnected} />
                <button style={styles.btn} disabled={!mountConnected || busy}
                  onClick={() => run(() => actions.gotoMount({
                    ra_hours: parseFloat(manualRa)  || 0,
                    dec_deg:  parseFloat(manualDec) || 0,
                  }))}>GoTo</button>
              </div>
            ) : (
              <div style={{ ...styles.inputRow, marginTop: 8 }}>
                <input style={{ ...styles.input, width: 80 }} value={manualAz}
                  onChange={e => setManualAz(e.target.value)} placeholder="Az°"
                  disabled={!mountConnected} />
                <input style={{ ...styles.input, width: 80 }} value={manualEl}
                  onChange={e => setManualEl(e.target.value)} placeholder="El°"
                  disabled={!mountConnected} />
                <button style={styles.btn} disabled={!mountConnected || busy}
                  onClick={() => run(() => actions.gotoMount({
                    azimuth:   parseFloat(manualAz) || 0,
                    elevation: parseFloat(manualEl) || 0,
                  }))}>GoTo</button>
              </div>
            )
          }

          {mountStatus?.position && (
            <div style={{ marginTop: 6 }}>
              <Row label="Az"  value={mountStatus.position.azimuth?.toFixed(3)}   unit="°" />
              <Row label="El"  value={mountStatus.position.elevation?.toFixed(3)} unit="°" />
              {mountStatus.position.ra_hours !== undefined && (
                <Row label="RA"  value={mountStatus.position.ra_hours?.toFixed(5)} unit="h" />
              )}
              {mountStatus.position.dec_deg !== undefined && (
                <Row label="Dec" value={mountStatus.position.dec_deg?.toFixed(4)}  unit="°" />
              )}
            </div>
          )}
        </Section>

        <Section title="Auto-Track">
          <div style={styles.rowFlex}>
            <StatusDot ok={trackingEnabled} />
            <span style={{ color: C.text, fontSize: 12 }}>
              {trackingEnabled ? 'Tracking active' : 'Tracking off'}
            </span>
          </div>
          <div style={{ ...styles.inputRow, marginTop: 8 }}>
            <button
              style={trackingEnabled ? styles.btnDanger : styles.btnGreen}
              disabled={!mountConnected || busy}
              onClick={() => run(() => actions.setTracking(!trackingEnabled))}
            >
              {trackingEnabled ? 'Stop Tracking' : 'Start Tracking'}
            </button>
          </div>
          <div style={{ marginTop: 4, fontSize: 10, color: C.muted }}>
            Mount must be connected. Slews to computed Az/El each second.
          </div>
        </Section>

        <Section title="Tracking Geometry">
          {tracking
            ? (
              <>
                <CompassRose azimuth={tracking.azimuth} elevation={tracking.elevation} />
                <div style={{ marginTop: 8 }}>
                  <Row label="Azimuth"    value={tracking.azimuth?.toFixed(2)}   unit="°" />
                  <Row label="Elevation"  value={tracking.elevation?.toFixed(2)} unit="°" />
                  <Row label="RA"         value={tracking.ra_hours?.toFixed(5)}  unit="h" />
                  <Row label="Dec"        value={tracking.dec_deg?.toFixed(4)}   unit="°" />
                  <Row label="Horiz dist" value={(tracking.distance_m / 1000).toFixed(2)} unit="km" />
                  <Row label="Slant"      value={(tracking.slant_m    / 1000).toFixed(2)} unit="km" />
                  <Row label="GS lat"     value={tracking.gs_lat}               unit="°" />
                  <Row label="GS lon"     value={tracking.gs_lon}               unit="°" />
                </div>
              </>
            )
            : (
              <div style={{ color: C.muted, fontSize: 12, padding: '24px 0', textAlign: 'center' }}>
                Waiting for GPS telemetry…
              </div>
            )
          }
          <div style={{ marginTop: 8, paddingTop: 6, borderTop: `1px solid ${C.border}` }}>
            <Row label="Telescope WS" value={wsReady ? 'connected' : 'disconnected'} />
          </div>
        </Section>

        <Section title="Camera">
          <div style={styles.rowFlex}>
            <StatusDot ok={cameraConnected} />
            <span style={{ color: C.text, fontSize: 12 }}>
              {cameraConnected ? `${cameraStatus.camera_name || 'Camera'} connected` : 'Disconnected'}
            </span>
          </div>
          <div style={{ ...styles.inputRow, marginTop: 8 }}>
            {cameraConnected
              ? <button style={styles.btnDanger} disabled={busy}
                  onClick={() => run(actions.disconnectCamera)}>Disconnect</button>
              : <button style={styles.btn} disabled={busy}
                  onClick={() => run(actions.connectCamera)}>Connect</button>
            }
          </div>

          {cameraConnected && (
            <>
              <div style={{ ...styles.inputRow, marginTop: 8 }}>
                <label style={styles.label}>Gain</label>
                <input type="number" style={{ ...styles.input, width: 80 }}
                  value={gain} onChange={e => setGain(Number(e.target.value))} />
                <label style={styles.label}>Exp (ms)</label>
                <input type="number" style={{ ...styles.input, width: 80 }}
                  value={exposureMs} onChange={e => setExposureMs(Number(e.target.value))} />
                <button style={styles.btn} disabled={busy}
                  onClick={() => run(() => actions.setCameraSettings({ gain, exposure_ms: exposureMs }))}>
                  Apply
                </button>
              </div>
              <div style={{ ...styles.inputRow, marginTop: 8 }}>
                <input style={styles.input} value={capturePath}
                  onChange={e => setCapturePath(e.target.value)}
                  placeholder="filename (auto if blank)" />
                <button style={styles.btnGreen} disabled={busy}
                  onClick={() => run(async () => {
                    const res = await actions.captureFrame(capturePath)
                    if (res.ok) {
                      setLastCapture(res.path)
                      const filename = res.path.split(/[\\/]/).pop()
                      await fetchImages(filename)
                      setSidebarOpen(true)
                    }
                  })}>
                  Capture
                </button>
              </div>
              {lastCapture && (
                <div style={{ marginTop: 4, fontSize: 10, color: C.green }}>
                  Saved: {lastCapture}
                </div>
              )}
            </>
          )}
        </Section>

      </div>

      {/* ============================================================
          Right sidebar — image viewer + list (toggleable)
      ============================================================ */}
      <div style={{
        ...styles.sidebar,
        width:      sidebarOpen ? SIDEBAR_W : '0',
        minWidth:   sidebarOpen ? SIDEBAR_W : '0',
        opacity:    sidebarOpen ? 1 : 0,
        pointerEvents: sidebarOpen ? 'auto' : 'none',
      }}>

        {/* Top: image viewer */}
        <div style={styles.imgViewer}>
          {!activeImg
            ? <div style={{ color: C.muted, fontSize: 11, margin: 'auto' }}>No captures yet.</div>
            : <img src={activeImg.full_url} alt={activeImg.filename}
                style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain', display: 'block' }} />
          }
        </div>

        {/* Caption + nav bar */}
        <div style={styles.imgCaption}>
          {activeImg
            ? (
              <>
                <span style={{ color: C.accent, fontSize: 10, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {activeImg.filename}
                </span>
                <span style={{ color: C.muted, fontSize: 10, whiteSpace: 'nowrap', margin: '0 6px' }}>
                  {new Date(activeImg.mtime * 1000).toLocaleTimeString()}&nbsp;·&nbsp;{activeImg.size_kb} KB
                </span>
                <button style={styles.navBtn} disabled={activeIndex === 0}
                  onClick={() => setActiveIndex(i => i - 1)}>◀</button>
                <span style={{ fontSize: 10, color: C.muted, padding: '0 6px', whiteSpace: 'nowrap' }}>
                  {activeIndex + 1}/{images.length}
                </span>
                <button style={styles.navBtn} disabled={activeIndex === images.length - 1}
                  onClick={() => setActiveIndex(i => i + 1)}>▶</button>
              </>
            )
            : <span style={{ color: C.muted, fontSize: 10 }}>No captures yet.</span>
          }
        </div>

        {/* Bottom: image list */}
        <div style={styles.imgList}>
          {images.map((img, idx) => (
            <div
              key={img.filename}
              style={{
                ...styles.imgListRow,
                background: idx === activeIndex ? '#0e2030' : 'transparent',
                borderLeft: `2px solid ${idx === activeIndex ? C.accent : 'transparent'}`,
              }}
              onClick={() => setActiveIndex(idx)}
            >
              <img src={img.url} alt=""
                style={{ width: 48, height: 36, objectFit: 'contain', background: '#000', flexShrink: 0, borderRadius: 2 }} />
              <div style={{ flex: 1, overflow: 'hidden' }}>
                <div style={{ fontSize: 10, color: C.accent, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {img.filename}
                </div>
                <div style={{ fontSize: 10, color: C.muted }}>
                  {new Date(img.mtime * 1000).toLocaleTimeString()}&nbsp;·&nbsp;{img.size_kb} KB
                </div>
              </div>
            </div>
          ))}
        </div>

      </div>

    </div>
  )
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const styles = {
  root: {
    display:    'flex',
    flex:       1,
    overflow:   'hidden',
    alignItems: 'stretch',
  },
  // Left column: all controls, scrollable
  leftCol: {
    flex:          1,
    display:       'flex',
    flexDirection: 'column',
    gap:           12,
    minWidth:      280,
    padding:       16,
    overflowY:     'auto',
  },
  // Toggle button row at top of left column
  toggleRow: {
    display:    'flex',
    alignItems: 'center',
    gap:        10,
  },
  // Right sidebar: fixed width, slides in/out
  sidebar: {
    display:        'flex',
    flexDirection:  'column',
    overflow:       'hidden',
    borderLeft:     `1px solid ${C.border}`,
    transition:     'width 0.25s ease, min-width 0.25s ease, opacity 0.2s ease',
    flexShrink:     0,
  },
  // Image viewer pane: 60% of sidebar height
  imgViewer: {
    flex:           '0 0 60%',
    display:        'flex',
    alignItems:     'center',
    justifyContent: 'center',
    background:     '#000',
    overflow:       'hidden',
    minHeight:      0,
  },
  // Caption + nav bar
  imgCaption: {
    display:      'flex',
    alignItems:   'center',
    gap:          4,
    padding:      '4px 8px',
    background:   C.surface,
    borderTop:    `1px solid ${C.border}`,
    borderBottom: `1px solid ${C.border}`,
    flexShrink:   0,
  },
  // Image list: 40% of sidebar height, scrollable
  imgList: {
    flex:      '0 0 40%',
    overflowY: 'auto',
  },
  imgListRow: {
    display:      'flex',
    alignItems:   'center',
    gap:          8,
    padding:      '5px 8px',
    cursor:       'pointer',
    borderBottom: `1px solid ${C.border}`,
  },
  navBtn: {
    background:   'transparent',
    border:       `1px solid ${C.border}`,
    borderRadius: 3,
    color:        C.muted,
    fontFamily:   'var(--font-mono)',
    fontSize:     10,
    padding:      '2px 6px',
    cursor:       'pointer',
  },
  section: {
    background:   C.surface,
    border:       `1px solid ${C.border}`,
    borderRadius: 6,
    padding:      12,
  },
  sectionTitle: {
    fontFamily:    'var(--font-mono)',
    fontSize:      10,
    letterSpacing: 2,
    color:         C.accent,
    textTransform: 'uppercase',
    marginBottom:  10,
  },
  row: {
    display:        'flex',
    justifyContent: 'space-between',
    fontSize:       12,
    padding:        '2px 0',
  },
  rowLabel: {
    color:      C.muted,
    fontFamily: 'var(--font-mono)',
  },
  rowValue: {
    color:      C.text,
    fontFamily: 'var(--font-mono)',
  },
  rowFlex: {
    display:    'flex',
    alignItems: 'center',
    fontSize:   12,
  },
  inputRow: {
    display:    'flex',
    gap:        6,
    alignItems: 'center',
    flexWrap:   'wrap',
  },
  input: {
    background:   '#161b22',
    border:       `1px solid ${C.border}`,
    borderRadius: 4,
    color:        C.text,
    fontFamily:   'var(--font-mono)',
    fontSize:     11,
    padding:      '4px 8px',
    flex:         1,
    minWidth:     60,
  },
  label: {
    color:      C.muted,
    fontSize:   11,
    fontFamily: 'var(--font-mono)',
  },
  btn: {
    background:   'transparent',
    border:       `1px solid ${C.accent}`,
    borderRadius: 4,
    color:        C.accent,
    fontFamily:   'var(--font-mono)',
    fontSize:     11,
    padding:      '4px 10px',
    cursor:       'pointer',
    whiteSpace:   'nowrap',
  },
  btnActive: {
    background:   'rgba(0,229,255,0.12)',
    border:       `1px solid ${C.accent}`,
    borderRadius: 4,
    color:        C.accent,
    fontFamily:   'var(--font-mono)',
    fontSize:     11,
    padding:      '4px 10px',
    cursor:       'pointer',
    whiteSpace:   'nowrap',
  },
  btnDanger: {
    background:   'transparent',
    border:       `1px solid ${C.red}`,
    borderRadius: 4,
    color:        C.red,
    fontFamily:   'var(--font-mono)',
    fontSize:     11,
    padding:      '4px 10px',
    cursor:       'pointer',
    whiteSpace:   'nowrap',
  },
  btnGreen: {
    background:   'transparent',
    border:       `1px solid ${C.green}`,
    borderRadius: 4,
    color:        C.green,
    fontFamily:   'var(--font-mono)',
    fontSize:     11,
    padding:      '4px 10px',
    cursor:       'pointer',
    whiteSpace:   'nowrap',
  },
}
