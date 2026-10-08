import { useMemo } from 'react'
import { Link } from 'react-router-dom'

import { Badge, Card, DataTable, EmptyState, Loading, StatCard, StatusDot } from '@/components/ui'
import { healthClass, hourLabel, milliseconds, number, relativeTime, seconds } from '@/lib/format'
import { useCameras, useEvents, useModelInfo, useSummary, useSystemStatus } from '@/hooks/useApi'
import { useToast } from '@/components/toast-context'
import type { EventItem } from '@/types/api'

/** The 10-second question: is anything happening, and is anything broken? */
export function Dashboard() {
  const toast = useToast()
  const status = useSystemStatus()
  const cameras = useCameras()
  const model = useModelInfo()
  const summary = useSummary()
  const recent = useEvents({ limit: 12 })

  const liveSessions = status.data?.sessions.items ?? []
  const degraded = liveSessions.filter((session) => session.state === 'degraded' || session.state === 'failed')
  const totalInZone = useMemo(
    () => Object.values(status.data?.sessions.items[0]?.in_zone ?? {}).reduce((sum, value) => sum + value, 0),
    [status.data],
  )

  // Surface a live zone event as a toast, once per event id.
  const seen = useMemo(() => new Set<string>(), [])
  useMemo(() => {
    for (const event of recent.data?.items.slice(0, 3) ?? []) {
      if (event.type !== 'ZONE_ENTERED' || !event.id || seen.has(event.id)) continue
      seen.add(event.id)
      toast.push({
        tone: event.severity === 'critical' ? 'crit' : event.severity === 'warning' ? 'warn' : 'info',
        title: `${event.type.replace(/_/g, ' ').toLowerCase()} · ${event.zone_name ?? event.camera_name}`,
        detail: `${event.camera_name} · ${relativeTime(event.occurred_at)}`,
      })
    }
  }, [recent.data, seen, toast])

  if (status.isLoading) return <Loading label="Reading system status…" />

  const health = status.data?.status ?? 'unknown'
  const modelState = model.data?.state ?? status.data?.model.state ?? 'unknown'

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Operations overview</h1>
          <p className="mt-0.5 text-sm text-ink-2">
            Restricted-zone intelligence · {status.data?.environment ?? 'local'} · no biometrics collected
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge className={healthClass(health)}>
            <StatusDot state={health} /> {health}
          </Badge>
          <Link to="/setup" className="btn-secondary">
            Add a camera
          </Link>
        </div>
      </header>

      {degraded.length > 0 && (
        <Card title="Needs attention" padded>
          <ul className="space-y-2">
            {degraded.map((session) => (
              <li key={session.session_id} className="flex flex-wrap items-center justify-between gap-2 text-sm">
                <span className="flex items-center gap-2">
                  <StatusDot state={session.state} />
                  <span className="font-medium">{session.camera_name}</span>
                  <span className="text-ink-2">{session.detail ?? session.reason_code ?? session.state}</span>
                </span>
                <Link to={`/cameras/${session.camera_id}`} className="btn-ghost">
                  Open
                </Link>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        <StatCard
          label="Cameras"
          value={cameras.data?.length ?? 0}
          hint={`${liveSessions.length} processing now`}
          state={cameras.data && cameras.data.length === 0 ? 'neutral' : 'ok'}
        />
        <StatCard
          label="Events today"
          value={number(summary.data?.totals.events ?? 0)}
          hint={
            summary.data?.avg_dwell_seconds
              ? `avg dwell ${seconds(summary.data.avg_dwell_seconds)}`
              : 'no dwell data yet'
          }
          state="neutral"
        />
        <StatCard
          label="In restricted zones"
          value={totalInZone}
          hint="across all live sessions"
          state={totalInZone > 0 ? 'warn' : 'neutral'}
        />
        <StatCard
          label="Detection latency"
          value={milliseconds(model.data ? undefined : undefined) === '—' ? '—' : milliseconds(liveSessions[0]?.inference_ms)}
          hint={model.data?.provider ?? 'no provider'}
          state="neutral"
        />
        <StatCard
          label="AI model"
          value={<span className="text-lg">{model.data?.name ?? status.data?.model.name ?? 'unavailable'}</span>}
          hint={modelState === 'ready' ? 'ready' : (status.data?.model.detail ?? modelState)}
          state={modelState === 'ready' ? 'ok' : 'crit'}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card
          className="xl:col-span-2"
          title="Live sessions"
          subtitle="Each session owns one camera's pipeline"
          actions={
            <Link to="/cameras" className="btn-ghost">
              Manage
            </Link>
          }
          padded={false}
        >
          {liveSessions.length === 0 ? (
            <div className="p-4">
              <EmptyState
                title="No session is running"
                icon="🎥"
                action={
                  <Link to="/cameras" className="btn-primary">
                    Open a camera
                  </Link>
                }
              >
                Start a session to detect people, evaluate your zones and generate events. Without camera
                hardware you can use a file source — the bundled synthetic fixture demonstrates the whole
                flow.
              </EmptyState>
            </div>
          ) : (
            <DataTable
              rows={liveSessions}
              rowKey={(row) => row.session_id}
              onRowClick={(row) => {
                window.location.assign(`/cameras/${row.camera_id}`)
              }}
              columns={[
                {
                  key: 'camera',
                  header: 'Camera',
                  render: (row) => (
                    <span className="flex items-center gap-2">
                      <StatusDot state={row.state} />
                      <span className="font-medium">{row.camera_name}</span>
                    </span>
                  ),
                },
                { key: 'state', header: 'State', render: (row) => <Badge className={healthClass(row.state)}>{row.state}</Badge> },
                { key: 'fps', header: 'FPS', className: 'tnum', render: (row) => row.processed_fps.toFixed(1) },
                { key: 'latency', header: 'Latency', className: 'tnum', render: (row) => milliseconds(row.inference_ms) },
                { key: 'tracks', header: 'Tracks', className: 'tnum', render: (row) => row.active_tracks },
                { key: 'events', header: 'Events', className: 'tnum', render: (row) => row.events_emitted },
                { key: 'age', header: 'Frame age', className: 'tnum', render: (row) => milliseconds(row.frame_age_ms) },
              ]}
            />
          )}
        </Card>

        <Card title="Events by hour (today)" subtitle="Restricted-zone activity">
          {!summary.data ? (
            <Loading label="Aggregating…" />
          ) : summary.data.hourly.length === 0 ? (
            <p className="py-6 text-center text-sm text-ink-3">No events recorded today.</p>
          ) : (
            <div className="flex h-40 items-end gap-1" role="img" aria-label="Events per hour today">
              {summary.data.hourly.map((bucket) => {
                const max = Math.max(...summary.data!.hourly.map((item) => item.count), 1)
                return (
                  <div key={bucket.bucket} className="group flex flex-1 flex-col items-center gap-1">
                    <div
                      className="w-full rounded-t bg-accent/70 transition-colors group-hover:bg-accent"
                      style={{ height: `${Math.max(3, (bucket.count / max) * 120)}px` }}
                      title={`${bucket.bucket}: ${bucket.count} event(s)`}
                    />
                    <span className="tnum text-[9px] text-ink-3">{hourLabel(bucket.bucket)}</span>
                  </div>
                )
              })}
            </div>
          )}
        </Card>
      </div>

      <Card
        title="Recent events"
        subtitle="Newest first"
        padded={false}
        actions={
          <Link to="/events" className="btn-ghost">
            View all
          </Link>
        }
      >
        {recent.isLoading ? (
          <Loading />
        ) : (recent.data?.items.length ?? 0) === 0 ? (
          <div className="p-4">
            <EmptyState title="No events yet" icon="📋">
              Events appear the moment a tracked person crosses into or out of a zone.
            </EmptyState>
          </div>
        ) : (
          <DataTable
            rows={recent.data!.items}
            rowKey={(row: EventItem) => row.id}
            onRowClick={(row) => {
              window.location.assign(`/events/${row.id}`)
            }}
            columns={[
              {
                key: 'type',
                header: 'Event',
                render: (row) => <EventCell event={row} />,
              },
              { key: 'camera', header: 'Camera', render: (row) => row.camera_name },
              { key: 'zone', header: 'Zone', render: (row) => row.zone_name ?? '—' },
              {
                key: 'severity',
                header: 'Severity',
                render: (row) => (
                  <Badge
                    className={
                      row.severity === 'critical'
                        ? 'border-crit/40 bg-crit/12 text-crit'
                        : row.severity === 'warning'
                          ? 'border-warn/40 bg-warn/12 text-warn'
                          : 'border-border bg-surface-2 text-ink-2'
                    }
                  >
                    {row.severity}
                  </Badge>
                ),
              },
              { key: 'when', header: 'When', className: 'tnum text-ink-2', render: (row) => relativeTime(row.occurred_at) },
            ]}
          />
        )}
      </Card>

      {modelState !== 'ready' && (
        <Card title="Detection is not available" subtitle="No events will be produced until this is fixed">
          <p className="text-sm text-ink-2">
            {status.data?.model.detail ??
              model.data?.error ??
              'No detection model is installed.'}{' '}
            On the server run <code className="font-mono">make model</code> to fetch it, then restart. Every
            other part of VisionOps — configuration, history, export — keeps working.
          </p>
          <Link to="/system" className="btn-secondary mt-3 inline-flex">
            Open system status
          </Link>
        </Card>
      )}
    </div>
  )
}

function EventCell({ event }: { event: EventItem }) {
  return (
    <span className="flex items-center gap-2">
      <span className="font-medium">{event.type.replace(/_/g, ' ').toLowerCase()}</span>
      {event.confidence !== null && (
        <span className="tnum text-xs text-ink-3">{(event.confidence * 100).toFixed(0)}%</span>
      )}
    </span>
  )
}
