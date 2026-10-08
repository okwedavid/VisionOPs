import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'

import { LiveSession, socketUrlForSession } from '@/lib/live'
import type { SessionStatePayload } from '@/types/api'

/** A minimal fake WebSocket, so reconnect logic is testable without a server. */
class FakeSocket {
  static instances: FakeSocket[] = []
  static OPEN = 1

  readyState = 0
  onopen: (() => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  readonly url: string

  constructor(url: string) {
    this.url = url
    FakeSocket.instances.push(this)
  }

  close(code = 1000): void {
    this.readyState = 3
    this.onclose?.({ code })
  }

  emit(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) })
  }

  open(): void {
    this.readyState = 1
    this.onopen?.()
  }

  drop(code = 1006): void {
    this.readyState = 3
    this.onclose?.({ code })
  }
}

function statePayload(overrides: Partial<SessionStatePayload> = {}): SessionStatePayload {
  return {
    type: 'session.state',
    session_id: 's1',
    camera_id: 'c1',
    camera_name: 'front',
    seq: 1,
    server_time: 1,
    source: {
      state: 'running',
      reason_code: null,
      detail: null,
      kind: 'file',
      fps: 5,
      frames: 10,
      dropped: 0,
      retries: 0,
      uptime_seconds: 2,
      position: null,
      duration: null,
    },
    ai: {
      state: 'ready',
      model: 'yolo11n',
      sha256: 'abc',
      provider: 'CPUExecutionProvider',
      reason_code: null,
      detail: null,
      latency_ms: 40,
    },
    stats: {
      active_tracks: 1,
      in_zone: {},
      processed_fps: 5,
      frame_age_ms: 10,
      frames_processed: 10,
      frames_dropped: 0,
      inference_errors: 0,
      events_emitted: 0,
      events_suppressed: 0,
    },
    tracks: [],
    zones: [],
    events: [],
    ...overrides,
  }
}

describe('LiveSession', () => {
  beforeEach(() => {
    FakeSocket.instances = []
    vi.stubGlobal('WebSocket', FakeSocket as unknown as typeof WebSocket)
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('builds a websocket URL that carries the API key', () => {
    const url = socketUrlForSession('abc123')
    expect(url).toContain('/api/v1/stream/sessions/abc123')
    expect(url.startsWith('ws://') || url.startsWith('wss://')).toBe(true)
  })

  it('reports connecting then open, and exposes the latest state', () => {
    const session = new LiveSession('s1')
    const states: string[] = []
    session.subscribe((snapshot) => states.push(snapshot.connection))

    session.connect()
    const socket = FakeSocket.instances[0]!
    socket.open()
    socket.emit(statePayload({ tracks: [{ id: 1, label: 'person', confidence: 0.9, box: [0, 0, 0.2, 0.9], age: 3, clip: false }] }))

    expect(states).toContain('connecting')
    expect(states).toContain('open')
    expect(session.latest.state?.tracks).toHaveLength(1)
    expect(session.snapshot.stale).toBe(false)
    session.close()
  })

  it('collects events from state and event messages, and dedupes by id', () => {
    const session = new LiveSession('s1')
    session.connect()
    const socket = FakeSocket.instances[0]!
    socket.open()

    socket.emit(
      statePayload({
        events: [{ id: 'e1', type: 'ZONE_ENTERED', severity: 'warning', occurred_at: 1, zone_id: 'z', zone_name: 'restricted', track_id: 1, metadata: {} }],
      }),
    )
    socket.emit(
      statePayload({
        events: [{ id: 'e1', type: 'ZONE_ENTERED', severity: 'warning', occurred_at: 1, zone_id: 'z', zone_name: 'restricted', track_id: 1, metadata: {} }],
      }),
    )
    socket.emit({
      type: 'session.event',
      session_id: 's1',
      server_time: 2,
      events: [{ id: 'e2', type: 'ZONE_EXITED', severity: 'info', occurred_at: 2, zone_id: 'z', zone_name: 'restricted', track_id: 1, metadata: {}, camera_id: 'c1', camera_name: 'front', confidence: 0.8, snapshot_path: null }],
    })

    expect(session.snapshot.events.map((event) => event.id)).toEqual(['e2', 'e1'])
    session.close()
  })

  it('marks the connection stale after the threshold and recovers on reconnect', () => {
    const session = new LiveSession('s1')
    session.connect()
    const socket = FakeSocket.instances[0]!
    socket.open()
    socket.emit(statePayload())
    expect(session.snapshot.stale).toBe(false)

    vi.advanceTimersByTime(4000)
    expect(session.snapshot.stale).toBe(true)

    socket.drop()
    expect(session.snapshot.connection).toBe('reconnecting')

    vi.advanceTimersByTime(5000)
    expect(FakeSocket.instances.length).toBe(2)
    session.close()
  })

  it('does not reconnect after an explicit close', () => {
    const session = new LiveSession('s1')
    session.connect()
    FakeSocket.instances[0]!.open()
    session.close()
    vi.advanceTimersByTime(60_000)
    expect(FakeSocket.instances.length).toBe(1)
    expect(session.snapshot.connection).toBe('closed')
  })

  it('reports a rejected key distinctly', () => {
    const session = new LiveSession('s1')
    session.connect()
    FakeSocket.instances[0]!.drop(4401)
    expect(session.snapshot.error).toContain('API key')
    session.close()
  })
})
