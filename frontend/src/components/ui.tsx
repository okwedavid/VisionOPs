import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from 'react'

import { cx } from '@/lib/cx'

// ---------------------------------------------------------------------------

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost'
  loading?: boolean
}

export function Button({ variant = 'secondary', loading = false, className, children, disabled, ...rest }: ButtonProps) {
  const variantClass = {
    primary: 'btn-primary',
    secondary: 'btn-secondary',
    danger: 'btn-danger',
    ghost: 'btn-ghost',
  }[variant]
  return (
    <button className={cx(variantClass, className)} disabled={disabled || loading} {...rest}>
      {loading && <Spinner size={12} />}
      {children}
    </button>
  )
}

// ---------------------------------------------------------------------------

export function Card({
  title,
  subtitle,
  actions,
  children,
  className,
  padded = true,
}: {
  title?: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  children?: ReactNode
  className?: string
  padded?: boolean
}) {
  return (
    <section className={cx('card', className)}>
      {(title || actions) && (
        <header className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0">
            {title && <h2 className="truncate text-sm font-semibold text-ink">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-ink-2">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={padded ? 'p-4' : ''}>{children}</div>
    </section>
  )
}

// ---------------------------------------------------------------------------

export function Badge({
  children,
  className,
  title,
}: {
  children: ReactNode
  className?: string
  title?: string
}) {
  return (
    <span className={cx('badge border', className)} title={title}>
      {children}
    </span>
  )
}

export function StatusDot({ state }: { state: string }) {
  const color =
    state === 'ok' || state === 'healthy' || state === 'running' || state === 'ready'
      ? 'bg-ok'
      : state === 'degraded'
        ? 'bg-warn'
        : state === 'failed' || state === 'critical' || state === 'error'
          ? 'bg-crit'
          : 'bg-ink-3'
  return <span className={cx('inline-block size-2 shrink-0 rounded-full', color)} aria-hidden="true" />
}

// ---------------------------------------------------------------------------

export function StatCard({
  label,
  value,
  hint,
  state,
  icon,
}: {
  label: string
  value: ReactNode
  hint?: ReactNode
  state?: 'ok' | 'warn' | 'crit' | 'neutral'
  icon?: ReactNode
}) {
  const accent = {
    ok: 'border-l-ok',
    warn: 'border-l-warn',
    crit: 'border-l-crit',
    neutral: 'border-l-border',
  }[state ?? 'neutral']
  return (
    <div className={cx('card border-l-2 p-3', accent)}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] font-medium uppercase tracking-wide text-ink-2">{label}</span>
        {icon && <span className="text-ink-3">{icon}</span>}
      </div>
      <div className="tnum mt-1.5 text-2xl font-semibold leading-none text-ink">{value}</div>
      {hint && <div className="mt-1.5 text-xs text-ink-3">{hint}</div>}
    </div>
  )
}

// ---------------------------------------------------------------------------

type BannerTone = 'info' | 'ok' | 'warn' | 'crit'

const BANNER_TONE: Record<BannerTone, string> = {
  info: 'border-info/40 bg-info/10 text-info',
  ok: 'border-ok/40 bg-ok/10 text-ok',
  warn: 'border-warn/40 bg-warn/10 text-warn',
  crit: 'border-crit/40 bg-crit/10 text-crit',
}

export function Banner({
  tone = 'info',
  title,
  children,
  actions,
  onDismiss,
  role = 'status',
}: {
  tone?: BannerTone
  title?: ReactNode
  children?: ReactNode
  actions?: ReactNode
  onDismiss?: () => void
  role?: 'status' | 'alert'
}) {
  return (
    <div
      role={role}
      className={cx('flex flex-wrap items-start justify-between gap-3 rounded-md border px-3 py-2.5 text-sm', BANNER_TONE[tone])}
    >
      <div className="min-w-0 flex-1">
        {title && <p className="font-semibold">{title}</p>}
        {children && <div className={cx(title ? 'mt-0.5' : '', 'text-ink-2')}>{children}</div>}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {actions}
        {onDismiss && (
          <button type="button" onClick={onDismiss} className="text-ink-3 hover:text-ink" aria-label="Dismiss">
            ✕
          </button>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------

export function EmptyState({
  icon,
  title,
  children,
  action,
}: {
  icon?: ReactNode
  title: string
  children?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border px-6 py-10 text-center">
      {icon && <div className="text-2xl text-ink-3">{icon}</div>}
      <p className="text-sm font-medium text-ink">{title}</p>
      {children && <div className="max-w-md text-xs leading-relaxed text-ink-2">{children}</div>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}

export function Spinner({ size = 16 }: { size?: number }) {
  return (
    <span
      className="inline-block animate-spin rounded-full border-2 border-current border-t-transparent"
      style={{ width: size, height: size }}
      aria-hidden="true"
    />
  )
}

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-10 text-sm text-ink-2" role="status">
      <Spinner />
      {label}
    </div>
  )
}

// ---------------------------------------------------------------------------

export function Field({
  label,
  hint,
  error,
  children,
  htmlFor,
}: {
  label: string
  hint?: ReactNode
  error?: string
  children: ReactNode
  htmlFor?: string
}) {
  return (
    <div>
      <label className="label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {error ? (
        <p className="mt-1 text-xs text-crit" role="alert">
          {error}
        </p>
      ) : (
        hint && <p className="mt-1 text-xs text-ink-3">{hint}</p>
      )}
    </div>
  )
}

export function TextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input className="input" {...props} />
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className="input" {...props} />
}

export function Toggle({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean
  onChange: (value: boolean) => void
  label: string
  disabled?: boolean
}) {
  return (
    <label className="flex cursor-pointer items-center gap-2 text-sm text-ink">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className="size-4 accent-accent"
      />
      {label}
    </label>
  )
}

// ---------------------------------------------------------------------------

export function RelativeTime({ value, className }: { value: string | number | Date | null; className?: string }) {
  return (
    <time className={className} dateTime={new Date(value ?? 0).toISOString()} title={new Date(value ?? 0).toLocaleString()}>
      {formatRelative(value)}
    </time>
  )
}

// Imported lazily to keep this module free of cycles.
import { relativeTime as formatRelative } from '@/lib/format'

// ---------------------------------------------------------------------------

export function Modal({
  open,
  title,
  onClose,
  children,
  footer,
}: {
  open: boolean
  title: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
}) {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" role="dialog" aria-modal="true">
      <div className="card w-full max-w-md">
        <header className="flex items-center justify-between border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">{title}</h2>
          <button type="button" onClick={onClose} className="btn-ghost px-2 py-1" aria-label="Close">
            ✕
          </button>
        </header>
        <div className="max-h-[70vh] overflow-y-auto p-4">{children}</div>
        {footer && <footer className="flex justify-end gap-2 border-t border-border px-4 py-3">{footer}</footer>}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------

export function DataTable<T>({
  rows,
  columns,
  onRowClick,
  emptyMessage = 'No rows',
  rowKey,
}: {
  rows: T[]
  columns: { key: string; header: ReactNode; render: (row: T) => ReactNode; className?: string }[]
  onRowClick?: (row: T) => void
  emptyMessage?: string
  rowKey: (row: T) => string
}) {
  if (rows.length === 0) {
    return <p className="py-8 text-center text-sm text-ink-3">{emptyMessage}</p>
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-border text-left">
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={cx('px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-ink-2', column.className)}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={rowKey(row)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              className={cx('border-b border-border/50 last:border-0', onRowClick && 'cursor-pointer hover:bg-surface-2')}
            >
              {columns.map((column) => (
                <td key={column.key} className={cx('px-3 py-2 align-middle', column.className)}>
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
