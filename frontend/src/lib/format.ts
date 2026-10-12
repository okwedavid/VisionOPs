/** Pure formatting and geometry helpers. Unit-tested: no DOM, no network. */

import type { NormalizedBox, NormalizedPolygon, Severity, ZoneSeverity } from '@/types/api'

const SEVERITY_LABEL: Record<string, string> = {
  info: 'Info',
  warning: 'Warning',
  critical: 'Critical',
  error: 'Error',
}

/** CSS classes per severity. Colour is never the only signal — the label carries meaning. */
export const SEVERITY_CLASS: Record<string, string> = {
  info: 'bg-surface-2 text-ink-2 border-border',
  warning: 'bg-warn/12 text-warn border-warn/40',
  critical: 'bg-crit/12 text-crit border-crit/40',
  error: 'bg-crit/20 text-crit border-crit/50',
}

export const HEALTH_CLASS: Record<string, string> = {
  ok: 'bg-ok/12 text-ok border-ok/40',
  healthy: 'bg-ok/12 text-ok border-ok/40',
  running: 'bg-ok/12 text-ok border-ok/40',
  ready: 'bg-ok/12 text-ok border-ok/40',
  degraded: 'bg-warn/12 text-warn border-warn/40',
  starting: 'bg-info/12 text-info border-info/40',
  loading: 'bg-info/12 text-info border-info/40',
  checking: 'bg-info/12 text-info border-info/40',
  unconfigured: 'bg-surface-2 text-ink-3 border-border',
  disabled: 'bg-surface-2 text-ink-3 border-border',
  failed: 'bg-crit/12 text-crit border-crit/40',
  stopped: 'bg-surface-2 text-ink-3 border-border',
  offline: 'bg-surface-2 text-ink-3 border-border',
}

const HEALTH_LABEL: Record<string, string> = {
  healthy: 'Healthy',
  degraded: 'Degraded',
  offline: 'Offline',
  checking: 'Checking…',
}

/** The operator-facing word for a four-state health verdict. */
export function healthLabel(state: string): string {
  return HEALTH_LABEL[state] ?? state
}

export function severityLabel(severity: Severity | ZoneSeverity | string): string {
  return SEVERITY_LABEL[severity] ?? String(severity)
}

export function severityClass(severity: string): string {
  return SEVERITY_CLASS[severity] ?? SEVERITY_CLASS.info ?? ''
}

export function healthClass(state: string): string {
  return HEALTH_CLASS[state] ?? HEALTH_CLASS.ok ?? ''
}

