import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { Badge, Button, Card, DataTable, EmptyState, Field, Loading, Select, TextInput, Toggle } from '@/components/ui'
import { useToast } from '@/components/toast-context'
import { useCameras, useDeleteZone, useUpdateZone, useZones } from '@/hooks/useApi'
import { absoluteTime, polygonArea, severityClass } from '@/lib/format'
import type { NormalizedPolygon, Zone, ZoneSeverity } from '@/types/api'

const SEVERITIES: ZoneSeverity[] = ['info', 'warning', 'critical']

/** Zone inventory across every camera, with inline editing. */
export function Zones() {
  const { cameraId } = useParams<{ cameraId: string }>()
  const toast = useToast()
  const cameras = useCameras()
  const filter = cameraId ?? ''
  const zones = useZones(filter || undefined)
  const updateZone = useUpdateZone()
  const deleteZone = useDeleteZone()
  const [editingId, setEditingId] = useState<string | null>(null)

  const cameraNames = useMemo(
    () => Object.fromEntries((cameras.data ?? []).map((camera) => [camera.id, camera.name])),
    [cameras.data],
  )

  if (zones.isLoading) return <Loading label="Loading zones…" />

  const rows = zones.data ?? []

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Zones</h1>
          <p className="mt-0.5 text-sm text-ink-2">
            A zone is a restricted area on one camera. Entry and exit generate events.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Select
            aria-label="Filter by camera"
            value={filter}
            onChange={(event) => {
              const value = event.target.value
              window.location.assign(value ? `/zones/${value}` : '/zones')
            }}
            className="max-w-xs"
          >
            <option value="">All cameras</option>
            {(cameras.data ?? []).map((camera) => (
              <option key={camera.id} value={camera.id}>
                {camera.name}
              </option>
            ))}
          </Select>
          <Link to="/cameras" className="btn-secondary">
            Draw on video
          </Link>
        </div>
      </header>

      <Card
        title="Configured zones"
        subtitle={`${rows.length} zone${rows.length === 1 ? '' : 's'}`}
        padded={false}
      >
        {rows.length === 0 ? (
          <div className="p-4">
            <EmptyState
              title="No zones configured"
              icon="▭"
              action={
                <Link to="/cameras" className="btn-primary">
                  Draw a zone on a camera
                </Link>
              }
            >
              Open a camera, start a session and drag on the video to draw a restricted area. You can fine-tune
              the numbers here afterwards.
            </EmptyState>
          </div>
        ) : (
          <DataTable
            rows={rows}
            rowKey={(row) => row.id}
            columns={[
              {
                key: 'name',
                header: 'Zone',
                render: (zone: Zone) => (
                  <span className="flex items-center gap-2">
                    <span
                      className="size-3 shrink-0 rounded-sm border border-white/20"
                      style={{ background: zone.color }}
                      aria-hidden="true"
                    />
                    <span className="font-medium">{zone.name}</span>
                    {!zone.enabled && <Badge className="border-border bg-surface-2 text-ink-3">off</Badge>}
                  </span>
                ),
              },
              { key: 'camera', header: 'Camera', render: (zone) => cameraNames[zone.camera_id] ?? '—' },
              {
                key: 'severity',
                header: 'Severity',
                render: (zone) => <Badge className={severityClass(zone.severity)}>{zone.severity}</Badge>,
              },
              {
                key: 'size',
                header: 'Area',
                className: 'tnum text-ink-2',
                render: (zone) => `${(polygonArea(zone.points as NormalizedPolygon) * 100).toFixed(1)}% of frame`,
              },
              { key: 'dwell', header: 'Dwell', className: 'tnum text-ink-2', render: (zone) => `${zone.min_dwell_seconds}s` },
              { key: 'cooldown', header: 'Cooldown', className: 'tnum text-ink-2', render: (zone) => `${zone.cooldown_seconds}s` },
              {
                key: 'classes',
                header: 'Classes',
                className: 'text-ink-2',
                render: (zone) => (zone.class_filter.length ? zone.class_filter.join(', ') : 'all'),
              },
              { key: 'created', header: 'Created', className: 'tnum text-ink-3', render: (zone) => absoluteTime(zone.created_at) },
              {
                key: 'actions',
                header: '',
                render: (zone) => (
                  <span className="flex justify-end gap-1">
                    <Button variant="ghost" onClick={() => setEditingId(zone.id)}>
                      Edit
                    </Button>
                    <Button
                      variant="ghost"
                      onClick={async () => {
                        try {
                          await deleteZone.mutateAsync(zone.id)
                          toast.push({ tone: 'info', title: 'Zone deleted', detail: 'Past events keep their zone name.' })
                        } catch (error) {
                          toast.push({ tone: 'crit', title: 'Delete failed', detail: (error as Error).message })
                        }
                      }}
                    >
                      Delete
                    </Button>
                  </span>
                ),
              },
            ]}
          />
        )}
      </Card>

      {editingId && (
        <ZoneEditor
          zone={rows.find((zone) => zone.id === editingId)!}
          busy={updateZone.isPending}
          onCancel={() => setEditingId(null)}
          onSave={async (patch) => {
            try {
              await updateZone.mutateAsync({ id: editingId, body: patch })
              toast.push({ tone: 'ok', title: 'Zone updated', detail: 'A running session picks this up immediately.' })
              setEditingId(null)
            } catch (error) {
              toast.push({ tone: 'crit', title: 'Update failed', detail: (error as Error).message })
            }
          }}
        />
      )}
    </div>
  )
}

