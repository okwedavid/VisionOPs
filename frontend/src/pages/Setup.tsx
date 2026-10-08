import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { Banner, Button, Card, Field, Select, TextInput } from '@/components/ui'
import { ApiKeyGate, KeyForm } from '@/pages/RequireApiKey'
import { useToast } from '@/components/toast-context'
import { api, getApiKey, setApiKey } from '@/lib/api'
import {
  useCreateCamera,
  useCreateSite,
  useCreateSource,
  useModelInfo,
  useStartSession,
  useSystemStatus,
} from '@/hooks/useApi'
import type { SourceKind } from '@/types/api'
import { cx } from '@/lib/cx'

/**
 * First-run wizard: key → site → camera → source → zone → start.
 *
 * Deliberately linear and short. Everything here is also reachable later from
 * the Cameras and Zones pages, so nothing is trapped in the wizard.
 */
export function Setup() {
  const toast = useToast()
  const navigate = useNavigate()
  const [key, setKey] = useState<string | null>(getApiKey())
  const [step, setStep] = useState(1)
  const [siteName, setSiteName] = useState('Main site')
  const [cameraName, setCameraName] = useState('')
  const [siteId, setSiteId] = useState<string | null>(null)
  const [cameraId, setCameraId] = useState<string | null>(null)
  const [sourceKind, setSourceKind] = useState<SourceKind>('browser')
  const [target, setTarget] = useState('')
  const [rtspUser, setRtspUser] = useState('')
  const [rtspPassword, setRtspPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const createSite = useCreateSite()
  const createCamera = useCreateCamera()
  const createSource = useCreateSource()
  const startSession = useStartSession()
  const model = useModelInfo(Boolean(key))
  const status = useSystemStatus(Boolean(key))

  if (!key) {
    return (
      <ApiKeyGate
        onSubmit={(value) => {
          setKey(value)
          toast.push({ tone: 'ok', title: 'Connected', detail: 'Now create a camera.' })
        }}
      />
    )
  }

  const run = async (action: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    try {
      await action()
    } catch (cause) {
      const message = (cause as Error).message
      setError(message)
      toast.push({ tone: 'crit', title: 'Step failed', detail: message })
    } finally {
      setBusy(false)
    }
  }

  const modelReady = (model.data?.state ?? 'unknown') === 'ready'

  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Set up VisionOps</h1>
        <p className="mt-0.5 text-sm text-ink-2">
          Four steps to your first restricted-zone event. You can change any of it later.
        </p>
      </header>

      {!modelReady && (
        <Banner tone="warn" title="No detection model yet">
          {status.data?.model.detail ?? model.data?.error ?? 'VisionOps cannot detect anything until a model is installed.'}{' '}
          Run <code className="font-mono">make model</code> on the server, then reload this page. You can still
          finish the setup — detection starts working as soon as the model is present.
        </Banner>
      )}

      {error && (
        <Banner tone="crit" title="Something went wrong" onDismiss={() => setError(null)}>
          {error}
        </Banner>
      )}

      <ol className="flex flex-wrap gap-2 text-xs">
        {['API key', 'Site', 'Camera', 'Source', 'Zone', 'Start'].map((label, index) => {
          const number = index + 1
          return (
            <li
              key={label}
              className={cx(
                'rounded-full border px-2.5 py-1',
                number === step
                  ? 'border-accent bg-accent/12 text-accent'
                  : number < step
                    ? 'border-ok/40 bg-ok/10 text-ok'
                    : 'border-border text-ink-3',
              )}
            >
              {number}. {label}
            </li>
          )
        })}
      </ol>

      {step === 1 && (
        <Card title="1 · API key" subtitle="This browser needs permission to talk to the API.">
          <KeyForm
            onSubmit={() => {
              setStep(2)
            }}
          />
          <p className="mt-3 text-xs text-ink-3">
            Already connected?{' '}
            <button type="button" className="underline" onClick={() => setStep(2)}>
              Skip to the next step
            </button>
          </p>
        </Card>
      )}

      {step === 2 && (
        <Card title="2 · Site" subtitle="A physical location that groups your cameras.">
          <div className="space-y-3">
            <Field label="Site name" htmlFor="site-name">
              <TextInput id="site-name" value={siteName} onChange={(event) => setSiteName(event.target.value)} />
            </Field>
            <Button
              variant="primary"
              loading={busy}
              onClick={() =>
                void run(async () => {
                  const site = await createSite.mutateAsync({ name: siteName.trim() || 'Main site' })
                  setSiteId(site.id)
                  setStep(3)
                  toast.push({ tone: 'ok', title: 'Site created', detail: site.name })
                })
              }
            >
              Create site
            </Button>
          </div>
        </Card>
      )}

      {step === 3 && (
        <Card title="3 · Camera" subtitle="A logical camera: a name, a location, and where its video comes from.">
          <div className="space-y-3">
            <Field label="Camera name" hint="Where it is, e.g. “Loading bay” or “Front door”." htmlFor="camera-name">
              <TextInput
                id="camera-name"
                value={cameraName}
                onChange={(event) => setCameraName(event.target.value)}
                placeholder="Loading bay"
              />
            </Field>
            <Button
              variant="primary"
              loading={busy}
              onClick={() =>
                void run(async () => {
                  const camera = await createCamera.mutateAsync({
                    name: cameraName.trim() || 'Camera 1',
                    site_id: siteId!,
                  })
                  setCameraId(camera.id)
                  setStep(4)
                  toast.push({ tone: 'ok', title: 'Camera created', detail: camera.name })
                })
              }
            >
              Create camera
            </Button>
          </div>
        </Card>
      )}

      {step === 4 && (
        <Card title="4 · Video source" subtitle="Where the frames come from.">
          <div className="space-y-3">
            <Field label="Source kind" htmlFor="source-kind">
              <Select
                id="source-kind"
                value={sourceKind}
                onChange={(event) => setSourceKind(event.target.value as SourceKind)}
              >
                <option value="browser">Browser webcam — uses this computer&apos;s camera</option>
                <option value="file">Video file — good for demos and testing</option>
                <option value="device">Local camera — a V4L2 device index on the server</option>
                <option value="rtsp">RTSP camera — a network camera</option>
              </Select>
            </Field>

            {sourceKind !== 'browser' && (
              <Field
                label={sourceKind === 'file' ? 'File path' : sourceKind === 'device' ? 'Device index' : 'RTSP URL'}
                hint={
                  sourceKind === 'file'
                    ? 'Relative paths are resolved inside the server media root.'
                    : sourceKind === 'device'
                      ? '0 is usually the first camera, e.g. /dev/video0.'
                      : 'Do not put credentials in the URL — use the fields below.'
                }
                htmlFor="source-target"
              >
                <TextInput
                  id="source-target"
                  value={target}
                  onChange={(event) => setTarget(event.target.value)}
                  placeholder={
                    sourceKind === 'file'
                      ? 'fixtures/zone-walkthrough.mp4'
                      : sourceKind === 'device'
                        ? '0'
                        : 'rtsp://camera.local:554/stream1'
                  }
                  className="font-mono"
                />
              </Field>
            )}

            {sourceKind === 'rtsp' && (
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="RTSP username" hint="Stored encrypted, never returned by the API." htmlFor="rtsp-user">
                  <TextInput id="rtsp-user" value={rtspUser} onChange={(event) => setRtspUser(event.target.value)} autoComplete="off" />
                </Field>
                <Field label="RTSP password" htmlFor="rtsp-password">
                  <TextInput
                    id="rtsp-password"
                    type="password"
                    value={rtspPassword}
                    onChange={(event) => setRtspPassword(event.target.value)}
                    autoComplete="new-password"
                  />
                </Field>
              </div>
            )}

            <Button
              variant="primary"
              loading={busy}
              onClick={() =>
                void run(async () => {
                  await createSource.mutateAsync({
                    cameraId: cameraId!,
                    body: {
                      name: 'primary',
                      kind: sourceKind,
                      target: sourceKind === 'browser' ? '' : target.trim(),
                      is_primary: true,
                      options: sourceKind === 'file' ? { loop: true } : {},
                      credentials:
                        sourceKind === 'rtsp' && rtspUser
                          ? { username: rtspUser, password: rtspPassword }
                          : undefined,
                    },
                  })
                  setStep(5)
                  toast.push({ tone: 'ok', title: 'Source added' })
                })
              }
            >
              Add source
            </Button>
          </div>
        </Card>
      )}

      {step === 5 && (
        <Card title="5 · Your first restricted zone" subtitle="Where should nobody be?">
          <p className="text-sm text-ink-2">
            The next screen lets you drag a rectangle directly on the video, which is far easier than typing
            coordinates. The wizard takes you there.
          </p>
          <div className="mt-4 flex flex-wrap gap-2">
            <Button variant="primary" onClick={() => navigate(`/cameras/${cameraId}`)}>
              Draw my zone
            </Button>
            <Button
              onClick={() =>
                void run(async () => {
                  await api.createZone({
                    camera_id: cameraId!,
                    name: 'restricted',
                    points: [
                      [0.34, 0.3],
                      [0.7, 0.3],
                      [0.7, 0.92],
                      [0.34, 0.92],
                    ],
                    severity: 'warning',
                  })
                  setStep(6)
                })
              }
            >
              Use a sensible default rectangle
            </Button>
          </div>
        </Card>
      )}

      {step === 6 && (
        <Card title="6 · Start processing" subtitle="Detection begins and events are recorded.">
          <p className="text-sm text-ink-2">
            {modelReady
              ? 'Everything is ready. Start the session and watch for the first event.'
              : 'The configuration is ready, but no detection model is installed, so no events will be produced yet. Install the model on the server, then start.'}
          </p>
          <div className="mt-4 flex flex-wrap gap-2">
            <Button
              variant="primary"
              loading={busy}
              onClick={() =>
                void run(async () => {
                  await startSession.mutateAsync({ cameraId: cameraId! })
                  toast.push({ tone: 'ok', title: 'Session started', detail: 'Watch the live view for events.' })
                  navigate(`/cameras/${cameraId}`)
                })
              }
            >
              Start session
            </Button>
            <Button onClick={() => navigate(`/cameras/${cameraId}`)}>Go to the camera</Button>
          </div>
        </Card>
      )}

      <Card title="Prefer the command line?">
        <p className="text-sm text-ink-2">
          One command seeds a complete demo wired to the bundled synthetic fixture video — no camera, no model
          download, no network:
        </p>
        <pre className="mt-2 overflow-x-auto rounded bg-surface-2 p-3 text-xs text-ink-2">make demo &amp;&amp; make dev</pre>
        <p className="mt-2 text-xs text-ink-3">
          The fixture is clearly labelled synthetic: it demonstrates the pipeline, zone logic and event flow,
          not detection accuracy.
        </p>
      </Card>

      <button
        type="button"
        className="text-xs text-ink-3 underline"
        onClick={() => {
          setApiKey(null)
          setKey(null)
          setStep(1)
        }}
      >
        Use a different API key
      </button>
    </div>
  )
}
