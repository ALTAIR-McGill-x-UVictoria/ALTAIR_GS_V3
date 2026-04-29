import { useState, useEffect, useRef, useMemo } from 'react'

const CMD_UPDATE_SETTING = 0xC3

// Build grouped field list from the live packet's fields array.
// group, label, unit, min, max all come from FieldMeta via the backend.
// Field index === field_id for UPDATE_SETTING (matches SETTING_KEYS order).
function buildGroups(fields) {
  const byGroup = {}
  const groupOrder = []
  fields.forEach((f, idx) => {
    const groupName = f.group || 'Other'
    if (!byGroup[groupName]) {
      byGroup[groupName] = []
      groupOrder.push(groupName)
    }
    byGroup[groupName].push({
      id:    idx,
      name:  f.name,
      label: f.label,
      unit:  f.unit,
      min:   f.min  ?? null,
      max:   f.max  ?? null,
    })
  })
  return groupOrder.map(title => ({ title, fields: byGroup[title] }))
}

function formatLive(v) {
  if (v == null) return '——'
  if (Math.abs(v) >= 1000) return v.toFixed(1)
  return parseFloat(v.toPrecision(4)).toString()
}

function AckPill({ isPending, ack }) {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    if (!ack) return
    setVisible(true)
    const t = setTimeout(() => setVisible(false), 8000)
    return () => clearTimeout(t)
  }, [ack])

  if (isPending) {
    return <span style={{ ...SV.pill, color: 'var(--muted)' }}>SENDING</span>
  }
  if (visible && ack) {
    if (ack.status === 0) return <span style={{ ...SV.pill, color: 'var(--ok, #22c55e)' }}>OK</span>
    return <span style={{ ...SV.pill, color: 'var(--error)' }}>REJECTED</span>
  }
  return <span style={SV.pill} />
}

