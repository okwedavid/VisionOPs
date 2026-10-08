import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  API_BASE,
  ApiError,
  api,
  clearApiKey,
  getApiKey,
  ingestSocketUrl,
  onApiKeyChange,
  setApiKey,
} from '@/lib/api'

/** A minimal `Response` stand-in: jsdom does not ship the fetch primitives. */
class FakeResponse {
  constructor(
    private readonly body: unknown,
    readonly status = 200,
    _type = 'application/json',
    readonly headers = new Headers(),
  ) {}

  get ok(): boolean {
    return this.status >= 200 && this.status < 300
  }

  async json(): Promise<unknown> {
    if (typeof this.body === 'string') return JSON.parse(this.body)
    return this.body
  }

  async text(): Promise<string> {
    return typeof this.body === 'string' ? this.body : JSON.stringify(this.body)
  }

  async blob(): Promise<Blob> {
    return new Blob(['csv'])
  }
}

function jsonResponse(body: unknown, status = 200): FakeResponse {
  const headers = new Headers({ 'content-type': 'application/json' })
  return new FakeResponse(body, status, 'application/json', headers)
}

function textResponse(body: string, type = 'text/plain', status = 200): FakeResponse {
  return new FakeResponse(body, status, type, new Headers({ 'content-type': type }))
}

const fetchMock = vi.fn()
const originalFetch = globalThis.fetch

beforeEach(() => {
  fetchMock.mockReset()
  globalThis.fetch = fetchMock as unknown as typeof fetch
  window.sessionStorage.clear()
})

afterEach(() => {
  globalThis.fetch = originalFetch
  vi.restoreAllMocks()
})

describe('api key storage', () => {
  it('round-trips the key through sessionStorage, never localStorage', () => {
    expect(getApiKey()).toBeNull()
    setApiKey('vo_abc_secret')
    expect(getApiKey()).toBe('vo_abc_secret')
    expect(window.localStorage.getItem('visionops.apiKey')).toBeNull()
    clearApiKey()
    expect(getApiKey()).toBeNull()
  })

  it('notifies listeners and stops after unsubscribe', () => {
    const seen: (string | null)[] = []
    const unsubscribe = onApiKeyChange((key) => seen.push(key))
    setApiKey('vo_one_x')
    clearApiKey()
    unsubscribe()
    setApiKey('vo_two_y')
    expect(seen).toEqual(['vo_one_x', null])
  })

  it('survives storage being unavailable', () => {
    vi.spyOn(window.sessionStorage, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(window.sessionStorage, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(getApiKey()).toBeNull()
    expect(() => setApiKey('vo_x_y')).not.toThrow()
    expect(() => clearApiKey()).not.toThrow()
  })
})

describe('ApiError', () => {
  it('classifies the status codes the UI branches on', () => {
    const unauthorized = new ApiError({ status: 401, code: 'unauthorized', detail: 'no', requestId: '-' })
    const limited = new ApiError({ status: 429, code: 'rate_limited', detail: 'slow', requestId: '-' })
    const missing = new ApiError({ status: 404, code: 'not_found', detail: 'gone', requestId: '-' })
    expect(unauthorized.isUnauthorized).toBe(true)
    expect(limited.isRateLimited).toBe(true)
    expect(missing.isNotFound).toBe(true)
    expect(unauthorized.fieldErrors).toEqual([])
    expect(unauthorized).toBeInstanceOf(Error)
  })
})

describe('request', () => {
  it('sends the API key, builds queries and parses JSON', async () => {
    setApiKey('vo_key_secret')
    fetchMock.mockResolvedValueOnce(jsonResponse({ status: 'ok' }))
    await expect(api.events({ limit: 5, camera_id: undefined, q: '' })).resolves.toEqual({
      status: 'ok',
    })
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe(`${API_BASE}/events?limit=5`)
    expect((init.headers as Record<string, string>)['X-API-Key']).toBe('vo_key_secret')
  })

  it('omits the key for anonymous endpoints', async () => {
    setApiKey('vo_key_secret')
    fetchMock.mockResolvedValueOnce(jsonResponse({ status: 'ok' }))
    await api.health()
    const [, init] = fetchMock.mock.calls[0]!
    expect((init.headers as Record<string, string>)['X-API-Key']).toBeUndefined()
  })

  it('returns undefined for 204 responses', async () => {
    fetchMock.mockResolvedValueOnce(new FakeResponse(null, 204))
    await expect(api.deleteSite('site-1')).resolves.toBeUndefined()
  })

  it('returns plain text when the response is not JSON', async () => {
    fetchMock.mockResolvedValueOnce(textResponse('hello'))
    await expect(api.health()).resolves.toBe('hello')
  })

  it('maps an RFC 9457 problem document to a typed error', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        {
          code: 'validation_error',
          detail: 'Bad zone',
          request_id: 'req-1',
          errors: [{ field: 'points', message: 'too few' }],
        },
        422,
      ),
    )
    const error = await api.createZone({} as never).catch((err: unknown) => err)
    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.status).toBe(422)
    expect(apiError.code).toBe('validation_error')
    expect(apiError.requestId).toBe('req-1')
    expect(apiError.fieldErrors).toEqual([{ field: 'points', message: 'too few' }])
  })

  it('falls back to the request-id header and a generic detail', async () => {
    const headers = new Headers({ 'content-type': 'text/plain', 'x-request-id': 'req-2' })
    fetchMock.mockResolvedValueOnce(new FakeResponse('boom', 500, 'text/plain', headers))
    const error = (await api.health().catch((err: unknown) => err)) as ApiError
    expect(error.code).toBe('error')
    expect(error.requestId).toBe('req-2')
    expect(error.message).toContain('status 500')
  })

  it('drops a rejected key and explains why', async () => {
    setApiKey('vo_key_secret')
    fetchMock.mockResolvedValueOnce(jsonResponse({ code: 'unauthorized' }, 401))
    const error = (await api.status().catch((err: unknown) => err)) as ApiError
    expect(error.isUnauthorized).toBe(true)
    expect(getApiKey()).toBeNull()
    expect(error.message).toContain('was not accepted')
  })

  it('turns a transport failure into a network ApiError', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const error = (await api.status().catch((err: unknown) => err)) as ApiError
    expect(error.status).toBe(0)
    expect(error.code).toBe('network_error')
  })

  it('rethrows an AbortError untouched', async () => {
    const abort = new DOMException('aborted', 'AbortError')
    fetchMock.mockRejectedValueOnce(abort)
    await expect(api.status()).rejects.toBe(abort)
  })
})

