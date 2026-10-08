/**
 * Geometry helpers shared by the overlay and the editor.
 *
 * `pointInPolygon` mirrors `visionops.vision.geometry.point_in_polygon`
 * exactly: the browser and the server must reach the same zone decision.
 * `tests/unit/test_geometry.py` asserts the same cases in Python.
 */

import type { NormalizedPolygon } from '@/types/api'

/** Even-odd (ray-casting) point-in-polygon test in normalised coordinates. */
export function pointInPolygon(point: [number, number], polygon: NormalizedPolygon): boolean {
  if (polygon.length < 3) return false
  const [x, y] = point
  let inside = false
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i, i += 1) {
    const [xi, yi] = polygon[i]!
    const [xj, yj] = polygon[j]!
    if (yi > y !== yj > y) {
      const xCross = xi + ((y - yi) * (xj - xi)) / (yj - yi)
      if (x < xCross) inside = !inside
    }
  }
  return inside
}

/** Whether a track's foot point is inside a zone, mirroring the server's rule. */
export function footPointInZones(
  box: [number, number, number, number],
  zones: { polygon: NormalizedPolygon; enabled: boolean; class_filter: string[] }[],
  label: string,
): boolean {
  const foot: [number, number] = [(box[0] + box[2]) / 2, box[3]]
  return zones.some(
    (zone) =>
      zone.enabled &&
      (zone.class_filter.length === 0 || zone.class_filter.includes(label)) &&
      pointInPolygon(foot, zone.polygon),
  )
}
