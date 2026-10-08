import { describe, expect, it } from 'vitest'

import { footPointInZones, pointInPolygon } from '@/lib/geometry'
import type { NormalizedPolygon } from '@/types/api'

/**
 * The browser must reach the *same* zone decision as the server. This mirrors
 * `visionops.vision.geometry.point_in_polygon`; `tests/unit/test_geometry.py`
 * asserts the identical cases in Python, so the two can never drift silently.
 */
describe('pointInPolygon mirrors the server rule', () => {
  const square: NormalizedPolygon = [
    [0.3, 0.3],
    [0.7, 0.3],
    [0.7, 0.9],
    [0.3, 0.9],
  ]

  it('accepts a point inside', () => {
    expect(pointInPolygon([0.5, 0.6], square)).toBe(true)
  })

  it('rejects a point outside', () => {
    expect(pointInPolygon([0.1, 0.1], square)).toBe(false)
    expect(pointInPolygon([0.9, 0.6], square)).toBe(false)
  })

  it('rejects a degenerate polygon', () => {
    expect(pointInPolygon([0.5, 0.5], [[0.1, 0.1]])).toBe(false)
  })

  it('respects the enabled flag and the class filter when scoring a track', () => {
    const zones = [
      { polygon: square, enabled: true, class_filter: [] as string[] },
    ]
    expect(footPointInZones([0.4, 0.4, 0.6, 0.7], zones, 'person')).toBe(true)
    expect(footPointInZones([0.4, 0.4, 0.6, 0.7], zones, 'forklift')).toBe(true)
    expect(footPointInZones([0.4, 0.4, 0.6, 0.7], [{ ...zones[0]!, enabled: false }], 'person')).toBe(false)
    expect(
      footPointInZones([0.4, 0.4, 0.6, 0.7], [{ ...zones[0]!, class_filter: ['person'] }], 'forklift'),
    ).toBe(false)
  })

  it('handles a concave polygon (L shape)', () => {
    const shape: NormalizedPolygon = [
      [0.0, 0.0],
      [1.0, 0.0],
      [1.0, 0.4],
      [0.4, 0.4],
      [0.4, 1.0],
      [0.0, 1.0],
    ]
    expect(pointInPolygon([0.8, 0.2], shape)).toBe(true) // top arm
    expect(pointInPolygon([0.2, 0.8], shape)).toBe(true) // left leg
    expect(pointInPolygon([0.8, 0.8], shape)).toBe(false) // the notch
  })
})
