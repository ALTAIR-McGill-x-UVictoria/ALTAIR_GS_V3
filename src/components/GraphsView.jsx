import { useState, useEffect } from 'react'
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Legend,
} from 'recharts'

const PALETTE = ['#00e5ff', '#ff4081', '#69ff47', '#ffd600', '#ff6d00', '#d500f9']

const LS_KEY = 'gs_graphs'

function buildChartData(plot, history) {
  const byT = new Map()
  for (const { label, field } of plot.lines) {
    const pts = history[label]?.[field] ?? []
    for (const { t, v } of pts) {
      if (!byT.has(t)) byT.set(t, { t })
      byT.get(t)[`${label}.${field}`] = v
    }
  }
  return Array.from(byT.values()).sort((a, b) => a.t - b.t)
}

function LineAdder({ packets, onAdd, onCancel }) {
  const labels = Object.keys(packets).filter(l => packets[l]?.fields?.length)
  const [selLabel, setSelLabel] = useState(labels[0] ?? '')
  const fields = packets[selLabel]?.fields ?? []
  const [selField, setSelField] = useState(fields[0]?.name ?? '')

  // sync selField when label changes
  function handleLabelChange(l) {
    setSelLabel(l)
    setSelField(packets[l]?.fields?.[0]?.name ?? '')
  }

  return (
    <div style={styles.adder}>
      <select
        style={styles.select}
        value={selLabel}
        onChange={e => handleLabelChange(e.target.value)}
      >
        {labels.map(l => <option key={l} value={l}>{l}</option>)}
      </select>
      <select
        style={styles.select}
        value={selField}
        onChange={e => setSelField(e.target.value)}
      >
        {(packets[selLabel]?.fields ?? []).map(f => (
          <option key={f.name} value={f.name}>{f.label || f.name}</option>
        ))}
      </select>
      <button style={styles.btnAccent} onClick={() => onAdd(selLabel, selField)}>Add</button>
      <button style={styles.btn} onClick={onCancel}>Cancel</button>
    </div>
  )
}

