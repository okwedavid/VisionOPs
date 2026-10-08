import { useCallback, useState, type ReactNode } from 'react'

import { Button, Card, Field, TextInput } from '@/components/ui'
import { setApiKey } from '@/lib/api'
import { useHasApiKey } from '@/hooks/useApi'

/**
 * Gate shown until an API key is entered.
 *
 * The key is user-entered, held in `sessionStorage` only, and never compiled
 * into the bundle. This is deliberate: see docs/FRONTEND.md §8.
 *
 * It subscribes to key changes rather than reading storage once, so a key the
 * server rejects drops back to this gate instead of leaving a page of
 * plausible-looking zeroes.
 */
export function RequireApiKey({ children }: { children: ReactNode }) {
  const connected = useHasApiKey()
  const connect = useCallback(() => {
    // `useHasApiKey` re-reads via the listener `setApiKey` fires.
  }, [])
  if (connected) return <>{children}</>
  return <ApiKeyGate onSubmit={connect} />
}

export function ApiKeyGate({ onSubmit }: { onSubmit: (key: string) => void }) {
  return (
    <div className="mx-auto max-w-md py-10">
      <Card title="Connect to VisionOps" subtitle="This deployment requires an API key.">
        <KeyForm onSubmit={onSubmit} />
        <p className="mt-4 text-xs leading-relaxed text-ink-3">
          Create one on the server with <code className="font-mono text-ink-2">visionops key add</code> (or{' '}
          <code className="font-mono text-ink-2">make env</code>). The key is stored in this browser tab only
          and is cleared when you close it.
        </p>
      </Card>
    </div>
  )
}

export function KeyForm({ onSubmit }: { onSubmit: (key: string) => void }) {
  const [value, setValue] = useState('')
  const [error, setError] = useState<string | null>(null)

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    const trimmed = value.trim()
    if (!/^vo_[0-9a-f]{12}_[A-Za-z0-9_-]{32,}$/.test(trimmed)) {
      setError('That does not look like a VisionOps key. Expected vo_<id>_<secret>.')
      return
    }
    setError(null)
    setApiKey(trimmed)
    onSubmit(trimmed)
  }

  return (
    <form onSubmit={submit} className="space-y-3">
      <Field label="API key" error={error ?? undefined} htmlFor="api-key">
        <TextInput
          id="api-key"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="vo_…"
          autoComplete="off"
          spellCheck={false}
          className="font-mono"
        />
      </Field>
      <Button type="submit" variant="primary" className="w-full">
        Connect
      </Button>
    </form>
  )
}
