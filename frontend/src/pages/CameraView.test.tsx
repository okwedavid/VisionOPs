import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ToastContext } from '@/components/toast-context'
import { CameraView } from '@/pages/CameraView'

class FakeWebSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  static instances: FakeWebSocket[] = []

  readonly url: string
  readyState = FakeWebSocket.CONNECTING
  binaryType = 'blob'
  onopen: ((event: Event) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  send = vi.fn()
  close = vi.fn(() => {
    this.readyState = FakeWebSocket.CLOSED
  })

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }
}

const mocks = vi.hoisted(() => ({
  startSession: vi.fn(),
  stopSession: vi.fn(),
  status: vi.fn(),
}))

vi.mock('@/hooks/useApi', () => ({
  useCamera: () => ({
    data: { id: 'cam-1', name: 'Front door', site_id: 'site-1', site_name: 'HQ' },
    isLoading: false,
  }),
  useCameras: () => ({
    data: [{ id: 'cam-1', name: 'Front door', site_id: 'site-1', site_name: 'HQ' }],
    isLoading: false,
  }),
  useSources: () => ({
    data: [
      {
        id: 'src-1',
        camera_id: 'cam-1',
        name: 'Laptop webcam',
        kind: 'browser',
        target: 'browser',
        is_primary: true,
        enabled: true,
      },
    ],
  }),
  useZones: () => ({ data: [] }),
  useSystemStatus: () => ({ data: mocks.status() }),
  useModelInfo: () => ({ data: { state: 'ready', classes: ['person', 'bicycle', 'car'] } }),
  useStartSession: () => ({ mutateAsync: mocks.startSession, isPending: false }),
  useStopSession: () => ({ mutateAsync: mocks.stopSession, isPending: false }),
  useCreateZone: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUpdateZone: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteZone: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useLiveSession: () => ({
    state: null,
    events: [],
    stale: false,
    connection: 'idle',
    error: null,
    lastMessageAt: null,
    live: null,
    ageMs: null,
  }),
}))

const originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
const originalVideoWidth = Object.getOwnPropertyDescriptor(HTMLVideoElement.prototype, 'videoWidth')
const originalVideoHeight = Object.getOwnPropertyDescriptor(HTMLVideoElement.prototype, 'videoHeight')
let getUserMedia: ReturnType<typeof vi.fn>

function status(sessions: unknown[] = []) {
  return {
    status: 'healthy',
    version: 'test',
    environment: 'test',
    timestamp: new Date().toISOString(),
    uptime_seconds: 1,
    model: { state: 'ready', reason_code: null, detail: 'yolo11n ready', required: false },
    sessions: { active: sessions.length, max: 2, items: sessions },
    checks: {},
    storage: {
      database: 'ok',
      snapshot_dir: 'disabled',
      disk_free_bytes: null,
      credential_encryption: 'not-configured',
    },
    counters: {},
    runtime: {},
  }
}

function runningSession(sessionId: string) {
  return {
    session_id: sessionId,
    camera_id: 'cam-1',
    camera_name: 'Front door',
    state: 'running',
    started_at: Date.now() / 1000,
    uptime_seconds: 3,
    target_fps: 10,
    source_kind: 'browser',
    source_id: 'src-1',
    reason_code: null,
    detail: null,
    processed_fps: 9.5,
    frame_age_ms: 100,
    active_tracks: 0,
    in_zone: {},
    events_emitted: 0,
    events_suppressed: 0,
    frames_processed: 30,
    frames_dropped: 0,
    inference_ms: 20,
    inference_errors: 0,
    ai: {},
    source: {},
    // Deliberately absent: system status never sends this. The page must derive
    // the ingest URL from the session id instead of waiting for it.
  }
}

function renderView() {
  const push = vi.fn()
  return render(
    <ToastContext.Provider value={{ push, dismiss: vi.fn() }}>
      <MemoryRouter initialEntries={['/cameras/cam-1']}>
        <Routes>
          <Route path="/cameras/:cameraId" element={<CameraView />} />
        </Routes>
      </MemoryRouter>
    </ToastContext.Provider>,
  )
}

beforeEach(() => {
  FakeWebSocket.instances = []
  vi.stubGlobal('WebSocket', FakeWebSocket)
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  )
  vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1))
  vi.stubGlobal('cancelAnimationFrame', vi.fn())
  mocks.startSession.mockReset()
  mocks.stopSession.mockReset()
  mocks.status.mockReset()
  mocks.status.mockReturnValue(status())
  mocks.startSession.mockResolvedValue({
    session_id: 'sess-1',
    camera_id: 'cam-1',
    camera_name: 'Front door',
    state: 'starting',
    target_fps: 10,
    source_kind: 'browser',
    ingest_ws_url: '/api/v1/stream/ingest?session_id=sess-1',
  })

  getUserMedia = vi.fn().mockResolvedValue({
    active: true,
    getTracks: () => [{ stop: vi.fn(), readyState: 'live', kind: 'video' }],
    getVideoTracks: () => [{ stop: vi.fn(), readyState: 'live', kind: 'video' }],
  })
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia },
  })

  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
  vi.spyOn(HTMLMediaElement.prototype, 'readyState', 'get').mockReturnValue(2)
  Object.defineProperty(HTMLVideoElement.prototype, 'videoWidth', { configurable: true, get: () => 640 })
  Object.defineProperty(HTMLVideoElement.prototype, 'videoHeight', { configurable: true, get: () => 480 })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    () => ({ drawImage: vi.fn() }) as unknown as CanvasRenderingContext2D,
  )
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation((callback: BlobCallback) => {
    callback(new Blob(['jpeg'], { type: 'image/jpeg' }))
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  if (originalMediaDevices) Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices)
  else delete (navigator as { mediaDevices?: unknown }).mediaDevices
  if (originalVideoWidth) Object.defineProperty(HTMLVideoElement.prototype, 'videoWidth', originalVideoWidth)
  else delete (HTMLVideoElement.prototype as { videoWidth?: unknown }).videoWidth
  if (originalVideoHeight) Object.defineProperty(HTMLVideoElement.prototype, 'videoHeight', originalVideoHeight)
  else delete (HTMLVideoElement.prototype as { videoHeight?: unknown }).videoHeight
})

describe('CameraView browser source', () => {
  it('asks for the camera from the Start click and derives the ingest URL from the new session', async () => {
    renderView()

    await userEvent.click(screen.getByRole('button', { name: /start session/i }))

    await waitFor(() => expect(mocks.startSession).toHaveBeenCalled())
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(mocks.startSession).toHaveBeenCalledWith({
      cameraId: 'cam-1',
      body: { source_id: 'src-1' },
    })
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    expect(FakeWebSocket.instances[0]!.url).toContain('/api/v1/stream/ingest?session_id=sess-1')
  })

  it('adopts a running session even when system status has no ingest_ws_url', async () => {
    mocks.status.mockReturnValue(status([runningSession('sess-adopted')]))
    renderView()

    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    expect(FakeWebSocket.instances[0]!.url).toContain('/api/v1/stream/ingest?session_id=sess-adopted')
  })
})
