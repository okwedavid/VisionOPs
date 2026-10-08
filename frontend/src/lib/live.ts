/**
 * Live-state WebSocket client.
 *
 * Design constraints (``docs/FRONTEND.md`` §4):
 *  - the overlay reads a mutable ref inside `requestAnimationFrame`, so a
 *    5–15 FPS state stream never triggers a React re-render;
 *  - only small HUD numbers enter React state, throttled;
 *  - reconnect uses exponential backoff with jitter, and a `stale` flag is
 *    surfaced rather than hidden — a silent stale overlay is a lie.
 */

import type { LiveEventMessage, LiveEvent, SessionStatePayload } from '@/types/api'
import { API_BASE, getApiKey } from './api'

export type ConnectionState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed' | 'error'

export interface LiveSnapshot {
  state: SessionStatePayload | null
  lastMessageAt: number | null
  /** Events seen on this socket, newest last, capped. */
  events: LiveEvent[]
  /** True when no message has arrived for longer than the stale threshold. */
  stale: boolean
  connection: ConnectionState
  error: string | null
}

const STALE_AFTER_MS = 3000
const MAX_BUFFERED_EVENTS = 50
const MAX_BACKOFF_MS = 30_000

export function socketUrlForSession(sessionId: string, path = 'sessions'): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const key = getApiKey()
  const params = new URLSearchParams()
  if (key) params.set('key', key)
  const query = params.toString()
  return `${protocol}//${window.location.host}${API_BASE}/stream/${path}/${sessionId}${query ? `?${query}` : ''}`
}

export class LiveSession {
  private socket: WebSocket | null = null
  private attempt = 0
  private reconnectTimer: number | null = null
  private staleTimer: number | null = null
  private closedByUs = false
  private readonly sessionId: string

  /** Read by the overlay on every animation frame. Never triggers a render. */
  readonly latest: { state: SessionStatePayload | null; lastMessageAt: number | null } = {
    state: null,
    lastMessageAt: null,
  }

  private listeners = new Set<(snapshot: LiveSnapshot) => void>()
  private events: LiveEvent[] = []
  private connection: ConnectionState = 'idle'
  private error: string | null = null

  constructor(sessionId: string) {
    this.sessionId = sessionId
  }

  get snapshot(): LiveSnapshot {
    return {
      state: this.latest.state,
      lastMessageAt: this.latest.lastMessageAt,
      events: this.events,
      stale: this.isStale(),
      connection: this.connection,
      error: this.error,
    }
  }

  private isStale(): boolean {
    const at = this.latest.lastMessageAt
    return at !== null && Date.now() - at > STALE_AFTER_MS
  }

  subscribe(listener: (snapshot: LiveSnapshot) => void): () => void {
    this.listeners.add(listener)
    listener(this.snapshot)
    return () => this.listeners.delete(listener)
  }

  private emit(): void {
    const snapshot = this.snapshot
    for (const listener of this.listeners) listener(snapshot)
  }

  connect(): void {
    if (this.socket && this.socket.readyState <= WebSocket.OPEN) return
    this.closedByUs = false
    this.connection = this.attempt === 0 ? 'connecting' : 'reconnecting'
    this.emit()

    let socket: WebSocket
    try {
      socket = new WebSocket(socketUrlForSession(this.sessionId))
    } catch {
      this.connection = 'error'
      this.error = 'Could not open a live-state connection.'
      this.emit()
      this.scheduleReconnect()
      return
    }
    this.socket = socket

    socket.onopen = () => {
      this.attempt = 0
      this.connection = 'open'
      this.error = null
      this.startStaleWatch()
      this.emit()
    }

    socket.onmessage = (message) => {
      this.latest.lastMessageAt = Date.now()
      try {
        const payload = JSON.parse(String(message.data)) as
          | SessionStatePayload
          | LiveEventMessage
          | { type: string }
        if (payload.type === 'session.state') {
          this.latest.state = payload as SessionStatePayload
          for (const event of (payload as SessionStatePayload).events ?? []) {
            this.pushEvent(event)
          }
        } else if (payload.type === 'session.event') {
          for (const event of (payload as LiveEventMessage).events ?? []) {
            this.pushEvent(event)
          }
        } else if (payload.type === 'session.error') {
          this.error = (payload as { detail?: string }).detail ?? 'The session reported an error.'
        }
      } catch {
        this.error = 'Received a malformed live-state message.'
      }
      this.emit()
    }

    socket.onerror = () => {
      this.connection = 'error'
      this.error = 'Live updates stopped. Reconnecting…'
      this.emit()
    }

    socket.onclose = (event) => {
      this.stopStaleWatch()
      this.socket = null
      if (this.closedByUs) {
        this.connection = 'closed'
        this.emit()
        return
      }
      this.connection = 'reconnecting'
      this.error =
        event.code === 4401
          ? 'The API key was rejected for live updates.'
          : `Live updates disconnected (code ${event.code}). Reconnecting…`
      this.emit()
      this.scheduleReconnect()
    }
  }

  private pushEvent(event: LiveEvent): void {
    // Dedupe by id: the server may resend on a reconnect.
    if (event.id && this.events.some((existing) => existing.id === event.id)) return
    this.events = [event, ...this.events].slice(0, MAX_BUFFERED_EVENTS)
  }

  private scheduleReconnect(): void {
    if (this.closedByUs || this.reconnectTimer !== null) return
    this.attempt += 1
    // Exponential backoff with jitter so many open tabs do not retry in lockstep.
    const base = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** Math.min(this.attempt, 5))
    const delay = base * (0.7 + Math.random() * 0.6)
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null
      this.connect()
    }, delay)
  }

  private startStaleWatch(): void {
    this.stopStaleWatch()
    // Re-emit once a second so the UI's "stale" badge reflects real time rather
    // than only when a message happens to arrive.
    this.staleTimer = window.setInterval(() => this.emit(), 1000)
  }

  private stopStaleWatch(): void {
    if (this.staleTimer !== null) {
      window.clearInterval(this.staleTimer)
      this.staleTimer = null
    }
  }

  close(): void {
    this.closedByUs = true
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.stopStaleWatch()
    this.socket?.close()
    this.socket = null
    this.connection = 'closed'
    this.emit()
  }
}
