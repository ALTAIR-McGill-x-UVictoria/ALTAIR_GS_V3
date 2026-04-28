import { useEffect, useMemo, useState } from 'react'
import { MapContainer, TileLayer, Marker, Polyline, Popup, ZoomControl, useMap } from 'react-leaflet'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'

// Ground station fallback position — used until live GPS fix arrives
const GS_LAT_DEFAULT =  45.5088
const GS_LON_DEFAULT = -73.5542

// Fix Leaflet default icon paths broken by bundlers
delete L.Icon.Default.prototype._getIconUrl
L.Icon.Default.mergeOptions({
  iconRetinaUrl: 'https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png',
  iconUrl:       'https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png',
  shadowUrl:     'https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png',
})

// Ground station marker — green square
const gsIcon = new L.DivIcon({
  className: '',
  html: `<svg width="20" height="20" viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">
           <rect x="3" y="3" width="14" height="14" fill="#22c55e" fill-opacity="0.25"
             stroke="#22c55e" stroke-width="1.5" rx="2"/>
           <rect x="7" y="7" width="6" height="6" fill="#22c55e" rx="1"/>
         </svg>`,
  iconSize:   [20, 20],
  iconAnchor: [10, 10],
})

/**
 * Compute the tip of a mount-pointing arrow.
 * azimuth_deg: 0=North, 90=East (clockwise)
 * length_m: arrow length in metres on the ground
 */
function arrowTip(lat, lon, azimuthDeg, lengthM) {
  const R = 6_371_000
  const dLat = (lengthM / R) * Math.cos(azimuthDeg * Math.PI / 180) * (180 / Math.PI)
  const dLon = (lengthM / R) * Math.sin(azimuthDeg * Math.PI / 180)
               / Math.cos(lat * Math.PI / 180) * (180 / Math.PI)
  return [lat + dLat, lon + dLon]
}

// Cyan aircraft marker to match the UI theme
const aircraftIcon = new L.DivIcon({
  className: '',
  html: `<svg width="24" height="24" viewBox="0 0 24 24" fill="none"
           xmlns="http://www.w3.org/2000/svg">
           <circle cx="12" cy="12" r="10" fill="#00d4ff" fill-opacity="0.2"
             stroke="#00d4ff" stroke-width="1.5"/>
           <circle cx="12" cy="12" r="4" fill="#00d4ff"/>
         </svg>`,
  iconSize:   [24, 24],
  iconAnchor: [12, 12],
})

// MODE: 'free' | 'payload' | 'both'
// Keeps the map centred according to the active follow mode.
// User zoom is always preserved — we only pan, never reset zoom.
// For 'both' mode, fitBounds only fires when the mode first activates.
function MapFollower({ position, gsPosition, mode }) {
  const map = useMap()

  useEffect(() => {
    if (mode === 'free') return

    if (mode === 'payload') {
      if (!position) return
      map.setView(position, map.getZoom(), { animate: true })
    } else if (mode === 'both') {
      if (!position) {
        map.setView(gsPosition, map.getZoom(), { animate: true })
        return
      }
      map.fitBounds([gsPosition, position], {
        padding:  [40, 40],
        maxZoom:  map.getZoom(),
        animate:  true,
      })
    }
  }, [position, gsPosition, mode, map])

  return null
}

// ---------------------------------------------------------------------------
// Altitude profile sidebar
// ---------------------------------------------------------------------------

// Altitude markers with labels and colours.
// altM: absolute altitude MSL in metres; shown as a tick on the scale.
const FIXED_MARKERS = [
  { altM: 10000, label: 'Jet stream',  color: '#60a5fa' },
  { altM: 18000, label: 'Stratosphere',color: '#a78bfa' },
]

// Stage-based altitude markers — fired state is injected at render time
const STAGE_ALT_MARKERS_BASE = [
  { altM: 25000, label: 'Termination', colorDefault: '#f97316', colorFired: '#fb923c', key: 'termination' },
  { altM: 30000, label: 'Burst',       colorDefault: '#ff4444', colorFired: '#f87171', key: 'burst'        },
]

const SIDEBAR_W = 110  // px

