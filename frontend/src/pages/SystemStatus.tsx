import { Link } from 'react-router-dom'

import { Badge, Card, DataTable, Loading, StatCard, StatusDot } from '@/components/ui'
import { bytes, milliseconds, number, reasonSentence, seconds } from '@/lib/format'
import { useModelInfo, useOrganization, useSystemStatus } from '@/hooks/useApi'

/** Is it healthy, and if not, exactly why? The first page support asks for. */
export function SystemStatus() {
  const status = useSystemStatus()
  const model = useModelInfo()
  const organization = useOrganization()

  if (status.isLoading) return <Loading label="Reading system status…" />
  if (!status.data) {
    return (
      <Card title="Status unavailable">
        <p className="text-sm text-ink-2">
          The API did not return a status document. Check that the server is running and that the API key in
          this browser is valid.
        </p>
      </Card>
    )
  }

  const data = status.data
  const session = data.sessions.items[0]
  const health = data.status

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">System status</h1>
          <p className="mt-0.5 text-sm text-ink-2">
            {organization.data?.name ?? 'VisionOps'} · {data.environment} · v{data.version} · up{' '}
            {seconds(data.uptime_seconds)}
          </p>
        </div>
        <Badge className={health === 'healthy' ? 'border-ok/40 bg-ok/12 text-ok' : health === 'degraded' ? 'border-warn/40 bg-warn/12 text-warn' : 'border-crit/40 bg-crit/12 text-crit'}>
          <StatusDot state={health} /> {health}
        </Badge>
      </header>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard
          label="Overall"
          value={health}
          hint={Object.entries(data.checks)
            .filter(([, check]) => check.state !== 'ok')
            .map(([name, check]) => `${name}: ${check.reason_code ?? check.state}`)
            .join(', ') || 'all subsystems ok'}
          state={health === 'healthy' ? 'ok' : health === 'degraded' ? 'warn' : 'crit'}
        />
        <StatCard
          label="Sessions"
          value={`${data.sessions.active} / ${data.sessions.max}`}
          hint="running / configured maximum"
          state={data.sessions.active >= data.sessions.max ? 'warn' : 'neutral'}
        />
        <StatCard
          label="Events recorded"
          value={number(data.counters.events_total ?? 0)}
          hint={`${number(data.counters.events_suppressed_total ?? 0)} suppressed by gates`}
          state={(data.counters.events_dropped_total ?? 0) > 0 ? 'crit' : 'neutral'}
        />
        <StatCard
          label="Free disk"
          value={bytes(data.storage.disk_free_bytes)}
          hint={data.storage.snapshot_dir}
          state="neutral"
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Detection model" subtitle="What the AI is actually running">
          <dl className="space-y-2 text-sm">
            <Row label="State" value={model.data?.state ?? data.model.state} />
            <Row label="Model" value={model.data?.name ?? data.model.name ?? '—'} />
            <Row label="Checksum" value={(model.data?.sha256 ?? data.model.sha256 ?? '—').slice(0, 24)} mono />
            <Row label="Provider" value={model.data?.provider ?? data.model.provider ?? '—'} />
            <Row
              label="Input"
              value={data.model.input_width ? `${data.model.input_width}×${data.model.input_height}` : '—'}
            />
            <Row label="Classes" value={number(data.model.classes_count)} />
            <Row label="Warmup" value={milliseconds(data.model.warmup_ms ?? undefined)} />
            <Row
              label="Last inference"
              value={milliseconds(session?.inference_ms)}
              hint={session ? `${session.inference_errors} error(s)` : undefined}
            />
          </dl>
          {data.model.detail && data.model.state !== 'ready' && (
            <p className="mt-3 rounded border border-warn/40 bg-warn/10 p-2.5 text-xs text-warn">
              {data.model.detail} — on the server run <code className="font-mono">make model</code> to install it.
              Everything else in VisionOps keeps working.
            </p>
          )}
          {model.data?.config && (
            <p className="mt-3 text-xs text-ink-3">
              Thresholds: confidence {String(model.data.config.confidence_threshold ?? '—')} · NMS IoU{' '}
              {String(model.data.config.nms_iou_threshold ?? '—')} · classes{' '}
              {Array.isArray(model.data.config.class_filter)
                ? (model.data.config.class_filter as string[]).join(', ') || 'all'
                : '—'}{' '}
              · target {String(model.data.config.target_fps ?? '—')} fps
            </p>
          )}
        </Card>

        <Card title="Subsystems" subtitle="Every check reports a state and a reason code">
          <ul className="space-y-2">
            {Object.entries(data.checks).map(([name, check]) => (
              <li key={name} className="flex items-start gap-2 text-sm">
                <span className="pt-1">
                  <StatusDot state={check.state} />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="font-medium capitalize">{name}</span>
                  <span className="ml-2 text-xs text-ink-3">{check.detail ?? check.reason_code ?? 'ok'}</span>
                </span>
                <Badge
                  className={
                    check.state === 'ok'
                      ? 'border-ok/40 bg-ok/12 text-ok'
                      : check.state === 'failed'
                        ? 'border-crit/40 bg-crit/12 text-crit'
                        : 'border-warn/40 bg-warn/12 text-warn'
                  }
                >
                  {check.state}
                </Badge>
              </li>
            ))}
          </ul>
          <div className="mt-4 border-t border-border pt-3 text-xs text-ink-3">
            <p>
              Credential encryption: <span className="text-ink-2">{data.storage.credential_encryption}</span>.{' '}
              {data.storage.credential_encryption === 'not-configured'
                ? 'Set VISIONOPS_MASTER_KEY to store RTSP credentials.'
                : 'Camera credentials are stored encrypted with AES-256-GCM.'}
            </p>
            <p className="mt-1">
              Snapshots: <span className="text-ink-2">{data.storage.snapshot_dir}</span>. Off by default; when
              enabled, an annotated JPEG is written per event and deleted with it.
            </p>
          </div>
        </Card>
      </div>

      <Card title="Active sessions" padded={false}>
        {data.sessions.items.length === 0 ? (
          <p className="px-4 py-8 text-center text-sm text-ink-3">No session is running.</p>
        ) : (
          <DataTable
            rows={data.sessions.items}
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
              { key: 'state', header: 'State', render: (row) => row.state },
              {
                key: 'detail',
                header: 'Detail',
                className: 'text-ink-2',
                render: (row) => reasonSentence(row.reason_code, row.detail) ?? '—',
              },
              { key: 'fps', header: 'FPS', className: 'tnum', render: (row) => row.processed_fps.toFixed(2) },
              { key: 'age', header: 'Frame age', className: 'tnum', render: (row) => milliseconds(row.frame_age_ms) },
              { key: 'frames', header: 'Frames', className: 'tnum', render: (row) => `${number(row.frames_processed)} / ${number(row.frames_dropped)}` },
              { key: 'events', header: 'Events', className: 'tnum', render: (row) => number(row.events_emitted) },
              { key: 'uptime', header: 'Uptime', className: 'tnum text-ink-2', render: (row) => seconds(row.uptime_seconds) },
            ]}
          />
        )}
      </Card>

      <Card title="Counters" subtitle="Since this process started">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {Object.entries(data.counters).map(([name, value]) => (
            <div key={name} className="flex items-baseline justify-between gap-2 border-b border-border/40 py-1 text-sm">
              <span className="font-mono text-xs text-ink-2">{name.replace('visionops_', '')}</span>
              <span className="tnum text-ink">{number(value)}</span>
            </div>
          ))}
        </div>
        <p className="mt-3 text-xs text-ink-3">
          Prometheus metrics are exposed at <code className="font-mono">/api/v1/system/metrics</code>. Nothing is
          sent anywhere unless you scrape it.
        </p>
      </Card>

      <Card title="Privacy" subtitle="What this deployment stores">
        <ul className="list-inside list-disc space-y-1 text-sm text-ink-2">
          <li>Events: type, timestamp, camera, zone, count, anonymous track id. No personal data.</li>
          <li>No facial recognition, no face detection, no embeddings, no identity — by design, not by omission.</li>
          <li>Video frames are processed in memory and never written, unless snapshots are explicitly enabled.</li>
          <li>Track ids are ephemeral and scoped to a single session; nothing links them across sessions.</li>
          <li>
            Events older than {organization.data?.retention_days ?? 90} days are deleted by the retention job.{' '}
            <Link to="/events" className="underline hover:text-ink-2">
              Export
            </Link>{' '}
            whatever you need first.
          </li>
        </ul>
      </Card>
    </div>
  )
}

function Row({ label, value, hint, mono }: { label: string; value: string; hint?: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border/40 pb-1.5 last:border-0">
      <dt className="shrink-0 text-xs uppercase tracking-wide text-ink-3">{label}</dt>
      <dd className={`tnum truncate text-right text-ink ${mono ? 'font-mono text-xs' : ''}`} title={value}>
        {value}
        {hint && <span className="ml-2 text-xs text-ink-3">{hint}</span>}
      </dd>
    </div>
  )
}
