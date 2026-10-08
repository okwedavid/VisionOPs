import { createContext, useContext } from 'react'

export type ToastTone = 'info' | 'ok' | 'warn' | 'crit'

export interface Toast {
  id: number
  tone: ToastTone
  title: string
  detail?: string
}

export interface ToastApi {
  push: (toast: Omit<Toast, 'id'>) => void
  dismiss: (id: number) => void
}

/**
 * Context and hook live apart from the provider component so that
 * `Toast.tsx` exports only a component — which keeps React Fast Refresh working
 * during development.
 */
export const ToastContext = createContext<ToastApi | null>(null)

export const TOAST_TONE: Record<ToastTone, string> = {
  info: 'border-info/50 bg-info/15 text-info',
  ok: 'border-ok/50 bg-ok/15 text-ok',
  warn: 'border-warn/50 bg-warn/15 text-warn',
  crit: 'border-crit/50 bg-crit/15 text-crit',
}

export function useToast(): ToastApi {
  const context = useContext(ToastContext)
  if (!context) throw new Error('useToast must be used inside <ToastProvider>')
  return context
}
