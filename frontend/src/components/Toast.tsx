import { useCallback, useMemo, useState, type ReactNode } from 'react'

import { TOAST_TONE, ToastContext, type Toast, type ToastApi } from './toast-context'

/**
 * Transient notifications for new events and action results.
 *
 * `aria-live="polite"` so a screen reader announces a new zone event without
 * stealing focus from whatever the operator is doing.
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((toast) => toast.id !== id))
  }, [])

  const push = useCallback(
    (toast: Omit<Toast, 'id'>) => {
      const id = Date.now() + Math.floor(Math.random() * 1000)
      setToasts((current) => [...current.slice(-4), { ...toast, id }])
      window.setTimeout(() => dismiss(id), toast.tone === 'crit' ? 12_000 : 6000)
    },
    [dismiss],
  )

  const api = useMemo<ToastApi>(() => ({ push, dismiss }), [push, dismiss])

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div
        aria-live="polite"
        aria-atomic="false"
        className="pointer-events-none fixed bottom-3 right-3 z-50 flex w-[min(24rem,calc(100vw-1.5rem))] flex-col gap-2"
      >
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={`pointer-events-auto rounded-md border px-3 py-2 shadow-lg backdrop-blur ${TOAST_TONE[toast.tone]}`}
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <p className="text-sm font-medium">{toast.title}</p>
                {toast.detail && <p className="mt-0.5 text-xs text-ink-2">{toast.detail}</p>}
              </div>
              <button
                type="button"
                onClick={() => dismiss(toast.id)}
                className="shrink-0 text-ink-3 hover:text-ink"
                aria-label="Dismiss notification"
              >
                ✕
              </button>
            </div>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