/**
 * Thin vertical altitude profile strip.
 *
 * Props:
 *   currentAlt  — current baro altitude MSL (m), or null
 *   maxAlt      — top of scale (m); defaults to 110% of burst altitude
 *   stage       — current flight_stage integer (0–8)
 *   stageNames  — {0: "Pre-flight", ...}
 */
// Track column width (px) — narrow strip on the left for the SVG track
const TRACK_COL = 22

// Minimum vertical gap between adjacent labels, as a percentage of sidebar height.
// ~3.5% ≈ 21px at 600px — enough for a two-line label block.
const LABEL_H_PCT = 3.5

/**
 * Resolve label positions to avoid overlap.
 * Works entirely in [0, 100] percentage space so label tops match the SVG ticks
 * exactly (both use the same `(1 - altM/maxAlt) * 100` formula).
 */
function resolveLabels(markers, maxAlt) {
  const items = markers.map(m => {
    const pct = (1 - Math.max(0, Math.min(1, m.altM / maxAlt))) * 100
    return { ...m, naturalPct: pct, adjustedPct: pct }
  })

  // Sort top-of-scale first (smallest pct value)
  items.sort((a, b) => a.naturalPct - b.naturalPct)

  // Push overlapping labels downward
  for (let i = 1; i < items.length; i++) {
    const minTop = items[i - 1].adjustedPct + LABEL_H_PCT
    if (items[i].adjustedPct < minTop) items[i].adjustedPct = minTop
  }

  return items.map(item => ({
    ...item,
    topPct:  `${item.adjustedPct}%`,
    tickPct: `${item.naturalPct}%`,   // kept for reference, SVG uses altM directly
  }))
}

