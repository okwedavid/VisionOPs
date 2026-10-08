/**
 * The live overlay canvas.
 *
 * Coordinate contract (docs/FRONTEND.md §2): the API sends NORMALISED geometry,
 * and this is the only place that multiplies by the *displayed* element size.
 * Because both the video and the canvas use the same displayed box, the overlay
 * cannot drift when the window resizes, the device pixel ratio changes, or the
 * source resolution differs from the CSS size.
 *
 * Performance: drawing happens in `requestAnimationFrame` from an imperative
 * handle, never from React state, so a 10 FPS state stream costs zero renders.
 */

import { useEffect, useRef } from 'react'

import { boxToRect, polygonToPoints } from '@/lib/format'
import { footPointInZones } from '@/lib/geometry'
import type { LiveSession } from '@/lib/live'
import type { LiveZone, NormalizedBox, NormalizedPolygon } from '@/types/api'

export interface OverlayZone {
  id: string
  name: string
  polygon: NormalizedPolygon
  color: string
  severity: string
  enabled: boolean
}

export interface LiveOverlayProps {
  live: LiveSession | null
  /** Zones from REST, used before the first live state arrives. */
  fallbackZones?: OverlayZone[]
  editing?: boolean
  draft?: NormalizedPolygon | null
  selectedTrackId?: number | null
  className?: string
}

const TRACK_COLOR = '#38bdf8'
const TRACK_COLOR_IN_ZONE = '#f59e0b'

