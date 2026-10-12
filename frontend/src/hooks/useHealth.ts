/**
 * The single source of truth for the health badge and the System page.
 *
 * Everything reads `GET /readyz` through this hook so the header badge, the
 * System page and any future panel can never disagree. It polls gently, keeps
 * the last good answer during a blip (marked *stale*), and never conflates an
 * authentication failure with the backend being offline.
 */

import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api'
import type { HealthState, ReadyCheck, ReadyResponse } from '@/types/api'

/** How often to re-check. Slow enough to be invisible, fast enough to notice. */
export const HEALTH_POLL_MS = 12_000

export interface HealthSnapshot {
  /** The four-state verdict the badge uses. */
  state: HealthState
  /** Per-subsystem checks from the last successful answer. */
  checks: Record<string, ReadyCheck>
  /** True when the last request reached the API (even if it was `unavailable`). */
  reachable: boolean
  /** True when the failure was an auth problem, not a network/offline problem. */
  authError: boolean
  /** True when the latest refresh failed and these checks are the last known good. */
  stale: boolean
  /** Wall-clock ms of the last successful answer, or null. */
  lastSuccessAt: number | null
  /** Server timestamp of the last successful answer, or null. */
  timestamp: string | null
}

export interface UseHealthResult extends HealthSnapshot {
  isRefreshing: boolean
  refresh: () => void
}

function statusOf(error: unknown): number | null {
  if (error && typeof error === 'object' && 'status' in error) {
    const value = (error as { status?: unknown }).status
    return typeof value === 'number' ? value : null
  }
  return null
}

function isAuthError(error: unknown): boolean {
  const status = statusOf(error)
  return status === 401 || status === 403
}

function isNetworkError(error: unknown): boolean {
  return statusOf(error) === 0
}

function baseState(data: ReadyResponse): HealthState {
  if (data.status === 'ready') return 'healthy'
  if (data.status === 'degraded') return 'degraded'
  return 'offline'
}

/**
 * Pure derivation of the badge state from the query result. Exported so the
 * state machine is unit-tested without rendering.
 */
export function deriveHealth(input: {
  data: ReadyResponse | undefined
  error: unknown
  lastSuccessAt: number | null
}): HealthSnapshot {
  const { data, error, lastSuccessAt } = input

  if (!data) {
    // Never received an answer yet.
    if (isAuthError(error)) {
      // A rejected key is not proof the backend is down.
      return empty('checking', { authError: true })
    }
    if (error) return empty('offline', {})
    return empty('checking', {})
  }

  const checks = data.checks
  const timestamp = data.timestamp

  // We have a last-good answer but the latest refresh could not reach the API.
  if (isNetworkError(error)) {
    return {
      state: 'offline',
      checks,
      reachable: false,
      authError: false,
      stale: true,
      lastSuccessAt,
      timestamp,
    }
  }

  return {
    state: baseState(data),
    checks,
    reachable: true,
    authError: isAuthError(error),
    stale: Boolean(error),
    lastSuccessAt,
    timestamp,
  }
}

function empty(
  state: HealthState,
  overrides: Partial<HealthSnapshot>,
): HealthSnapshot {
  return {
    state,
    checks: {},
    reachable: false,
    authError: false,
    stale: false,
    lastSuccessAt: null,
    timestamp: null,
    ...overrides,
  }
}

export function useHealthStatus(enabled = true): UseHealthResult {
  const query = useQuery({
    queryKey: ['system', 'ready'],
    queryFn: ({ signal }) => api.ready(signal),
    enabled,
    // Polling that pauses in a background tab and resumes on focus keeps the
    // state fresh without a storm of requests when nobody is looking.
    refetchInterval: HEALTH_POLL_MS,
    refetchIntervalInBackground: false,
    staleTime: 4_000,
    retry: 2,
    refetchOnWindowFocus: true,
  })

  const snapshot = deriveHealth({
    data: query.data,
    error: query.error,
    lastSuccessAt: query.dataUpdatedAt || null,
  })

  return {
    ...snapshot,
    isRefreshing: query.isFetching,
    refresh: () => {
      void query.refetch()
    },
  }
}
