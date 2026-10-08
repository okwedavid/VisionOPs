/**
 * Typed API client.
 *
 * One place that knows about HTTP: the base path, the API-key header, and the
 * conversion of an RFC 9457 problem document into a typed error the UI can
 * render field-by-field. No component calls `fetch` directly.
 */

import type {
  ApiKeyCreated,
  ApiKeyMeta,
  Camera,
  EventPage,
  EventSummary,
  ModelInfoResponse,
  Organization,
  SessionInfo,
  Site,
  SystemStatus,
  VideoSource,
  Zone,
} from '@/types/api'

export const API_BASE = '/api/v1'
const API_KEY_STORAGE = 'visionops.apiKey'

/** An error carrying the server's problem document. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId: string
  readonly fieldErrors: { field: string; message: string }[]

  constructor(init: {
    status: number
    code: string
    detail: string
    requestId: string
    fieldErrors?: { field: string; message: string }[]
  }) {
    super(init.detail)
    this.name = 'ApiError'
    this.status = init.status
    this.code = init.code
    this.requestId = init.requestId
    this.fieldErrors = init.fieldErrors ?? []
  }

  get isUnauthorized(): boolean {
    return this.status === 401
  }

  get isRateLimited(): boolean {
    return this.status === 429
  }

  get isNotFound(): boolean {
    return this.status === 404
  }
}

const keyListeners = new Set<(key: string | null) => void>()

export function getApiKey(): string | null {
  try {
    // sessionStorage, not localStorage: a long-lived secret should not survive
    // the browser session, and it is never baked into the bundle.
    return window.sessionStorage.getItem(API_KEY_STORAGE)
  } catch {
    return null
  }
}

export function setApiKey(value: string | null): void {
  try {
    if (value) window.sessionStorage.setItem(API_KEY_STORAGE, value)
    else window.sessionStorage.removeItem(API_KEY_STORAGE)
  } catch {
    /* storage disabled — the key still works for this page load */
  }
  for (const listener of keyListeners) listener(value)
}

export function onApiKeyChange(listener: (key: string | null) => void): () => void {
  keyListeners.add(listener)
  return () => keyListeners.delete(listener)
}

export function clearApiKey(): void {
  setApiKey(null)
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  body?: unknown
  query?: Record<string, string | number | boolean | null | undefined>
  signal?: AbortSignal
  /** Skip the API key (public endpoints). */
  anonymous?: boolean
}

