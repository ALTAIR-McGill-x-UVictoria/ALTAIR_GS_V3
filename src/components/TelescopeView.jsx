import { useState, useEffect, useRef } from 'react'
import { useTelescope } from '../hooks/useTelescope'
import { useSerial } from '../hooks/useSerial'

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

// Strips a leading zero as the user types past it (e.g. "0" + "5" -> "05"
// should read as "5"), without forcing an empty field back to "0" the way
// `Number(e.target.value) || 0` would — that round-trip is what causes a
// number <input>'s displayed text to get stuck with a leading zero.
function stripLeadingZero(v) {
  return v.replace(/^0+(?=\d)/, '')
}

// Looks up the "seconds" the backend already parsed for a shutter choice
// (see backend/camera_canon.py: _build_iso_shutter_choices) instead of
// re-parsing "1/125"-style strings here — Bulb has no numeric value and
// isn't settable via this dropdown (see the ⚠ reliability flag).
function canonShutterSeconds(value, shutterChoices) {
  const found = (shutterChoices || []).find(s => s.value === value)
  return found ? found.seconds : null
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
    autoCaptureEnabled,
    gpsSource,
    pointingOffset,
    actions,
  } = useTelescope()

  const { ports: serialPorts, refreshPorts } = useSerial()

  const [mountPort,    setMountPort]   = useState('')
  const [mountType,    setMountType]   = useState('nexstar')
  const [cameraType,   setCameraType]  = useState('zwo')
  const [gain,         setGain]        = useState('150')
  const [exposureMs,   setExposureMs]  = useState('1000')
  const [canonIso,      setCanonIso]      = useState('100')
  const [canonShutter,  setCanonShutter]  = useState('1/125')
  const [canonAperture, setCanonAperture] = useState('')
  const [manualAz,     setManualAz]    = useState('')
  const [manualEl,     setManualEl]    = useState('')
  const [manualRa,     setManualRa]    = useState('')
  const [manualDec,    setManualDec]   = useState('')
  const [testOffsetN,  setTestOffsetN] = useState('500')
  const [testOffsetE,  setTestOffsetE] = useState('0')
  const [testOffsetAlt, setTestOffsetAlt] = useState('1000')
  const [testLat,      setTestLat]     = useState('')
  const [testLon,      setTestLon]     = useState('')
  const [testAlt,      setTestAlt]     = useState('')
  const [testGpsError, setTestGpsError] = useState(null)
  const [testGpsLast,  setTestGpsLast]  = useState(null)
  const [capturePath,  setCapturePath] = useState('')
  const [lastCapture,  setLastCapture] = useState(null)
  const [busy,         setBusy]        = useState(false)
  const [solving,      setSolving]     = useState(false)
  const [solveMethod,  setSolveMethod] = useState('web')
  const [solveResult,  setSolveResult] = useState(null)
  const [solveError,   setSolveError]  = useState(null)
  const [applyArmed,   setApplyArmed]  = useState(false)
  const [applying,     setApplying]    = useState(false)
  const [applyError,   setApplyError]  = useState(null)
  const [applyDone,    setApplyDone]   = useState(false)
  const [offsetError,  setOffsetError] = useState(null)
  const [offsetDone,   setOffsetDone]  = useState(false)
  const [offsetAzInput, setOffsetAzInput] = useState('0')
  const [offsetElInput, setOffsetElInput] = useState('0')
  const [offsetBusy,    setOffsetBusy]    = useState(false)
  const solveAbortRef = useRef(null)
  const [images,       setImages]      = useState([])
  const [activeIndex,  setActiveIndex] = useState(0)
  const [sidebarOpen,  setSidebarOpen] = useState(true)
  const [mountError,   setMountError]  = useState(null)
  const [cameraError,  setCameraError] = useState(null)
  const [captureDir,      setCaptureDirInput] = useState('')
  const [captureDirSaved, setCaptureDirSaved] = useState('')
  const [captureDirError, setCaptureDirError] = useState(null)
  const [captureDirBusy,  setCaptureDirBusy]  = useState(false)

  const run = async (fn, setError = null) => {
    setBusy(true)
    if (setError) setError(null)
    try {
      const res = await fn()
      if (setError && res && res.ok === false) {
        setError(res.error || 'Request failed')
      }
      return res
    } catch (e) {
      if (setError) setError(e.message || 'Request failed')
    } finally {
      setBusy(false)
    }
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

  useEffect(() => {
    actions.getCaptureDir().then(cfg => {
      if (cfg?.capture_dir) {
        setCaptureDirInput(cfg.capture_dir)
        setCaptureDirSaved(cfg.capture_dir)
      }
    }).catch(() => { /* backend not ready */ })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    refreshPorts()
  }, [refreshPorts])

  // Keep the manual offset inputs showing what the backend actually has
  // applied (e.g. after a plate-solve Apply Correction, or another client's
  // edit) rather than stale local state — but only while the operator isn't
  // actively mid-edit here, so a live update doesn't yank text out from
  // under them while typing.
  const offsetInputsFocused = useRef(false)
  useEffect(() => {
    if (offsetInputsFocused.current || !pointingOffset) return
    setOffsetAzInput(String(pointingOffset.azimuth_deg ?? 0))
    setOffsetElInput(String(pointingOffset.elevation_deg ?? 0))
  }, [pointingOffset])

  // Flat-earth offset -> lat/lon, good enough at the few-km ranges this
  // panel is used for. Mirrors backend/emulator.py's _M_PER_DEG constants.
  const M_PER_DEG_LAT = 111_000
  const M_PER_DEG_LON = 80_000 // approx at ~45°N; fine for a pre-flight smoke test

  function offsetToLatLon(offsetNm, offsetEm) {
    const gsLat = tracking?.gs_lat ?? 45.5088
    const gsLon = tracking?.gs_lon ?? -73.5542
    return {
      lat: gsLat + offsetNm / M_PER_DEG_LAT,
      lon: gsLon + offsetEm / M_PER_DEG_LON,
    }
  }

  async function sendTestGps(lat, lon, alt) {
    setTestGpsError(null)
    const res = await run(() => actions.setDebugGps(lat, lon, alt), setTestGpsError)
    if (res?.ok) setTestGpsLast({ lat, lon, alt })
  }

  const mountConnected  = mountStatus?.connected  ?? false
  const activeMountType = mountStatus?.mount_type ?? mountType
  const cameraConnected = cameraStatus?.connected ?? false
  const activeImg       = images[activeIndex]

  // cameraStatus.camera_type ("zwo"/"canon") is the source of truth once
  // connected. Before that, fall back to the pending selection so the
  // ISO/shutter labels and hint below are right immediately, without
  // waiting on a round trip. ZWO's gain is a real sensor gain in dB-ish
  // arbitrary units; the Canon backend (camera_canon.py) reinterprets the
  // same gain/exposure_ms fields as ISO and shutter speed, snapped
  // server-side to the nearest value the camera actually offers — see that
  // module's docstring.
  const activeCameraType = cameraStatus?.camera_type ?? cameraType
  const isCanon = activeCameraType === 'canon'

  // Keep the ISO/shutter dropdowns pointed at whatever the camera actually
  // reports (on connect, and after any external change — e.g. another
  // client's Apply) rather than a stale default that may not even be in
  // the current choice list.
  useEffect(() => {
    if (!isCanon || !cameraStatus?.connected) return
    if (cameraStatus.gain !== undefined && String(cameraStatus.gain) !== canonIso) {
      setCanonIso(String(cameraStatus.gain))
    }
    const shutterChoices = cameraStatus.shutter || []
    if (shutterChoices.length && !shutterChoices.some(s => s.value === canonShutter)) {
      const closest = shutterChoices.reduce((best, s) => {
        if (s.seconds == null) return best
        const target = (cameraStatus.exposure_ms || 0) / 1000
        return (best == null || Math.abs(s.seconds - target) < Math.abs(best.seconds - target)) ? s : best
      }, null)
      if (closest) setCanonShutter(closest.value)
    }
    if (cameraStatus.aperture && cameraStatus.aperture !== canonAperture) {
      const apertureChoices = cameraStatus.aperture_choices || []
      // The camera can report aperture in a different string form than the
      // choices list uses (e.g. bare "8.0" vs "ƒ/8.0" — see
      // _set_aperture's docstring in camera_canon.py) — match by parsed
      // f-stop value rather than exact string so the dropdown still lands
      // on the right option.
      const reported = parseFloat(String(cameraStatus.aperture).replace(/[^\d.]/g, ''))
      const match = apertureChoices.find(v => v === cameraStatus.aperture)
        || apertureChoices.find(v => parseFloat(v.replace(/[^\d.]/g, '')) === reported)
      if (match) setCanonAperture(match)
    }
  }, [isCanon, cameraStatus]) // eslint-disable-line react-hooks/exhaustive-deps

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

        <div style={styles.columnsRow}>
        <div style={styles.column}>

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
              <option value="am5">ZWO AM5 (ASCOM)</option>
              <option value="indi">AM3/AM5 (INDI)</option>
            </select>
            {mountType === 'nexstar' && (
              <select
                style={styles.input}
                value={mountPort}
                onChange={e => setMountPort(e.target.value)}
                onFocus={refreshPorts}
                disabled={mountConnected}
              >
                <option value="">— select port —</option>
                {serialPorts.map(p => (
                  <option key={p.device} value={p.device}>
                    {p.device}{p.is_lr900p ? ' ★ LR-900p' : ''} — {p.description}
                  </option>
                ))}
              </select>
            )}
            {mountType === 'indi' && (
              <input style={styles.input} value={mountPort}
                onChange={e => setMountPort(e.target.value)}
                placeholder="INDI host (e.g. localhost or 192.168.1.50:7624)"
                disabled={mountConnected} />
            )}
            {mountConnected
              ? <button style={styles.btnDanger} disabled={busy}
                  onClick={() => run(actions.disconnectMount, setMountError)}>Disconnect</button>
              : <button style={styles.btn} disabled={busy}
                  onClick={() => run(() => actions.connectMount(mountType, mountPort), setMountError)}>Connect</button>
            }
          </div>

          {(activeMountType === 'am5' || activeMountType === 'indi')
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
                  }), setMountError)}>GoTo</button>
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
                  }), setMountError)}>GoTo</button>
              </div>
            )
          }

          {mountError && (
            <div style={{ marginTop: 6, fontSize: 11, color: C.red }}>{mountError}</div>
          )}

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
              onClick={() => run(() => actions.setTracking(!trackingEnabled), setMountError)}
            >
              {trackingEnabled ? 'Stop Tracking' : 'Start Tracking'}
            </button>
          </div>
          <div style={{ marginTop: 4, fontSize: 10, color: C.muted }}>
            Mount must be connected. Slews to computed Az/El each second.
          </div>

          <div style={{ marginTop: 10, paddingTop: 8, borderTop: `1px solid ${C.border}` }}>
            <label style={{ ...styles.label, display: 'block', marginBottom: 4 }}>GPS Source</label>
            <select
              style={styles.input}
              value={gpsSource}
              onChange={e => run(() => actions.setGpsSource(e.target.value), setMountError)}
              disabled={busy}
            >
              <option value="mavlink">MavlinkGps (Pixhawk-fused)</option>
              <option value="local">LocalGps (onboard MAX-M10M)</option>
            </select>
            <div style={{ marginTop: 4, fontSize: 10, color: C.muted }}>
              The two update independently and can go stale/zero at different
              times. If tracking aims at a bad target, try switching source.
            </div>
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

        <Section title="Pointing Offset">
          <div style={{ fontSize: 10, color: C.muted, marginBottom: 8 }}>
            Manual Az/El correction added to every computed tracking target
            (backend/tracking.py) — never touches the mount driver, so it
            works the same for any mount type. Adjust by hand, or fill it
            from a plate solve below. Resets to zero on backend restart.
          </div>
          <div style={styles.inputRow}>
            <input style={{ ...styles.input, width: 90 }} value={offsetAzInput}
              onFocus={() => { offsetInputsFocused.current = true }}
              onBlur={()  => { offsetInputsFocused.current = false }}
              onChange={e => setOffsetAzInput(e.target.value)} placeholder="Az offset °" />
            <input style={{ ...styles.input, width: 90 }} value={offsetElInput}
              onFocus={() => { offsetInputsFocused.current = true }}
              onBlur={()  => { offsetInputsFocused.current = false }}
              onChange={e => setOffsetElInput(e.target.value)} placeholder="El offset °" />
            <button style={styles.btnGreen} disabled={offsetBusy}
              onClick={async () => {
                setOffsetBusy(true)
                setOffsetError(null)
                setOffsetDone(false)
                try {
                  const res = await actions.setPointingOffset(
                    parseFloat(offsetAzInput) || 0, parseFloat(offsetElInput) || 0)
                  if (res.ok) setOffsetDone(true)
                  else setOffsetError(res.error || 'Failed to set offset')
                } catch (e) {
                  setOffsetError(e.message || 'Failed to set offset')
                } finally {
                  setOffsetBusy(false)
                }
              }}>
              Apply
            </button>
            <button style={styles.btn} disabled={offsetBusy}
              onClick={async () => {
                setOffsetBusy(true)
                setOffsetError(null)
                try {
                  await actions.setPointingOffset(0, 0)
                  setOffsetAzInput('0')
                  setOffsetElInput('0')
                  setOffsetDone(false)
                } catch (e) {
                  setOffsetError(e.message || 'Failed to clear offset')
                } finally {
                  setOffsetBusy(false)
                }
              }}>
              Clear
            </button>
          </div>
          {pointingOffset && (pointingOffset.azimuth_deg || pointingOffset.elevation_deg) ? (
            <div style={{ marginTop: 6, fontSize: 10, color: C.accent }}>
              Active: Az {pointingOffset.azimuth_deg >= 0 ? '+' : ''}
              {pointingOffset.azimuth_deg.toFixed(3)}°,
              {' '}El {pointingOffset.elevation_deg >= 0 ? '+' : ''}
              {pointingOffset.elevation_deg.toFixed(3)}°
            </div>
          ) : (
            <div style={{ marginTop: 6, fontSize: 10, color: C.muted }}>No offset applied.</div>
          )}
          {offsetError && (
            <div style={{ marginTop: 4, fontSize: 11, color: C.red }}>{offsetError}</div>
          )}
          {offsetDone && (
            <div style={{ marginTop: 4, fontSize: 11, color: C.green }}>
              Offset applied — future tracking corrected.
            </div>
          )}
        </Section>

        <Section title="Test Tracking (no payload GPS)">
          <div style={{ fontSize: 10, color: C.muted, marginBottom: 8 }}>
            Injects a fake payload GPS fix via /api/debug/set_gps so you can
            confirm goto + Auto-Track work before launch, without waiting on
            real telemetry. Connect the mount first.
          </div>

          <label style={{ ...styles.label, display: 'block', marginBottom: 4 }}>
            Offset from ground station
          </label>
          <div style={styles.inputRow}>
            <input style={{ ...styles.input, width: 70 }} value={testOffsetN}
              onChange={e => setTestOffsetN(e.target.value)} placeholder="N (m)" />
            <input style={{ ...styles.input, width: 70 }} value={testOffsetE}
              onChange={e => setTestOffsetE(e.target.value)} placeholder="E (m)" />
            <input style={{ ...styles.input, width: 70 }} value={testOffsetAlt}
              onChange={e => setTestOffsetAlt(e.target.value)} placeholder="Alt (m MSL)" />
          </div>
          <div style={{ ...styles.inputRow, marginTop: 6 }}>
            <button style={styles.btn} disabled={!mountConnected || busy}
              onClick={() => {
                const { lat, lon } = offsetToLatLon(
                  parseFloat(testOffsetN) || 0,
                  parseFloat(testOffsetE) || 0,
                )
                sendTestGps(lat, lon, parseFloat(testOffsetAlt) || 0)
              }}>
              Inject Fake Fix
            </button>
          </div>

          <div style={{ marginTop: 10, paddingTop: 8, borderTop: `1px solid ${C.border}` }}>
            <label style={{ ...styles.label, display: 'block', marginBottom: 4 }}>
              Or raw lat / lon / alt
            </label>
            <div style={styles.inputRow}>
              <input style={{ ...styles.input, width: 90 }} value={testLat}
                onChange={e => setTestLat(e.target.value)} placeholder="Lat °" />
              <input style={{ ...styles.input, width: 90 }} value={testLon}
                onChange={e => setTestLon(e.target.value)} placeholder="Lon °" />
              <input style={{ ...styles.input, width: 80 }} value={testAlt}
                onChange={e => setTestAlt(e.target.value)} placeholder="Alt (m)" />
            </div>
            <div style={{ ...styles.inputRow, marginTop: 6 }}>
              <button style={styles.btn} disabled={!mountConnected || busy || !testLat || !testLon}
                onClick={() => sendTestGps(
                  parseFloat(testLat) || 0,
                  parseFloat(testLon) || 0,
                  parseFloat(testAlt) || 0,
                )}>
                Inject Raw Fix
              </button>
            </div>
          </div>

          {testGpsError && (
            <div style={{ marginTop: 6, fontSize: 11, color: C.red }}>{testGpsError}</div>
          )}
          {testGpsLast && (
            <div style={{ marginTop: 6, fontSize: 10, color: C.muted }}>
              Last injected: {testGpsLast.lat.toFixed(6)}, {testGpsLast.lon.toFixed(6)}, {testGpsLast.alt.toFixed(0)} m
              — watch Tracking Geometry above and confirm the mount slews.
              With Auto-Track on, injecting a new fix here should re-slew
              within ~1 s without touching GoTo.
            </div>
          )}
          {!mountConnected && (
            <div style={{ marginTop: 6, fontSize: 10, color: C.yellow }}>
              Connect the mount above to enable injection.
            </div>
          )}
        </Section>

        </div>
        <div style={styles.column}>

        <Section title="Camera">
          <div style={styles.rowFlex}>
            <StatusDot ok={cameraConnected} />
            <span style={{ color: C.text, fontSize: 12 }}>
              {cameraConnected
                ? `${cameraStatus.camera_name || 'Camera'} connected`
                : 'Disconnected'}
            </span>
          </div>
          <div style={{ ...styles.inputRow, marginTop: 8 }}>
            <select
              style={{ ...styles.input, flex: 0, minWidth: 130 }}
              value={cameraType}
              onChange={e => setCameraType(e.target.value)}
              disabled={cameraConnected}
            >
              <option value="zwo">ZWO ASI585MC</option>
              <option value="canon">Canon T3i (test)</option>
            </select>
            {cameraConnected
              ? <button style={styles.btnDanger} disabled={busy}
                  onClick={() => run(actions.disconnectCamera, setCameraError)}>Disconnect</button>
              : <button style={styles.btn} disabled={busy}
                  onClick={() => run(() => actions.connectCamera(cameraType), setCameraError)}>Connect</button>
            }
          </div>

          {cameraError && (
            <div style={{ marginTop: 6, fontSize: 11, color: C.red }}>{cameraError}</div>
          )}

          <div style={{ marginTop: 10, paddingTop: 8, borderTop: `1px solid ${C.border}` }}>
            <label style={{ ...styles.label, display: 'block', marginBottom: 4 }}>Save Directory</label>
            <div style={styles.inputRow}>
              <input style={styles.input} value={captureDir}
                onChange={e => setCaptureDirInput(e.target.value)}
                placeholder="captures/" />
              <button style={styles.btn} disabled={captureDirBusy || !captureDir.trim()}
                onClick={async () => {
                  setCaptureDirBusy(true)
                  setCaptureDirError(null)
                  try {
                    const res = await actions.setCaptureDir(captureDir.trim())
                    if (res.ok) {
                      setCaptureDirInput(res.capture_dir)
                      setCaptureDirSaved(res.capture_dir)
                    } else {
                      setCaptureDirError(res.error || 'Failed to set directory')
                    }
                  } catch (e) {
                    setCaptureDirError(e.message || 'Failed to set directory')
                  } finally {
                    setCaptureDirBusy(false)
                  }
                }}>
                Set
              </button>
            </div>
            {captureDirError && (
              <div style={{ marginTop: 4, fontSize: 11, color: C.red }}>{captureDirError}</div>
            )}
            {captureDirSaved && (
              <div style={{ marginTop: 4, fontSize: 10, color: C.muted }}>
                Captures save to: {captureDirSaved}
              </div>
            )}
          </div>

          {cameraConnected && (
            <>
              {isCanon ? (
                <>
                  <div style={{ ...styles.inputRow, marginTop: 8, flexWrap: 'wrap' }}>
                    <label style={styles.label}>ISO</label>
                    <select
                      style={{ ...styles.input, width: 90 }}
                      value={canonIso}
                      onChange={e => setCanonIso(e.target.value)}
                    >
                      {(cameraStatus?.iso || []).map(v => (
                        <option key={v} value={v}>{v}</option>
                      ))}
                    </select>
                    <label style={styles.label}>Shutter</label>
                    <select
                      style={{ ...styles.input, width: 100 }}
                      value={canonShutter}
                      onChange={e => setCanonShutter(e.target.value)}
                    >
                      {(cameraStatus?.shutter || []).map(s => (
                        <option key={s.value} value={s.value}>
                          {s.value}{s.value !== 'Bulb' && 's'}{!s.reliable ? ' ⚠' : ''}
                        </option>
                      ))}
                    </select>
                    <label style={styles.label}>Aperture</label>
                    <select
                      style={{ ...styles.input, width: 90 }}
                      value={canonAperture}
                      onChange={e => setCanonAperture(e.target.value)}
                      disabled={!(cameraStatus?.aperture_choices || []).length}
                    >
                      {(cameraStatus?.aperture_choices || []).map(v => (
                        <option key={v} value={v}>{v}</option>
                      ))}
                    </select>
                    <button style={styles.btn} disabled={busy}
                      onClick={() => run(() => actions.setCameraSettings({
                        gain: Number(canonIso) || 0,
                        exposure_ms: Math.round((canonShutterSeconds(canonShutter, cameraStatus?.shutter) || 0) * 1000),
                        ...(canonAperture ? { aperture: canonAperture } : {}),
                      }), setCameraError)}>
                      Apply
                    </button>
                    <button style={styles.btn} disabled={busy}
                      onClick={() => run(actions.refreshCameraSettings, setCameraError)}>
                      Read from Camera
                    </button>
                  </div>
                  <div style={{ marginTop: 4, fontSize: 10, color: C.muted }}>
                    Dropdowns are the camera's own current choices (varies with the
                    attached lens — aperture range and shutter/flash sync limits both
                    depend on it) — picking one always sets exactly that value, no
                    snapping. ⚠ = Bulb, which has no fixed duration and isn't
                    supported by this app (set it on the camera body itself). Every
                    other listed shutter speed, whole/decimal seconds included, sets
                    reliably, and so does every aperture value. "Auto" ISO can't be
                    set remotely on some bodies either — if Apply doesn't change it,
                    use a fixed ISO value instead. Aperture has no effect if the
                    attached lens has no aperture ring/motor (e.g. some manual
                    lenses) — the dropdown is disabled if the camera reports no
                    aperture choices at all. "Read from Camera" re-queries ISO/
                    shutter/aperture from the camera without changing anything —
                    use it after adjusting a setting on the camera body directly.
                  </div>
                </>
              ) : (
                <div style={{ ...styles.inputRow, marginTop: 8 }}>
                  <label style={styles.label}>Gain</label>
                  <input type="number" style={{ ...styles.input, width: 80 }}
                    value={gain} onChange={e => setGain(stripLeadingZero(e.target.value))} />
                  <label style={styles.label}>Exp (ms)</label>
                  <input type="number" style={{ ...styles.input, width: 80 }}
                    value={exposureMs} onChange={e => setExposureMs(stripLeadingZero(e.target.value))} />
                  <button style={styles.btn} disabled={busy}
                    onClick={() => run(() => actions.setCameraSettings({
                      gain: Number(gain) || 0,
                      exposure_ms: Number(exposureMs) || 0,
                    }), setCameraError)}>
                    Apply
                  </button>
                </div>
              )}
              <div style={{ ...styles.inputRow, marginTop: 8 }}>
                <input style={styles.input} value={capturePath}
                  onChange={e => setCapturePath(e.target.value)}
                  placeholder="filename (auto if blank, saved as .fits)" />
                <button style={styles.btnGreen} disabled={busy}
                  onClick={() => run(async () => {
                    const res = await actions.captureFrame(capturePath)
                    if (res.ok) {
                      setLastCapture(res.path)
                      const filename = res.path.split(/[\\/]/).pop()
                      await fetchImages(filename)
                      setSidebarOpen(true)
                    }
                    return res
                  }, setCameraError)}>
                  Capture
                </button>
              </div>
              {lastCapture && (
                <div style={{ marginTop: 4, fontSize: 10, color: C.green }}>
                  Saved: {lastCapture}{isCanon ? ' (+ matching .cr2 RAW)' : ''}
                </div>
              )}
              <div style={{ marginTop: 4, fontSize: 10, color: C.muted }}>
                Reusing a name never overwrites — a repeat gets _1, _2, ... appended.
                {isCanon && ' Canon captures also save the original CR2 RAW file alongside the FITS.'}
              </div>

              <div style={{ ...styles.rowFlex, marginTop: 12, paddingTop: 10, borderTop: `1px solid ${C.border}` }}>
                <StatusDot ok={autoCaptureEnabled} />
                <span style={{ color: C.text, fontSize: 12 }}>
                  {autoCaptureEnabled ? 'Auto-capture active' : 'Auto-capture off'}
                </span>
              </div>
              <div style={{ ...styles.inputRow, marginTop: 8 }}>
                <button
                  style={autoCaptureEnabled ? styles.btnDanger : styles.btnGreen}
                  disabled={busy}
                  onClick={() => run(() => actions.setAutoCapture(!autoCaptureEnabled), setCameraError)}
                >
                  {autoCaptureEnabled ? 'Stop Auto-Capture' : 'Start Auto-Capture'}
                </button>
              </div>
              <div style={{ marginTop: 4, fontSize: 10, color: C.muted }}>
                Continuously captures at the current gain/exposure. Automatically
                pauses around :25-:30 and :55-:60 of every minute (beacon LEDs).
              </div>
            </>
          )}
        </Section>

        <Section title="Plate Solve">
          <div style={{ marginBottom: 8, fontSize: 10, color: C.muted }}>
            Works on any capture already on disk — the camera doesn't need to be
            connected to solve a previously saved image.
          </div>
          <div style={{ ...styles.inputRow, marginBottom: 8 }}>
            <label style={styles.label}>Solver</label>
            <select
              style={{ ...styles.input, flex: 0, minWidth: 160 }}
              value={solveMethod}
              onChange={e => setSolveMethod(e.target.value)}
              disabled={solving}
            >
              <option value="web">astrometry.net (web API)</option>
              <option value="local">Local (Docker solve-field)</option>
            </select>
          </div>
          <div style={{ ...styles.inputRow }}>
            <button style={styles.btn} disabled={solving || !activeImg}
              onClick={async () => {
                setSolving(true)
                setSolveError(null)
                setSolveResult(null)
                setApplyArmed(false)
                setApplyError(null)
                setApplyDone(false)
                const controller = new AbortController()
                solveAbortRef.current = controller
                try {
                  const res = await actions.solveFrame(activeImg.filename, controller.signal, solveMethod)
                  if (res.ok) setSolveResult(res)
                  else setSolveError(
                    (res.error || 'Solve failed')
                    + (res.elapsed_s != null ? ` (after ${res.elapsed_s.toFixed(1)}s)` : ''))
                } catch (e) {
                  if (e.name === 'AbortError') {
                    setSolveError('Cancelled — the solve may still finish server-side, '
                      + 'but this page stopped waiting for it.')
                  } else {
                    setSolveError(e.message || 'Solve failed')
                  }
                } finally {
                  solveAbortRef.current = null
                  setSolving(false)
                }
              }}>
              {solving ? 'Solving…' : 'Plate-Solve Current Image'}
            </button>
            {solving && (
              <button style={styles.btnDanger}
                onClick={() => solveAbortRef.current?.abort()}>
                Cancel
              </button>
            )}
          </div>
          <div style={{ marginTop: 4, fontSize: 10, color: C.muted }}>
            {activeImg
              ? <>Solves "{activeImg.filename}" using {solveMethod === 'local'
                  ? 'the local Docker solve-field engine (no network round-trip, '
                    + 'needs Docker + index files set up on the server)'
                  : 'astrometry.net\'s web API (can take a minute or more)'}
                  {' '}and reports how far off the mount actually is —
                  read-only, nothing is sent to the mount.</>
              : 'Capture or select an image first.'}
          </div>
          {solving && (
            <div style={{ marginTop: 6, fontSize: 10, color: C.muted }}>
              {solveMethod === 'local'
                ? 'Solving locally via Docker — other controls still work while you wait.'
                : 'Submitted to astrometry.net — this can take a while, other controls '
                  + 'still work while you wait. Cancel stops this page from waiting on it; '
                  + 'the solve may still complete on astrometry.net\'s end regardless.'}
            </div>
          )}
          {solveError && (
            <div style={{ marginTop: 6, fontSize: 11, color: C.red }}>{solveError}</div>
          )}
          {solveResult && (
            <div style={{
              marginTop: 6, padding: 8, borderRadius: 4,
              border: `1px solid ${C.border}`, background: '#0e2030',
              fontSize: 11, color: C.text, lineHeight: 1.6,
            }}>
              <div>Solved center ({solveResult.method === 'local' ? 'local' : 'web'}
                {solveResult.elapsed_s != null ? `, ${solveResult.elapsed_s.toFixed(1)}s` : ''}):
                {' '}RA {solveResult.solved_ra_hours?.toFixed(4)}h,
                Dec {solveResult.solved_dec_deg?.toFixed(4)}°</div>
              {solveResult.offset_azimuth_deg !== undefined ? (
                <>
                  <div style={{ marginTop: 4, color: C.accent, fontWeight: 600 }}>
                    Add to mount: Az {solveResult.offset_azimuth_deg >= 0 ? '+' : ''}
                    {solveResult.offset_azimuth_deg.toFixed(3)}°,
                    {' '}El {solveResult.offset_elevation_deg >= 0 ? '+' : ''}
                    {solveResult.offset_elevation_deg.toFixed(3)}°
                  </div>
                  <div style={{ marginTop: 2, fontSize: 10, color: C.muted }}>
                    Mount reported Az {solveResult.mount_azimuth?.toFixed(3)}°,
                    El {solveResult.mount_elevation?.toFixed(3)}° at capture time —
                    total error {(solveResult.err_vs_mount_arcsec / 60).toFixed(1)}′
                  </div>

                  <div style={{ marginTop: 8, paddingTop: 8, borderTop: `1px solid ${C.border}` }}>
                    <button style={styles.btnGreen} disabled={applying}
                      onClick={async () => {
                        setApplying(true)
                        setOffsetError(null)
                        setOffsetDone(false)
                        try {
                          const res = await actions.setPointingOffset(
                            solveResult.offset_azimuth_deg, solveResult.offset_elevation_deg)
                          if (res.ok) {
                            setOffsetAzInput(String(solveResult.offset_azimuth_deg))
                            setOffsetElInput(String(solveResult.offset_elevation_deg))
                            setOffsetDone(true)
                          } else {
                            setOffsetError(res.error || 'Failed to set offset')
                          }
                        } catch (e) {
                          setOffsetError(e.message || 'Failed to set offset')
                        } finally {
                          setApplying(false)
                        }
                      }}>
                      {applying ? 'Applying…' : 'Use This Offset'}
                    </button>
                    <div style={{ marginTop: 4, fontSize: 10, color: C.muted }}>
                      Sets the Pointing Offset above to this Az/El delta — never
                      touches the mount driver, works for any mount type. Fine-tune
                      it manually in the Pointing Offset section once applied.
                    </div>
                    {offsetError && (
                      <div style={{ marginTop: 4, fontSize: 11, color: C.red }}>{offsetError}</div>
                    )}
                    {offsetDone && (
                      <div style={{ marginTop: 4, fontSize: 11, color: C.green }}>
                        Offset applied — future tracking corrected.
                      </div>
                    )}

                    {activeMountType === 'am5' && (
                      <div style={{ marginTop: 10, paddingTop: 8, borderTop: `1px solid ${C.border}` }}>
                        <button
                          style={applyArmed ? styles.btnDanger : styles.btn}
                          disabled={applying || !mountConnected}
                          onClick={async () => {
                            if (!applyArmed) { setApplyArmed(true); return }
                            setApplying(true)
                            setApplyError(null)
                            try {
                              const res = await actions.applySolveCorrection(
                                solveResult.solved_ra_hours, solveResult.solved_dec_deg)
                              if (res.ok) setApplyDone(true)
                              else setApplyError(res.error || 'Apply failed')
                            } catch (e) {
                              setApplyError(e.message || 'Apply failed')
                            } finally {
                              setApplying(false)
                              setApplyArmed(false)
                            }
                          }}>
                          {applying ? 'Syncing…'
                            : applyArmed ? 'Confirm: sync mount to solved position'
                            : 'Apply via ASCOM Sync Instead'}
                        </button>
                        {applyArmed && !applying && (
                          <button style={{ ...styles.btn, marginLeft: 6 }}
                            onClick={() => setApplyArmed(false)}>
                            Cancel
                          </button>
                        )}
                        <div style={{ marginTop: 4, fontSize: 10, color: C.muted }}>
                          {!mountConnected
                            ? 'Mount not connected.'
                            : applyArmed
                              ? 'This tells the mount\'s ASCOM driver it is actually at the '
                                + 'solved RA/Dec above (a sync — the mount does not physically '
                                + 'move). Click again to confirm, or Cancel.'
                              : 'Alternative to the offset above: syncs the AM5\'s ASCOM '
                                + 'alignment model directly. No physical movement.'}
                        </div>
                        {applyError && (
                          <div style={{ marginTop: 4, fontSize: 11, color: C.red }}>{applyError}</div>
                        )}
                        {applyDone && (
                          <div style={{ marginTop: 4, fontSize: 11, color: C.green }}>
                            Synced — mount alignment updated.
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </>
              ) : (
                <div style={{ marginTop: 4, fontSize: 10, color: C.muted }}>
                  No mount pointing recorded in this capture's metadata to compare against.
                </div>
              )}
            </div>
          )}
        </Section>

        </div>
        </div>

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
            : (() => {
                // Only overlay if the solve we have is for the image actually
                // on screen — solveResult isn't cleared on nav/thumbnail
                // clicks, so without this a stale solve would draw on the
                // wrong frame.
                const overlay = solveResult
                  && solveResult.filename === activeImg.filename
                  && solveResult.naxis1 && solveResult.naxis2
                  ? solveResult : null
                return (
                  <div style={{
                    position: 'relative', maxWidth: '100%', maxHeight: '100%',
                    aspectRatio: overlay ? `${overlay.naxis1} / ${overlay.naxis2}` : undefined,
                    display: 'flex',
                  }}>
                    <img src={activeImg.full_url} alt={activeImg.filename}
                      style={{ maxWidth: '100%', maxHeight: '100%', width: overlay ? '100%' : undefined,
                        height: overlay ? '100%' : undefined, objectFit: 'contain', display: 'block' }} />
                    {overlay && (
                      <svg
                        viewBox={`0 0 ${overlay.naxis1} ${overlay.naxis2}`}
                        style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}
                      >
                        {(() => {
                          const [cx, cy] = overlay.center_px
                          const r = overlay.naxis1 * 0.02
                          const fontSize = overlay.naxis1 * 0.018
                          const mount = overlay.mount_px
                          return (
                            <>
                              {mount && (
                                <line x1={cx} y1={cy} x2={mount[0]} y2={mount[1]}
                                  stroke={C.yellow} strokeWidth={overlay.naxis1 * 0.0015} strokeDasharray="6,4" />
                              )}
                              {/* Solved center — cyan crosshair */}
                              <g stroke={C.accent} strokeWidth={overlay.naxis1 * 0.002}>
                                <line x1={cx - r} y1={cy} x2={cx + r} y2={cy} />
                                <line x1={cx} y1={cy - r} x2={cx} y2={cy + r} />
                                <circle cx={cx} cy={cy} r={r * 0.5} fill="none" />
                              </g>
                              <text x={cx + r * 1.2} y={cy - r * 0.3} fill={C.accent}
                                fontSize={fontSize} fontFamily="monospace">
                                actual center
                              </text>
                              {mount && (
                                <>
                                  {/* Mount-reported position — yellow marker */}
                                  <g stroke={C.yellow} strokeWidth={overlay.naxis1 * 0.002}>
                                    <line x1={mount[0] - r} y1={mount[1] - r} x2={mount[0] + r} y2={mount[1] + r} />
                                    <line x1={mount[0] - r} y1={mount[1] + r} x2={mount[0] + r} y2={mount[1] - r} />
                                  </g>
                                  <text x={mount[0] + r * 1.2} y={mount[1] - r * 0.3} fill={C.yellow}
                                    fontSize={fontSize} fontFamily="monospace">
                                    mount belief
                                  </text>
                                </>
                              )}
                            </>
                          )
                        })()}
                      </svg>
                    )}
                  </div>
                )
              })()
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
  // Groups related sections side by side (Mount/Tracking vs Camera/Plate
  // Solve) on wide viewports; stacks back to one column when narrow so it
  // still works in the sidebar-open layout / smaller windows.
  columnsRow: {
    display:               'grid',
    gridTemplateColumns:   'repeat(auto-fit, minmax(320px, 1fr))',
    gap:                   12,
    alignItems:            'start',
  },
  column: {
    display:       'flex',
    flexDirection: 'column',
    gap:           12,
    minWidth:      0,
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