function AltitudeSidebar({ currentAlt, maxAlt, stage, stageNames, terminationFired, burstDetected }) {
  const balloonPct = currentAlt != null
    ? (1 - Math.max(0, Math.min(1, currentAlt / maxAlt))) * 100
    : null

  const stageName = stageNames?.[stage] ?? null

  const stageMarkers = STAGE_ALT_MARKERS_BASE.map(m => ({
    altM:   m.altM,
    label:  m.label,
    color:  (m.key === 'termination' ? terminationFired : burstDetected) ? m.colorFired : m.colorDefault,
    dashed: !(m.key === 'termination' ? terminationFired : burstDetected),
    fired:  m.key === 'termination' ? terminationFired : burstDetected,
  }))

  // Merge, filter, resolve collisions
  const rawMarkers = [
    ...FIXED_MARKERS.map(m => ({ ...m, dashed: false, fired: false })),
    ...stageMarkers,
  ].filter(m => m.altM <= maxAlt)

  const allMarkers = resolveLabels(rawMarkers, maxAlt)

  return (
    <div style={s.sidebar}>
      <div style={s.sidebarHeader}>ALTITUDE</div>

      {/*
        Two-layer layout inside scaleWrap:
          Layer 1 (left): narrow SVG — track line, ticks, balloon icon.
                          Uses preserveAspectRatio="none" so the track always
                          fills 100% height. No text → no distortion visible.
          Layer 2 (right): absolutely-positioned HTML divs for all labels.
                           top is set via altToPct() so positions match the SVG.
                           HTML text is never affected by SVG scaling.
      */}
      <div style={s.scaleWrap}>

        {/* SVG layer — track + ticks + balloon only, no text.
            top/bottom match scaleWrap padding so SVG coords align with CSS % labels. */}
        <svg
          width={TRACK_COL}
          height="calc(100% - 16px)"
          viewBox={`0 0 ${TRACK_COL} 100`}
          preserveAspectRatio="none"
          style={{ position: 'absolute', left: 0, top: 8, bottom: 8 }}
        >
          <defs>
            <linearGradient id="altGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%"   stopColor="#00d4ff" stopOpacity="0.85" />
              <stop offset="100%" stopColor="#00d4ff" stopOpacity="0.08" />
            </linearGradient>
          </defs>

          {/* Track background */}
          <rect x={9} y={0} width={4} height={100} rx={2} fill="#1e293b" />

          {/* Filled portion from ground up to balloon */}
          {balloonPct != null && (
            <rect x={9} y={balloonPct} width={4} height={100 - balloonPct}
              rx={2} fill="url(#altGrad)" />
          )}

          {/* Ticks — always at true altitude position */}
          {allMarkers.map(({ altM, color, dashed, fired }) => {
            const y = (1 - altM / maxAlt) * 100
            return (
              <g key={altM}>
                <line
                  x1={5} y1={y} x2={TRACK_COL - 1} y2={y}
                  stroke={color} strokeWidth={fired ? 2 : (dashed ? 1 : 1.5)}
                  strokeOpacity={fired ? 1 : 0.85}
                  strokeDasharray={dashed ? '3 1.5' : undefined}
                />
                {fired && (
                  <circle cx={11} cy={y} r={2.5} fill={color} fillOpacity={0.95} />
                )}
              </g>
            )
          })}

          {/* Ground tick */}
          <line x1={4} y1={100} x2={TRACK_COL - 1} y2={100}
            stroke="#22c55e" strokeWidth={2} />

        </svg>

        {/* Balloon / parachute image — absolutely positioned HTML so it never stretches */}
        {balloonPct != null && (
          <img
            src={stage >= 6 ? '/parachute.svg' : '/balloon.svg'}
            alt={stage >= 6 ? 'parachute' : 'balloon'}
            style={{
              position:      'absolute',
              left:          TRACK_COL / 2 - 16,
              top:           `${balloonPct}%`,
              transform:     'translateY(-100%)',
              width:         32,
              height:        'auto',
              pointerEvents: 'none',
            }}
          />
        )}

        {/* HTML label layer — uses collision-resolved topPct, never squished */}
        {allMarkers.map(({ altM, label, color, dashed, fired, topPct }) => (
          <div key={altM} style={{
            position:   'absolute',
            left:       TRACK_COL + 2,
            right:      0,
            top:        topPct,
            transform:  'translateY(-50%)',
            lineHeight: 1.1,
            pointerEvents: 'none',
          }}>
            <div style={{
              fontSize:     9,
              fontFamily:   'monospace',
              color,
              opacity:      fired ? 1 : (dashed ? 0.9 : 0.8),
              fontWeight:   fired ? 700 : 400,
              whiteSpace:   'nowrap',
              overflow:     'hidden',
              textOverflow: 'ellipsis',
            }}>
              {label}
            </div>
            <div style={{
              fontSize:   8,
              fontFamily: 'monospace',
              color,
              opacity:    fired ? 0.75 : 0.5,
            }}>
              {fired ? '● FIRED' : (altM >= 1000 ? `${altM / 1000}k m` : `${altM} m`)}
            </div>
          </div>
        ))}

        {/* Ground label */}
        <div style={{
          position:   'absolute',
          left:       TRACK_COL + 2,
          bottom:     0,
          fontSize:   9,
          fontFamily: 'monospace',
          color:      '#22c55e',
          opacity:    0.8,
          pointerEvents: 'none',
        }}>
          GND
        </div>
      </div>

      {/* Footer */}
      <div style={s.sidebarFooter}>
        {currentAlt != null
          ? <><span style={s.altValue}>{Math.round(currentAlt).toLocaleString()}</span>
              <span style={s.altUnit}> m</span></>
          : <span style={s.altMuted}>—</span>
        }
        {stageName && <div style={s.stagePill}>{stageName}</div>}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function MapView({ gpsPacket, envPacket, evtPacket, history, tracking, stageNames, gsGps }) {
  const [followMode, setFollowMode] = useState('payload') // 'free' | 'payload' | 'both'

  const position = useMemo(() => {
    if (!gpsPacket) return null
    const lat = gpsPacket.fields.find(f => f.name === 'lat')?.value
    const lon = gpsPacket.fields.find(f => f.name === 'lon')?.value
    if (!lat || !lon || (lat === 0 && lon === 0)) return null
    return [lat, lon]
  }, [gpsPacket])

  const alt    = gpsPacket?.fields.find(f => f.name === 'alt')?.value ?? 0
  const relAlt = gpsPacket?.fields.find(f => f.name === 'relative_alt')?.value ?? 0
  const hdg    = gpsPacket?.fields.find(f => f.name === 'hdg')?.value ?? 0

  // Barometric altitude from environment packet (more accurate for altitude profile)
  const baroAltRaw = envPacket?.fields.find(f => f.name === 'baro_alt')?.value ?? null
  // Use baro_alt when it has a plausible value (> 0), else fall back to GPS alt
  const baroAlt = (baroAltRaw != null && baroAltRaw > 0) ? baroAltRaw : (alt > 0 ? alt : null)

  // Current flight stage and event flags from event packet
  const stage            = evtPacket?.fields.find(f => f.name === 'flight_stage')?.value ?? 0
  const terminationFired = (evtPacket?.fields.find(f => f.name === 'termination_fired')?.value ?? 0) === 1
  const burstDetected    = (evtPacket?.fields.find(f => f.name === 'burst_detected')?.value    ?? 0) === 1

  // Top of altitude scale = 110% of burst altitude
  const BURST_ALT_M = STAGE_ALT_MARKERS_BASE.find(m => m.key === 'burst')?.altM ?? 30_000
  const SCALE_MAX   = Math.round(BURST_ALT_M * 1.1)

  // Build track from history
  const track = useMemo(() => {
    const latHist = history?.lat ?? []
    const lonHist = history?.lon ?? []
    const len = Math.min(latHist.length, lonHist.length)
    const points = []
    for (let i = 0; i < len; i++) {
      const lat = latHist[i]?.v
      const lon = lonHist[i]?.v
      if (lat && lon && !(lat === 0 && lon === 0)) {
        points.push([lat, lon])
      }
    }
    return points
  }, [history])

  // Live GS position from u-blox 7; fall back to hardcoded default
  const gsLat = gsGps?.lat ?? GS_LAT_DEFAULT
  const gsLon = gsGps?.lon ?? GS_LON_DEFAULT
  const gsPosition = [gsLat, gsLon]

  // Distance and bearing from GS to payload
  const distBearing = useMemo(() => {
    if (!position) return null
    const [lat1, lon1] = [gsLat * Math.PI / 180, gsLon * Math.PI / 180]
    const [lat2, lon2] = [position[0] * Math.PI / 180, position[1] * Math.PI / 180]
    const R = 6_371_000
    const dLat = lat2 - lat1, dLon = lon2 - lon1
    const a = Math.sin(dLat/2)**2 + Math.cos(lat1)*Math.cos(lat2)*Math.sin(dLon/2)**2
    const distM = 2 * R * Math.asin(Math.sqrt(a))
    const y = Math.sin(lon2 - lon1) * Math.cos(lat2)
    const x = Math.cos(lat1)*Math.sin(lat2) - Math.sin(lat1)*Math.cos(lat2)*Math.cos(lon2-lon1)
    const bearingDeg = ((Math.atan2(y, x) * 180 / Math.PI) + 360) % 360
    return { distM, bearingDeg }
  }, [position, gsLat, gsLon])

  // Mount pointing arrow — 500 m long on the ground, direction = azimuth
  const mountArrow = useMemo(() => {
    const az = tracking?.azimuth
    if (az == null) return null
    const tip = arrowTip(gsLat, gsLon, az, 500)
    return { az, tip, elevation: tracking.elevation ?? null }
  }, [tracking, gsLat, gsLon])

  const defaultCenter = position ?? gsPosition
  const defaultZoom   = position ? 14 : 12

  return (
    <div style={s.root}>
      {/* Left altitude profile sidebar */}
      <AltitudeSidebar
        currentAlt={baroAlt}
        maxAlt={SCALE_MAX}
        stage={stage}
        stageNames={stageNames}
        terminationFired={terminationFired}
        burstDetected={burstDetected}
      />

      {/* Map area */}
      <div style={s.mapWrap}>
        {/* Payload info overlay */}
        <div style={s.overlay}>
          {position ? (
            <>
              <InfoRow label="Lat"     value={position[0].toFixed(6)} unit="°" />
              <InfoRow label="Lon"     value={position[1].toFixed(6)} unit="°" />
              <InfoRow label="Alt MSL" value={alt.toFixed(1)}         unit="m" />
              <InfoRow label="Alt AGL" value={relAlt.toFixed(1)}      unit="m" />
              <InfoRow label="Heading" value={hdg.toFixed(1)}         unit="°" />
              {distBearing && <>
                <InfoRow
                  label="Distance"
                  value={distBearing.distM >= 1000
                    ? (distBearing.distM / 1000).toFixed(2)
                    : distBearing.distM.toFixed(0)}
                  unit={distBearing.distM >= 1000 ? 'km' : 'm'}
                />
                <InfoRow label="Bearing" value={distBearing.bearingDeg.toFixed(1)} unit="°" />
              </>}
            </>
          ) : (
            <span style={{ color: 'var(--muted)', fontSize: 11 }}>Waiting for GPS fix…</span>
          )}
        </div>

        {/* Left-column overlays: GS GPS + mount + alt events stacked */}
        <div style={s.leftOverlayCol}>
          {/* Ground station GPS fix badge */}
          <div style={s.gsGpsOverlay}>
            <div style={{ fontSize: 9, letterSpacing: 1, color: gsGps ? '#22c55e' : 'var(--muted)', marginBottom: 2 }}>
              GS GPS {gsGps ? '● LIVE' : '○ DEFAULT'}
            </div>
            {gsGps
              ? <>
                  <InfoRow label="Lat" value={gsLat.toFixed(5)} unit="°" />
                  <InfoRow label="Lon" value={gsLon.toFixed(5)} unit="°" />
                  <InfoRow label="Alt" value={gsGps.alt.toFixed(1)} unit="m" />
                  <InfoRow label="Sats" value={gsGps.sats} unit="" />
                </>
              : <span style={{ color: 'var(--muted)', fontSize: 10 }}>{gsLat.toFixed(4)}°, {gsLon.toFixed(4)}°</span>
            }
          </div>

          {mountArrow && (
            <div style={s.mountOverlay}>
              <div style={{ color: '#22c55e', fontSize: 10, letterSpacing: 1, marginBottom: 2 }}>MOUNT</div>
              <InfoRow label="Az"  value={mountArrow.az.toFixed(1)}       unit="°" />
              {mountArrow.elevation != null && (
                <InfoRow label="El"  value={mountArrow.elevation.toFixed(1)} unit="°" />
              )}
            </div>
          )}

          {/* Altitude event threshold indicators */}
          <div style={s.altOverlay}>
            <div style={{ color: 'var(--muted)', fontSize: 9, letterSpacing: 1, marginBottom: 4 }}>ALT EVENTS</div>
            {[
              { label: 'TERMINATION',  altM: 25000, fired: terminationFired, color: '#f97316' },
              { label: 'BURST', altM: 30000, fired: burstDetected,    color: '#ff4444' },
            ].map(({ label, altM, fired, color }) => (
              <div key={label} style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 3 }}>
                <div style={{
                  width: 8, height: 8, borderRadius: '50%',
                  background: fired ? color : 'transparent',
                  border: `1.5px solid ${color}`,
                  flexShrink: 0,
                  boxShadow: fired ? `0 0 5px ${color}` : 'none',
                }} />
                <span style={{ color: fired ? color : 'var(--muted)', fontFamily: 'monospace', fontSize: 10, fontWeight: fired ? 700 : 400, minWidth: 36 }}>
                  {label}
                </span>
                <span style={{ color: fired ? color : '#4b5563', fontFamily: 'monospace', fontSize: 9 }}>
                  {fired ? 'FIRED' : `${altM / 1000}km`}
                </span>
              </div>
            ))}
          </div>
        </div>

        {/* Follow mode buttons */}
        <div style={s.followBtns}>
          {[
            { mode: 'free',    label: 'Free' },
            { mode: 'payload', label: 'Payload' },
            { mode: 'both',    label: 'GS + Payload' },
          ].map(({ mode, label }) => (
            <button
              key={mode}
              style={{ ...s.followBtn, ...(followMode === mode ? s.followBtnActive : {}) }}
              onClick={() => setFollowMode(mode)}
            >
              {label}
            </button>
          ))}
        </div>

        <MapContainer
          center={defaultCenter}
          zoom={defaultZoom}
          style={s.map}
          zoomControl={false}
        >
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://stadiamaps.com/">Stadia Maps</a>'
            url="https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/{z}/{x}/{y}{r}.png"
            maxZoom={20}
          />

          <ZoomControl position="bottomleft" />
          <MapFollower position={position} gsPosition={gsPosition} mode={followMode} />

          {track.length > 1 && (
            <Polyline
              positions={track}
              pathOptions={{ color: '#00d4ff', weight: 2, opacity: 0.7 }}
            />
          )}

          {/* Ground station marker */}
          <Marker position={gsPosition} icon={gsIcon}>
            <Popup>
              <div style={{ fontFamily: 'monospace', fontSize: 12 }}>
                <b>Ground Station</b><br />
                Lat: {gsLat.toFixed(6)}°<br />
                Lon: {gsLon.toFixed(6)}°<br />
                {gsGps
                  ? <>Alt: {gsGps.alt.toFixed(1)} m &nbsp;|&nbsp; {gsGps.sats} sats &nbsp;|&nbsp; HDOP {gsGps.hdop.toFixed(1)}</>
                  : <span style={{ color: '#888' }}>GPS: default position</span>
                }
              </div>
            </Popup>
          </Marker>

          {/* Mount pointing arrow — shaft + arrowhead as two polylines */}
          {mountArrow && (() => {
            const [lat2, lon2] = mountArrow.tip
            const headLen = 120
            const leftPt  = arrowTip(lat2, lon2, (mountArrow.az + 180 + 30) % 360, headLen)
            const rightPt = arrowTip(lat2, lon2, (mountArrow.az + 180 - 30) % 360, headLen)
            return (
              <>
                <Polyline
                  positions={[gsPosition, mountArrow.tip]}
                  pathOptions={{ color: '#22c55e', weight: 2.5, opacity: 0.9 }}
                />
                <Polyline
                  positions={[mountArrow.tip, leftPt]}
                  pathOptions={{ color: '#22c55e', weight: 2.5, opacity: 0.9 }}
                />
                <Polyline
                  positions={[mountArrow.tip, rightPt]}
                  pathOptions={{ color: '#22c55e', weight: 2.5, opacity: 0.9 }}
                />
              </>
            )
          })()}

          {position && (
            <Marker position={position} icon={aircraftIcon}>
              <Popup>
                <div style={{ fontFamily: 'monospace', fontSize: 12 }}>
                  <b>ALTAIR V2</b><br />
                  Lat: {position[0].toFixed(6)}°<br />
                  Lon: {position[1].toFixed(6)}°<br />
                  Alt MSL: {alt.toFixed(1)} m<br />
                  Alt AGL: {relAlt.toFixed(1)} m<br />
                  Heading: {hdg.toFixed(1)}°
                </div>
              </Popup>
            </Marker>
          )}
        </MapContainer>
      </div>
    </div>
  )
}

