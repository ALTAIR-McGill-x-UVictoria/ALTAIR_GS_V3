import { useState, useEffect, useRef, useMemo } from 'react'
import { apiFetch } from '../api'

const CMD_UPDATE_SETTING = 0xC3
const CMD_RADIO_CONFIG   = 0xC5

const RADIO_DATA_RATE_LABELS = ['Low', 'Mid', 'High']
const RADIO_TX_POWER_LABELS  = ['Low', 'Mid', 'High']

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
  const isValid = draft.trim() !== '' && isFinite(parsed)
  const canSend = isValid && !isPending
  const rangeHint = null

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

function RadioConfigPanel({ lastAck, currentStage = -1 }) {
  const locked = currentStage >= 1

  const [defaults, setDefaults] = useState(null)
  const [live,     setLive]     = useState(null)
  const [linked,   setLinked]   = useState(false)

  const [draftRate,    setDraftRate]    = useState(1)
  const [draftPower,   setDraftPower]   = useState(2)
  const [draftChannel, setDraftChannel] = useState('0')

  const [pending,    setPending]    = useState(false)
  const [ackResult,  setAckResult]  = useState(null)  // { status, gs_switched } | null
  const [ackVisible, setAckVisible] = useState(false)
  const ackTimerRef = useRef(null)

  // Fetch config from backend. Pass refresh=true to force a live modem read
  // (suspends telemetry for ~1-4 s — only use for explicit user action).
  async function fetchConfig(refresh = false) {
    try {
      const url  = refresh ? '/api/radio/config?refresh=1' : '/api/radio/config'
      const res  = await fetch(url)
      const data = await res.json()
      setDefaults(data.defaults)
      setLinked(data.linked ?? false)
      if (data.live) {
        setLive(data.live)
        setDraftRate(data.live.data_rate)
        setDraftPower(data.live.tx_power)
        setDraftChannel(String(data.live.channel))
      } else if (data.defaults) {
        setDraftRate(data.defaults.data_rate)
        setDraftPower(data.defaults.tx_power)
        setDraftChannel(String(data.defaults.channel))
      }
    } catch {}
  }

  useEffect(() => { fetchConfig() }, [])

  // Watch for the FC ACK on cmd 0xC5
  useEffect(() => {
    if (!lastAck || lastAck.cmd_id !== CMD_RADIO_CONFIG) return
    if (!pending) return
    setPending(false)
    setAckResult({ status: lastAck.status })
    setAckVisible(true)
    clearTimeout(ackTimerRef.current)
    ackTimerRef.current = setTimeout(() => setAckVisible(false), 8000)
    if (lastAck.status === 0) fetchConfig()
  }, [lastAck])

  async function handleApply() {
    if (locked || pending) return
    setPending(true)
    setAckResult(null)
    setAckVisible(false)
    clearTimeout(ackTimerRef.current)
    // 8 s timeout guard in case no ACK arrives
    ackTimerRef.current = setTimeout(() => setPending(false), 8000)
    try {
      const res  = await apiFetch('/api/radio/config', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ data_rate: draftRate, tx_power: draftPower, channel: Number(draftChannel) || 0 }),
      })
      const data = await res.json()
      if (!data.ok) {
        clearTimeout(ackTimerRef.current)
        setPending(false)
        setAckResult({ status: -1, error: data.error })
        setAckVisible(true)
        ackTimerRef.current = setTimeout(() => setAckVisible(false), 8000)
      } else if (data.emulated) {
        clearTimeout(ackTimerRef.current)
        setPending(false)
        setAckResult({ status: 0, gs_switched: false })
        setAckVisible(true)
        ackTimerRef.current = setTimeout(() => setAckVisible(false), 8000)
      }
      // Real hardware: keep pending=true, wait for WS ACK via lastAck effect above
    } catch {
      clearTimeout(ackTimerRef.current)
      setPending(false)
    }
  }

  const pillColor = !ackVisible ? 'transparent'
    : ackResult?.status === 0  ? '#22c55e'
    : '#ff4444'
  const pillText = pending        ? 'SENDING'
    : !ackVisible                 ? ''
    : ackResult?.status === 0     ? (ackResult?.gs_switched === false ? 'FC OK' : 'APPLIED')
    : ackResult?.error            ? 'REJECTED'
    : 'NACK'

  return (
    <div style={SV.group}>
      <div style={SV.groupTitle}>Radio Configuration</div>

      {locked && (
        <div style={{
          background:    'rgba(239,68,68,0.10)',
          border:        '1px solid rgba(239,68,68,0.35)',
          color:         '#ef4444',
          fontFamily:    'var(--font-mono)',
          fontSize:      10,
          letterSpacing: 1,
          padding:       '5px 10px',
          marginBottom:  6,
        }}>
          RADIO CONFIG LOCKED — VEHICLE ARMED OR IN FLIGHT
        </div>
      )}

      {/* GS modem link indicator + refresh */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
        <span style={{
          width: 7, height: 7, borderRadius: '50%', flexShrink: 0,
          background: linked ? '#22c55e' : '#607080',
          display: 'inline-block',
        }} />
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--muted)' }}>
          GS modem {linked ? 'linked' : 'not linked'}
        </span>
        {live && (
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--accent)', marginLeft: 8 }}>
            live: DR={RADIO_DATA_RATE_LABELS[live.data_rate]} PWR={RADIO_TX_POWER_LABELS[live.tx_power]} CH={live.channel}
          </span>
        )}
        <button
          onClick={() => fetchConfig(true)}
          disabled={locked || pending}
          title="Read live config from GS modem (suspends telemetry briefly)"
          style={{ ...SV.sendBtn, ...(locked || pending ? SV.sendBtnDisabled : SV.sendBtnActive), marginLeft: 8, padding: '2px 8px', fontSize: 9 }}
        >
          READ MODEM
        </button>
      </div>

      {/* Controls row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        {/* Data rate */}
        <div style={SV.radioField}>
          <span style={SV.radioLabel}>Data Rate</span>
          <select
            disabled={locked || pending}
            value={draftRate}
            onChange={e => setDraftRate(Number(e.target.value))}
            style={SV.radioSelect}
          >
            {RADIO_DATA_RATE_LABELS.map((l, i) => (
              <option key={i} value={i}>{l}</option>
            ))}
          </select>
        </div>

        {/* TX power */}
        <div style={SV.radioField}>
          <span style={SV.radioLabel}>TX Power</span>
          <select
            disabled={locked || pending}
            value={draftPower}
            onChange={e => setDraftPower(Number(e.target.value))}
            style={SV.radioSelect}
          >
            {RADIO_TX_POWER_LABELS.map((l, i) => (
              <option key={i} value={i}>{l}</option>
            ))}
          </select>
        </div>

        {/* Channel */}
        <div style={SV.radioField}>
          <span style={SV.radioLabel}>Channel (0–63)</span>
          <input
            type="number"
            min={0} max={63} step={1}
            disabled={locked || pending}
            value={draftChannel}
            onChange={e => setDraftChannel(e.target.value.replace(/^0+(?=\d)/, ''))}
            onBlur={() => setDraftChannel(String(Math.max(0, Math.min(63, Number(draftChannel) || 0))))}
            style={{ ...SV.input, width: 70 }}
          />
        </div>

        {/* Apply button */}
        <button
          disabled={locked || pending}
          onClick={handleApply}
          style={{
            ...SV.sendBtn,
            ...(locked || pending ? SV.sendBtnDisabled : SV.sendBtnActive),
          }}
        >
          {pending ? 'SENDING…' : 'APPLY'}
        </button>

        {/* ACK pill */}
        <span style={{ ...SV.pill, color: pillColor, minWidth: 72 }}>
          {pillText}
        </span>

        {/* Default indicator */}
        {defaults && (
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--muted)', marginLeft: 4 }}>
            default: DR={RADIO_DATA_RATE_LABELS[defaults.data_rate]} PWR={RADIO_TX_POWER_LABELS[defaults.tx_power]} CH={defaults.channel}
          </span>
        )}
      </div>
    </div>
  )
}

