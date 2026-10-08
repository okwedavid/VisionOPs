import { describe, expect, it } from 'vitest'

import {
  absoluteTime,
  boxToRect,
  bytes,
  clamp01,
  confidence,
  eventTypeLabel,
  hourLabel,
  isValidPolygon,
  milliseconds,
  number,
  percent,
  polygonArea,
  polygonBounds,
  polygonToPoints,
  polygonToRectangle,
  reasonSentence,
  rectangleFromDrag,
  relativeTime,
  seconds,
  severityLabel,
  snap,
} from '@/lib/format'
import type { NormalizedPolygon } from '@/types/api'

describe('severity and event labels', () => {
  it('humanises event type names', () => {
    expect(eventTypeLabel('ZONE_ENTERED')).toBe('Zone Entered')
  })

  it('labels severities and passes unknown values through', () => {
    expect(severityLabel('critical')).toBe('Critical')
    expect(severityLabel('weird')).toBe('weird')
  })
})

describe('numeric formatting', () => {
  it('formats percentages and confidence', () => {
    expect(percent(0.5)).toBe('50%')
    expect(confidence(0.912)).toBe('91.2%')
    expect(confidence(null)).toBe('—')
  })

  it('formats numbers, milliseconds, seconds and bytes', () => {
    expect(number(1234)).toBe((1234).toLocaleString())
    expect(number(null)).toBe('—')
    expect(milliseconds(12.4)).toBe('12 ms')
    expect(milliseconds(1500)).toBe('1.5 s')
    expect(milliseconds(null)).toBe('—')
    expect(seconds(45)).toBe('45 s')
    expect(seconds(90)).toBe('1m 30s')
    expect(seconds(3700)).toBe('1h 1m')
    expect(bytes(512)).toBe('512 B')
    expect(bytes(2 * 1024 * 1024)).toBe('2.0 MB')
    expect(bytes(null)).toBe('—')
  })
})

describe('time formatting', () => {
  it('formats absolute time and rejects invalid input', () => {
    expect(absoluteTime('2026-01-01T10:00:00Z')).not.toBe('—')
    expect(absoluteTime(null)).toBe('—')
    expect(absoluteTime('not a date')).toBe('—')
  })

  it('formats relative time in both directions', () => {
    const now = Date.parse('2026-01-01T12:00:00Z')
    expect(relativeTime('2026-01-01T11:59:58Z', now)).toBe('just now')
    expect(relativeTime('2026-01-01T11:59:30Z', now)).toBe('30s ago')
    expect(relativeTime('2026-01-01T11:30:00Z', now)).toBe('30m ago')
    expect(relativeTime('2026-01-01T09:00:00Z', now)).toBe('3h ago')
    expect(relativeTime('2025-12-30T12:00:00Z', now)).toBe('2d ago')
    expect(relativeTime('2026-01-01T12:05:00Z', now)).toBe('in 5m')
  })

  it('shortens API hour buckets', () => {
    expect(hourLabel('2026-01-01 14')).toBe('14:00')
    expect(hourLabel('nonsense')).toBe('nonsense')
  })
})

describe('geometry with normalised coordinates', () => {
  it('converts a box to a pixel rectangle', () => {
    const rect = boxToRect([0.1, 0.2, 0.5, 0.8], 1000, 500)
    expect(rect.x).toBe(100)
    expect(rect.y).toBe(100)
    expect(rect.width).toBeCloseTo(400, 6)
    expect(rect.height).toBeCloseTo(300, 6)
  })

  it('serialises a polygon for canvas output', () => {
    const points = polygonToPoints(
      [
        [0, 0],
        [0.5, 1],
      ],
      200,
      100,
    )
    expect(points).toBe('0.0,0.0 100.0,100.0')
  })

  it('clamps and snaps', () => {
    expect(clamp01(-1)).toBe(0)
    expect(clamp01(2)).toBe(1)
    expect(snap(0.3333)).toBeCloseTo(0.335, 5)
  })

  it('builds a rectangle from a drag, in any direction', () => {
    const forward = rectangleFromDrag(10, 20, 110, 220, 200, 400)
    const backward = rectangleFromDrag(110, 220, 10, 20, 200, 400)
    expect(forward).toEqual(backward)
    expect(forward).toHaveLength(4)
    for (const [x, y] of forward) {
      expect(x).toBeGreaterThanOrEqual(0)
      expect(x).toBeLessThanOrEqual(1)
      expect(y).toBeGreaterThanOrEqual(0)
      expect(y).toBeLessThanOrEqual(1)
    }
  })

  it('validates polygons', () => {
    const square: NormalizedPolygon = [
      [0.1, 0.1],
      [0.5, 0.1],
      [0.5, 0.5],
    ]
    expect(isValidPolygon(square)).toBe(true)
    expect(isValidPolygon([[0.1, 0.1]])).toBe(false)
    expect(
      isValidPolygon([
        [0.1, 0.1],
        [1.4, 0.1],
        [0.5, 0.5],
      ]),
    ).toBe(false)
  })

  it('computes bounds, area and the equivalent rectangle', () => {
    const square: NormalizedPolygon = [
      [0.2, 0.3],
      [0.6, 0.3],
      [0.6, 0.9],
      [0.2, 0.9],
    ]
    expect(polygonBounds(square)).toEqual([0.2, 0.3, 0.6, 0.9])
    expect(polygonArea(square)).toBeCloseTo(0.24, 6)
    expect(polygonToRectangle(square)).toEqual([
      [0.2, 0.3],
      [0.6, 0.3],
      [0.6, 0.9],
      [0.2, 0.9],
    ])
  })
})

describe('reason codes', () => {
  it('prefers the server-provided detail', () => {
    expect(reasonSentence('CAMERA_BUSY', 'Camera index 0 is present but busy')).toBe(
      'Camera index 0 is present but busy',
    )
  })

  it('translates known codes and degrades gracefully for unknown ones', () => {
    expect(reasonSentence('RTSP_TIMEOUT')).toBe('The camera did not respond in time.')
    expect(reasonSentence('SOMETHING_NEW')).toBe('something new')
    expect(reasonSentence(null)).toBeNull()
  })
})
