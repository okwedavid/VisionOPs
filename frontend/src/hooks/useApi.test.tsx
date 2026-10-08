import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  useApiKeys,
  useCamera,
  useCameras,
  useCreateApiKey,
  useCreateCamera,
  useCreateSite,
  useCreateSource,
  useCreateZone,
  useDeleteZone,
  useEvent,
  useEvents,
  useHasApiKey,
  useLiveSession,
  useModelInfo,
  useOrganization,
  useRevokeApiKey,
  useSites,
  useSources,
  useStartSession,
  useStopSession,
  useSummary,
  useSystemStatus,
  useUpdateZone,
  useZones,
} from '@/hooks/useApi'

const { apiMock, getApiKeyMock, onApiKeyChangeMock, liveSessions } = vi.hoisted(() => ({
  apiMock: {
    status: vi.fn(),
    model: vi.fn(),
    organization: vi.fn(),
    sites: vi.fn(),
    cameras: vi.fn(),
    camera: vi.fn(),
    sources: vi.fn(),
    zones: vi.fn(),
    events: vi.fn(),
    event: vi.fn(),
    summary: vi.fn(),
    apiKeys: vi.fn(),
    createZone: vi.fn(),
    updateZone: vi.fn(),
    deleteZone: vi.fn(),
    createCamera: vi.fn(),
    createSource: vi.fn(),
    createSite: vi.fn(),
    startSession: vi.fn(),
    stopSession: vi.fn(),
    createApiKey: vi.fn(),
    revokeApiKey: vi.fn(),
  },
  getApiKeyMock: vi.fn(() => null as string | null),
  onApiKeyChangeMock: vi.fn((_cb: (key: string | null) => void) => () => undefined),
  liveSessions: [] as unknown[],
}))

vi.mock('@/lib/api', () => ({
  api: apiMock,
  getApiKey: getApiKeyMock,
  onApiKeyChange: onApiKeyChangeMock,
}))

vi.mock('@/lib/live', () => {
  type Listener = (snapshot: unknown) => void
  class LiveSession {
    readonly sessionId: string
    connected = false
    closed = false
    readonly listeners = new Set<Listener>()
    constructor(sessionId: string) {
      this.sessionId = sessionId
      liveSessions.push(this)
    }
    subscribe(listener: Listener): () => void {
      this.listeners.add(listener)
      return () => this.listeners.delete(listener)
    }
    connect(): void {
      this.connected = true
    }
    close(): void {
      this.closed = true
    }
    emit(snapshot: unknown): void {
      for (const listener of this.listeners) listener(snapshot)
    }
  }
  return { LiveSession }
})

function newClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
}

function wrapperFor(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  liveSessions.length = 0
  apiMock.status.mockResolvedValue({ status: 'ok' })
  apiMock.model.mockResolvedValue({ state: 'ready' })
  apiMock.organization.mockResolvedValue({ id: 'org-1', name: 'VisionOps' })
  apiMock.sites.mockResolvedValue([])
  apiMock.cameras.mockResolvedValue([])
  apiMock.camera.mockResolvedValue({ id: 'cam-1', name: 'front' })
  apiMock.sources.mockResolvedValue([])
  apiMock.zones.mockResolvedValue([])
  apiMock.events.mockResolvedValue({ items: [], next_after: null })
  apiMock.event.mockResolvedValue({ id: 'evt-1' })
  apiMock.summary.mockResolvedValue({ total: 0 })
  apiMock.apiKeys.mockResolvedValue([])
  apiMock.createZone.mockResolvedValue({ id: 'zone-1' })
  apiMock.updateZone.mockResolvedValue({ id: 'zone-1' })
  apiMock.deleteZone.mockResolvedValue(undefined)
  apiMock.createCamera.mockResolvedValue({ id: 'cam-1' })
  apiMock.createSource.mockResolvedValue({ id: 'src-1' })
  apiMock.createSite.mockResolvedValue({ id: 'site-1' })
  apiMock.startSession.mockResolvedValue({ session_id: 'sess-1' })
  apiMock.stopSession.mockResolvedValue(undefined)
  apiMock.createApiKey.mockResolvedValue({ id: 'key-1', key: 'vo_x_y' })
  apiMock.revokeApiKey.mockResolvedValue(undefined)
  getApiKeyMock.mockReturnValue(null)
  onApiKeyChangeMock.mockReturnValue(() => undefined)
})

