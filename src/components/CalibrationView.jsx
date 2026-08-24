import { useState, useEffect } from 'react'
import { useTelescope } from '../hooks/useTelescope'
import { useCalibration } from '../hooks/useCalibration'

// Same palette/style tokens as TelescopeView.jsx — kept local rather than a
// shared module since neither file currently imports from the other.
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

function fieldValue(packet, name) {
  return packet?.fields?.find(f => f.name === name)?.value
}

/**
 * Camera Calibration tab — captures against the precision integrating
 * sphere source, correlating each exposure with the sphere's DAC/current
 * telemetry (Lighting packet) and the UVIC PDRO photodiode readout
 * (PhotodiodeSignal packet) for the duration of the shutter.
 *
 * Camera connect/disconnect/settings reuse useTelescope()'s existing
 * actions — same camera_controller instance backend-side as the Telescope
 * tab, since ZWO/Canon hardware only has one physical camera attached at a
 * time. Capture itself goes through /api/calibration/capture instead of
 * /api/telescope/camera/capture, which saves capture.fits/.txt/.csv together
 * into a new calibration_logs/<UTC timestamp>_<label>/ subdirectory per
 * capture rather than a single file in the gallery's capture dir — see
 * backend/calibration.py.
 *
 * Props:
 *   packets — the same `packets` map App.jsx already threads to every tab
 *             (from useTelemetry()); read here for live Lighting +
 *             PhotodiodeSignal fields so this tab doesn't open a second
 *             telemetry connection.
 */
