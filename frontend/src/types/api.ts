/**
 * API DTO types.
 *
 * These mirror the Pydantic schemas in `backend/visionops/schemas/dto.py`. The
 * backend is the source of truth and generates its own OpenAPI document; this
 * file is the hand-maintained client mirror, kept honest by `docs/API.md` and by
 * the contract test in `docs/TESTING.md`.
 *
 * Rule that matters most: all geometry (`points`, `box`) is NORMALISED to
 * `[0, 1]` with the origin at the top-left. Never multiply by pixels here.
 */

export type Severity = 'info' | 'warning' | 'critical' | 'error'
export type ZoneSeverity = 'info' | 'warning' | 'critical'
export type SourceKind = 'browser' | 'device' | 'file' | 'rtsp'
export type ZoneKind = 'polygon' | 'line'
export type SessionState = 'starting' | 'running' | 'degraded' | 'failed' | 'stopped'

/**
 * The four states the health badge may show.
 *
 * `checking` is the honest initial state before the first answer arrives;
 * `offline` means we cannot reach the API at all (or a required dependency is
 * down). Authentication failures are never treated as `offline`.
 */
export type HealthState = 'checking' | 'healthy' | 'degraded' | 'offline'

/** One subsystem in a readiness response (`GET /readyz`). */
export interface ReadyCheck {
  state: string
  reason_code: string | null
  detail: string | null
  required: boolean
}

export interface ReadyResponse {
  status: 'ready' | 'degraded' | 'unavailable'
  timestamp: string
  checks: Record<string, ReadyCheck>
}

/** `[x1, y1, x2, y2]` in normalised frame coordinates. */
export type NormalizedBox = [number, number, number, number]
/** `[[x, y], …]` in normalised frame coordinates. */
export type NormalizedPoint = [number, number]
export type NormalizedPolygon = NormalizedPoint[]

export interface Organization {
  id: string
  name: string
  slug: string
  timezone: string
  retention_days: number
  snapshots_enabled: boolean
  min_dwell_seconds: number
  event_cooldown_seconds: number
  settings: Record<string, unknown>
  created_at: string
}

export interface Site {
  id: string
  name: string
  timezone: string | null
  address: string | null
  description: string | null
  created_at: string
  updated_at: string
}

export interface Camera {
  id: string
  name: string
  description: string | null
  site_id: string
  site_name: string | null
  status: 'online' | 'offline' | 'degraded' | 'disabled'
  target_fps: number
  settings: Record<string, unknown>
  last_seen_at: string | null
  zone_count: number
  source_count: number
  created_at: string
}

export interface VideoSource {
  id: string
  camera_id: string
  name: string
  kind: SourceKind
  target: string
  is_primary: boolean
  options: Record<string, unknown>
  /** Write-only credentials: the API never returns the secret. */
  credentials: { configured: boolean }
  created_at: string
}