describe('useHasApiKey', () => {
  it('starts false with no key and flips when a key arrives', () => {
    let listener: ((key: string | null) => void) | null = null
    onApiKeyChangeMock.mockImplementation((cb: (key: string | null) => void) => {
      listener = cb
      return () => undefined
    })
    const { result } = renderHook(() => useHasApiKey())
    expect(result.current).toBe(false)
    act(() => listener?.('vo_k_s'))
    expect(result.current).toBe(true)
    act(() => listener?.(null))
    expect(result.current).toBe(false)
  })

  it('starts true when a key is already stored', () => {
    getApiKeyMock.mockReturnValue('vo_stored_key')
    const { result } = renderHook(() => useHasApiKey())
    expect(result.current).toBe(true)
  })
})

describe('query hooks', () => {
  it('exposes system status', async () => {
    const { result } = renderHook(() => useSystemStatus(), { wrapper: wrapperFor(newClient()) })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(apiMock.status).toHaveBeenCalledOnce()
  })

  it('respects the enabled flag', () => {
    const client = newClient()
    const { result } = renderHook(() => useSystemStatus(false), { wrapper: wrapperFor(client) })
    expect(result.current.fetchStatus).toBe('idle')
    expect(apiMock.status).not.toHaveBeenCalled()
  })

  it('loads model, organisation, sites, cameras, sources, zones and keys', async () => {
    const client = newClient()
    const wrapper = wrapperFor(client)
    const model = renderHook(() => useModelInfo(), { wrapper })
    const organization = renderHook(() => useOrganization(), { wrapper })
    const sites = renderHook(() => useSites(), { wrapper })
    const cameras = renderHook(() => useCameras(), { wrapper })
    const camera = renderHook(() => useCamera('cam-1'), { wrapper })
    const sources = renderHook(() => useSources('cam-1'), { wrapper })
    const zones = renderHook(() => useZones('cam-1'), { wrapper })
    const allZones = renderHook(() => useZones(undefined), { wrapper })
    const keys = renderHook(() => useApiKeys(), { wrapper })
    await waitFor(() => expect(model.result.current.isSuccess).toBe(true))
    await waitFor(() => expect(keys.result.current.isSuccess).toBe(true))
    expect(organization.result.current.isSuccess).toBe(true)
    expect(sites.result.current.isSuccess).toBe(true)
    expect(cameras.result.current.isSuccess).toBe(true)
    expect(camera.result.current.isSuccess).toBe(true)
    expect(sources.result.current.isSuccess).toBe(true)
    expect(zones.result.current.isSuccess).toBe(true)
    expect(allZones.result.current.isSuccess).toBe(true)
    expect(apiMock.zones).toHaveBeenCalledWith(undefined)
  })

  it('does not fetch a camera, source or event without an id', () => {
    const wrapper = wrapperFor(newClient())
    renderHook(() => useCamera(undefined), { wrapper })
    renderHook(() => useSources(undefined), { wrapper })
    renderHook(() => useEvent(undefined), { wrapper })
    expect(apiMock.camera).not.toHaveBeenCalled()
    expect(apiMock.sources).not.toHaveBeenCalled()
    expect(apiMock.event).not.toHaveBeenCalled()
  })

  it('loads events with filters, a single event and a summary', async () => {
    const wrapper = wrapperFor(newClient())
    const events = renderHook(() => useEvents({ severity: 'warning' }), { wrapper })
    const event = renderHook(() => useEvent('evt-1'), { wrapper })
    const summary = renderHook(() => useSummary(true, 'cam-1'), { wrapper })
    await waitFor(() => expect(events.result.current.isSuccess).toBe(true))
    await waitFor(() => expect(event.result.current.isSuccess).toBe(true))
    await waitFor(() => expect(summary.result.current.isSuccess).toBe(true))
    expect(apiMock.events).toHaveBeenCalledWith({ severity: 'warning' })
    expect(apiMock.summary).toHaveBeenCalledWith({ camera_id: 'cam-1' })
  })
})