function buildQuery(query: RequestOptions['query']): string {
  if (!query) return ''
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined || value === '') continue
    params.set(key, String(value))
  }
  const text = params.toString()
  return text ? `?${text}` : ''
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, query, signal, anonymous = false } = options
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (!anonymous) {
    const key = getApiKey()
    if (key) headers['X-API-Key'] = key
  }

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}${buildQuery(query)}`, {
      method,
      headers,
      signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch (error) {
    if ((error as Error)?.name === 'AbortError') throw error
    throw new ApiError({
      status: 0,
      code: 'network_error',
      detail:
        'Could not reach the VisionOps API. Check that the server is running and reachable on this network.',
      requestId: '-',
    })
  }

  if (response.status === 204) return undefined as T

  const contentType = response.headers.get('content-type') ?? ''
  if (!response.ok) {
    let problem: Record<string, unknown> = {}
    if (contentType.includes('json')) {
      problem = (await response.json().catch(() => ({}))) as Record<string, unknown>
    }
    const errors = Array.isArray(problem.errors) ? (problem.errors as { field: string; message: string }[]) : []
    // A rejected key must not leave the UI rendering a plausible-looking empty
    // dashboard — that reads as "no cameras, no events" and sends an operator
    // hunting for a data problem that does not exist. Drop the key so the gate
    // comes back with an explanation.
    if (response.status === 401 && !anonymous) {
      clearApiKey()
      throw new ApiError({
        status: response.status,
        code: (problem.code as string) ?? 'unauthorized',
        detail:
          'This API key was not accepted. It may be wrong, revoked, or from another server.',
        requestId: (problem.request_id as string) ?? response.headers.get('x-request-id') ?? '-',
        fieldErrors: errors,
      })
    }
    throw new ApiError({
      status: response.status,
      code: (problem.code as string) ?? 'error',
      detail:
        (problem.detail as string) ??
        (contentType.includes('json')
          ? `Request failed with status ${response.status}.`
          : `Request failed with status ${response.status}.`),
      requestId: (problem.request_id as string) ?? response.headers.get('x-request-id') ?? '-',
      fieldErrors: errors,
    })
  }

  if (contentType.includes('json')) return (await response.json()) as T
  return (await response.text()) as unknown as T
}

export interface ZoneInput {
  camera_id: string
  name: string
  description?: string | null
  kind?: 'polygon' | 'line'
  points: number[][]
  color?: string
  severity?: 'info' | 'warning' | 'critical'
  class_filter?: string[]
  cooldown_seconds?: number
  min_dwell_seconds?: number
  enabled?: boolean
}

export interface ZonePatch {
  name?: string
  description?: string | null
  points?: number[][]
  color?: string
  severity?: 'info' | 'warning' | 'critical'
  class_filter?: string[]
  cooldown_seconds?: number
  min_dwell_seconds?: number
  enabled?: boolean
}

export const api = {
  // -- system ------------------------------------------------------------
  health: () => request<{ status: string; version: string; uptime_seconds: number }>('/healthz', { anonymous: true }),
  ready: () => request<{ status: string; checks: Record<string, unknown> }>('/readyz', { anonymous: true }),
  status: () => request<SystemStatus>('/system/status'),
  model: () => request<ModelInfoResponse>('/models/current'),
  metricsText: async (): Promise<string> => {
    const response = await fetch(`${API_BASE}/system/metrics`, {
      headers: { 'X-API-Key': getApiKey() ?? '' },
    })
    if (!response.ok) throw new ApiError({ status: response.status, code: 'error', detail: 'metrics unavailable', requestId: '-' })
    return response.text()
  },

  // -- resources ---------------------------------------------------------
  organization: () => request<Organization>('/organizations/self'),
  sites: () => request<Site[]>('/sites'),
  createSite: (body: { name: string; timezone?: string; description?: string }) =>
    request<Site>('/sites', { method: 'POST', body }),
  updateSite: (id: string, body: Partial<Site>) =>
    request<Site>(`/sites/${id}`, { method: 'PATCH', body }),
  deleteSite: (id: string) => request<void>(`/sites/${id}`, { method: 'DELETE' }),

  cameras: (query?: { site_id?: string; status?: string; q?: string }) =>
    request<Camera[]>('/cameras', { query }),
  camera: (id: string) => request<Camera>(`/cameras/${id}`),
  createCamera: (body: { name: string; site_id: string; description?: string; target_fps?: number }) =>
    request<Camera>('/cameras', { method: 'POST', body }),
  updateCamera: (id: string, body: Record<string, unknown>) =>
    request<Camera>(`/cameras/${id}`, { method: 'PATCH', body }),
  deleteCamera: (id: string) => request<void>(`/cameras/${id}`, { method: 'DELETE' }),

  sources: (cameraId: string) => request<VideoSource[]>(`/cameras/${cameraId}/sources`),
  createSource: (
    cameraId: string,
    body: {
      name: string
      kind: 'browser' | 'device' | 'file' | 'rtsp'
      target?: string
      is_primary?: boolean
      options?: Record<string, unknown>
      credentials?: { username: string; password: string }
    },
  ) => request<VideoSource>(`/cameras/${cameraId}/sources`, { method: 'POST', body }),

  // -- zones -------------------------------------------------------------
  zones: (cameraId?: string) => request<Zone[]>('/zones', { query: { camera_id: cameraId } }),
  zone: (id: string) => request<Zone>(`/zones/${id}`),
  createZone: (body: ZoneInput) => request<Zone>('/zones', { method: 'POST', body }),
  updateZone: (id: string, body: ZonePatch) => request<Zone>(`/zones/${id}`, { method: 'PATCH', body }),
  deleteZone: (id: string) => request<void>(`/zones/${id}`, { method: 'DELETE' }),

  // -- sessions ----------------------------------------------------------
  startSession: (cameraId: string, body: { source_id?: string; target_fps?: number } = {}) =>
    request<SessionInfo>(`/cameras/${cameraId}/session`, { method: 'POST', body }),
  session: (cameraId: string) => request<SessionInfo>(`/cameras/${cameraId}/session`),
  stopSession: (cameraId: string) => request<void>(`/cameras/${cameraId}/session`, { method: 'DELETE' }),

  // -- events ------------------------------------------------------------
  events: (query: {
    limit?: number
    camera_id?: string
    zone_id?: string
    type?: string
    severity?: string
    since?: string
    until?: string
    q?: string
    after?: string
  }) => request<EventPage>('/events', { query }),
  event: (id: string) => request<EventPage['items'][number]>(`/events/${id}`),
  summary: (query?: { since?: string; until?: string; camera_id?: string; zone_id?: string }) =>
    request<EventSummary>('/events/summary', { query }),
  exportUrl: async (query: { since: string; until: string; format?: 'csv' | 'json' }): Promise<Blob> => {
    const response = await fetch(`${API_BASE}/events/export${buildQuery(query)}`, {
      headers: { 'X-API-Key': getApiKey() ?? '' },
    })
    if (!response.ok) throw new Error('export failed')
    return response.blob()
  },
  async downloadExport(query: { since: string; until: string; format?: 'csv' | 'json' }): Promise<void> {
    const blob = await this.exportUrl(query)
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `visionops-events.${query.format ?? 'csv'}`
    anchor.click()
    URL.revokeObjectURL(url)
  },

  // -- api keys ----------------------------------------------------------
  apiKeys: () => request<ApiKeyMeta[]>('/apikeys'),
  createApiKey: (body: { name: string; permissions?: string[] }) =>
    request<ApiKeyCreated>('/apikeys', { method: 'POST', body }),
  revokeApiKey: (id: string) => request<void>(`/apikeys/${id}`, { method: 'DELETE' }),
}

/** The ingest WebSocket URL for a browser-webcam session. */
export function ingestSocketUrl(sessionId: string, targetFps: number, key: string | null): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const params = new URLSearchParams({ session_id: sessionId, target_fps: String(targetFps) })
  if (key) params.set('key', key)
  return `${protocol}//${window.location.host}${API_BASE}/stream/ingest?${params.toString()}`
}
