import { Link, useParams } from 'react-router-dom'

import { Badge, Card, EmptyState, Loading, StatCard } from '@/components/ui'
import { absoluteTime, confidence, number, severityClass, seconds } from '@/lib/format'
import { useEvent } from '@/hooks/useApi'
import type { NormalizedBox } from '@/types/api'

/** Everything known about one event, with the evidence box drawn to scale. */
export function EventDetail() {
  const { eventId } = useParams<{ eventId: string }>()
  const event = useEvent(eventId)

  if (event.isLoading) return <Loading label="Loading event…" />
  if (!event.data) {
    return (
      <EmptyState
        title="Event not found"
        icon="🔎"
        action={
          <Link to="/events" className="btn-secondary">
            Back to events
          </Link>
        }
      >
        It may have been removed by the retention job, or the id may belong to another organisation.
      </EmptyState>
    )
  }

  const row = event.data
  const box = Array.isArray(row.metadata.box) ? (row.metadata.box as NormalizedBox) : null
  const foot = Array.isArray(row.metadata.foot_point) ? (row.metadata.foot_point as [number, number]) : null
  const dwell = typeof row.metadata.dwell_seconds === 'number' ? row.metadata.dwell_seconds : null

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-center gap-3">
        <Link to="/events" className="btn-ghost">
          ← Events
        </Link>
        <h1 className="text-xl font-semibold tracking-tight">{row.type.replace(/_/g, ' ').toLowerCase()}</h1>
        <Badge className={severityClass(row.severity)}>{row.severity}</Badge>
        <span className="tnum ml-auto text-xs text-ink-3">{absoluteTime(row.occurred_at)}</span>
      </header>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard label="Camera" value={<span className="text-base">{row.camera_name}</span>} state="neutral" />
        <StatCard label="Zone" value={<span className="text-base">{row.zone_name ?? '—'}</span>} state="neutral" />
        <StatCard label="Track" value={<span className="text-base">{row.track_id ? `#${row.track_id}` : '—'}</span>} hint="ephemeral, session-scoped" state="neutral" />
        <StatCard
          label="Confidence"
          value={confidence(row.confidence)}
          state={row.confidence && row.confidence > 0.7 ? 'ok' : row.confidence ? 'warn' : 'neutral'}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Evidence" subtitle="Where the tracked object was in the frame">
          {box ? (
            <div>
              <div className="relative aspect-video w-full overflow-hidden rounded border border-border bg-black">
                {foot && (
                  <div
                    className="absolute"
                    style={{
                      left: `${(box[0] + box[2]) / 2 * 100}%`,
                      top: `${box[3] * 100}%`,
                      width: `${(box[2] - box[0]) * 100}%`,
                      height: `${(box[3] - box[1]) * 100}%`,
                      transform: 'translate(-50%, -50%)',
                    }}
                  >
                    <div className="size-full border-2 border-accent" />
                    <span
                      className="absolute left-1/2 top-full size-2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-warn"
                      aria-hidden="true"
                    />
                    <span className="absolute -top-0.5 left-0 text-[10px] text-accent">foot point</span>
                  </div>
                )}
              </div>
              <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                <Row label="Box (normalised)" value={box.map((value) => value.toFixed(3)).join(', ')} />
                {foot && <Row label="Foot point" value={`${foot[0].toFixed(3)}, ${foot[1].toFixed(3)}`} />}
                <Row label="Class" value={String(row.metadata.class ?? '—')} />
                <Row label="Frame size" value={`${String(row.metadata.frame_width ?? '—')} x ${String(row.metadata.frame_height ?? '—')}`} />
                <Row label="Track age" value={`${number(Number(row.metadata.track_age_frames ?? 0))} frames`} />
                {dwell !== null && <Row label="Time in zone" value={seconds(dwell)} />}
              </dl>
              <p className="mt-3 text-xs leading-relaxed text-ink-3">
                Coordinates are normalised to 0–1, so they remain meaningful whatever the camera resolution is.
                The foot point (bottom-centre) is what the zone decision is based on, because that is where
                the person meets the floor.
              </p>
            </div>
          ) : (
            <p className="py-6 text-center text-sm text-ink-3">This event carries no geometry.</p>
          )}
        </Card>

        <Card title="Record" subtitle="Stored, append-only">
          <dl className="space-y-2 text-sm">
            <Row label="Event id" value={row.id} mono />
            <Row label="Schema version" value={String(row.schema_version)} />
            <Row label="Occurred at" value={absoluteTime(row.occurred_at)} />
            <Row label="Recorded at" value={absoluteTime(row.received_at)} />
            <Row label="Session" value={row.session_id ?? '—'} mono />
            <Row label="Organisation" value={row.organization_id} mono />
            <Row label="Snapshot" value={row.snapshot_path ?? 'disabled'} />
          </dl>
        </Card>

        <Card title="Provenance" subtitle="Which model produced this" className="lg:col-span-2">
          <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
            <Row label="Model" value={String(row.metadata.model ?? '—')} />
            <Row label="Model checksum" value={String(row.metadata.model_sha256 ?? '—')} mono />
            <Row label="Source kind" value={String(row.metadata.source_kind ?? '—')} />
            <Row label="Pipeline version" value={String(row.metadata.pipeline_version ?? '—')} />
          </dl>
          <p className="mt-3 text-xs leading-relaxed text-ink-3">
            The model checksum is recorded with every event so a result can always be tied to the exact weights
            that produced it.
          </p>
        </Card>
      </div>
    </div>
  )
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border/40 pb-1.5 last:border-0">
      <dt className="shrink-0 text-xs uppercase tracking-wide text-ink-3">{label}</dt>
      <dd className={`tnum truncate text-right text-ink ${mono ? 'font-mono text-xs' : ''}`} title={value}>
        {value}
      </dd>
    </div>
  )
}