function InfoRow({ label, value, unit }) {
  return (
    <div style={s.infoRow}>
      <span style={{ color: 'var(--muted)', minWidth: 60 }}>{label}</span>
      <span style={{ color: 'var(--accent)', fontWeight: 700 }}>{value}</span>
      <span style={{ color: 'var(--muted)' }}>{unit}</span>
    </div>
  )
}

const s = {
  root: {
    display:  'flex',
    flex:     1,
    height:   '100%',
    overflow: 'hidden',
  },
  // ---- Sidebar ----
  sidebar: {
    width:           SIDEBAR_W,
    flexShrink:      0,
    display:         'flex',
    flexDirection:   'column',
    background:      'rgba(11, 14, 20, 0.97)',
    borderRight:     '1px solid var(--border)',
    overflow:        'hidden',
  },
  sidebarHeader: {
    textAlign:     'center',
    fontSize:      9,
    letterSpacing: 2,
    color:         'var(--muted)',
    padding:       '8px 0 4px',
    borderBottom:  '1px solid var(--border)',
    flexShrink:    0,
    fontFamily:    'var(--font-mono)',
  },
  scaleWrap: {
    flex:      1,
    minHeight: 0,
    position:  'relative',
    padding:   '8px 0',
  },
  sidebarFooter: {
    borderTop:     '1px solid var(--border)',
    padding:       '6px 0 8px',
    textAlign:     'center',
    flexShrink:    0,
    display:       'flex',
    flexDirection: 'column',
    alignItems:    'center',
    gap:           4,
  },
  altValue: {
    fontFamily: 'var(--font-mono)',
    fontSize:   13,
    fontWeight: 700,
    color:      'var(--accent)',
  },
  altUnit: {
    fontFamily: 'var(--font-mono)',
    fontSize:   9,
    color:      'var(--muted)',
    marginLeft: 2,
  },
  altMuted: {
    color:      'var(--muted)',
    fontSize:   12,
    fontFamily: 'var(--font-mono)',
  },
  stagePill: {
    fontSize:     8,
    letterSpacing: 0.5,
    color:        'var(--accent)',
    background:   'rgba(0,212,255,0.1)',
    border:       '1px solid rgba(0,212,255,0.25)',
    borderRadius: 3,
    padding:      '1px 5px',
    fontFamily:   'var(--font-mono)',
    maxWidth:     SIDEBAR_W - 16,
    overflow:     'hidden',
    textOverflow: 'ellipsis',
    whiteSpace:   'nowrap',
  },
  // ---- Map area ----
  mapWrap: {
    position: 'relative',
    flex:     1,
    height:   '100%',
  },
  map: {
    height: '100%',
    width:  '100%',
    background: '#0b0e14',
  },
  overlay: {
    position:   'absolute',
    top:        12,
    right:      12,
    zIndex:     1000,
    background: 'rgba(20, 24, 32, 0.92)',
    border:     '1px solid var(--border)',
    borderRadius: 6,
    padding:    '8px 12px',
    display:    'flex',
    flexDirection: 'column',
    gap:        4,
    minWidth:   180,
    fontFamily: 'var(--font-mono)',
    fontSize:   12,
  },
  infoRow: {
    display:    'flex',
    gap:        8,
    alignItems: 'baseline',
  },
  leftOverlayCol: {
    position:      'absolute',
    top:           12,
    left:          12,
    zIndex:        1000,
    display:       'flex',
    flexDirection: 'column',
    gap:           6,
    alignItems:    'stretch',
  },
  gsGpsOverlay: {
    background:  'rgba(20, 24, 32, 0.92)',
    border:      '1px solid rgba(34, 197, 94, 0.25)',
    borderRadius: 6,
    padding:     '8px 12px',
    display:     'flex',
    flexDirection: 'column',
    gap:         3,
    minWidth:    140,
    fontFamily:  'var(--font-mono)',
    fontSize:    12,
  },
  mountOverlay: {
    background: 'rgba(20, 24, 32, 0.92)',
    border:     '1px solid rgba(34, 197, 94, 0.4)',
    borderRadius: 6,
    padding:    '8px 12px',
    display:    'flex',
    flexDirection: 'column',
    gap:        4,
    minWidth:   120,
    fontFamily: 'var(--font-mono)',
    fontSize:   12,
  },
  altOverlay: {
    background: 'rgba(20, 24, 32, 0.92)',
    border:     '1px solid var(--border)',
    borderRadius: 6,
    padding:    '8px 12px',
    fontFamily: 'var(--font-mono)',
    fontSize:   12,
  },
  followBtns: {
    position:   'absolute',
    bottom:     28,
    right:      12,
    zIndex:     1000,
    display:    'flex',
    flexDirection: 'column',
    gap:        3,
    alignItems: 'flex-end',
  },
  followBtn: {
    background:    'rgba(20, 24, 32, 0.92)',
    border:        '1px solid #1e2d3d',
    borderRadius:  4,
    color:         '#8b949e',
    fontFamily:    'var(--font-mono)',
    fontSize:      10,
    letterSpacing: 0.5,
    padding:       '4px 10px',
    cursor:        'pointer',
    textAlign:     'left',
  },
  followBtnActive: {
    border:     '1px solid #00d4ff',
    color:      '#00d4ff',
    background: 'rgba(0, 212, 255, 0.1)',
  },
}