function PlotCard({ plot, packets, history, onRemovePlot, onAddLine, onRemoveLine }) {
  const [adding, setAdding] = useState(false)
  const data = buildChartData(plot, history)

  function handleAdd(label, field) {
    onAddLine(plot.id, label, field)
    setAdding(false)
  }

  return (
    <div style={styles.card}>
      {/* Header */}
      <div style={styles.cardHeader}>
        <div style={styles.chips}>
          {plot.lines.map(({ label, field }, i) => (
            <span key={`${label}.${field}`} style={{ ...styles.chip, borderColor: PALETTE[i % PALETTE.length] }}>
              <span style={{ color: PALETTE[i % PALETTE.length] }}>{label}</span>
              <span style={styles.chipSep}> › </span>
              {field}
              <button style={styles.chipX} onClick={() => onRemoveLine(plot.id, label, field)}>×</button>
            </span>
          ))}
          {!adding && (
            <button style={styles.btnSmall} onClick={() => setAdding(true)}>+ Add Line</button>
          )}
        </div>
        <button style={styles.btnRemove} onClick={() => onRemovePlot(plot.id)}>× Remove</button>
      </div>

      {/* Inline adder */}
      {adding && (
        <LineAdder
          packets={packets}
          onAdd={handleAdd}
          onCancel={() => setAdding(false)}
        />
      )}

      {/* Chart */}
      {plot.lines.length === 0 ? (
        <div style={styles.emptyPlot}>Add a line to start plotting</div>
      ) : (
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={data} margin={{ top: 4, right: 16, left: 0, bottom: 4 }}>
            <CartesianGrid
              strokeDasharray="3 3"
              stroke="rgba(255,255,255,0.08)"
              horizontalPoints={[]}
              verticalPoints={[]}
            />
            <CartesianGrid
              strokeDasharray="1 4"
              stroke="rgba(255,255,255,0.03)"
              horizontalCoordinatesGenerator={({ yAxis }) => {
                if (!yAxis) return []
                const { niceTicks } = yAxis
                if (!niceTicks || niceTicks.length < 2) return []
                const step = (niceTicks[1] - niceTicks[0]) / 4
                const pts = []
                for (let i = 0; i < niceTicks.length - 1; i++) {
                  for (let j = 1; j < 4; j++) pts.push(niceTicks[i] + step * j)
                }
                return pts
              }}
            />
            <XAxis
              dataKey="t"
              type="number"
              domain={['dataMin', 'dataMax']}
              padding={{ left: 0, right: 0 }}
              tickFormatter={v => v.toFixed(0) + 's'}
              tick={{ fill: 'var(--muted)', fontSize: 10 }}
              axisLine={false}
              tickLine={false}
            />
            <YAxis
              tick={{ fill: 'var(--muted)', fontSize: 10 }}
              axisLine={false}
              tickLine={false}
              width={48}
            />
            <Tooltip
              contentStyle={styles.tooltip}
              labelFormatter={v => `t = ${Number(v).toFixed(2)}s`}
              formatter={(value, name) => [Number(value).toFixed(4), name]}
            />
            <Legend
              wrapperStyle={{ fontSize: 10, color: 'var(--muted)' }}
            />
            {plot.lines.map(({ label, field }, i) => (
              <Line
                key={`${label}.${field}`}
                type="monotone"
                dataKey={`${label}.${field}`}
                stroke={PALETTE[i % PALETTE.length]}
                dot={false}
                isAnimationActive={false}
                strokeWidth={1.5}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}

export default function GraphsView({ packets, history }) {
  const [plots, setPlots] = useState(() => {
    try { return JSON.parse(localStorage.getItem(LS_KEY) ?? 'null') ?? [] }
    catch { return [] }
  })

  useEffect(() => {
    localStorage.setItem(LS_KEY, JSON.stringify(plots))
  }, [plots])

  function addPlot() {
    setPlots(prev => [...prev, { id: Date.now(), lines: [] }])
  }

  function removePlot(id) {
    setPlots(prev => prev.filter(p => p.id !== id))
  }

  function addLine(id, label, field) {
    setPlots(prev => prev.map(p => {
      if (p.id !== id) return p
      // prevent duplicate
      if (p.lines.some(l => l.label === label && l.field === field)) return p
      return { ...p, lines: [...p.lines, { label, field }] }
    }))
  }

  function removeLine(id, label, field) {
    setPlots(prev => prev.map(p => {
      if (p.id !== id) return p
      return { ...p, lines: p.lines.filter(l => !(l.label === label && l.field === field)) }
    }))
  }

  return (
    <div style={styles.root}>
      {/* Top bar */}
      <div style={styles.topBar}>
        <button style={styles.btnAccent} onClick={addPlot}>+ Add Plot</button>
      </div>

      {/* Plots */}
      <div style={styles.plotList}>
        {plots.length === 0 ? (
          <div style={styles.emptyState}>No plots yet — click + Add Plot to begin</div>
        ) : (
          plots.map(plot => (
            <PlotCard
              key={plot.id}
              plot={plot}
              packets={packets}
              history={history}
              onRemovePlot={removePlot}
              onAddLine={addLine}
              onRemoveLine={removeLine}
            />
          ))
        )}
      </div>
    </div>
  )
}

const styles = {
  root: {
    flex:          1,
    display:       'flex',
    flexDirection: 'column',
    overflow:      'hidden',
    background:    'var(--bg)',
  },
  topBar: {
    display:        'flex',
    justifyContent: 'flex-end',
    padding:        '8px 16px',
    borderBottom:   '1px solid var(--border)',
    flexShrink:     0,
  },
  plotList: {
    flex:      1,
    overflowY: 'auto',
    padding:   '12px 16px',
    display:   'flex',
    flexDirection: 'column',
    gap:       12,
  },
  emptyState: {
    margin:    'auto',
    color:     'var(--muted)',
    fontSize:  13,
    textAlign: 'center',
    paddingTop: 60,
  },
  card: {
    background:   'var(--surface)',
    border:       '1px solid var(--border)',
    borderRadius: 6,
    padding:      '10px 14px',
  },
  cardHeader: {
    display:        'flex',
    alignItems:     'center',
    justifyContent: 'space-between',
    marginBottom:   8,
    gap:            8,
  },
  chips: {
    display:    'flex',
    flexWrap:   'wrap',
    gap:        6,
    alignItems: 'center',
  },
  chip: {
    display:      'flex',
    alignItems:   'center',
    gap:          3,
    border:       '1px solid',
    borderRadius: 4,
    padding:      '2px 6px',
    fontSize:     11,
    color:        'var(--text)',
    fontFamily:   'var(--font-mono)',
  },
  chipSep: {
    color: 'var(--muted)',
  },
  chipX: {
    background:  'none',
    border:      'none',
    color:       'var(--muted)',
    cursor:      'pointer',
    padding:     '0 0 0 4px',
    fontSize:    13,
    lineHeight:  1,
  },
  emptyPlot: {
    textAlign:  'center',
    color:      'var(--muted)',
    fontSize:   12,
    padding:    '40px 0',
  },
  adder: {
    display:     'flex',
    gap:         6,
    alignItems:  'center',
    marginBottom: 8,
    flexWrap:    'wrap',
  },
  select: {
    background:   'var(--bg)',
    border:       '1px solid var(--border)',
    borderRadius: 4,
    color:        'var(--text)',
    fontFamily:   'var(--font-mono)',
    fontSize:     11,
    padding:      '3px 6px',
  },
  btn: {
    background:   'transparent',
    border:       '1px solid var(--border)',
    borderRadius: 4,
    color:        'var(--muted)',
    fontFamily:   'var(--font-mono)',
    fontSize:     11,
    padding:      '3px 10px',
    cursor:       'pointer',
  },
  btnAccent: {
    background:   'transparent',
    border:       '1px solid var(--accent)',
    borderRadius: 4,
    color:        'var(--accent)',
    fontFamily:   'var(--font-mono)',
    fontSize:     11,
    padding:      '3px 10px',
    cursor:       'pointer',
  },
  btnSmall: {
    background:   'transparent',
    border:       '1px solid var(--border)',
    borderRadius: 4,
    color:        'var(--muted)',
    fontFamily:   'var(--font-mono)',
    fontSize:     10,
    padding:      '2px 8px',
    cursor:       'pointer',
  },
  btnRemove: {
    background:   'transparent',
    border:       '1px solid #ff4444',
    borderRadius: 4,
    color:        '#ff4444',
    fontFamily:   'var(--font-mono)',
    fontSize:     10,
    padding:      '2px 8px',
    cursor:       'pointer',
    flexShrink:   0,
  },
  tooltip: {
    background:  'var(--surface)',
    border:      '1px solid var(--border)',
    borderRadius: 4,
    fontSize:    11,
    fontFamily:  'var(--font-mono)',
    color:       'var(--text)',
  },
}