export default function SettingsView({ packets, lastAck, overrideChecks = false, onOverrideChange, currentStage = -1 }) {
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
          next[idx] = String(parseFloat(f.value.toPrecision(6)))
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
      await apiFetch('/api/fc/command/update_setting', {
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

      {/* Override toggle */}
      <div style={{
        ...SV.overrideBar,
        background:   overrideChecks ? 'rgba(239,68,68,0.10)' : 'transparent',
        borderBottom: `1px solid ${overrideChecks ? 'rgba(239,68,68,0.4)' : 'var(--border)'}`,
      }}>
        <div style={SV.overrideLeft}>
          <span style={{ ...SV.overrideLabel, color: overrideChecks ? '#ef4444' : 'var(--muted)' }}>
            CHECK OVERRIDE
          </span>
          <span style={SV.overrideDesc}>
            Bypasses ARM / LAUNCH OK pre-flight check requirements
          </span>
        </div>
        <button
          style={{
            ...SV.overrideBtn,
            background:  overrideChecks ? 'rgba(239,68,68,0.20)' : 'rgba(255,255,255,0.04)',
            border:      `1px solid ${overrideChecks ? '#ef4444' : 'var(--border)'}`,
            color:       overrideChecks ? '#ef4444' : 'var(--muted)',
          }}
          onClick={() => onOverrideChange?.(!overrideChecks)}
        >
          {overrideChecks ? 'OVERRIDE ON' : 'OVERRIDE OFF'}
        </button>
      </div>

      {!hasData && (
        <div style={SV.noBanner}>
          NO FLIGHT SETTINGS DATA — commands can still be sent
        </div>
      )}

      <div style={SV.scrollArea}>
        <RadioConfigPanel lastAck={lastAck} currentStage={currentStage} />

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
  overrideBar: {
    display:    'flex',
    alignItems: 'center',
    gap:        12,
    padding:    '8px 20px',
    flexShrink: 0,
    transition: 'background 0.15s ease',
  },
  overrideLeft: {
    flex:          1,
    display:       'flex',
    flexDirection: 'column',
    gap:           2,
  },
  overrideLabel: {
    fontFamily:    'var(--font-mono)',
    fontSize:      10,
    fontWeight:    700,
    letterSpacing: 2,
    textTransform: 'uppercase',
  },
  overrideDesc: {
    fontFamily: 'var(--font-mono)',
    fontSize:   9,
    color:      'var(--muted)',
    letterSpacing: 0.5,
  },
  overrideBtn: {
    fontFamily:    'var(--font-mono)',
    fontSize:      10,
    fontWeight:    700,
    letterSpacing: 1,
    padding:       '4px 12px',
    borderRadius:  3,
    cursor:        'pointer',
    flexShrink:    0,
    transition:    'all 0.15s ease',
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
  radioField: {
    display:       'flex',
    flexDirection: 'column',
    gap:           3,
  },
  radioLabel: {
    fontFamily:    'var(--font-mono)',
    fontSize:      9,
    color:         'var(--muted)',
    letterSpacing: 0.5,
    textTransform: 'uppercase',
  },
  radioSelect: {
    background:   'var(--surface)',
    border:       '1px solid var(--border)',
    borderRadius: 3,
    color:        'var(--text)',
    fontFamily:   'var(--font-mono)',
    fontSize:     12,
    padding:      '3px 6px',
    outline:      'none',
    cursor:       'pointer',
  },
}
