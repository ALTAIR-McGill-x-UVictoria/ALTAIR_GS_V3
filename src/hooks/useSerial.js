import { useState, useCallback } from 'react'

/**
 * Thin wrapper around the backend REST API for port management.
 */
export function useSerial() {
  const [ports,    setPorts]    = useState([])
  const [loading,  setLoading]  = useState(false)
  const [error,    setError]    = useState(null)

  const refreshPorts = useCallback(async () => {
    try {
      const res = await fetch('/api/ports')
      setPorts(await res.json())
    } catch (e) {
      setError(e.message)
    }
  }, [])

  const connectPort = useCallback(async (port, baud = 57600) => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ port, baud }),
      })
      const data = await res.json()
      if (!data.ok) setError(data.error)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  const disconnectPort = useCallback(async () => {
    await fetch('/api/disconnect', { method: 'POST' })
  }, [])

  return { ports, loading, error, refreshPorts, connectPort, disconnectPort }
}
