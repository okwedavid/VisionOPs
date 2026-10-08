import { useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { Badge, Button, Card, DataTable, EmptyState, Field, Loading, Select, StatCard, TextInput } from '@/components/ui'
import { api } from '@/lib/api'
import { absoluteTime, number, relativeTime, severityClass } from '@/lib/format'
import { useCameras, useEvents, useSummary } from '@/hooks/useApi'
import type { EventItem, Severity } from '@/types/api'
import { cx } from '@/lib/cx'

const TYPES = ['', 'ZONE_ENTERED', 'ZONE_EXITED', 'SOURCE_STARTED', 'SOURCE_STOPPED', 'SOURCE_FAILED', 'MODEL_LOADED', 'MODEL_FAILED']
const SEVERITIES: (Severity | '')[] = ['', 'info', 'warning', 'critical', 'error']

/** Event history with the filters an operator actually uses. */
export function Events() {
  const [params, setParams] = useSearchParams()
  const cameras = useCameras()
  const [search, setSearch] = useState(params.get('q') ?? '')

  const filters = useMemo(
    () => ({
      camera_id: params.get('camera_id') || undefined,
      type: params.get('type') || undefined,
      severity: params.get('severity') || undefined,
      since: params.get('since') || undefined,
      until: params.get('until') || undefined,
      q: params.get('q') || undefined,
      limit: 100,
    }),
    [params],
  )
  const events = useEvents(filters)
  const summary = useSummary(true, filters.camera_id)

  const setFilter = (key: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    setParams(next, { replace: true })
  }

  const applySearch = (event: React.FormEvent) => {
    event.preventDefault()
    setFilter('q', search.trim())
  }

  const items = events.data?.items ?? []
  const hasFilters = Boolean(filters.camera_id || filters.type || filters.severity || filters.since || filters.q)

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Events</h1>
          <p className="mt-0.5 text-sm text-ink-2">
            Every event is a durable record with a timestamp, a camera, a zone and an anonymous track id.
          </p>
        </div>
        <Button
          onClick={() => {
            const to = new Date()
            const from = new Date(to.getTime() - 24 * 3600 * 1000)
            void api
              .downloadExport({ since: from.toISOString(), until: to.toISOString(), format: 'csv' })
              .catch(() => undefined)
          }}
        >
          Export last 24 h (CSV)
        </Button>
      </header>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard label="Total" value={number(summary.data?.totals.events ?? 0)} state="neutral" />
        <StatCard label="Warnings" value={number(summary.data?.totals.by_severity.warning ?? 0)} state="warn" />
        <StatCard label="Critical" value={number(summary.data?.totals.by_severity.critical ?? 0)} state="crit" />
        <StatCard
          label="Avg dwell"
          value={summary.data?.avg_dwell_seconds ? `${summary.data.avg_dwell_seconds.toFixed(1)} s` : '—'}
          hint="time inside a zone"
          state="neutral"
        />
      </div>

      <Card title="Filters" subtitle="Filters apply to the list and the summary above">
        <form onSubmit={applySearch} className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <Field label="Camera" htmlFor="filter-camera">
            <Select id="filter-camera" value={filters.camera_id ?? ''} onChange={(event) => setFilter('camera_id', event.target.value)}>
              <option value="">All cameras</option>
              {(cameras.data ?? []).map((camera) => (
                <option key={camera.id} value={camera.id}>
                  {camera.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Type" htmlFor="filter-type">
            <Select id="filter-type" value={filters.type ?? ''} onChange={(event) => setFilter('type', event.target.value)}>
              {TYPES.map((type) => (
                <option key={type} value={type}>
                  {type ? type.replace(/_/g, ' ').toLowerCase() : 'All types'}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Severity" htmlFor="filter-severity">
            <Select
              id="filter-severity"
              value={filters.severity ?? ''}
              onChange={(event) => setFilter('severity', event.target.value)}
            >
              {SEVERITIES.map((severity) => (
                <option key={severity} value={severity}>
                  {severity || 'All severities'}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Search" htmlFor="filter-search">
            <TextInput
              id="filter-search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="zone or camera name"
            />
          </Field>
          <div className="flex items-end gap-2">
            <Button type="submit" variant="primary">
              Apply
            </Button>
            {hasFilters && (
              <Button
                variant="ghost"
                onClick={() => {
                  setSearch('')
                  setParams(new URLSearchParams(), { replace: true })
                }}
              >
                Clear
              </Button>
            )}
          </div>
        </form>
      </Card>

      <Card
        title="History"
        subtitle={items.length > 0 ? `${items.length} shown (newest first)` : undefined}
        padded={false}
      >
        {events.isLoading ? (
          <Loading />
        ) : items.length === 0 ? (
          <div className="p-4">
            <EmptyState title="No events match these filters" icon="🔍">
              {hasFilters
                ? 'Try clearing the filters or widening the time range.'
                : 'Events appear when a tracked person crosses a zone boundary. Start a camera session to generate them.'}
            </EmptyState>
          </div>
        ) : (
          <DataTable
            rows={items}
            rowKey={(row) => row.id}
            onRowClick={(row) => {
              window.location.assign(`/events/${row.id}`)
            }}
            columns={[
              {
                key: 'type',
                header: 'Event',
                render: (row: EventItem) => (
                  <span className="flex items-center gap-2">
                    <span className="font-medium">{row.type.replace(/_/g, ' ').toLowerCase()}</span>
                  </span>
                ),
              },
              {
                key: 'severity',
                header: 'Severity',
                render: (row) => (
                  <Badge className={severityClass(row.severity)}>{row.severity}</Badge>
                ),
              },
              { key: 'camera', header: 'Camera', render: (row) => row.camera_name },
              { key: 'zone', header: 'Zone', render: (row) => row.zone_name ?? '—' },
              { key: 'track', header: 'Track', className: 'tnum text-ink-3', render: (row) => (row.track_id ? `#${row.track_id}` : '—') },
              {
                key: 'conf',
                header: 'Conf.',
                className: 'tnum',
                render: (row) => (row.confidence === null ? '—' : `${(row.confidence * 100).toFixed(0)}%`),
              },
              {
                key: 'when',
                header: 'Occurred',
                className: 'tnum text-ink-2',
                render: (row) => <span title={absoluteTime(row.occurred_at)}>{relativeTime(row.occurred_at)}</span>,
              },
            ]}
          />
        )}
      </Card>

      {items.length > 0 && (
        <p className={cx('text-xs text-ink-3')}>
          Events are append-only and are never edited. Deletion happens only through the retention job.{' '}
          <Link to="/system" className="underline hover:text-ink-2">
            See retention settings
          </Link>
          .
        </p>
      )}
    </div>
  )
}
