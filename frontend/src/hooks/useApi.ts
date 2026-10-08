/**
 * Data hooks. All server state funnels through TanStack Query so caching,
 * polling and invalidation live in one place (docs/FRONTEND.md §4).
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { api, getApiKey, onApiKeyChange } from '@/lib/api'
import type { EventSummary, LiveEvent, SessionStatePayload } from '@/types/api'
import { LiveSession } from '@/lib/live'

/** Tracks whether a key has been entered, so the app can gate the UI on it. */
export function useHasApiKey(): boolean {
  const [hasKey, setHasKey] = useState<boolean>(() => Boolean(getApiKey()))
  useEffect(() => onApiKeyChange((key) => setHasKey(Boolean(key))), [])
  return hasKey
}

export function useSystemStatus(enabled = true) {
  return useQuery({
    queryKey: ['system', 'status'],
    queryFn: api.status,
    enabled,
    // Polled: this is the page an operator watches, and it is cheap.
    refetchInterval: 4000,
    staleTime: 2000,
    retry: 1,
  })
}

export function useModelInfo(enabled = true) {
  return useQuery({ queryKey: ['model'], queryFn: api.model, enabled, staleTime: 30_000 })
}

export function useOrganization(enabled = true) {
  return useQuery({ queryKey: ['organization'], queryFn: api.organization, enabled, staleTime: 60_000 })
}

export function useSites(enabled = true) {
  return useQuery({ queryKey: ['sites'], queryFn: api.sites, enabled, staleTime: 30_000 })
}

export function useCameras(enabled = true) {
  return useQuery({
    queryKey: ['cameras'],
    queryFn: () => api.cameras(),
    enabled,
    staleTime: 10_000,
  })
}

export function useCamera(cameraId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: ['camera', cameraId],
    queryFn: () => api.camera(cameraId!),
    enabled: enabled && Boolean(cameraId),
    staleTime: 5_000,
  })
}

export function useSources(cameraId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: ['sources', cameraId],
    queryFn: () => api.sources(cameraId!),
    enabled: enabled && Boolean(cameraId),
    staleTime: 30_000,
  })
}

export function useZones(cameraId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: ['zones', cameraId ?? 'all'],
    queryFn: () => api.zones(cameraId),
    enabled,
    staleTime: 5_000,
  })
}

export interface EventFilters {
  camera_id?: string
  zone_id?: string
  type?: string
  severity?: string
  q?: string
  since?: string
  until?: string
  limit?: number
  after?: string
}

export function useEvents(filters: EventFilters = {}, enabled = true) {
  return useQuery({
    queryKey: ['events', filters],
    queryFn: () => api.events(filters),
    enabled,
    staleTime: 2000,
    placeholderData: (previous) => previous,
  })
}

export function useEvent(eventId: string | undefined) {
  return useQuery({
    queryKey: ['event', eventId],
    queryFn: () => api.event(eventId!),
    enabled: Boolean(eventId),
    staleTime: 30_000,
  })
}

export function useSummary(enabled = true, cameraId?: string) {
  return useQuery({
    queryKey: ['summary', cameraId ?? 'all'],
    queryFn: () => api.summary({ camera_id: cameraId }),
    enabled,
    staleTime: 15_000,
    refetchInterval: 15_000,
  })
}

export function useApiKeys(enabled = true) {
  return useQuery({ queryKey: ['apikeys'], queryFn: api.apiKeys, enabled, staleTime: 30_000 })
}

// ---------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------

function useInvalidatingMutation<TArgs, TResult>(
  mutationFn: (args: TArgs) => Promise<TResult>,
  /** Query keys to invalidate, computed from the arguments that were sent. */
  keysFor: (args: TArgs) => string[][] | string[][],
) {
  const client = useQueryClient()
  return useMutation<TResult, Error, TArgs>({
    mutationFn,
    onSuccess: async (_result, args) => {
      // Precise invalidation: touch only what the change can affect.
      for (const key of keysFor(args)) await client.invalidateQueries({ queryKey: key })
    },
  })
}

export function useCreateZone() {
  return useInvalidatingMutation(
    (body: Parameters<typeof api.createZone>[0]) => api.createZone(body),
    () => [['zones'], ['summary'], ['cameras']],
  )
}