function ZoneEditor({
  zone,
  busy,
  onSave,
  onCancel,
}: {
  zone: Zone
  busy?: boolean
  onSave: (patch: Parameters<ReturnType<typeof useUpdateZone>['mutateAsync']>[0]['body']) => void
  onCancel: () => void
}) {
  const [name, setName] = useState(zone.name)
  const [color, setColor] = useState(zone.color)
  const [severity, setSeverity] = useState<ZoneSeverity>(zone.severity)
  const [minDwell, setMinDwell] = useState(zone.min_dwell_seconds)
  const [cooldown, setCooldown] = useState(zone.cooldown_seconds)
  const [classes, setClasses] = useState(zone.class_filter.join(', '))
  const [enabled, setEnabled] = useState(zone.enabled)
  const points = zone.points as NormalizedPolygon
  const bounds = useMemo(() => {
    const xs = points.map(([x]) => x)
    const ys = points.map(([, y]) => y)
    return { x1: Math.min(...xs), y1: Math.min(...ys), x2: Math.max(...xs), y2: Math.max(...ys) }
  }, [points])

  return (
    <Card
      title={`Edit “${zone.name}”`}
      subtitle="Changes apply to a running session without restarting it"
      actions={
        <>
          <Button onClick={onCancel}>Cancel</Button>
          <Button
            variant="primary"
            loading={busy}
            onClick={() =>
              onSave({
                name: name.trim() || zone.name,
                color,
                severity,
                min_dwell_seconds: minDwell,
                cooldown_seconds: cooldown,
                class_filter: classes
                  .split(',')
                  .map((value) => value.trim())
                  .filter(Boolean),
                enabled,
              })
            }
          >
            Save
          </Button>
        </>
      }
    >
      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-3">
          <Field label="Name" htmlFor="zone-edit-name">
            <TextInput id="zone-edit-name" value={name} onChange={(event) => setName(event.target.value)} />
          </Field>
          <Field label="Severity" htmlFor="zone-edit-severity">
            <Select
              id="zone-edit-severity"
              value={severity}
              onChange={(event) => setSeverity(event.target.value as ZoneSeverity)}
            >
              {SEVERITIES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Colour" htmlFor="zone-edit-color">
            <TextInput
              id="zone-edit-color"
              value={color}
              onChange={(event) => setColor(event.target.value)}
              className="font-mono"
            />
          </Field>
          <Field
            label="Class filter"
            hint="Comma-separated. Empty means every class the model reports."
            htmlFor="zone-edit-classes"
          >
            <TextInput id="zone-edit-classes" value={classes} onChange={(event) => setClasses(event.target.value)} />
          </Field>
        </div>

        <div className="space-y-3">
          <div className="relative aspect-video w-full overflow-hidden rounded border border-border bg-black">
            <div
              className="absolute border-2"
              style={{
                left: `${bounds.x1 * 100}%`,
                top: `${bounds.y1 * 100}%`,
                width: `${(bounds.x2 - bounds.x1) * 100}%`,
                height: `${(bounds.y2 - bounds.y1) * 100}%`,
                borderColor: color,
                background: `${color}1f`,
              }}
            />
            <span className="absolute left-2 top-2 text-[10px] text-ink-2">
              {Math.round(bounds.x1 * 100)}%,{Math.round(bounds.y1 * 100)}% → {Math.round(bounds.x2 * 100)}%,
              {Math.round(bounds.y2 * 100)}%
            </span>
          </div>
          <Field
            label="Minimum dwell (seconds)"
            hint="How long a person must stay inside before an entry is confirmed. Prevents boundary flapping."
            htmlFor="zone-edit-dwell"
          >
            <TextInput
              id="zone-edit-dwell"
              type="number"
              min={0}
              max={3600}
              step={0.1}
              value={minDwell}
              onChange={(event) => setMinDwell(Number(event.target.value))}
              className="tnum"
            />
          </Field>
          <Field
            label="Cooldown (seconds)"
            hint="Minimum gap between two identical events for this zone. Stops alert storms in busy areas."
            htmlFor="zone-edit-cooldown"
          >
            <TextInput
              id="zone-edit-cooldown"
              type="number"
              min={0}
              max={86400}
              step={1}
              value={cooldown}
              onChange={(event) => setCooldown(Number(event.target.value))}
              className="tnum"
            />
          </Field>
          <Toggle checked={enabled} onChange={setEnabled} label="Zone enabled" />
        </div>
      </div>
    </Card>
  )
}