export default function CalibrationView({ packets }) {
  const { cameraStatus, actions: telescopeActions } = useTelescope()
  const { actions } = useCalibration()

  const [cameraType,  setCameraType]  = useState('zwo')
  const [gain,        setGain]        = useState('150')
  const [exposureMs,  setExposureMs]  = useState('1000')
  const [canonIso,      setCanonIso]      = useState('100')
  const [canonShutter,  setCanonShutter]  = useState('1/125')
  const [canonAperture, setCanonAperture] = useState('')
  const [busy,         setBusy]        = useState(false)
  const [cameraError,  setCameraError] = useState(null)

  const [targetA,      setTargetA]      = useState('0.28')
  const [brightnessBusy, setBrightnessBusy] = useState(false)
  const [brightnessError, setBrightnessError] = useState(null)
  const [brightnessAck,  setBrightnessAck]  = useState(null)

  const [captureLabel,  setCaptureLabel]  = useState('')
  const [marginPreS,   setMarginPreS]   = useState('0.5')
  const [marginPostS,  setMarginPostS]  = useState('0.5')
  const [capturing,    setCapturing]    = useState(false)
  const [captureError, setCaptureError] = useState(null)
  const [lastCapture,  setLastCapture]  = useState(null)

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

  const cameraConnected = cameraStatus?.connected ?? false

  // cameraStatus.camera_type ("zwo"/"canon") is the source of truth once
  // connected — same derivation as TelescopeView.jsx, since both tabs
  // control the same camera_controller instance backend-side.
  const activeCameraType = cameraStatus?.camera_type ?? cameraType
  const isCanon = activeCameraType === 'canon'

  // Keep the ISO/shutter/aperture dropdowns pointed at whatever the camera
  // actually reports (on connect, and after any external change — e.g. the
  // Telescope tab applying a setting) rather than a stale default. Mirrors
  // TelescopeView.jsx's identical effect.
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
      const reported = parseFloat(String(cameraStatus.aperture).replace(/[^\d.]/g, ''))
      const match = apertureChoices.find(v => v === cameraStatus.aperture)
        || apertureChoices.find(v => parseFloat(v.replace(/[^\d.]/g, '')) === reported)
      if (match) setCanonAperture(match)
    }
  }, [isCanon, cameraStatus]) // eslint-disable-line react-hooks/exhaustive-deps

  const lightingPkt    = packets?.Lighting ?? null
  const photodiodePkt  = packets?.PhotodiodeSignal ?? null

  const sphereOn        = !!fieldValue(lightingPkt, 'sphere_on')
  const sphereDacCode   = fieldValue(lightingPkt, 'sphere_dac_code')
  const sphereCurrentA  = fieldValue(lightingPkt, 'sphere_current_a')
  const sphereTempC     = fieldValue(lightingPkt, 'sphere_temperature_c')
  const observationActive = !!fieldValue(lightingPkt, 'observation_active')

  const sergeantV = photodiodePkt?.fields?.find(f => f.name === 'sergeant_voltage_v')?.value
  const soldierV  = photodiodePkt?.fields?.find(f => f.name === 'soldier_voltage_v')?.value
  const sampleHz  = photodiodePkt?.sample_hz

  const submitBrightness = async () => {
    const val = Number(targetA)
    if (!Number.isFinite(val) || val < 0) {
      setBrightnessError('Target current must be a non-negative number')
      return
    }
    setBrightnessBusy(true)
    setBrightnessError(null)
    setBrightnessAck(null)
    try {
      const res = await actions.setBrightness(val)
      if (res.ok) {
        setBrightnessAck(`Commanded ${val.toFixed(3)} A${res.emulated ? ' (emulated)' : ''}`)
      } else {
        setBrightnessError(res.error || 'Command failed')
      }
    } catch (e) {
      setBrightnessError(e.message || 'Command failed')
    } finally {
      setBrightnessBusy(false)
    }
  }

  const submitCapture = async () => {
    setCapturing(true)
    setCaptureError(null)
    try {
      const res = await actions.captureCalibration(
        captureLabel || undefined,
        Number(marginPreS) || 0,
        Number(marginPostS) || 0,
      )
      if (res.ok) {
        setLastCapture(res)
      } else {
        setCaptureError(res.error || 'Capture failed')
      }
    } catch (e) {
      setCaptureError(e.message || 'Capture failed')
    } finally {
      setCapturing(false)
    }
  }

  return (
    <div style={styles.root}>
      <div style={styles.leftCol}>
        <div style={styles.columnsRow}>

          {/* -------------------------------------------------------- */}
          {/* Sphere source control + live telemetry                    */}
          {/* -------------------------------------------------------- */}
          <div style={styles.column}>
            <Section title="Integrating Sphere Source">
              <div style={styles.rowFlex}>
                <StatusDot ok={sphereOn} />
                <span style={{ color: C.text, fontSize: 12 }}>
                  {sphereOn ? 'Sphere on' : 'Sphere off'}
                  {observationActive && (
                    <span style={{ color: C.yellow, marginLeft: 8 }}>· observation window active</span>
                  )}
                </span>
              </div>

              <Row label="DAC code"      value={sphereDacCode} unit="code" />
              <Row label="Drive current" value={sphereCurrentA?.toFixed?.(4) ?? sphereCurrentA} unit="A" />
              <Row label="Temperature"   value={sphereTempC?.toFixed?.(1) ?? sphereTempC} unit="°C" />

              <div style={{ marginTop: 10, paddingTop: 8, borderTop: `1px solid ${C.border}` }}>
                <label style={{ ...styles.label, display: 'block', marginBottom: 4 }}>
                  Target drive current (A)
                </label>
                <div style={styles.inputRow}>
                  <input
                    type="number" step="0.01" min="0"
                    style={{ ...styles.input, width: 100 }}
                    value={targetA}
                    onChange={e => setTargetA(stripLeadingZero(e.target.value))}
                  />
                  <button style={styles.btn} disabled={brightnessBusy}
                    onClick={submitBrightness}>
                    Set Brightness
                  </button>
                </div>
                {brightnessError && (
                  <div style={{ marginTop: 6, fontSize: 11, color: C.red }}>{brightnessError}</div>
                )}
                {brightnessAck && !brightnessError && (
                  <div style={{ marginTop: 6, fontSize: 11, color: C.green }}>{brightnessAck}</div>
                )}
                <div style={{ marginTop: 6, fontSize: 10, color: C.muted }}>
                  Sent to the FC as an UpdateSetting command (field 18); the flight
                  computer's LightingTask re-engages its current-hold PI loop at the
                  new target. Requires the sphere already be on (it latches on
                  automatically when the FC's lighting task starts) — this only
                  changes the held current, it doesn't turn the source on/off.
                </div>
              </div>
            </Section>

            <Section title="UVIC PDRO Photodiode Readout">
              <Row label="Sergeant channel" value={sergeantV?.toFixed?.(6) ?? sergeantV} unit="V" />
              <Row label="Soldier channel"  value={soldierV?.toFixed?.(6) ?? soldierV}   unit="V" />
              <Row label="Sample rate"      value={sampleHz} unit="Hz" />
              {!photodiodePkt && (
                <div style={{ marginTop: 8, fontSize: 11, color: C.muted }}>
                  Waiting for PhotodiodeSignal telemetry...
                </div>
              )}
            </Section>
          </div>

          {/* -------------------------------------------------------- */}
          {/* Camera + calibration capture                              */}
          {/* -------------------------------------------------------- */}
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
                      onClick={() => run(telescopeActions.disconnectCamera, setCameraError)}>Disconnect</button>
                  : <button style={styles.btn} disabled={busy}
                      onClick={() => run(() => telescopeActions.connectCamera(cameraType), setCameraError)}>Connect</button>
                }
              </div>
              {cameraError && (
                <div style={{ marginTop: 6, fontSize: 11, color: C.red }}>{cameraError}</div>
              )}
              <div style={{ marginTop: 6, fontSize: 10, color: C.muted }}>
                Same camera connection as the Telescope tab — connecting or changing
                settings here affects that tab too, since both control one physical
                camera.
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
                          onClick={() => run(() => telescopeActions.setCameraSettings({
                            gain: Number(canonIso) || 0,
                            exposure_ms: Math.round((canonShutterSeconds(canonShutter, cameraStatus?.shutter) || 0) * 1000),
                            ...(canonAperture ? { aperture: canonAperture } : {}),
                          }), setCameraError)}>
                          Apply
                        </button>
                        <button style={styles.btn} disabled={busy}
                          onClick={() => run(telescopeActions.refreshCameraSettings, setCameraError)}>
                          Read from Camera
                        </button>
                      </div>
                      <div style={{ marginTop: 4, fontSize: 10, color: C.muted }}>
                        Dropdowns are the camera's own current choices (varies with the
                        attached lens). ⚠ = Bulb, not supported by this app. "Read from
                        Camera" re-queries ISO/shutter/aperture without changing anything.
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
                        onClick={() => run(() => telescopeActions.setCameraSettings({
                          gain: Number(gain) || 0,
                          exposure_ms: Number(exposureMs) || 0,
                        }), setCameraError)}>
                        Apply
                      </button>
                    </div>
                  )}
                </>
              )}
            </Section>

            <Section title="Calibration Capture">
              <div style={{ marginBottom: 8, fontSize: 10, color: C.muted }}>
                Captures one FITS frame exactly like the Telescope tab, but also
                buffers sphere (Lighting) and PDRO (PhotodiodeSignal) telemetry from
                just before the shutter opens to just after it closes. Every capture
                gets its own new subdirectory under calibration_logs/ containing
                capture.fits, capture.txt (full system state), and capture.csv
                (every buffered sample) together.
              </div>

              <div style={styles.inputRow}>
                <input style={styles.input} value={captureLabel}
                  onChange={e => setCaptureLabel(e.target.value)}
                  placeholder="label for the subdirectory (optional)" />
              </div>
              <div style={{ ...styles.inputRow, marginTop: 8 }}>
                <label style={styles.label}>Pre-margin (s)</label>
                <input type="number" step="0.1" min="0" style={{ ...styles.input, width: 70 }}
                  value={marginPreS} onChange={e => setMarginPreS(stripLeadingZero(e.target.value))} />
                <label style={styles.label}>Post-margin (s)</label>
                <input type="number" step="0.1" min="0" style={{ ...styles.input, width: 70 }}
                  value={marginPostS} onChange={e => setMarginPostS(stripLeadingZero(e.target.value))} />
              </div>

              <div style={{ ...styles.inputRow, marginTop: 8 }}>
                <button style={styles.btnGreen} disabled={capturing || !cameraConnected}
                  onClick={submitCapture}>
                  {capturing ? 'Capturing…' : 'Capture Calibration Frame'}
                </button>
              </div>
              {!cameraConnected && (
                <div style={{ marginTop: 6, fontSize: 11, color: C.muted }}>
                  Connect the camera above before capturing.
                </div>
              )}
              {captureError && (
                <div style={{ marginTop: 6, fontSize: 11, color: C.red }}>{captureError}</div>
              )}
              {lastCapture && !captureError && (
                <div style={{ marginTop: 8, fontSize: 11, color: C.green }}>
                  Saved to: {lastCapture.dir}
                  <div style={{ color: C.muted, marginTop: 2 }}>
                    capture.fits, capture.txt, capture.csv
                    {' — '}{lastCapture.lighting_samples} sphere samples, {lastCapture.photodiode_samples} PDRO samples
                  </div>
                </div>
              )}
              <div style={{ marginTop: 8, fontSize: 10, color: C.muted }}>
                Reusing a label never overwrites — a repeat gets _1, _2, ... appended
                to the subdirectory name.
              </div>
            </Section>
          </div>
        </div>
      </div>
    </div>
  )
}

const styles = {
  root: {
    display:    'flex',
    flex:       1,
    overflow:   'hidden',
    alignItems: 'stretch',
  },
  leftCol: {
    flex:          1,
    display:       'flex',
    flexDirection: 'column',
    gap:           12,
    minWidth:      280,
    padding:       16,
    overflowY:     'auto',
  },
  columnsRow: {
    display:             'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
    gap:                 12,
    alignItems:          'start',
  },
  column: {
    display:       'flex',
    flexDirection: 'column',
    gap:           12,
    minWidth:      0,
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
