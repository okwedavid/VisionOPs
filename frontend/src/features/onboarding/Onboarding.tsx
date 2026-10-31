import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { Button, Card } from '@/components/ui'
import { COMPLETE_KEY, PROGRESS_KEY } from './session'

type TourStep = { title: string; path: string; target: string; body: string; action: string }

const steps: TourStep[] = [
  { title: 'Dashboard', path: '/', target: 'dashboard-status', body: 'Check camera, model, and event health before acting. Counts and charts come from the connected VisionOps server.', action: 'Look for anything marked degraded or unavailable.' },
  { title: 'Cameras', path: '/cameras', target: 'camera-workspace', body: 'Choose a camera and start its processing session. A local webcam preview alone does not mean the server is receiving frames.', action: 'Open a camera and review its source and session state.' },
  { title: 'Live monitoring', path: '/cameras', target: 'camera-video', body: 'The video is paired with server-confirmed detections, zones, and connection state.', action: 'Compare the source, AI, and live-update indicators.' },
  { title: 'Zones', path: '/zones', target: 'zones-page', body: 'Zones are saved areas on a camera. The pipeline evaluates tracked objects against the configured shape and class filter.', action: 'Create or review a monitored area.' },
  { title: 'Detection details', path: '/cameras', target: 'object-details', body: 'Select a live track to inspect its model label, confidence, temporary track reference, zone relationship, and observations in this view.', action: 'Track IDs are anonymous and limited to the current processing session.' },
  { title: 'Events', path: '/events', target: 'events-page', body: 'The event history contains recorded zone transitions from actual processing sessions.', action: 'Open an event to review its timestamp and available evidence.' },
  { title: 'Analytics', path: '/', target: 'dashboard-analytics', body: 'Summary metrics aggregate recorded events. Empty charts mean the system has not recorded matching activity in the selected period.', action: 'Use these summaries to spot operational patterns.' },
]