function SettingRow({ field, liveValue, draft, onDraftChange, onSend, isPending, ack, touched }) {
  const parsed  = parseFloat(draft)
  const isNum   = draft.trim() !== '' && isFinite(parsed)
  const inRange = isNum
    && (field.min == null || parsed >= field.min)
    && (field.max == null || parsed <= field.max)
  const isValid = inRange
  const canSend = isValid && !isPending

  const rangeHint = field.min != null && field.max != null
    ? `${formatLive(field.min)} – ${formatLive(field.max)}`
    : field.min != null ? `≥ ${formatLive(field.min)}`
    : field.max != null ? `≤ ${formatLive(field.max)}`
    : null

  return (
    <div style={SV.row}>
      <div style={SV.rowLabel}>
        <span style={SV.labelText}>{field.label}</span>
        <span style={SV.labelUnit}> ({field.unit})</span>
        {rangeHint && <span style={SV.rangeHint}>[{rangeHint}]</span>}
      </div>
      <div style={SV.liveVal}>{formatLive(liveValue)}</div>
      <input
        style={{ ...SV.input, ...(touched && !isValid ? SV.inputError : {}) }}
        type="text"
        value={draft}
        onChange={e => onDraftChange(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter' && canSend) onSend() }}
        placeholder="value"
      />
      <button
        style={{ ...SV.sendBtn, ...(canSend ? SV.sendBtnActive : SV.sendBtnDisabled) }}
        disabled={!canSend}
        onClick={onSend}
      >
        SET
      </button>
      <AckPill isPending={isPending} ack={ack} />
    </div>
  )
}

export default function SettingsView({ packets, lastAck }) {
  const [drafts, setDrafts] = useState({})
  const [touched, setTouched] = useState(new Set())
  const [pendingFieldId, setPendingFieldId] = useState(null)
  const [fieldAcks, setFieldAcks] = useState({})
  const ackTimeoutRef = useRef(null)

  const settingsPkt = packets['FlightSettings']
  const hasData = settingsPkt != null

  // Derive groups from the live packet fields (field index === field_id)
  const groups = useMemo(
    () => (settingsPkt?.fields ? buildGroups(settingsPkt.fields) : []),
    [settingsPkt?.fields]
  )

  function getLiveValue(fieldName) {
    return settingsPkt?.fields?.find(f => f.name === fieldName)?.value ?? null
  }

  // Pre-populate drafts once per field when live data first arrives.
  // Clamp to the field's valid range so emulator out-of-range values don't
  // immediately fail validation before the user has touched the input.
  useEffect(() => {
    if (!settingsPkt?.fields) return
    setDrafts(prev => {
      const next = { ...prev }
      settingsPkt.fields.forEach((f, idx) => {
        if (next[idx] === undefined && f.value != null) {
          let v = f.value
          if (f.min != null) v = Math.max(v, f.min)
          if (f.max != null) v = Math.min(v, f.max)
          next[idx] = String(parseFloat(v.toPrecision(6)))
        }
      })
      return next
    })
  }, [settingsPkt])

  // Correlate ACK to the last-sent field
  useEffect(() => {
    if (!lastAck) return
    if (lastAck.cmd_id !== CMD_UPDATE_SETTING) return
    if (pendingFieldId === null) return
    clearTimeout(ackTimeoutRef.current)
    setFieldAcks(prev => ({ ...prev, [pendingFieldId]: { status: lastAck.status, wall_ms: lastAck.wall_ms } }))
    setPendingFieldId(null)
  }, [lastAck])

  async function sendUpdate(fieldId, rawValue) {
    const value = parseFloat(rawValue)
    if (!isFinite(value)) return

    setPendingFieldId(fieldId)
    setFieldAcks(prev => { const n = { ...prev }; delete n[fieldId]; return n })

    clearTimeout(ackTimeoutRef.current)
    ackTimeoutRef.current = setTimeout(() => setPendingFieldId(null), 5000)

    try {
      await fetch('/api/fc/command/update_setting', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ field_id: fieldId, value }),
      })
    } catch {
      clearTimeout(ackTimeoutRef.current)
      setPendingFieldId(null)
    }
  }

  return (
    <div style={SV.root}>
      <div style={SV.header}>
        <span style={SV.title}>FLIGHT SETTINGS</span>
        {hasData && <span style={SV.liveTag}>LIVE</span>}
      </div>

      {!hasData && (
        <div style={SV.noBanner}>
          NO FLIGHT SETTINGS DATA — commands can still be sent
        </div>
      )}

      <div style={SV.scrollArea}>
        {groups.map(group => (
          <div key={group.title} style={SV.group}>
            <div style={SV.groupTitle}>{group.title}</div>
            {group.fields.map(field => (
              <SettingRow
                key={field.id}
                field={field}
                liveValue={getLiveValue(field.name)}
                draft={drafts[field.id] ?? ''}
                onDraftChange={val => {
                  setDrafts(prev => ({ ...prev, [field.id]: val }))
                  setTouched(prev => new Set([...prev, field.id]))
                }}
                onSend={() => sendUpdate(field.id, drafts[field.id])}
                isPending={pendingFieldId === field.id}
                ack={fieldAcks[field.id] ?? null}
                touched={touched.has(field.id)}
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}

const SV = {
  root: {
    flex:          1,
    display:       'flex',
    flexDirection: 'column',
    overflow:      'hidden',
    background:    'var(--bg)',
  },
  header: {
    display:      'flex',
    alignItems:   'center',
    gap:          12,
    padding:      '10px 20px 8px',
    borderBottom: '1px solid var(--border)',
    flexShrink:   0,
  },
  title: {
    fontFamily:    'var(--font-mono)',
    fontSize:      11,
    letterSpacing: 2,
    color:         'var(--muted)',
    textTransform: 'uppercase',
  },
  liveTag: {
    fontSize:      9,
    fontFamily:    'var(--font-mono)',
    color:         '#22c55e',
    letterSpacing: 1,
  },
  noBanner: {
    background:    'rgba(245,158,11,0.08)',
    border:        '1px solid rgba(245,158,11,0.3)',
    color:         'var(--warn)',
    fontFamily:    'var(--font-mono)',
    fontSize:      11,
    padding:       '8px 20px',
    flexShrink:    0,
    letterSpacing: 1,
  },
  scrollArea: {
    flex:          1,
    overflowY:     'auto',
    padding:       '14px 20px',
    display:       'flex',
    flexDirection: 'column',
    gap:           22,
  },
  group: {
    display:       'flex',
    flexDirection: 'column',
    gap:           2,
  },
  groupTitle: {
    fontSize:      10,
    fontFamily:    'var(--font-mono)',
    letterSpacing: 2,
    color:         'var(--muted)',
    textTransform: 'uppercase',
    paddingBottom: 6,
    borderBottom:  '1px solid var(--border)',
    marginBottom:  4,
  },
  row: {
    display:    'flex',
    alignItems: 'center',
    gap:        10,
    padding:    '5px 0',
    borderBottom: '1px solid rgba(255,255,255,0.04)',
  },
  rowLabel: {
    flex:       1,
    minWidth:   0,
    display:    'flex',
    alignItems: 'baseline',
    gap:        3,
  },
  labelText: {
    fontFamily:   'var(--font-mono)',
    fontSize:     12,
    color:        'var(--text)',
    whiteSpace:   'nowrap',
    overflow:     'hidden',
    textOverflow: 'ellipsis',
  },
  labelUnit: {
    fontFamily: 'var(--font-mono)',
    fontSize:   10,
    color:      'var(--muted)',
    whiteSpace: 'nowrap',
  },
  rangeHint: {
    fontFamily: 'var(--font-mono)',
    fontSize:   9,
    color:      'var(--muted)',
    whiteSpace: 'nowrap',
    opacity:    0.6,
  },
  liveVal: {
    width:      90,
    textAlign:  'right',
    fontFamily: 'var(--font-mono)',
    fontSize:   12,
    color:      'var(--accent)',
    flexShrink: 0,
  },
  input: {
    width:      110,
    background: 'var(--surface)',
    border:     '1px solid var(--border)',
    borderRadius: 3,
    color:      'var(--text)',
    fontFamily: 'var(--font-mono)',
    fontSize:   12,
    padding:    '3px 6px',
    outline:    'none',
    flexShrink: 0,
  },
  inputError: {
    borderColor: 'var(--error)',
  },
  sendBtn: {
    fontFamily:  'var(--font-mono)',
    fontSize:    10,
    fontWeight:  700,
    letterSpacing: 1,
    padding:     '3px 8px',
    borderRadius: 3,
    cursor:      'pointer',
    flexShrink:  0,
  },
  sendBtnActive: {
    border:     '1px solid var(--accent)',
    background: 'rgba(0,212,255,0.10)',
    color:      'var(--accent)',
  },
  sendBtnDisabled: {
    border:     '1px solid var(--border)',
    background: 'rgba(255,255,255,0.03)',
    color:      'var(--muted)',
    cursor:     'not-allowed',
  },
  pill: {
    width:         64,
    fontFamily:    'var(--font-mono)',
    fontSize:      9,
    letterSpacing: 1,
    textAlign:     'center',
    flexShrink:    0,
    display:       'inline-block',
  },
}
