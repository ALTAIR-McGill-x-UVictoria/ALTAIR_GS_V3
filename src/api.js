// Shared operator-token handling for control (non-GET) requests. Read-only
// GET routes and both WebSockets need no token — see backend/main.py's
// _require_admin_token middleware for the server-side half of this.
const TOKEN_KEY = 'altair_admin_token'

export function getAdminToken() {
  return sessionStorage.getItem(TOKEN_KEY) || ''
}

export function setAdminToken(token) {
  if (token) sessionStorage.setItem(TOKEN_KEY, token)
  else sessionStorage.removeItem(TOKEN_KEY)
}

// Drop-in replacement for fetch() on control endpoints — attaches the
// operator token (if unlocked) as a Bearer header.
export function apiFetch(path, options = {}) {
  const token = getAdminToken()
  const headers = { ...(options.headers || {}) }
  if (token) headers.Authorization = `Bearer ${token}`
  return fetch(path, { ...options, headers })
}
