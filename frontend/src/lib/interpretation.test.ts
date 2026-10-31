import { describe, expect, it } from 'vitest'

import { interpretTrack, modelClassId } from './interpretation'
import type { LiveTrack, LiveZone } from '@/types/api'

const track: LiveTrack = { id: 12, label: 'person', confidence: 0.94, box: [0.2, 0.2, 0.4, 0.8], age: 4, clip: false }
const zone: LiveZone = { id: 'z1', name: 'Restricted Storage', kind: 'polygon', polygon: [[0, 0], [0.6, 0], [0.6, 1], [0, 1]], color: '#f00', severity: 'warning', enabled: true, class_filter: ['person'], cooldown_seconds: 0, min_dwell_seconds: 0, occupancy: 1 }

describe('interpretTrack', () => {
  it('maps labels using active model metadata and safely handles unsupported classes', () => {
    expect(modelClassId(['person', 'bicycle', 'car'], 'car')).toBe(2)
    expect(modelClassId(['person'], 'forklift')).toBeNull()
    expect(modelClassId(undefined, 'person')).toBeNull()
  })

  it('describes only a live position inside a matching enabled zone', () => {
    const result = interpretTrack(track, [zone], [], 'Warehouse 01')
    expect(result.zone?.name).toBe('Restricted Storage')
    expect(result.explanation).toContain('detected inside Restricted Storage')
  })

  it('does not invent a zone or a historical event', () => {
    const result = interpretTrack({ ...track, box: [0.7, 0.2, 0.9, 0.8] }, [zone], [], 'Warehouse 01')
    expect(result.zone).toBeNull()
    expect(result.explanation).toContain('No restricted-zone event was generated')
  })

  it('keeps a real prior zone event in the interpretation when the track is now outside', () => {
    const result = interpretTrack(
      { ...track, box: [0.7, 0.2, 0.9, 0.8] },
      [zone],
      [{ id: 'evt1', type: 'ZONE_ENTERED', severity: 'warning', occurred_at: 10, zone_id: zone.id, zone_name: zone.name, track_id: track.id, metadata: {} }],
      'Warehouse 01',
    )
    expect(result.explanation).toContain('still contains a zone entered event')
    expect(result.explanation).toContain(zone.name)
  })
})
