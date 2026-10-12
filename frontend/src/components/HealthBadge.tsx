/**
 * The header health badge and its diagnostics panel.
 *
 * The badge is one click away from the reason: each subsystem, whether it is
 * required, the last time we checked, and a refresh button. The panel reads the
 * shared `useHealthStatus` result, so it always agrees with the System page.
 */

import { useEffect, useId, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { Badge, Button, StatusDot } from '@/components/ui'
import { cx } from '@/lib/cx'
import { healthClass, healthLabel, reasonSentence, relativeTime } from '@/lib/format'
import { useHealthStatus, type HealthSnapshot } from '@/hooks/useHealth'

function checkSentence(name: string, check: { state: string; reason_code: string | null; detail: string | null }): string {
  return reasonSentence(check.reason_code, check.detail) ?? `${name} is ${check.state}.`
}

export function HealthPanel({
  snapshot,
  onRefresh,
  className,
}: {
  snapshot: HealthSnapshot & { isRefreshing?: boolean; refresh?: () => void }
  onRefresh?: () => void
  className?: string
}) {
  const refresh = onRefresh ?? snapshot.refresh
  const checks = Object.entries(snapshot.checks)
  return (
    <div className={cx('w-80 max-w-[calc(100vw-1.5rem)] text-left', className)}>
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-sm font-semibold text-ink">{healthLabel(snapshot.state)}</p>
          <p className="text-xs text-ink-3">
            {snapshot.stale
              ? `Last confirmed ${relativeTime(snapshot.lastSuccessAt)}`
              : `Checked ${relativeTime(snapshot.lastSuccessAt)}`}
          </p>
        </div>
        {refresh && (
          <Button variant="ghost" className="px-2 py-1 text-xs" loading={snapshot.isRefreshing} onClick={refresh}>
            Refresh
          </Button>
        )}
      </div>

      {snapshot.stale && (
        <p className="mt-2 rounded border border-warn/40 bg-warn/10 p-2 text-xs text-warn">
          The latest health check did not succeed. Showing the last known state.
        </p>
      )}

      {checks.length === 0 ? (
        <p className="mt-3 text-xs text-ink-3">No subsystems reported yet.</p>
      ) : (
        <ul className="mt-3 space-y-2">
          {checks.map(([name, check]) => (
            <li key={name} className="flex items-start gap-2 text-sm">
              <span className="pt-1">
                <StatusDot state={check.state} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="font-medium capitalize">{name}</span>
                {check.required && (
                  <span className="ml-1.5 text-[10px] uppercase tracking-wide text-ink-3">required</span>
                )}
                <span className="block text-xs text-ink-2">{checkSentence(name, check)}</span>
              </span>
              <Badge
                className={cx(
                  'shrink-0',
                  check.state === 'ok' || check.state === 'healthy'
                    ? 'border-ok/40 bg-ok/12 text-ok'
                    : check.state === 'failed'
                      ? 'border-crit/40 bg-crit/12 text-crit'
                      : 'border-warn/40 bg-warn/12 text-warn',
                )}
              >
                {check.state}
              </Badge>
            </li>
          ))}
        </ul>
      )}

      <div className="mt-3 border-t border-border pt-2">
        <Link to="/system" className="text-xs text-ink-2 underline hover:text-ink">
          Open System status
        </Link>
      </div>
    </div>
  )
}

export function HealthBadge() {
  const health = useHealthStatus()
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const panelId = useId()

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <div className="relative" ref={root}>
      <button
        type="button"
        className={cx('badge border cursor-pointer', healthClass(health.state))}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        title={`System health: ${health.state}. Click for details.`}
        onClick={() => setOpen((value) => !value)}
      >
        <StatusDot state={health.state} />
        {healthLabel(health.state)}
      </button>
      {open && (
        <div
          id={panelId}
          role="dialog"
          aria-label="System health"
          className="absolute right-0 z-40 mt-2 rounded-md border border-border bg-bg p-3 shadow-lg"
        >
          <HealthPanel snapshot={health} />
        </div>
      )}
    </div>
  )
}