export function eventTypeLabel(type: string): string {
  return type
    .toLowerCase()
    .split('_')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

export function percent(value: number, digits = 0): string {
  return `${(value * 100).toFixed(digits)}%`
}

export function confidence(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return percent(value, 1)
}

export function number(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

export function milliseconds(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  if (value >= 1000) return `${(value / 1000).toFixed(1)} s`
  return `${Math.round(value)} ms`
}

export function seconds(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  if (value < 60) return `${Math.round(value)} s`
  const minutes = Math.floor(value / 60)
  if (minutes < 60) return `${minutes}m ${Math.round(value % 60)}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

export function bytes(value: number | null | undefined): string {
  if (!value) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = value
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
}

function toDate(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined) return null
  const date = value instanceof Date ? value : new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

export function absoluteTime(value: string | number | Date | null | undefined): string {
  const date = toDate(value)
  if (!date) return '—'
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function clockTime(value: string | number | Date | null | undefined): string {
  const date = toDate(value)
  if (!date) return '—'
  return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export function relativeTime(value: string | number | Date | null | undefined, now = Date.now()): string {
  const date = toDate(value)
  if (!date) return '—'
  const deltaMs = now - date.getTime()
  const future = deltaMs < 0
  const delta = Math.abs(deltaMs)
  if (delta < 5000) return 'just now'
  const text =
    delta < 60_000
      ? `${Math.floor(delta / 1000)}s`
      : delta < 3_600_000
        ? `${Math.floor(delta / 60_000)}m`
        : delta < 86_400_000
          ? `${Math.floor(delta / 3_600_000)}h`
          : `${Math.floor(delta / 86_400_000)}d`
  return future ? `in ${text}` : `${text} ago`
}

/** Turn an API hour bucket (`2026-01-01 14`) into a short label. */
export function hourLabel(bucket: string): string {
  const match = /(\d{2})$/.exec(bucket)
  if (!match) return bucket
  return `${match[1]}:00`
}

// ---------------------------------------------------------------------------
// Geometry (normalised coordinates — see ADR-019)
// ---------------------------------------------------------------------------

export interface PixelRect {
  x: number
  y: number
  width: number
  height: number
}

/** Normalised box → pixel rectangle for the *displayed* element size. */
export function boxToRect(box: NormalizedBox, width: number, height: number): PixelRect {
  return {
    x: box[0] * width,
    y: box[1] * height,
    width: (box[2] - box[0]) * width,
    height: (box[3] - box[1]) * height,
  }
}

export function polygonToPoints(polygon: NormalizedPolygon, width: number, height: number): string {
  return polygon.map(([x, y]) => `${(x * width).toFixed(1)},${(y * height).toFixed(1)}`).join(' ')
}

export function clamp01(value: number): number {
  return value < 0 ? 0 : value > 1 ? 1 : value
}

/** Snap to a 0.5 % grid so coordinates are readable and stable. */
export function snap(value: number, step = 0.005): number {
  return Math.round(value / step) * step
}

export function rectangleFromDrag(
  startX: number,
  startY: number,
  endX: number,
  endY: number,
  elementWidth: number,
  elementHeight: number,
): NormalizedPolygon {
  const x1 = clamp01(snap(startX / elementWidth))
  const y1 = clamp01(snap(startY / elementHeight))
  const x2 = clamp01(snap(endX / elementWidth))
  const y2 = clamp01(snap(endY / elementHeight))
  return [
    [Math.min(x1, x2), Math.min(y1, y2)],
    [Math.max(x1, x2), Math.min(y1, y2)],
    [Math.max(x1, x2), Math.max(y1, y2)],
    [Math.min(x1, x2), Math.max(y1, y2)],
  ] as NormalizedPolygon
}

export function isValidPolygon(points: NormalizedPolygon): boolean {
  if (points.length < 3) return false
  return points.every(
    ([x, y]) =>
      Number.isFinite(x) &&
      Number.isFinite(y) &&
      x >= 0 &&
      x <= 1 &&
      y >= 0 &&
      y <= 1,
  )
}

/** Bounding box of a polygon, as normalised `[x1, y1, x2, y2]`. */
export function polygonBounds(points: NormalizedPolygon): NormalizedBox {
  const xs = points.map(([x]) => x)
  const ys = points.map(([, y]) => y)
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)]
}

export function polygonArea(points: NormalizedPolygon): number {
  let total = 0
  for (let index = 0; index < points.length; index += 1) {
    const [x1, y1] = points[index]!
    const [x2, y2] = points[(index + 1) % points.length]!
    total += x1 * y2 - x2 * y1
  }
  return Math.abs(total / 2)
}

export function polygonToRectangle(points: NormalizedPolygon): number[][] {
  const [x1, y1, x2, y2] = polygonBounds(points)
  return [
    [x1, y1],
    [x2, y1],
    [x2, y2],
    [x1, y2],
  ]
}

/** Human-readable reason code → operator-facing sentence. */
export function reasonSentence(code: string | null | undefined, detail?: string | null): string | null {
  if (detail) return detail
  if (!code) return null
  const map: Record<string, string> = {
    CAMERA_BUSY: 'The camera is in use by another application.',
    CAMERA_NOT_FOUND: 'No camera was found at that index.',
    RTSP_TIMEOUT: 'The camera did not respond in time.',
    RTSP_AUTH_FAILED: 'The camera rejected the credentials.',
    FILE_NOT_FOUND: 'The video file was not found.',
    FILE_DECODE_ERROR: 'The video file could not be decoded.',
    SOURCE_CLOSED: 'The video source closed.',
    SOURCE_STALLED: 'No new frames are arriving.',
    SOURCE_ENDED: 'The video reached its end.',
    WAITING_FOR_CLIENT: 'Waiting for the browser to share its camera.',
    MODEL_MISSING: 'No detection model is installed.',
    MODEL_CORRUPT: 'The detection model could not be loaded.',
    MODEL_UNAVAILABLE: 'The detection model is unavailable.',
    MODEL_LOADING: 'The detection model is still loading.',
    MODEL_DISABLED: 'Detection is disabled.',
    MODEL_ERROR: 'Inference failed recently; retrying.',
    DISK_LOW: 'There is little free disk space left.',
    STORAGE_UNAVAILABLE: 'The snapshot directory is not writable.',
    DB_UNREACHABLE: 'The database is unreachable.',
    HEALTH_TIMEOUT: 'A health check took too long to answer.',
    PROBE_ERROR: 'A health check failed unexpectedly.',
  }
  return map[code] ?? code.replace(/_/g, ' ').toLowerCase()
}
