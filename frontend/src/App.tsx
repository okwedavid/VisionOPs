import { Component, useEffect, type ErrorInfo, type ReactNode } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'

import { Badge, Button, StatusDot } from './components/ui'
import { healthClass, reasonSentence, seconds } from './lib/format'
import { clearApiKey, getApiKey } from './lib/api'
import { useSystemStatus } from './hooks/useApi'

/** Catches render errors and shows a readable panel — never a blank page. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  override state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error): { error: Error } {
    return { error }
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('VisionOps UI error', error, info.componentStack)
  }

  override render(): ReactNode {
    if (!this.state.error) return this.props.children
    return (
      <div className="flex min-h-screen items-center justify-center p-6">
        <div className="card max-w-lg p-6">
          <h1 className="text-base font-semibold text-crit">The interface hit an error</h1>
          <p className="mt-2 text-sm text-ink-2">
            This is a bug in the web app, not in your cameras or data. Reloading usually clears it. If it
            persists, the server logs will contain the matching stack trace.
          </p>
          <pre className="mt-3 max-h-40 overflow-auto rounded bg-surface-2 p-3 text-xs text-ink-2">
            {this.state.error.message}
          </pre>
          <div className="mt-4 flex gap-2">
            <Button variant="primary" onClick={() => window.location.reload()}>
              Reload
            </Button>
            <Button onClick={() => this.setState({ error: null })}>Dismiss</Button>
          </div>
        </div>
      </div>
    )
  }
}

const NAV = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/cameras', label: 'Cameras' },
  { to: '/zones', label: 'Zones' },
  { to: '/events', label: 'Events' },
  { to: '/system', label: 'System' },
]

export function AppLayout() {
  const location = useLocation()
  const { data: status } = useSystemStatus(Boolean(getApiKey()))
  const hasKey = Boolean(getApiKey())

  // Move focus to the main region on navigation so keyboard and screen-reader
  // users are not stranded at the top of the document.
  useEffect(() => {
    document.getElementById('main')?.focus()
  }, [location.pathname])

  const health = status?.status ?? 'unknown'
  const active = status?.sessions.active ?? 0

  return (
    <div className="flex min-h-screen flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-accent focus:px-3 focus:py-2 focus:text-sm focus:text-bg"
      >
        Skip to content
      </a>

      <header className="sticky top-0 z-30 border-b border-border bg-bg/95 backdrop-blur">
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-3 px-3 py-2.5 sm:px-5">
          <NavLink to="/" className="flex items-center gap-2" aria-label="VisionOps home">
            <svg viewBox="0 0 32 32" className="size-7" aria-hidden="true">
              <rect width="32" height="32" rx="7" fill="#111a2b" />
              <circle cx="16" cy="12" r="5" fill="none" stroke="#38bdf8" strokeWidth="2.5" />
              <path d="M6 27c1.5-6 5-9 10-9s8.5 3 10 9" fill="none" stroke="#38bdf8" strokeWidth="2.5" strokeLinecap="round" />
              <rect x="18" y="4" width="12" height="11" rx="2" fill="none" stroke="#f97316" strokeWidth="2" strokeDasharray="3 2" />
            </svg>
            <span className="text-sm font-semibold tracking-tight">
              VisionOps
              <span className="ml-1.5 text-[10px] font-normal uppercase tracking-widest text-ink-3">
                v{status?.version ?? '0.1.0'}
              </span>
            </span>
          </NavLink>

          <nav aria-label="Main" className="order-3 -mx-1 flex w-full gap-1 overflow-x-auto sm:order-none sm:mx-0 sm:w-auto">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `whitespace-nowrap rounded-md px-2.5 py-1.5 text-sm transition-colors ${
                    isActive ? 'bg-surface-2 text-ink' : 'text-ink-2 hover:bg-surface-2/60 hover:text-ink'
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-2">
            {hasKey ? (
              <>
                <Badge className={healthClass(health)} title={`System health: ${health}`}>
                  <StatusDot state={health} />
                  {health}
                </Badge>
                {active > 0 && (
                  <Badge className="border-border bg-surface-2 text-ink-2" title="Active processing sessions">
                    {active} live
                  </Badge>
                )}
                <span className="hidden text-xs text-ink-3 md:inline">
                  up {seconds(status?.uptime_seconds)}
                </span>
                <Button variant="ghost" onClick={() => { clearApiKey(); window.location.assign('/') }}>
                  Sign out
                </Button>
              </>
            ) : (
              <NavLink to="/setup" className="btn-primary">
                Set up
              </NavLink>
            )}
          </div>
        </div>
      </header>

      <main id="main" tabIndex={-1} className="mx-auto w-full max-w-[1600px] flex-1 px-3 py-4 outline-none sm:px-5 sm:py-6">
        <Outlet />
      </main>

      <footer className="border-t border-border px-3 py-3 text-center text-[11px] text-ink-3 sm:px-5">
        VisionOps analyses objects in zones — never identities. No facial recognition, no biometrics, no
        personal data.
        {status?.status === 'degraded' && status.sessions.items.some((s) => s.reason_code) && (
          <span className="ml-2 text-warn">
            {reasonSentence(status.sessions.items.find((s) => s.reason_code)?.reason_code)}
          </span>
        )}
      </footer>
    </div>
  )
}
