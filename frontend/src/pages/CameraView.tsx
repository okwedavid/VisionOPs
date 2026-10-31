import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { Badge, Banner, Button, Card, EmptyState, Field, Loading, Modal, Select, StatusDot, TextInput } from '@/components/ui'
import { useToast } from '@/components/toast-context'
import { LiveOverlay, type OverlayZone } from '@/features/live-overlay/LiveOverlay'
import { useWebcam, webcamStateMessage } from '@/features/webcam/useWebcam'
import { getApiKey, ingestSocketUrl } from '@/lib/api'
import {
  clamp01,
  milliseconds,
  number,
  reasonSentence,
  relativeTime,
  rectangleFromDrag,
  seconds,
  severityClass,
} from '@/lib/format'
import {
  useCamera,
  useCameras,
  useCreateZone,
  useDeleteZone,
  useLiveSession,
  useModelInfo,
  useSources,
  useStartSession,
  useStopSession,
  useSystemStatus,
  useUpdateZone,
  useZones,
} from '@/hooks/useApi'
import type { NormalizedPolygon, Zone, ZoneSeverity } from '@/types/api'
import { cx } from '@/lib/cx'
import { interpretTrack, modelClassId } from '@/lib/interpretation'
import type { LiveTrack, LiveZone } from '@/types/api'

const ZONE_COLORS = ['#f97316', '#22c55e', '#3b82f6', '#ef4444', '#a855f7', '#eab308']

export function CameraView() {
  const { cameraId } = useParams<{ cameraId: string }>()
  const navigate = useNavigate()
  const toast = useToast()
  const cameras = useCameras()
  const [selectedId, setSelectedId] = useState<string | undefined>(cameraId)

  useEffect(() => {
    if (cameraId) setSelectedId(cameraId)
  }, [cameraId])

  useEffect(() => {
    if (!selectedId && cameras.data && cameras.data.length > 0) {
      const first = cameras.data[0]!
      setSelectedId(first.id)
      navigate(`/cameras/${first.id}`, { replace: true })
    }
  }, [cameras.data, selectedId, navigate])

  if (cameras.isLoading) return <Loading label="Loading cameras…" />

  // A URL that names a camera must say *that* camera is missing. Showing
  // "no cameras yet" here would send an operator to create a second camera when
  // the one they asked for simply does not exist (or is not theirs).
  if (cameraId && cameras.data && !cameras.data.some((camera) => camera.id === cameraId)) {
    return <EmptyState title="Camera not found" action={<Link to="/cameras" className="btn-secondary">All cameras</Link>} />
  }

  if (!cameras.data || cameras.data.length === 0) {
    return (
      <div className="space-y-4">
        {/* Keep the page heading in the empty state: a page with no <h1> is
            unusable by a screen reader, and this is the very first screen a
            new operator sees. */}
        <h1 className="text-xl font-semibold tracking-tight">Cameras</h1>
        <div data-tour="camera-workspace">
        <EmptyState
          title="No cameras yet"
          icon="🎥"
          action={
            <Link to="/setup" className="btn-primary">
              Run the setup wizard
            </Link>
          }
        >
          VisionOps needs one camera to start. The wizard creates a site, a camera and its first video source
          in about a minute.
        </EmptyState>
        </div>
        <div data-tour="camera-video" className="rounded-lg border border-border bg-black/30 p-6 text-center text-sm text-ink-2">
          A live preview appears here after a camera source is configured and a session starts.
        </div>
        <div data-tour="object-details"><Card title="Live detections" subtitle="Available after a camera session starts">
          Server-confirmed objects, confidence values, model class IDs and temporary track references appear here. No sample detections are presented as live data.
        </Card></div>
      </div>
    )
  }

  if (!selectedId) return <Loading />

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold tracking-tight">Camera</h1>
        <Select
          aria-label="Choose a camera"
          value={selectedId}
          onChange={(event) => {
            setSelectedId(event.target.value)
            navigate(`/cameras/${event.target.value}`)
          }}
          className="max-w-xs"
        >
          {cameras.data.map((camera) => (
            <option key={camera.id} value={camera.id}>
              {camera.name}
              {camera.site_name ? ` — ${camera.site_name}` : ''}
            </option>
          ))}
        </Select>
        <Link to="/zones" className="btn-ghost ml-auto">
          Manage zones
        </Link>
      </header>

      <CameraWorkspace cameraId={selectedId} onToast={toast.push} />
    </div>
  )
}

