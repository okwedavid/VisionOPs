import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { deriveHealth, useHealthStatus, HEALTH_POLL_MS } from '@/hooks/useHealth'
import type { ReadyResponse } from '@/types/api'

const { readyMock } = vi.hoisted(() => ({ readyMock: vi.fn() }))

vi.mock('@/lib/api', () => ({
  api: { ready: readyMock },
}))

const READY: ReadyResponse = {
  status: 'ready',
  timestamp: '2026-01-01T00:00:00+00:00',
  checks: {
    api: { state: 'ok', reason_code: null, detail: 'API responding', required: true },
    database: { state: 'ok', reason_code: null, detail: '1.2 ms', required: true },
    model: { state: 'ok', reason_code: null, detail: 'yolo11n ready', required: false },
  },
}

function newClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
}

function wrapperFor(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
}

function networkError(): Error & { status: number } {
  return Object.assign(new Error('fetch failed'), { status: 0 })
}

beforeEach(() => {
  vi.clearAllMocks()
  readyMock.mockResolvedValue(READY)
})

describe('deriveHealth', () => {
  it('is checking before any answer arrives', () => {
    const snapshot = deriveHealth({ data: undefined, error: null, lastSuccessAt: null })
    expect(snapshot.state).toBe('checking')
    expect(snapshot.reachable).toBe(false)
    expect(snapshot.stale).toBe(false)
  })

  it('is offline when the backend cannot be reached', () => {
    const snapshot = deriveHealth({ data: undefined, error: networkError(), lastSuccessAt: null })
    expect(snapshot.state).toBe('offline')
    expect(snapshot.authError).toBe(false)
  })

  it('treats an authentication failure as checking, never offline', () => {
    const snapshot = deriveHealth({
      data: undefined,
      error: Object.assign(new Error('nope'), { status: 401 }),
      lastSuccessAt: null,
    })
    expect(snapshot.state).toBe('checking')
    expect(snapshot.authError).toBe(true)
  })

  it('maps the readiness verdicts to the four states', () => {
    expect(deriveHealth({ data: READY, error: null, lastSuccessAt: 1 }).state).toBe('healthy')
    expect(
      deriveHealth({ data: { ...READY, status: 'degraded' }, error: null, lastSuccessAt: 1 }).state,
    ).toBe('degraded')
    expect(
      deriveHealth({ data: { ...READY, status: 'unavailable' }, error: null, lastSuccessAt: 1 }).state,
    ).toBe('offline')
  })

  it('exposes the checks and timestamps of the last good answer', () => {
    const snapshot = deriveHealth({ data: READY, error: null, lastSuccessAt: 1234 })
    expect(snapshot.lastSuccessAt).toBe(1234)
    expect(snapshot.timestamp).toBe(READY.timestamp)
    expect(snapshot.checks.api?.required).toBe(true)
    expect(snapshot.reachable).toBe(true)
  })

  it('keeps the last known state but marks it stale after a failed refresh', () => {
    const snapshot = deriveHealth({ data: READY, error: networkError(), lastSuccessAt: 99 })
    expect(snapshot.state).toBe('offline')
    expect(snapshot.stale).toBe(true)
    expect(snapshot.checks.database).toBeDefined()
  })

  it('keeps a degraded verdict and flags staleness on a server error', () => {
    const snapshot = deriveHealth({
      data: { ...READY, status: 'degraded' },
      error: Object.assign(new Error('boom'), { status: 500 }),
      lastSuccessAt: 5,
    })
    expect(snapshot.state).toBe('degraded')
    expect(snapshot.stale).toBe(true)
    expect(snapshot.reachable).toBe(true)
  })
})

describe('useHealthStatus', () => {
  it('reports the backend health', async () => {
    const { result } = renderHook(() => useHealthStatus(), { wrapper: wrapperFor(newClient()) })
    await waitFor(() => expect(result.current.state).toBe('healthy'))
    expect(result.current.checks.database?.state).toBe('ok')
    expect(readyMock).toHaveBeenCalledOnce()
  })

  it('passes an abort signal so a superseded poll can be cancelled', async () => {
    renderHook(() => useHealthStatus(), { wrapper: wrapperFor(newClient()) })
    await waitFor(() => expect(readyMock).toHaveBeenCalledOnce())
    expect(readyMock.mock.calls[0]?.[0]).toBeInstanceOf(AbortSignal)
  })

  it('does not poll when disabled', () => {
    const { result } = renderHook(() => useHealthStatus(false), { wrapper: wrapperFor(newClient()) })
    expect(result.current.state).toBe('checking')
    expect(readyMock).not.toHaveBeenCalled()
  })

  it('refreshes on demand', async () => {
    const { result } = renderHook(() => useHealthStatus(), { wrapper: wrapperFor(newClient()) })
    await waitFor(() => expect(result.current.state).toBe('healthy'))
    result.current.refresh()
    await waitFor(() => expect(readyMock).toHaveBeenCalledTimes(2))
  })

  it('polls on the configured interval', () => {
    expect(HEALTH_POLL_MS).toBeGreaterThanOrEqual(10_000)
    expect(HEALTH_POLL_MS).toBeLessThanOrEqual(15_000)
  })
})