function hexToRgba(hex: string, alpha: number): string {
  const clean = hex.replace('#', '')
  if (clean.length !== 6) return `rgba(56, 189, 248, ${alpha})`
  const r = parseInt(clean.slice(0, 2), 16)
  const g = parseInt(clean.slice(2, 4), 16)
  const b = parseInt(clean.slice(4, 6), 16)
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

function drawBox(
  ctx: CanvasRenderingContext2D,
  box: NormalizedBox,
  width: number,
  height: number,
  label: string,
  color: string,
  dpr: number,
): void {
  const rect = boxToRect(box, width, height)
  const inset = 0.5 / dpr
  ctx.strokeStyle = color
  ctx.lineWidth = 2 * (dpr > 1 ? 1.5 : 1)
  ctx.strokeRect(rect.x + inset, rect.y + inset, Math.max(0, rect.width - inset * 2), Math.max(0, rect.height - inset * 2))

  // Foot point: the point the zone decision is actually based on.
  const footX = ((box[0] + box[2]) / 2) * width
  const footY = box[3] * height
  ctx.beginPath()
  ctx.arc(footX, footY, 3.5 * dpr, 0, Math.PI * 2)
  ctx.fillStyle = color
  ctx.fill()

  const fontSize = 11 * dpr
  ctx.font = `${fontSize}px ui-sans-serif, system-ui, sans-serif`
  const text = label
  const metrics = ctx.measureText(text)
  const padX = 3 * dpr
  const boxHeight = fontSize + 4 * dpr
  const top = Math.max(0, rect.y - boxHeight)
  ctx.fillStyle = 'rgba(11, 18, 32, 0.82)'
  ctx.fillRect(rect.x, top, metrics.width + padX * 2, boxHeight)
  ctx.fillStyle = color
  ctx.fillText(text, rect.x + padX, top + fontSize - 2 * dpr)
}

export function LiveOverlay({
  live,
  fallbackZones = [],
  editing = false,
  draft = null,
  selectedTrackId = null,
  className,
}: LiveOverlayProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    const container = containerRef.current
    if (!canvas || !container) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    let frame = 0
    let disposed = false

    const resize = (): void => {
      const rect = container.getBoundingClientRect()
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      canvas.width = Math.max(1, Math.round(rect.width * dpr))
      canvas.height = Math.max(1, Math.round(rect.height * dpr))
      canvas.style.width = `${rect.width}px`
      canvas.style.height = `${rect.height}px`
    }

    const draw = (): void => {
      if (disposed) return
      resize()
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      const width = canvas.width / dpr
      const height = canvas.height / dpr
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx.clearRect(0, 0, width, height)

      const state = live?.latest.state
      const zoneList: LiveZone[] =
        state?.zones && state.zones.length > 0
          ? state.zones
          : (fallbackZones as unknown as LiveZone[]).map((zone) => ({
              ...zone,
              kind: 'polygon' as const,
              class_filter: [],
              cooldown_seconds: 0,
              min_dwell_seconds: 0,
              occupancy: 0,
            }))

      for (const zone of zoneList) {
        if (!zone.polygon || zone.polygon.length < 2) continue
        const color = zone.enabled ? (zone.color || '#f97316') : '#6b7c99'
        ctx.beginPath()
        const points = polygonToPoints(zone.polygon, width, height)
        points.split(' ').forEach((pair, index) => {
          const [x, y] = pair.split(',')
          if (index === 0) ctx.moveTo(Number(x), Number(y))
          else ctx.lineTo(Number(x), Number(y))
        })
        ctx.closePath()
        ctx.fillStyle = hexToRgba(color, zone.enabled ? 0.12 : 0.05)
        ctx.fill()

        ctx.setLineDash(zone.enabled ? [] : [6, 4])
        ctx.strokeStyle = color
        ctx.lineWidth = 2
        ctx.stroke()
        ctx.setLineDash([])

        if (zone.occupancy > 0) {
          ctx.beginPath()
          ctx.arc(width - 26, 26, 13, 0, Math.PI * 2)
          ctx.fillStyle = color
          ctx.fill()
          ctx.fillStyle = '#0b1220'
          ctx.font = `bold ${13}px ui-sans-serif, system-ui, sans-serif`
          ctx.textAlign = 'center'
          ctx.textBaseline = 'middle'
          ctx.fillText(String(zone.occupancy), width - 26, 27)
          ctx.textAlign = 'start'
          ctx.textBaseline = 'alphabetic'
        }

        const first = zone.polygon[0]
        if (first) {
          ctx.font = `${11}px ui-sans-serif, system-ui, sans-serif`
          const label = zone.name
          const metrics = ctx.measureText(label)
          const lx = first[0] * width
          const ly = Math.max(14, first[1] * height - 6)
          ctx.fillStyle = 'rgba(11, 18, 32, 0.85)'
          ctx.fillRect(lx, ly - 13, metrics.width + 8, 15)
          ctx.fillStyle = color
          ctx.fillText(label, lx + 4, ly - 2)
        }
      }

      if (draft && draft.length > 1) {
        ctx.beginPath()
        draft.forEach(([x, y], index) => {
          if (index === 0) ctx.moveTo(x * width, y * height)
          else ctx.lineTo(x * width, y * height)
        })
        if (draft.length >= 3) ctx.closePath()
        ctx.setLineDash([5, 4])
        ctx.strokeStyle = '#e6edf7'
        ctx.lineWidth = 2
        ctx.stroke()
        ctx.setLineDash([])
        ctx.fillStyle = 'rgba(56, 189, 248, 0.1)'
        if (draft.length >= 3) ctx.fill()
      }

      for (const track of state?.tracks ?? []) {
        if (track.clip) continue // foot point unreliable at frame edges
        const inZone = footPointInZones(track.box, state?.zones ?? [], track.label)
        const color = inZone ? TRACK_COLOR_IN_ZONE : TRACK_COLOR
        const selected = selectedTrackId !== null && selectedTrackId === track.id
        drawBox(
          ctx,
          track.box,
          width,
          height,
          `#${track.id} ${(track.confidence * 100).toFixed(0)}%`,
          selected ? '#22c55e' : color,
          dpr,
        )
      }

      frame = requestAnimationFrame(draw)
    }

    resize()
    frame = requestAnimationFrame(draw)
    const observer = new ResizeObserver(resize)
    observer.observe(container)
    return () => {
      disposed = true
      cancelAnimationFrame(frame)
      observer.disconnect()
    }
  }, [live, fallbackZones, draft, selectedTrackId])

  return (
    <div ref={containerRef} className={className ?? 'absolute inset-0'}>
      <canvas ref={canvasRef} className="pointer-events-none block size-full" aria-hidden={!editing} />
      {editing && <p className="sr-only">Zone editor overlay. Drag on the video to draw a zone.</p>}
    </div>
  )
}