export function useUpdateZone() {
  return useInvalidatingMutation(
    (args: { id: string; body: Parameters<typeof api.updateZone>[1] }) => api.updateZone(args.id, args.body),
    () => [['zones'], ['summary']],
  )
}

export function useDeleteZone() {
  return useInvalidatingMutation(
    (id: string) => api.deleteZone(id),
    () => [['zones'], ['summary'], ['cameras']],
  )
}

export function useCreateCamera() {
  return useInvalidatingMutation(
    (body: Parameters<typeof api.createCamera>[0]) => api.createCamera(body),
    () => [['cameras'], ['system', 'status']],
  )
}

export function useCreateSource() {
  return useInvalidatingMutation(
    (args: { cameraId: string; body: Parameters<typeof api.createSource>[1] }) =>
      api.createSource(args.cameraId, args.body),
    (args) => [['sources', args.cameraId], ['cameras']],
  )
}

export function useCreateSite() {
  return useInvalidatingMutation((body: Parameters<typeof api.createSite>[0]) => api.createSite(body), () => [['sites']])
}

export function useStartSession() {
  return useInvalidatingMutation(
    (args: { cameraId: string; body?: { source_id?: string; target_fps?: number } }) =>
      api.startSession(args.cameraId, args.body ?? {}),
    (args) => [
      ['system', 'status'],
      ['session', args.cameraId],
    ],
  )
}

export function useStopSession() {
  return useInvalidatingMutation(
    (cameraId: string) => api.stopSession(cameraId),
    (cameraId) => [
      ['system', 'status'],
      ['session', cameraId],
    ],
  )
}

export function useCreateApiKey() {
  return useInvalidatingMutation(
    (body: Parameters<typeof api.createApiKey>[0]) => api.createApiKey(body),
    () => [['apikeys']],
  )
}

export function useRevokeApiKey() {
  return useInvalidatingMutation((id: string) => api.revokeApiKey(id), () => [['apikeys']])
}

// ---------------------------------------------------------------------------
// Live session
// ---------------------------------------------------------------------------

export interface UseLiveSessionResult {
  state: SessionStatePayload | null
  events: LiveEvent[]
  stale: boolean
  connection: string
  error: string | null
  /** Wall-clock time of the last message received on the socket. */
  lastMessageAt: number | null
  /** The imperative handle the canvas reads on every animation frame. */
  live: LiveSession | null
  ageMs: number | null
}

/**
 * Subscribe to a session's live state.
 *
 * The returned `live` object is intentionally *not* React state: the overlay
 * draws from it inside `requestAnimationFrame`. Only throttled HUD numbers are
 * mirrored into state, so a 10 FPS state stream costs no renders.
 */
export function useLiveSession(sessionId: string | null, hudIntervalMs = 250): UseLiveSessionResult {
  const [live, setLive] = useState<LiveSession | null>(null)
  const [hud, setHud] = useState<{
    state: SessionStatePayload | null
    events: LiveEvent[]
    stale: boolean
    connection: string
    error: string | null
    lastMessageAt: number | null
  }>({ state: null, events: [], stale: false, connection: 'idle', error: null, lastMessageAt: null })
  const [ageMs, setAgeMs] = useState<number | null>(null)

  useEffect(() => {
    if (!sessionId) {
      setLive(null)
      setHud({ state: null, events: [], stale: false, connection: 'idle', error: null, lastMessageAt: null })
      return
    }
    const session = new LiveSession(sessionId)
    setLive(session)
    let lastPush = 0
    const unsubscribe = session.subscribe((snapshot) => {
      const now = performance.now()
      if (now - lastPush >= hudIntervalMs || snapshot.events.length > 0) {
        lastPush = now
        setHud({
          state: snapshot.state,
          events: snapshot.events,
          stale: snapshot.stale,
          connection: snapshot.connection,
          error: snapshot.error,
          lastMessageAt: snapshot.lastMessageAt,
        })
        setAgeMs(snapshot.lastMessageAt === null ? null : Date.now() - snapshot.lastMessageAt)
      }
    })
    session.connect()
    return () => {
      unsubscribe()
      session.close()
    }
  }, [sessionId, hudIntervalMs])

  return { ...hud, live, ageMs }
}

export type { EventSummary }
