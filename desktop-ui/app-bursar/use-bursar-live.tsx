import { useQueryClient } from '@tanstack/react-query'
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

/**
 * Bursar live event stream. The dashboard backend the desktop app already
 * spawns exposes a `/api/plugins/bursar/events` WebSocket that tails the
 * engine's append-only `events` table (see plugin_api.py). Rather than wait on
 * the React Query poll, we open that socket once and, on each batch of new
 * events, coalesce a cache invalidation so every Bursar page refreshes the
 * instant the dispatcher clears a market — a genuine push feed.
 *
 * Auth: the desktop app spawns its backend on loopback, where the dashboard's
 * WS gate accepts the legacy `?token=<session-token>` (the same token the IPC
 * `api` bridge uses). Gated/remote binds reject it; there we simply never reach
 * `live` and the polling backstop carries the pages, so the demo is robust.
 */

export type BursarLiveStatus = 'connecting' | 'live' | 'reconnecting' | 'offline'

interface BursarLiveState {
  status: BursarLiveStatus
  lastEventAt: number | null
}

const BursarLiveContext = createContext<BursarLiveState>({ lastEventAt: null, status: 'offline' })

export function useBursarLiveStatus(): BursarLiveState {
  return useContext(BursarLiveContext)
}

// Build a ws(s):// URL to the Bursar event stream from the desktop connection
// descriptor. Returns null if the connection can't yield a usable URL.
function buildEventsUrl(conn: { baseUrl?: string; token?: string } | null, since: number): string | null {
  if (!conn?.baseUrl) {
    return null
  }
  let url: URL
  try {
    url = new URL('/api/plugins/bursar/events', conn.baseUrl)
  } catch {
    return null
  }
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  if (conn.token) {
    url.searchParams.set('token', conn.token)
  }
  url.searchParams.set('since', String(since))
  return url.toString()
}

// Give up to the polling backstop after this many failures to ever open the
// socket (the signature of a gated/remote bind that won't take `?token=`).
const FRESH_MAX_FAILS = 4

export function BursarLiveProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient()
  const [status, setStatus] = useState<BursarLiveStatus>('connecting')
  const [lastEventAt, setLastEventAt] = useState<number | null>(null)

  useEffect(() => {
    let ws: WebSocket | null = null
    let disposed = false
    let cursor = 0
    let fails = 0
    let everOpen = false
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let invalidateTimer: ReturnType<typeof setTimeout> | null = null

    // Coalesce a burst of event frames into a single cache refresh. Skip the
    // static price catalog — it never changes mid-session.
    const refresh = () => {
      if (invalidateTimer) {
        return
      }
      invalidateTimer = setTimeout(() => {
        invalidateTimer = null
        void qc.invalidateQueries({ predicate: q => q.queryKey[0] === 'bursar' && q.queryKey[1] !== 'catalog' })
      }, 200)
    }

    const scheduleReconnect = () => {
      if (disposed) {
        return
      }
      if (!everOpen && fails >= FRESH_MAX_FAILS) {
        // Never reached the socket — almost certainly a gated/remote bind that
        // won't accept `?token=`. Stop retrying and let polling carry the pages.
        setStatus('offline')
        return
      }
      setStatus('reconnecting')
      const delay = Math.min(800 * 2 ** fails, 15000)
      fails += 1
      reconnectTimer = setTimeout(() => void connect(), delay)
    }

    const connect = async () => {
      if (disposed) {
        return
      }
      const desktop = window.hermesDesktop
      if (!desktop?.getConnection) {
        setStatus('offline')
        return
      }
      const conn = await desktop.getConnection().catch(() => null)
      if (disposed) {
        return
      }
      const url = buildEventsUrl(conn, cursor)
      if (!url) {
        scheduleReconnect()
        return
      }
      let socket: WebSocket
      try {
        socket = new WebSocket(url)
      } catch {
        scheduleReconnect()
        return
      }
      ws = socket

      socket.onopen = () => {
        if (disposed) {
          return
        }
        everOpen = true
        fails = 0
        setStatus('live')
      }
      socket.onmessage = event => {
        if (disposed) {
          return
        }
        try {
          const data = JSON.parse(event.data as string) as { cursor?: number; events?: unknown[] }
          if (typeof data.cursor === 'number') {
            cursor = data.cursor
          }
          if (Array.isArray(data.events) && data.events.length > 0) {
            setLastEventAt(Date.now())
            refresh()
          }
        } catch {
          // Ignore a malformed frame; the next one resyncs the cursor.
        }
      }
      socket.onclose = () => {
        if (disposed) {
          return
        }
        ws = null
        scheduleReconnect()
      }
      socket.onerror = () => {
        // `onclose` follows and drives reconnect; close here so we don't leave a
        // half-open socket dangling on platforms that fire error without close.
        try {
          socket.close()
        } catch {
          // already closing
        }
      }
    }

    void connect()

    return () => {
      disposed = true
      if (reconnectTimer) {
        clearTimeout(reconnectTimer)
      }
      if (invalidateTimer) {
        clearTimeout(invalidateTimer)
      }
      if (ws) {
        ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null
        try {
          ws.close()
        } catch {
          // already closing
        }
      }
    }
  }, [qc])

  const value = useMemo<BursarLiveState>(() => ({ lastEventAt, status }), [lastEventAt, status])
  return <BursarLiveContext.Provider value={value}>{children}</BursarLiveContext.Provider>
}
