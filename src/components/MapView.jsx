import { useEffect, useRef, useMemo } from 'react'
import { MapContainer, TileLayer, Marker, Polyline, Popup, useMap } from 'react-leaflet'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'

// Fix Leaflet default icon paths broken by bundlers
delete L.Icon.Default.prototype._getIconUrl
L.Icon.Default.mergeOptions({
  iconRetinaUrl: 'https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png',
  iconUrl:       'https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png',
  shadowUrl:     'https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png',
})

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

// Keeps the map centred on the latest position when autoFollow is on
function MapFollower({ position, follow }) {
  const map = useMap()
  const prevFollow = useRef(follow)

  useEffect(() => {
    if (!position) return
    if (follow) {
      map.setView(position, map.getZoom(), { animate: true })
    }
    prevFollow.current = follow
  }, [position, follow, map])

  return null
}

export default function MapView({ gpsPacket, history }) {
  const autoFollow = useRef(true)

  const position = useMemo(() => {
    if (!gpsPacket) return null
    const lat = gpsPacket.fields.find(f => f.name === 'lat')?.value
    const lon = gpsPacket.fields.find(f => f.name === 'lon')?.value
    if (!lat || !lon || (lat === 0 && lon === 0)) return null
    return [lat, lon]
  }, [gpsPacket])

  const alt = gpsPacket?.fields.find(f => f.name === 'alt')?.value ?? 0
  const relAlt = gpsPacket?.fields.find(f => f.name === 'relative_alt')?.value ?? 0
  const hdg = gpsPacket?.fields.find(f => f.name === 'hdg')?.value ?? 0

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

  const defaultCenter = [45.0, 0.0]
  const defaultZoom  = 3

  return (
    <div style={styles.wrapper}>
      {/* Info overlay */}
      <div style={styles.overlay}>
        {position ? (
          <>
            <InfoRow label="Lat"     value={position[0].toFixed(6)} unit="°" />
            <InfoRow label="Lon"     value={position[1].toFixed(6)} unit="°" />
            <InfoRow label="Alt MSL" value={alt.toFixed(1)}         unit="m" />
            <InfoRow label="Alt AGL" value={relAlt.toFixed(1)}      unit="m" />
            <InfoRow label="Heading" value={hdg.toFixed(1)}         unit="°" />
          </>
        ) : (
          <span style={{ color: 'var(--muted)', fontSize: 11 }}>Waiting for GPS fix…</span>
        )}
      </div>

      <MapContainer
        center={position ?? defaultCenter}
        zoom={position ? 16 : defaultZoom}
        style={styles.map}
        zoomControl={true}
      >
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />

        <MapFollower position={position} follow={autoFollow.current} />

        {track.length > 1 && (
          <Polyline
            positions={track}
            pathOptions={{ color: '#00d4ff', weight: 2, opacity: 0.7 }}
          />
        )}

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
  )
}

function InfoRow({ label, value, unit }) {
  return (
    <div style={styles.infoRow}>
      <span style={{ color: 'var(--muted)', minWidth: 60 }}>{label}</span>
      <span style={{ color: 'var(--accent)', fontWeight: 700 }}>{value}</span>
      <span style={{ color: 'var(--muted)' }}>{unit}</span>
    </div>
  )
}

const styles = {
  wrapper: {
    position: 'relative',
    flex: 1,
    height: '100%',
  },
  map: {
    height: '100%',
    width: '100%',
    background: '#0b0e14',
  },
  overlay: {
    position: 'absolute',
    top: 12,
    right: 12,
    zIndex: 1000,
    background: 'rgba(20, 24, 32, 0.92)',
    border: '1px solid var(--border)',
    borderRadius: 6,
    padding: '8px 12px',
    display: 'flex',
    flexDirection: 'column',
    gap: 4,
    minWidth: 180,
    fontFamily: 'var(--font-mono)',
    fontSize: 12,
  },
  infoRow: {
    display: 'flex',
    gap: 8,
    alignItems: 'baseline',
  },
}