describe('specialist endpoints', () => {
  it('metricsText returns the raw exposition', async () => {
    setApiKey('vo_key_secret')
    fetchMock.mockResolvedValueOnce(textResponse('# HELP visionops'))
    await expect(api.metricsText()).resolves.toBe('# HELP visionops')
  })

  it('metricsText throws when metrics are unavailable', async () => {
    fetchMock.mockResolvedValueOnce(textResponse('nope', 'text/plain', 503))
    await expect(api.metricsText()).rejects.toBeInstanceOf(ApiError)
  })

  it('exportUrl returns a blob and errors on failure', async () => {
    fetchMock.mockResolvedValueOnce(new FakeResponse('', 200, 'text/csv'))
    await expect(api.exportUrl({ since: 'a', until: 'b' })).resolves.toBeInstanceOf(Blob)
    fetchMock.mockResolvedValueOnce(new FakeResponse('', 500))
    await expect(api.exportUrl({ since: 'a', until: 'b' })).rejects.toThrow('export failed')
  })

  it('downloadExport clicks a temporary anchor', async () => {
    const click = vi.fn()
    const createUrl = vi.fn(() => 'blob:fake')
    const revokeUrl = vi.fn()
    vi.stubGlobal('URL', { createObjectURL: createUrl, revokeObjectURL: revokeUrl })
    vi.spyOn(document, 'createElement').mockReturnValue({ click } as unknown as HTMLElement)
    fetchMock.mockResolvedValueOnce(new FakeResponse('', 200, 'text/csv'))
    await api.downloadExport({ since: 'a', until: 'b' })
    expect(createUrl).toHaveBeenCalledOnce()
    expect(click).toHaveBeenCalledOnce()
    expect(revokeUrl).toHaveBeenCalledWith('blob:fake')
  })
})

describe('every wrapper targets the documented route', () => {
  it('calls the expected paths and methods', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}))
    const cases: [string, () => Promise<unknown>, string][] = [
      ['status', () => api.status(), '/system/status'],
      ['model', () => api.model(), '/models/current'],
      ['organization', () => api.organization(), '/organizations/self'],
      ['sites', () => api.sites(), '/sites'],
      ['createSite', () => api.createSite({ name: 's' }), '/sites'],
      ['updateSite', () => api.updateSite('id', { name: 's' }), '/sites/id'],
      ['cameras', () => api.cameras({ site_id: 'x' }), '/cameras?site_id=x'],
      ['camera', () => api.camera('id'), '/cameras/id'],
      ['createCamera', () => api.createCamera({ name: 'c', site_id: 's' }), '/cameras'],
      ['updateCamera', () => api.updateCamera('id', { name: 'c' }), '/cameras/id'],
      ['deleteCamera', () => api.deleteCamera('id'), '/cameras/id'],
      ['sources', () => api.sources('id'), '/cameras/id/sources'],
      [
        'createSource',
        () => api.createSource('id', { name: 's', kind: 'file' }),
        '/cameras/id/sources',
      ],
      ['zones', () => api.zones('id'), '/zones?camera_id=id'],
      ['zone', () => api.zone('id'), '/zones/id'],
      ['createZone', () => api.createZone({ camera_id: 'c', name: 'z', points: [[0, 0]] }), '/zones'],
      ['updateZone', () => api.updateZone('id', { name: 'z' }), '/zones/id'],
      ['deleteZone', () => api.deleteZone('id'), '/zones/id'],
      ['startSession', () => api.startSession('id'), '/cameras/id/session'],
      ['session', () => api.session('id'), '/cameras/id/session'],
      ['stopSession', () => api.stopSession('id'), '/cameras/id/session'],
      ['event', () => api.event('id'), '/events/id'],
      ['summary', () => api.summary(), '/events/summary'],
      ['apiKeys', () => api.apiKeys(), '/apikeys'],
      ['createApiKey', () => api.createApiKey({ name: 'k' }), '/apikeys'],
      ['revokeApiKey', () => api.revokeApiKey('id'), '/apikeys/id'],
    ]
    for (const [name, call, path] of cases) {
      fetchMock.mockClear()
      await call()
      expect(fetchMock.mock.calls[0]?.[0], name).toBe(`${API_BASE}${path}`)
    }
  })
})

describe('ingestSocketUrl', () => {
  it('builds a same-origin ws URL and carries the key when present', () => {
    const url = ingestSocketUrl('sess-1', 15, 'vo_key_secret')
    expect(url.startsWith('ws://')).toBe(true)
    expect(url).toContain(`${API_BASE}/stream/ingest?`)
    expect(url).toContain('session_id=sess-1')
    expect(url).toContain('target_fps=15')
    expect(url).toContain('key=vo_key_secret')
  })

  it('omits the key when there is none', () => {
    const url = ingestSocketUrl('sess-2', 5, null)
    expect(url).not.toContain('key=')
  })
})