function goTo(path: string): void {
  // Plain navigation so a click works whether or not a router is mounted in a
  // test harness; the router still handles back/forward normally.
  window.location.assign(path)
}

function CameraWorkspace({
  cameraId,
  onToast,
}: {
  cameraId: string
  onToast: (toast: { tone: 'info' | 'ok' | 'warn' | 'crit'; title: string; detail?: string }) => void
}) {
  const camera = useCamera(cameraId)
  const sources = useSources(cameraId)
  const zones = useZones(cameraId)
  const status = useSystemStatus()
  const modelInfo = useModelInfo()
  const startSession = useStartSession()
  const stopSession = useStopSession()
  const createZone = useCreateZone()
  const updateZone = useUpdateZone()
  const deleteZone = useDeleteZone()

  const [sessionId, setSessionId] = useState<string | null>(null)
  const [sessionError, setSessionError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<NormalizedPolygon | null>(null)
  const [selectedTrackId, setSelectedTrackId] = useState<number | null>(null)
  const [trackObservations, setTrackObservations] = useState<Record<number, { track: LiveTrack; first: number; last: number }>>({})
  const [zoneModal, setZoneModal] = useState<{ mode: 'create'; polygon: NormalizedPolygon } | { mode: 'edit'; zone: Zone } | null>(null)

  const live = useLiveSession(sessionId)
  const webcam = useWebcam()
  const { start: startWebcam, deliver: deliverFrames, stop: stopWebcam } = webcam

  const session = status.data?.sessions.items.find((item) => item.camera_id === cameraId)
  const source = sources.data?.find((item) => item.is_primary) ?? sources.data?.[0]
  const sourceId = source?.id
  const isRunning = Boolean(session) && session?.state !== 'stopped'
  const isBrowserSource = source?.kind === 'browser'
  const modelReady = (status.data?.model.state ?? 'unknown') === 'ready'

  // Adopt the running session's id so the socket attaches to it.
  useEffect(() => {
    if (session && session.session_id !== sessionId) setSessionId(session.session_id)
  }, [session, sessionId])

  // Browser source: adopt a session started elsewhere (or from a previous page)
  // and keep its camera/ingest socket alive. The system-status payload does not
  // contain ingest_ws_url, so derive it from the session id instead of waiting
  // for a field the backend never sends.
  useEffect(() => {
    if (!isRunning || !isBrowserSource || !session?.session_id) return
    let cancelled = false
    const sessionIdForFrames = session.session_id
    const targetFps = session.target_fps

    void (async () => {
      const ok = await startWebcam()
      if (!ok || cancelled) return
      deliverFrames(ingestSocketUrl(sessionIdForFrames, targetFps, getApiKey()), targetFps)
    })()

    return () => {
      cancelled = true
      stopWebcam()
    }
  }, [isRunning, isBrowserSource, session?.session_id, session?.target_fps, startWebcam, deliverFrames, stopWebcam])

  const start = useCallback(async () => {
    setBusy(true)
    setSessionError(null)
    try {
      if (!sourceId) {
        setSessionError('This camera has no video source. Add a source before starting a session.')
        return
      }

      // Keep getUserMedia inside the click handler. Starting the camera after an
      // awaited HTTP request loses the user gesture some browsers require.
      if (isBrowserSource) {
        const cameraReady = await startWebcam()
        if (!cameraReady) {
          onToast({
            tone: 'crit',
            title: 'Camera could not start',
            detail: 'See the camera message above the video for what to fix.',
          })
          return
        }
      }

      const info = await startSession.mutateAsync({ cameraId, body: { source_id: sourceId } })
      setSessionId(info.session_id)
      if (isBrowserSource) {
        deliverFrames(ingestSocketUrl(info.session_id, info.target_fps, getApiKey()), info.target_fps)
      }
      onToast({ tone: 'ok', title: 'Session started', detail: `${info.camera_name} · ${info.source_kind} source` })
    } catch (error) {
      stopWebcam()
      const message = (error as Error).message
      setSessionError(message)
      onToast({ tone: 'crit', title: 'Could not start the session', detail: message })
    } finally {
      setBusy(false)
    }
  }, [
    cameraId,
    deliverFrames,
    isBrowserSource,
    onToast,
    sourceId,
    startSession,
    startWebcam,
    stopWebcam,
  ])

  const stop = useCallback(async () => {
    setBusy(true)
    try {
      await stopSession.mutateAsync(cameraId)
      setSessionId(null)
      stopWebcam()
      onToast({ tone: 'info', title: 'Session stopped' })
    } catch (error) {
      onToast({ tone: 'crit', title: 'Could not stop the session', detail: (error as Error).message })
    } finally {
      setBusy(false)
    }
  }, [cameraId, onToast, stopSession, stopWebcam])

  const saveZone = useCallback(
    async (input: { name: string; polygon: NormalizedPolygon; color: string; severity: ZoneSeverity }) => {
      try {
        if (zoneModal?.mode === 'edit') {
          await updateZone.mutateAsync({
            id: zoneModal.zone.id,
            body: { name: input.name, points: input.polygon, color: input.color, severity: input.severity },
          })
          onToast({ tone: 'ok', title: 'Zone updated' })
        } else {
          await createZone.mutateAsync({
            camera_id: cameraId,
            name: input.name,
            points: input.polygon,
            color: input.color,
            severity: input.severity,
          })
          onToast({ tone: 'ok', title: 'Zone created', detail: 'Entry and exit events will be generated.' })
        }
        setZoneModal(null)
        setDraft(null)
        setEditing(false)
      } catch (error) {
        onToast({ tone: 'crit', title: 'Could not save the zone', detail: (error as Error).message })
      }
    },
    [cameraId, createZone, onToast, updateZone, zoneModal],
  )

  const removeZone = useCallback(
    async (zone: Zone) => {
      try {
        await deleteZone.mutateAsync(zone.id)
        onToast({ tone: 'info', title: 'Zone deleted', detail: 'Historical events keep their zone name.' })
      } catch (error) {
        onToast({ tone: 'crit', title: 'Could not delete the zone', detail: (error as Error).message })
      }
    },
    [deleteZone, onToast],
  )

  const openZoneFromDraft = useCallback(
    (polygon: NormalizedPolygon) => setZoneModal({ mode: 'create', polygon }),
    [],
  )

  const liveEvents = live.events
  const totalInZone = Object.values(live.state?.stats.in_zone ?? session?.in_zone ?? {}).reduce((a, b) => a + b, 0)

  useEffect(() => {
    setSelectedTrackId(null)
    setTrackObservations({})
  }, [sessionId])

  useEffect(() => {
    if (!live.state) return
    const observedAt = live.state.server_time * 1000
    setTrackObservations((current) => {
      const next = { ...current }
      for (const track of live.state!.tracks) {
        const previous = next[track.id]
        next[track.id] = { track, first: previous?.first ?? observedAt, last: observedAt }
      }
      const ids = Object.keys(next)
      for (const id of ids.slice(0, Math.max(0, ids.length - 100))) delete next[Number(id)]
      return next
    })
  }, [live.state, live.lastMessageAt])

  const selectedObservation = selectedTrackId === null ? undefined : trackObservations[selectedTrackId]
  const selectedTrack = live.state?.tracks.find((track) => track.id === selectedTrackId) ?? selectedObservation?.track
  const selectedInterpretation = selectedTrack
    ? interpretTrack(selectedTrack, (live.state?.zones ?? []) as LiveZone[], live.events, camera.data?.name ?? 'camera')
    : null
  const classIdFor = (label: string) => modelClassId(modelInfo.data?.classes, label)

  if (camera.isLoading) return <Loading label="Loading camera…" />
  if (!camera.data) {
    return <EmptyState title="Camera not found" action={<Link to="/cameras" className="btn-secondary">All cameras</Link>} />
  }

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
      <div data-tour="camera-workspace" className="space-y-4">
        {sessionError && <Banner tone="crit" title="Could not start">{sessionError}</Banner>}

        {isBrowserSource && webcam.error && webcam.state !== 'live' && (
          <Banner
            tone={webcam.state === 'denied' ? 'warn' : 'crit'}
            title="Camera could not start"
            role="alert"
          >
            {webcam.error}
          </Banner>
        )}

        {session && session.state === 'degraded' && (
          <Banner tone="warn" title={`Source ${session.state}`} actions={<Button onClick={() => void start()}>Retry</Button>}>
            {reasonSentence(session.reason_code, session.detail)}
          </Banner>
        )}
        {session && session.state === 'failed' && (
          <Banner tone="crit" title="Source failed" role="alert">
            {reasonSentence(session.reason_code, session.detail)} — check System status for details.
          </Banner>
        )}
        {!modelReady && (
          <Banner tone="warn" title="Detection is unavailable">
            {status.data?.model.detail ?? 'No model is loaded, so no detections or events will be produced.'}{' '}
            The pipeline still runs, so the video and health remain visible.
          </Banner>
        )}
        {live.stale && isRunning && (
          <Banner tone="warn" title="Live updates are stale">
            The last state arrived {relativeTime(live.lastMessageAt)}. The overlay shows the last known
            state; history remains correct.
          </Banner>
        )}

        <Card padded={false} className="overflow-hidden">
          <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
            <Button
              variant={isRunning ? 'secondary' : 'primary'}
              onClick={() => (isRunning ? void stop() : void start())}
              loading={busy}
              disabled={!sourceId && !isRunning}
              title={!sourceId && !isRunning ? 'Add a video source before starting a session' : undefined}
            >
              {isRunning ? 'Stop session' : 'Start session'}
            </Button>
            <Button
              onClick={() => {
                setEditing((value) => !value)
                setDraft(null)
              }}
              disabled={!isRunning}
              title={isRunning ? 'Draw a zone on the video' : 'Start a session to draw zones'}
            >
              {editing ? 'Finish editing' : 'Draw zone'}
            </Button>
            <span className="ml-auto flex items-center gap-1.5 text-xs text-ink-2">
              <StatusDot state={isRunning ? (session?.state ?? 'running') : 'stopped'} />
              {isRunning ? (session?.state ?? 'running') : 'stopped'}
              {session && (
                <>
                  <span className="tnum">· {session.processed_fps.toFixed(1)} fps</span>
                  <span className="tnum">· {milliseconds(session.inference_ms)}</span>
                </>
              )}
            </span>
          </div>

          <VideoStage
            isBrowser={isBrowserSource}
            isRunning={isRunning}
            videoRef={webcam.videoRef}
            previewCameraId={cameraId}
            live={live.live}
            fallbackZones={(zones.data ?? []).map(toOverlayZone)}
            editing={editing}
            draft={draft}
            onDraftChange={setDraft}
            webcamState={webcam.state}
            webcamError={webcam.error}
            uploadError={webcam.uploadError}
            sentFrames={webcam.sentFrames}
            uploadState={webcam.uploadState}
            connection={live.connection}
            aiState={live.state
              ? live.state.ai.state === 'ready' && live.state.stats.frames_processed > 0
                ? 'active'
                : live.state.ai.state === 'ready' ? 'waiting for first frame' : live.state.ai.state
              : session?.state === 'running' ? 'waiting for server state' : 'inactive'}
            lastUpdate={live.lastMessageAt}
            selectedTrackId={selectedTrackId}
            dataTour="camera-video"
          />

          {editing && (
            <div className="flex flex-wrap items-center gap-2 border-t border-border px-3 py-2 text-xs text-ink-2">
              <span>Drag on the video to draw a rectangle.</span>
              {draft && draft.length >= 3 && (
                <>
                  <span className="tnum text-ink-3">
                    x {Math.round(Math.min(...draft.map((p) => p[0])) * 100)}–
                    {Math.round(Math.max(...draft.map((p) => p[0])) * 100)}%, y{' '}
                    {Math.round(Math.min(...draft.map((p) => p[1])) * 100)}–
                    {Math.round(Math.max(...draft.map((p) => p[1])) * 100)}%
                  </span>
                  <Button variant="primary" onClick={() => openZoneFromDraft(draft)}>
                    Save this zone
                  </Button>
                  <Button variant="ghost" onClick={() => setDraft(null)}>
                    Discard
                  </Button>
                </>
              )}
            </div>
          )}
        </Card>

        {isBrowserSource && !isRunning && (
          <Banner tone="info" title="Webcam source">
            Starting a session will ask for camera permission and stream frames to this server over your local
            network. Frames are never written to disk and never leave your machine.
          </Banner>
        )}

        {!source && (
          <Banner
            tone="warn"
            title="This camera has no video source"
            actions={
              <Button onClick={() => goTo('/setup')}>Add a source</Button>
            }
          >
            A session needs a source: a browser webcam, a local device, a video file or an RTSP camera.
          </Banner>
        )}
      </div>

      <div className="space-y-4">
        <div data-tour="object-details">
        <Card title="Live detections" subtitle={live.state ? `${live.state.tracks.length} current track(s) · references end with this session` : 'Waiting for server-confirmed inference'} padded={false}>
          {live.state?.tracks.length ? (
            <ul className="max-h-48 divide-y divide-border overflow-y-auto">
              {live.state.tracks.map((track) => (
                <li key={track.id}>
                  <button type="button" className={`w-full px-3 py-2 text-left hover:bg-surface-2 ${selectedTrackId === track.id ? 'bg-surface-2' : ''}`} onClick={() => setSelectedTrackId(track.id)}>
                    <span className="flex items-center justify-between gap-2 text-sm"><span className="font-medium">{track.label} · Track #{track.id}</span><span className="tnum text-ink-2">{(track.confidence * 100).toFixed(0)}%</span></span>
                    <span className="mt-0.5 block text-xs text-ink-3">Class ID {classIdFor(track.label) ?? 'unavailable'} · seen {track.age} frame(s)</span>
                  </button>
                </li>
              ))}
            </ul>
          ) : <p className="px-3 py-4 text-sm text-ink-3">{live.state?.ai.state === 'ready' ? 'No objects in the latest server inference result.' : 'Start a camera session to receive actual detections.'}</p>}
          {selectedTrack && selectedInterpretation && (
            <div className="border-t border-border p-3">
              <h3 className="text-sm font-semibold">{selectedTrack.label} · Track #{selectedTrack.id}</h3>
              <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
                <Metric label="Confidence" value={`${(selectedTrack.confidence * 100).toFixed(1)}%`} />
                <Metric label="Model class ID" value={String(classIdFor(selectedTrack.label) ?? 'unavailable')} />
                <Metric label="Camera" value={camera.data.name} />
                <Metric label="Zone" value={selectedInterpretation.zone?.name ?? selectedInterpretation.status} />
                <Metric label="First observed here" value={selectedObservation ? new Date(selectedObservation.first).toLocaleTimeString() : 'current view'} />
                <Metric label="Last observed here" value={selectedObservation ? new Date(selectedObservation.last).toLocaleTimeString() : 'current view'} />
              </dl>
              <p className="mt-3 text-xs leading-5 text-ink-2">{selectedInterpretation.explanation}</p>
              <p className="mt-2 text-[11px] text-ink-3">Track references are anonymous and temporary. Observation times are held only in this browser view.</p>
              {live.events.filter((event) => event.track_id === selectedTrack.id).map((event, index) => <p key={event.id ?? `${event.type}-${index}`} className="mt-2 text-xs text-warn">{event.type.replace(/_/g, ' ').toLowerCase()} · {event.zone_name ?? 'zone'} · {relativeTime(new Date(event.occurred_at * 1000))}</p>)}
              <p className="mt-2 break-all text-[11px] text-ink-3">Box: [{selectedTrack.box.map((value) => value.toFixed(3)).join(', ')}] · normalized frame coordinates</p>
            </div>
          )}
        </Card>
        </div>
        <Card title="Session">
          <dl className="grid grid-cols-2 gap-x-3 gap-y-2 text-sm">
            <Metric label="State" value={isRunning ? (session?.state ?? '—') : 'stopped'} />
            <Metric label="Source" value={source ? `${source.kind}` : 'none'} />
            <Metric label="FPS" value={session ? session.processed_fps.toFixed(2) : '—'} />
            <Metric label="Frame age" value={milliseconds(session?.frame_age_ms)} />
            <Metric label="Inference" value={milliseconds(session?.inference_ms)} />
            <Metric label="Tracks" value={session ? number(session.active_tracks) : '—'} />
            <Metric label="In zones" value={isRunning ? number(totalInZone) : '—'} />
            <Metric label="Events" value={session ? number(session.events_emitted) : '—'} />
            <Metric label="Suppressed" value={session ? number(session.events_suppressed) : '—'} />
            <Metric label="Uptime" value={session ? seconds(session.uptime_seconds) : '—'} />
          </dl>
          {isBrowserSource && isRunning && (
            <p className="mt-3 text-xs text-ink-3">
              Webcam: {webcamStateMessage(webcam.state)} · {number(webcam.sentFrames)} frames delivered
            </p>
          )}
        </Card>

        <Card
          title="Zones"
          subtitle={`${zones.data?.length ?? 0} configured`}
          actions={
            <Button onClick={() => goTo(`/zones/${cameraId}`)}>Manage</Button>
          }
          padded={false}
        >
          {(zones.data?.length ?? 0) === 0 ? (
            <div className="p-4">
              <EmptyState title="No zones yet" icon="▭">
                A zone is a restricted area. When a tracked person&apos;s feet cross into it, VisionOps
                records an event with a timestamp, the track and the zone.
              </EmptyState>
            </div>
          ) : (
            <ul className="divide-y divide-border">
              {(zones.data ?? []).map((zone) => {
                const occupancy = live.state?.zones.find((item) => item.id === zone.id)?.occupancy ?? 0
                return (
                  <li key={zone.id} className="flex items-center gap-2 px-3 py-2">
                    <span className="size-2.5 shrink-0 rounded-sm" style={{ background: zone.color }} aria-hidden="true" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium">{zone.name}</span>
                      <span className="block text-[11px] text-ink-3">
                        {zone.severity} · dwell {zone.min_dwell_seconds}s · cooldown {zone.cooldown_seconds}s
                      </span>
                    </span>
                    {occupancy > 0 && <Badge className="border-warn/40 bg-warn/12 text-warn">{occupancy} in</Badge>}
                    {!zone.enabled && <Badge className="border-border bg-surface-2 text-ink-3">off</Badge>}
                    <button
                      type="button"
                      className="btn-ghost px-1.5 py-1"
                      aria-label={`Delete zone ${zone.name}`}
                      onClick={() => void removeZone(zone)}
                    >
                      ✕
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </Card>

        <Card
          title="Live events"
          subtitle={isRunning ? 'From this session' : 'Start a session to see events'}
          padded={false}
        >
          {liveEvents.length === 0 ? (
            <p className="px-4 py-6 text-center text-sm text-ink-3">
              {isRunning ? 'No zone events yet in this session.' : 'Not running.'}
            </p>
          ) : (
            <ul className="max-h-72 divide-y divide-border overflow-y-auto">
              {liveEvents.map((event, index) => (
                <li key={`${event.id ?? index}`} className="px-3 py-2">
                  <div className="flex items-center gap-2">
                    <Badge className={severityClass(event.severity)}>{event.severity}</Badge>
                    <span className="truncate text-sm font-medium">{event.type.replace(/_/g, ' ').toLowerCase()}</span>
                  </div>
                  <p className="mt-0.5 text-xs text-ink-3">
                    {event.zone_name ?? 'no zone'} · {relativeTime(new Date(event.occurred_at * 1000))}
                    {typeof event.metadata.dwell_seconds === 'number'
                      ? ` · dwell ${seconds(event.metadata.dwell_seconds as number)}`
                      : ''}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      {zoneModal && (
        <ZoneModal
          initial={
            zoneModal.mode === 'edit'
              ? {
                  name: zoneModal.zone.name,
                  polygon: zoneModal.zone.points as NormalizedPolygon,
                  color: zoneModal.zone.color,
                  severity: zoneModal.zone.severity,
                }
              : { name: 'restricted', polygon: zoneModal.polygon, color: ZONE_COLORS[0]!, severity: 'warning' as ZoneSeverity }
          }
          busy={createZone.isPending || updateZone.isPending}
          onCancel={() => {
            setZoneModal(null)
            setDraft(null)
          }}
          onSave={saveZone}
        />
      )}
    </div>
  )
}

/** Adapt a persisted zone for the overlay, which prefers the live shape. */
function toOverlayZone(zone: Zone) {
  return {
    id: zone.id,
    name: zone.name,
    polygon: zone.points as NormalizedPolygon,
    color: zone.color,
    severity: zone.severity,
    enabled: zone.enabled,
  }
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-ink-3">{label}</dt>
      <dd className="tnum text-sm text-ink">{value}</dd>
    </div>
  )
}

/** Video element + overlay, with drag-to-draw when the editor is active. */
function VideoStage({
  isBrowser,
  isRunning,
  videoRef,
  previewCameraId,
  live,
  fallbackZones,
  editing,
  draft,
  onDraftChange,
  webcamState,
  webcamError,
  uploadError,
  sentFrames,
  uploadState,
  connection,
  aiState,
  lastUpdate,
  selectedTrackId,
  dataTour,
}: {
  isBrowser: boolean
  isRunning: boolean
  videoRef: React.RefObject<HTMLVideoElement | null>
  previewCameraId: string
  live: ReturnType<typeof useLiveSession>['live']
  fallbackZones: OverlayZone[]
  editing: boolean
  draft: NormalizedPolygon | null
  onDraftChange: (polygon: NormalizedPolygon | null) => void
  webcamState: string
  webcamError: string | null
  uploadError: string | null
  sentFrames: number
  uploadState: string
  connection: string
  aiState: string
  lastUpdate: number | null
  selectedTrackId: number | null
  dataTour: string
}) {
  const stageRef = useRef<HTMLDivElement | null>(null)
  const dragStart = useRef<{ x: number; y: number } | null>(null)

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!editing) return
    const rect = stageRef.current?.getBoundingClientRect()
    if (!rect) return
    dragStart.current = { x: event.clientX - rect.left, y: event.clientY - rect.top }
    ;(event.target as Element).setPointerCapture?.(event.pointerId)
  }

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!editing || !dragStart.current) return
    const rect = stageRef.current?.getBoundingClientRect()
    if (!rect) return
    onDraftChange(
      rectangleFromDrag(
        dragStart.current.x,
        dragStart.current.y,
        event.clientX - rect.left,
        event.clientY - rect.top,
        rect.width,
        rect.height,
      ),
    )
  }

  const onPointerUp = () => {
    if (!editing) return
    dragStart.current = null
  }

  const noVideo = !isRunning

  return (
    <div
      ref={stageRef}
      data-tour={dataTour}
      className={cx('relative aspect-video w-full overflow-hidden bg-black', editing && 'cursor-crosshair')}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
    >
      {isBrowser ? (
        <video ref={videoRef} muted playsInline className="size-full object-contain" aria-label="Webcam preview" />
      ) : (
        <img
          src={`/api/v1/cameras/${previewCameraId}/preview.mjpg`}
          alt="Camera preview with detection overlay"
          className="size-full object-contain"
          onError={(event) => {
            // No MJPEG while stopped: say so rather than showing a broken image.
            ;(event.currentTarget as HTMLImageElement).style.visibility = 'hidden'
          }}
        />
      )}

      {isRunning && (
        <LiveOverlay
          live={live}
          fallbackZones={fallbackZones}
          editing={editing}
          draft={draft}
          selectedTrackId={selectedTrackId}
        />
      )}

      {noVideo && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-bg/80 p-6 text-center">
          <p className="text-sm font-medium text-ink">Session stopped</p>
          <p className="max-w-sm text-xs text-ink-2">
            Press <strong>Start session</strong> to begin detection. The video preview, overlays and zone
            events only exist while a session is running.
          </p>
        </div>
      )}

      {isBrowser && isRunning && webcamState !== 'live' && (
        <div className="absolute inset-x-0 bottom-0 bg-bg/90 p-3 text-xs text-warn">
          {webcamError ?? 'Starting the camera…'}
        </div>
      )}

      {isBrowser && isRunning && webcamState === 'live' && uploadError && (
        <div className="absolute inset-x-0 bottom-0 bg-bg/90 p-3 text-xs text-warn">{uploadError}</div>
      )}

      {isBrowser && (
        <div className="absolute right-2 top-2 flex flex-wrap justify-end gap-1 rounded bg-bg/85 p-1.5 text-[10px] text-ink-2" aria-live="polite">
          <span className="rounded bg-surface-2 px-1.5 py-0.5">Local preview: {webcamState === 'live' ? 'active' : 'inactive'}</span>
          <span className="rounded bg-surface-2 px-1.5 py-0.5">Frames: {uploadState === 'open' ? sentFrames > 0 ? 'transmitting' : 'connected · waiting' : uploadState}</span>
          <span className="rounded bg-surface-2 px-1.5 py-0.5">Backend inference: {aiState}</span>
          <span className="rounded bg-surface-2 px-1.5 py-0.5">Live updates: {connection}</span>
          {lastUpdate !== null && <span className="rounded bg-surface-2 px-1.5 py-0.5">Updated {relativeTime(new Date(lastUpdate))}</span>}
        </div>
      )}

      {isBrowser && isRunning && sentFrames > 0 && (
        <span className="absolute left-2 top-2 rounded bg-bg/70 px-1.5 py-0.5 text-[10px] text-ink-2">
          webcam · {sentFrames} frames sent
        </span>
      )}
    </div>
  )
}

export function ZoneModal({
  initial,
  busy,
  onSave,
  onCancel,
}: {
  initial: { name: string; polygon: NormalizedPolygon; color: string; severity: ZoneSeverity }
  busy?: boolean
  onSave: (input: { name: string; polygon: NormalizedPolygon; color: string; severity: ZoneSeverity }) => void
  onCancel: () => void
}) {
  const [name, setName] = useState(initial.name)
  const [color, setColor] = useState(initial.color)
  const [severity, setSeverity] = useState<ZoneSeverity>(initial.severity)
  const [points, setPoints] = useState<NormalizedPolygon>(initial.polygon)
  const [error, setError] = useState<string | null>(null)

  const bounds = useMemo(() => {
    const xs = points.map(([x]) => x)
    const ys = points.map(([, y]) => y)
    return {
      x1: Math.min(...xs),
      y1: Math.min(...ys),
      x2: Math.max(...xs),
      y2: Math.max(...ys),
    }
  }, [points])

  const setBound = (key: 'x1' | 'y1' | 'x2' | 'y2', raw: string) => {
    const value = clamp01(Number(raw) / 100)
    if (Number.isNaN(value)) return
    setPoints(() => {
      const next = { ...bounds, [key]: value }
      return [
        [next.x1, next.y1],
        [next.x2, next.y1],
        [next.x2, next.y2],
        [next.x1, next.y2],
      ] as NormalizedPolygon
    })
  }

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    if (!name.trim()) {
      setError('Give the zone a name so operators can recognise it.')
      return
    }
    if (points.length < 3) {
      setError('A zone needs at least three points.')
      return
    }
    setError(null)
    onSave({ name: name.trim(), polygon: points, color, severity })
  }

  return (
    <Modal
      open
      title="Restricted zone"
      onClose={onCancel}
      footer={
        <>
          <Button onClick={onCancel}>Cancel</Button>
          <Button variant="primary" onClick={submit} loading={busy}>
            Save zone
          </Button>
        </>
      }
    >
      <form onSubmit={submit} className="space-y-3">
        <Field label="Name" error={error ?? undefined} htmlFor="zone-name">
          <TextInput id="zone-name" value={name} onChange={(event) => setName(event.target.value)} maxLength={120} />
        </Field>

        <Field label="Severity" hint="Controls the event severity and alert colour." htmlFor="zone-severity">
          <Select
            id="zone-severity"
            value={severity}
            onChange={(event) => setSeverity(event.target.value as ZoneSeverity)}
          >
            <option value="info">Info — record only</option>
            <option value="warning">Warning — needs attention</option>
            <option value="critical">Critical — act now</option>
          </Select>
        </Field>

        <div>
          <span className="label">Position (% of frame)</span>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {(['x1', 'y1', 'x2', 'y2'] as const).map((key) => (
              <Field key={key} label={key.toUpperCase()}>
                <TextInput
                  type="number"
                  min={0}
                  max={100}
                  step={1}
                  value={Math.round(bounds[key] * 100)}
                  onChange={(event) => setBound(key, event.target.value)}
                  className="tnum"
                />
              </Field>
            ))}
          </div>
          <p className="mt-1 text-xs text-ink-3">
            Coordinates are normalised (0–100 %) so the zone survives a change of camera resolution.
          </p>
        </div>

        <div>
          <span className="label">Colour</span>
          <div className="flex gap-2">
            {ZONE_COLORS.map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setColor(option)}
                aria-label={`Use colour ${option}`}
                className={cx('size-7 rounded-md border-2', color === option ? 'border-ink' : 'border-transparent')}
                style={{ background: option }}
              />
            ))}
          </div>
        </div>

        <p className="text-xs text-ink-3">
          Entry and exit are decided on each person&apos;s foot point (bottom-centre of the detection box), with
          a dwell threshold and a cooldown to prevent boundary flapping.
        </p>
      </form>
    </Modal>
  )
}
