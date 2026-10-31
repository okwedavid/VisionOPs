import type { LiveEvent, LiveTrack, LiveZone } from '@/types/api'
import { footPointInZones } from './geometry'

export type ObjectInterpretation = {
  zone: LiveZone | null
  status: string
  explanation: string
}

/** Resolve a model class index only from class names exposed by the active model. */
export function modelClassId(classes: string[] | undefined, label: string): number | null {
  if (!classes) return null
  const index = classes.indexOf(label)
  return index >= 0 ? index : null
}

export function interpretTrack(track: LiveTrack, zones: LiveZone[], events: LiveEvent[], cameraName: string): ObjectInterpretation {
  const eligible = zones.filter((zone) => zone.enabled && (!zone.class_filter.length || zone.class_filter.includes(track.label)))
  const inside = eligible.find((zone) => footPointInZones(track.box, [zone], track.label)) ?? null
  const latestEvent = events.find((event) => event.track_id === track.id && event.zone_id)
  if (inside) return { zone: inside, status: 'Inside monitored zone', explanation: `${track.label} detected inside ${inside.name} on ${cameraName}. This is a live track reference for the current camera session.` }
  if (latestEvent?.type.includes('EXIT')) {
    const name = latestEvent.zone_name ?? 'a monitored zone'
    return { zone: null, status: `Exited ${name}`, explanation: `Track #${track.id} exited ${name} on ${cameraName}, according to the recorded session event.` }
  }
  if (latestEvent) {
    const name = latestEvent.zone_name ?? 'a monitored zone'
    return {
      zone: null,
      status: 'Outside monitored zones',
      explanation: `${track.label} is currently outside enabled monitored zones on ${cameraName}. The session history still contains a ${latestEvent.type.replace(/_/g, ' ').toLowerCase()} event for ${name}.`,
    }
  }
  return {
    zone: null,
    status: eligible.length ? 'Outside monitored zones' : 'No applicable zone',
    explanation: eligible.length
      ? `${track.label} detected on ${cameraName}. No restricted-zone event was generated for this current position.`
      : `${track.label} detected on ${cameraName}. No enabled zone currently applies to this object class.`,
  }
}
