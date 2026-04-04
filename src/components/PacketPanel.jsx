import { LineChart, Line, ResponsiveContainer, Tooltip, YAxis } from 'recharts'

const SPARKLINE_COLORS = [
  '#00d4ff', '#7c3aed', '#22c55e', '#f59e0b', '#ef4444', '#ec4899',
]

export default function PacketPanel({ label, packet, history }) {
  if (!packet) {
    return (
      <div style={styles.panel}>
        <div style={styles.header}>
          <span style={styles.label}>{label}</span>
          <span style={{ ...styles.badge, background: 'var(--border)', color: 'var(--muted)' }}>
            waiting…
          </span>
        </div>
        <div style={{ color: 'var(--muted)', padding: '12px 0', fontSize: 11 }}>
          No data received yet.
        </div>
      </div>
    )
  }

  const { fields, seq, timestamp, dropped } = packet

  return (
    <div style={styles.panel}>
      <div style={styles.header}>
        <span style={styles.label}>{label}</span>
        <span style={styles.meta}>seq {seq}</span>
        {dropped > 0 && (
          <span style={{ ...styles.badge, background: 'var(--error)' }}>
            -{dropped} dropped
          </span>
        )}
        <span style={{ ...styles.badge, background: 'var(--border)', color: 'var(--muted)', marginLeft: 'auto' }}>
          t={timestamp.toFixed(2)}s
        </span>
      </div>

      <div style={styles.fields}>
        {fields.map((f, i) => {
          const sparkData = history?.[f.name] ?? []
          const color = SPARKLINE_COLORS[i % SPARKLINE_COLORS.length]

          return (
            <div key={f.name} style={styles.fieldRow}>
              <div style={styles.fieldLeft}>
                <span style={{ color: 'var(--muted)', fontSize: 11 }}>{f.label}</span>
                <span style={{ color, fontWeight: 700, fontSize: 15 }}>
                  {formatValue(f.value)}
                </span>
                <span style={{ color: 'var(--muted)', fontSize: 10 }}>{f.unit}</span>
              </div>

              <div style={styles.sparkline}>
                {sparkData.length > 2 && (
                  <ResponsiveContainer width="100%" height={32}>
                    <LineChart data={sparkData}>
                      <YAxis domain={['auto', 'auto']} hide />
                      <Tooltip
                        contentStyle={{ background: 'var(--surface)', border: '1px solid var(--border)', fontSize: 10 }}
                        formatter={(v) => [formatValue(v), f.label]}
                        labelFormatter={(t) => `t=${Number(t).toFixed(2)}s`}
                      />
                      <Line
                        type="monotone"
                        dataKey="v"
                        stroke={color}
                        strokeWidth={1.5}
                        dot={false}
                        isAnimationActive={false}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function formatValue(v) {
  if (Math.abs(v) >= 1000) return v.toFixed(0)
  if (Math.abs(v) >= 10)   return v.toFixed(2)
  return v.toFixed(4)
}

const styles = {
  panel: {
    background: 'var(--surface)',
    border: '1px solid var(--border)',
    borderRadius: 8,
    padding: '12px 16px',
    display: 'flex',
    flexDirection: 'column',
    gap: 8,
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    borderBottom: '1px solid var(--border)',
    paddingBottom: 8,
  },
  label: {
    fontWeight: 700,
    fontSize: 13,
    color: 'var(--accent)',
    letterSpacing: 1,
    textTransform: 'uppercase',
  },
  meta: {
    fontSize: 10,
    color: 'var(--muted)',
  },
  badge: {
    fontSize: 10,
    borderRadius: 4,
    padding: '1px 6px',
    color: 'var(--text)',
  },
  fields: {
    display: 'flex',
    flexDirection: 'column',
    gap: 4,
  },
  fieldRow: {
    display: 'flex',
    alignItems: 'center',
    gap: 12,
  },
  fieldLeft: {
    display: 'flex',
    flexDirection: 'column',
    minWidth: 130,
    gap: 1,
  },
  sparkline: {
    flex: 1,
    height: 32,
  },
}