export interface Zone {
  id: string
  camera_id: string
  name: string
  description: string | null
  kind: ZoneKind
  points: NormalizedPolygon
  color: string
  severity: ZoneSeverity
  class_filter: string[]
  cooldown_seconds: number
  min_dwell_seconds: number
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface EventItem {
  id: string
  schema_version: number
  type: string
  severity: Severity
  occurred_at: string
  received_at: string
  organization_id: string
  camera_id: string | null
  camera_name: string
  zone_id: string | null
  zone_name: string | null
  track_id: number | null
  session_id: string | null
  confidence: number | null
  metadata: Record<string, unknown>
  snapshot_path: string | null
}

export interface EventPage {
  items: EventItem[]
  next_cursor: string | null
  has_more: boolean
}

export interface EventSummary {
  window: { from: string; to: string }
  totals: { events: number; by_severity: Partial<Record<Severity, number>> }
  by_type: { type: string; count: number }[]
  by_camera: {
    camera_id: string | null
    camera_name: string
    count: number
    entries: number
    exits: number
  }[]
  by_zone: { zone_id: string | null; zone_name: string | null; count: number }[]
  hourly: { bucket: string; count: number }[]
  avg_dwell_seconds: number | null
}

export interface LiveTrack {
  id: number
  label: string
  confidence: number
  box: NormalizedBox
  age: number
  clip: boolean
}

export interface LiveZone {
  id: string
  name: string
  kind: ZoneKind
  polygon: NormalizedPolygon
  color: string
  severity: ZoneSeverity
  enabled: boolean
  class_filter: string[]
  cooldown_seconds: number
  min_dwell_seconds: number
  occupancy: number
}

export interface LiveEvent {
  id: string | null
  type: string
  severity: Severity
  occurred_at: number
  zone_id: string | null
  zone_name: string | null
  track_id: number | null
  metadata: Record<string, unknown>
}

export interface SessionStatePayload {
  type: 'session.state'
  session_id: string
  camera_id: string
  camera_name: string
  seq: number | null
  server_time: number
  source: {
    state: SessionState
    reason_code: string | null
    detail: string | null
    kind: SourceKind
    fps: number
    frames: number
    dropped: number
    retries: number
    uptime_seconds: number
    position: number | null
    duration: number | null
  }
  ai: {
    state: 'ready' | 'degraded' | 'failed' | string
    model: string | null
    sha256: string | null
    provider: string | null
    reason_code: string | null
    detail: string | null
    latency_ms: number | null
  }
  stats: {
    active_tracks: number
    in_zone: Record<string, number>
    processed_fps: number
    frame_age_ms: number | null
    frames_processed: number
    frames_dropped: number
    inference_errors: number
    events_emitted: number
    events_suppressed: number
  }
  tracks: LiveTrack[]
  zones: LiveZone[]
  events: LiveEvent[]
}

export interface LiveEventMessage {
  type: 'session.event'
  session_id: string
  server_time: number
  events: (LiveEvent & { camera_id: string; camera_name: string; confidence: number | null; snapshot_path: string | null })[]
}

export interface SessionInfo {
  session_id: string
  camera_id: string
  camera_name: string
  state: SessionState
  started_at: number
  uptime_seconds: number
  target_fps: number
  source_kind: SourceKind
  source_id: string
  reason_code: string | null
  detail: string | null
  processed_fps: number
  frame_age_ms: number | null
  active_tracks: number
  in_zone: Record<string, number>
  events_emitted: number
  events_suppressed: number
  frames_processed: number
  frames_dropped: number
  inference_ms: number | null
  inference_errors: number
  ai: Record<string, unknown>
  source: Record<string, unknown>
  ingest_ws_url: string | null
}

export interface SystemStatus {
  status: 'healthy' | 'degraded' | 'failed'
  version: string
  environment: string
  timestamp?: string
  uptime_seconds: number
  model: {
    state: string
    name?: string
    sha256?: string
    provider?: string
    input_width?: number
    input_height?: number
    classes_count?: number
    warmup_ms?: number | null
    reason_code: string | null
    detail: string | null
  }
  sessions: { active: number; max: number; items: SessionInfo[] }
  checks: Record<string, { state: string; reason_code: string | null; detail: string | null; required?: boolean }>
  storage: {
    database: string
    snapshot_dir: string
    disk_free_bytes: number | null
    credential_encryption: string
  }
  counters: Record<string, number>
  runtime: Record<string, number | null>
}

export interface ModelInfoResponse {
  state: string
  name?: string
  sha256?: string
  provider?: string
  classes?: string[]
  error?: string | null
  config?: Record<string, unknown>
}

export interface ApiKeyMeta {
  id: string
  name: string
  key_prefix: string
  permissions: string[]
  rate_limit_rps: number | null
  last_used_at: string | null
  expires_at: string | null
  revoked: boolean
  created_at: string
}

export interface ApiKeyCreated extends ApiKeyMeta {
  key: string
  warning: string
}

/** RFC 9457 problem document returned by every error response. */
export interface Problem {
  type?: string
  title?: string
  status?: number
  detail?: string
  instance?: string
  code?: string
  request_id?: string
  errors?: { field: string; message: string; type?: string }[]
}