describe('mutations invalidate the right query keys', () => {
  it('useCreateZone', async () => {
    const client = newClient()
    const spy = vi.spyOn(client, 'invalidateQueries')
    const { result } = renderHook(() => useCreateZone(), { wrapper: wrapperFor(client) })
    await act(async () => {
      await result.current.mutateAsync({ camera_id: 'c', name: 'z', points: [[0, 0]] })
    })
    expect(apiMock.createZone).toHaveBeenCalledOnce()
    expect(spy).toHaveBeenCalledWith({ queryKey: ['zones'] })
    expect(spy).toHaveBeenCalledWith({ queryKey: ['cameras'] })
  })

  it('useUpdateZone and useDeleteZone', async () => {
    const client = newClient()
    const spy = vi.spyOn(client, 'invalidateQueries')
    const update = renderHook(() => useUpdateZone(), { wrapper: wrapperFor(client) })
    const remove = renderHook(() => useDeleteZone(), { wrapper: wrapperFor(client) })
    await act(async () => {
      await update.result.current.mutateAsync({ id: 'z1', body: { name: 'renamed' } })
    })
    await act(async () => {
      await remove.result.current.mutateAsync('z1')
    })
    expect(apiMock.updateZone).toHaveBeenCalledWith('z1', { name: 'renamed' })
    expect(apiMock.deleteZone).toHaveBeenCalledWith('z1')
    expect(spy).toHaveBeenCalledWith({ queryKey: ['zones'] })
  })

  it('useCreateCamera and useCreateSite', async () => {
    const client = newClient()
    const spy = vi.spyOn(client, 'invalidateQueries')
    const camera = renderHook(() => useCreateCamera(), { wrapper: wrapperFor(client) })
    const site = renderHook(() => useCreateSite(), { wrapper: wrapperFor(client) })
    await act(async () => {
      await camera.result.current.mutateAsync({ name: 'c', site_id: 's' })
    })
    await act(async () => {
      await site.result.current.mutateAsync({ name: 's' })
    })
    expect(apiMock.createCamera).toHaveBeenCalledOnce()
    expect(apiMock.createSite).toHaveBeenCalledOnce()
    expect(spy).toHaveBeenCalledWith({ queryKey: ['cameras'] })
    expect(spy).toHaveBeenCalledWith({ queryKey: ['sites'] })
  })

  it('useCreateSource', async () => {
    const client = newClient()
    const spy = vi.spyOn(client, 'invalidateQueries')
    const { result } = renderHook(() => useCreateSource(), { wrapper: wrapperFor(client) })
    await act(async () => {
      await result.current.mutateAsync({ cameraId: 'cam-1', body: { name: 's', kind: 'file' } })
    })
    expect(apiMock.createSource).toHaveBeenCalledWith('cam-1', { name: 's', kind: 'file' })
    expect(spy).toHaveBeenCalledWith({ queryKey: ['sources', 'cam-1'] })
  })

  it('useStartSession and useStopSession', async () => {
    const client = newClient()
    const spy = vi.spyOn(client, 'invalidateQueries')
    const start = renderHook(() => useStartSession(), { wrapper: wrapperFor(client) })
    const stop = renderHook(() => useStopSession(), { wrapper: wrapperFor(client) })
    await act(async () => {
      await start.result.current.mutateAsync({ cameraId: 'cam-1' })
    })
    await act(async () => {
      await stop.result.current.mutateAsync('cam-1')
    })
    expect(apiMock.startSession).toHaveBeenCalledWith('cam-1', {})
    expect(apiMock.stopSession).toHaveBeenCalledWith('cam-1')
    expect(spy).toHaveBeenCalledWith({ queryKey: ['session', 'cam-1'] })
  })

  it('useCreateApiKey and useRevokeApiKey', async () => {
    const client = newClient()
    const spy = vi.spyOn(client, 'invalidateQueries')
    const create = renderHook(() => useCreateApiKey(), { wrapper: wrapperFor(client) })
    const revoke = renderHook(() => useRevokeApiKey(), { wrapper: wrapperFor(client) })
    await act(async () => {
      await create.result.current.mutateAsync({ name: 'ops' })
    })
    await act(async () => {
      await revoke.result.current.mutateAsync('key-1')
    })
    expect(apiMock.createApiKey).toHaveBeenCalledWith({ name: 'ops' })
    expect(apiMock.revokeApiKey).toHaveBeenCalledWith('key-1')
    expect(spy).toHaveBeenCalledWith({ queryKey: ['apikeys'] })
  })
})

describe('useLiveSession', () => {
  it('is idle without a session id', () => {
    const { result } = renderHook(() => useLiveSession(null))
    expect(result.current.connection).toBe('idle')
    expect(result.current.live).toBeNull()
    expect(liveSessions).toHaveLength(0)
  })

  it('connects, mirrors snapshots into the HUD and closes on unmount', () => {
    const { result, unmount } = renderHook(() => useLiveSession('sess-1', 0))
    const session = liveSessions[0] as {
      emit: (snapshot: unknown) => void
      connected: boolean
      closed: boolean
      listeners: Set<unknown>
    }
    expect(session.connected).toBe(true)
    expect(result.current.live).not.toBeNull()

    act(() =>
      session.emit({
        state: { seq: 7 },
        events: [{ id: 'e1' }],
        stale: false,
        connection: 'open',
        error: null,
        lastMessageAt: Date.now(),
      }),
    )
    expect(result.current.connection).toBe('open')
    expect(result.current.events).toHaveLength(1)
    expect(result.current.ageMs).not.toBeNull()

    unmount()
    expect(session.closed).toBe(true)
    expect(session.listeners.size).toBe(0)
  })
})