export function Onboarding() {
  const location = useLocation()
  const navigate = useNavigate()
  const [mode, setMode] = useState<'welcome' | 'tour' | 'closed'>(() => {
    try {
      if (sessionStorage.getItem(COMPLETE_KEY) === '1') return 'closed'
      const progress = JSON.parse(sessionStorage.getItem(PROGRESS_KEY) ?? 'null') as { mode?: string } | null
      return progress?.mode === 'tour' ? 'tour' : 'welcome'
    } catch { return 'welcome' }
  })
  const [index, setIndex] = useState(() => {
    try {
      const progress = JSON.parse(sessionStorage.getItem(PROGRESS_KEY) ?? 'null') as { index?: number } | null
      return typeof progress?.index === 'number' ? Math.max(0, Math.min(steps.length - 1, progress.index)) : 0
    } catch { return 0 }
  })
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const step = steps[index]

  useEffect(() => {
    try { sessionStorage.setItem(PROGRESS_KEY, JSON.stringify({ mode, index })) } catch { /* session storage may be disabled */ }
  }, [index, mode])

  useEffect(() => {
    if (mode !== 'closed') dialogRef.current?.focus()
  }, [mode])

  useEffect(() => {
    if (mode !== 'tour' || !step) return
    if (location.pathname !== step.path) navigate(step.path)
    const applyHighlight = () => {
      document.querySelectorAll('.tour-highlight').forEach((element) => element.classList.remove('tour-highlight'))
      const target = document.querySelector(`[data-tour="${step.target}"]`)
      if (!target) return false
      target.classList.add('tour-highlight')
      target.scrollIntoView({ block: 'center', behavior: 'smooth' })
      return true
    }
    const timer = window.setTimeout(() => { applyHighlight() }, 100)
    const observer = new MutationObserver(() => { if (applyHighlight()) observer.disconnect() })
    observer.observe(document.body, { childList: true, subtree: true })
    return () => { window.clearTimeout(timer); observer.disconnect() }
  }, [location.pathname, mode, navigate, step])

  useEffect(() => () => {
    document.querySelectorAll('.tour-highlight').forEach((element) => element.classList.remove('tour-highlight'))
  }, [])

  const close = useCallback(() => {
    try { sessionStorage.setItem(COMPLETE_KEY, '1') } catch { /* session storage may be disabled */ }
    document.querySelectorAll('.tour-highlight').forEach((element) => element.classList.remove('tour-highlight'))
    setMode('closed')
  }, [])

  const onDialogKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') { event.preventDefault(); close(); return }
    if (event.key !== 'Tab') return
    const focusable = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), [tabindex]:not([tabindex="-1"])'))
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
  }

  if (mode === 'closed') return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center overflow-y-auto bg-black/75 p-4">
      <div ref={dialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="onboarding-title" onKeyDown={onDialogKeyDown} className="w-full max-w-xl outline-none">
      <Card className="w-full max-w-xl border-accent/40 shadow-2xl shadow-black/40" padded>
        {mode === 'welcome' ? (
          <>
            <p className="text-xs font-semibold uppercase tracking-[0.2em] text-accent">VisionOps · operator guide</p>
            <h2 id="onboarding-title" className="mt-3 text-2xl font-semibold">Camera footage, turned into actionable intelligence.</h2>
            <p className="mt-3 text-sm leading-6 text-ink-2">VisionOps analyzes supported objects in camera feeds, evaluates configured zones, and records relevant activity as reviewable events.</p>
            <ul className="mt-4 grid gap-2 text-sm text-ink-2 sm:grid-cols-2">
              <li className="rounded-md bg-surface-2 p-3">Reduce time spent watching every feed.</li>
              <li className="rounded-md bg-surface-2 p-3">See important activity and zone transitions.</li>
              <li className="rounded-md bg-surface-2 p-3">Review event records with camera context.</li>
              <li className="rounded-md bg-surface-2 p-3">Keep video processing and identities separate.</li>
            </ul>
            <div className="mt-6 flex flex-wrap justify-end gap-2">
              <Button variant="ghost" onClick={() => { navigate('/'); close() }}>Explore dashboard</Button>
              <Button variant="primary" onClick={() => { setIndex(0); setMode('tour') }}>Start guided demo</Button>
            </div>
          </>
        ) : (
          <>
            <div className="flex items-center justify-between gap-3">
              <p className="text-xs font-semibold uppercase tracking-[0.2em] text-accent">Guided tour · {index + 1} of {steps.length}</p>
              <button className="btn-ghost px-2 py-1" onClick={close}>Skip tour</button>
            </div>
            <div className="mt-3 h-1.5 overflow-hidden rounded bg-surface-2" role="progressbar" aria-valuemin={1} aria-valuemax={steps.length} aria-valuenow={index + 1} aria-label="Tour progress">
              <div className="h-full bg-accent transition-all" style={{ width: `${((index + 1) / steps.length) * 100}%` }} />
            </div>
            <h2 id="onboarding-title" className="mt-4 text-xl font-semibold">{step?.title}</h2>
            <p className="mt-2 text-sm leading-6 text-ink-2">{step?.body}</p>
            <p className="mt-3 rounded-md border border-border bg-surface-2 p-3 text-sm">{step?.action}</p>
            <p className="mt-3 text-xs text-ink-3">Synthetic demo footage is labelled as DEMO. Live detections only appear when the server confirms inference.</p>
            <div className="mt-5 flex justify-between gap-2">
              <Button onClick={() => setIndex((value) => Math.max(0, value - 1))} disabled={index === 0}>Back</Button>
              {index === steps.length - 1 ? <Button variant="primary" onClick={close}>Finish</Button> : <Button variant="primary" onClick={() => setIndex((value) => Math.min(steps.length - 1, value + 1))}>Next</Button>}
            </div>
          </>
        )}
      </Card>
      </div>
    </div>
  )
}

