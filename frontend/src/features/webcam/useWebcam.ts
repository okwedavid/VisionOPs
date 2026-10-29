/**
 * Webcam capture and frame delivery.
 *
 * The browser is the capture device (ADR-009): the server needs no camera
 * library, the demo works on any laptop, and frames never leave the operator's
 * own machine or LAN. Frames are pushed as binary JPEG over the ingest
 * WebSocket at the server's requested FPS.
 *
 * Every failure is a first-class state with instructions — a denied permission
 * must never look like a silent blank video.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

export type WebcamState = 'idle' | 'requesting' | 'live' | 'denied' | 'unavailable' | 'busy' | 'error' | 'stopped'
export type WebcamUploadState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'error' | 'closed'

export interface UseWebcamResult {
  state: WebcamState
  uploadState: WebcamUploadState
  videoRef: React.RefObject<HTMLVideoElement | null>
  stream: MediaStream | null
  error: string | null
  uploadError: string | null
  /** Request camera access. Returns false (with `state` set) when it fails. */
  start: () => Promise<boolean>
  /** Connect the ingest socket and start delivering frames. Safe to call repeatedly. */
  deliver: (ingestUrl: string, targetFps: number) => void
  stop: () => void
  sentFrames: number
}

const PERMISSION_HELP =
  'Allow camera access in your browser, then press Start again. If the prompt is gone, click the camera icon in the address bar, or use a device/file source instead.'

const SOCKET_HELP =
  'The frame-upload connection failed. Check that the API key is set and the server is reachable, then press Start again.'

function hasVideoDimensions(video: HTMLVideoElement): boolean {
  return video.readyState >= 2 && video.videoWidth > 0 && video.videoHeight > 0
}

/** Wait until the element has decoded a frame we can draw to a canvas. */
function waitForVideoDimensions(video: HTMLVideoElement, timeoutMs = 5000): Promise<void> {
  if (hasVideoDimensions(video)) return Promise.resolve()

  return new Promise((resolve, reject) => {
    let settled = false
    const events = ['loadedmetadata', 'loadeddata', 'canplay', 'playing', 'resize'] as const

    const cleanup = () => {
      for (const event of events) video.removeEventListener(event, check)
      window.clearTimeout(timer)
    }

    const finish = (error?: Error) => {
      if (settled) return
      settled = true
      cleanup()
      if (error) reject(error)
      else resolve()
    }

    const check = () => {
      if (hasVideoDimensions(video)) finish()
    }

    const timer = window.setTimeout(
      () =>
        finish(
          new Error(
            'The camera started but did not produce video frames. Close other apps using the camera and press Start again.',
          ),
        ),
      timeoutMs,
    )

    for (const event of events) video.addEventListener(event, check)
    check()
  })
}

function classifyCameraError(cause: unknown): { state: WebcamState; error: string } {
  const name = (cause as DOMException)?.name
  const detail = (cause as Error)?.message

  if (name === 'NotAllowedError' || name === 'SecurityError') {
    return { state: 'denied', error: PERMISSION_HELP }
  }
  if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
    return { state: 'unavailable', error: 'No camera was found on this device. Add a device or file source instead.' }
  }
  if (name === 'NotReadableError' || name === 'TrackStartError') {
    return {
      state: 'busy',
      error:
        'The camera is in use by another app or browser tab. Close the other app, then press Start again.',
    }
  }
  if (name === 'OverconstrainedError' || name === 'ConstraintNotSatisfiedError') {
    return {
      state: 'unavailable',
      error: 'This camera does not support the requested video mode. Try a different camera or a file source.',
    }
  }
  if (name === 'AbortError') {
    return {
      state: 'error',
      error: 'The camera was interrupted before it could start. Press Start again.',
    }
  }

  return {
    state: 'error',
    error: `The camera could not be started: ${detail ?? String(cause)}`,
  }
}

export function useWebcam(): UseWebcamResult {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const socketRef = useRef<WebSocket | null>(null)
  const socketUrlRef = useRef<string | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const timerRef = useRef<number | null>(null)
  const reconnectTimerRef = useRef<number | null>(null)
  const reconnectAttemptRef = useRef(0)
  const targetFpsRef = useRef(5)
  const generationRef = useRef(0)
  const startPromiseRef = useRef<Promise<boolean> | null>(null)
  const stoppedRef = useRef(true)

  const [state, setState] = useState<WebcamState>('idle')
  const [uploadState, setUploadState] = useState<WebcamUploadState>('idle')
  const [error, setError] = useState<string | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [stream, setStream] = useState<MediaStream | null>(null)
  const [sentFrames, setSentFrames] = useState(0)

  const clearReconnect = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current)
      reconnectTimerRef.current = null
    }
  }, [])

  const closeSocket = useCallback(() => {
    const socket = socketRef.current
    socketRef.current = null
    if (!socket) return
    socket.onopen = null
    socket.onerror = null
    socket.onmessage = null
    socket.onclose = null
    try {
      socket.close()
    } catch {
      /* already closed */
    }
  }, [])

  const stopDelivery = useCallback(() => {
    stoppedRef.current = true
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current)
      timerRef.current = null
    }
    clearReconnect()
    closeSocket()
    socketUrlRef.current = null
    reconnectAttemptRef.current = 0
    setUploadState('closed')
  }, [clearReconnect, closeSocket])

  const stop = useCallback(() => {
    generationRef.current += 1
    stopDelivery()
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    setStream(null)
    const video = videoRef.current
    if (video) video.srcObject = null
    setState((current) => (current === 'idle' ? current : 'stopped'))
  }, [stopDelivery])

  const start = useCallback(async (): Promise<boolean> => {
    const existing = streamRef.current
    if (existing && existing.getVideoTracks().some((track) => track.readyState === 'live')) {
      const video = videoRef.current
      if (video) {
        if (video.srcObject !== existing) video.srcObject = existing
        if (video.paused) await video.play().catch(() => undefined)
      }
      setState('live')
      return true
    }

    if (startPromiseRef.current) return startPromiseRef.current

    const generation = ++generationRef.current
    const attempt = (async (): Promise<boolean> => {
      if (!navigator.mediaDevices?.getUserMedia) {
        setState('unavailable')
        setError(
          'This browser does not expose camera access over a secure context. Use https:// or http://localhost, or choose a device or file source instead.',
        )
        return false
      }

      setState('requesting')
      setError(null)
      setUploadError(null)
      let media: MediaStream | null = null

      try {
        media = await navigator.mediaDevices.getUserMedia({
          video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'environment' },
          audio: false,
        })

        if (generation !== generationRef.current) {
          media.getTracks().forEach((track) => track.stop())
          return false
        }
        if (!media.getVideoTracks().length) {
          throw new Error('The selected device did not provide a video track.')
        }

        streamRef.current = media
        setStream(media)
        setSentFrames(0)

        const video = videoRef.current
        if (!video) {
          throw new Error('The video preview is not ready. Reload the page and press Start again.')
        }

        video.srcObject = media
        await video.play().catch((cause: unknown) => {
          if ((cause as DOMException)?.name === 'NotAllowedError') {
            throw new Error('The browser blocked video playback. Press Start again to allow it.')
          }
          throw cause
        })
        await waitForVideoDimensions(video)

        if (generation !== generationRef.current) {
          media.getTracks().forEach((track) => track.stop())
          return false
        }

        setState('live')
        return true
      } catch (cause) {
        if (generation !== generationRef.current) {
          media?.getTracks().forEach((track) => track.stop())
          return false
        }
        media?.getTracks().forEach((track) => track.stop())
        if (streamRef.current === media) {
          streamRef.current = null
          setStream(null)
          const video = videoRef.current
          if (video) video.srcObject = null
        }
        const failure = classifyCameraError(cause)
        setState(failure.state)
        setError(failure.error)
        return false
      }
    })()

    startPromiseRef.current = attempt
    try {
      return await attempt
    } finally {
      if (startPromiseRef.current === attempt) startPromiseRef.current = null
    }
  }, [])

  const deliver = useCallback(
    (ingestUrl: string, targetFps: number) => {
      const fps = Math.max(0.5, Math.min(30, targetFps))
      targetFpsRef.current = fps
      stoppedRef.current = false

      const capture = () => {
        const video = videoRef.current
        const socket = socketRef.current
        if (!video || !socket || socket.readyState !== WebSocket.OPEN) return
        if (video.readyState < 2 || video.videoWidth === 0 || video.videoHeight === 0) return

        const canvas = (canvasRef.current ??= document.createElement('canvas'))
        // Cap the capture size: the model input is far smaller, and the network
        // is a LAN hop — there is no reason to send 4K.
        const maxWidth = 960
        const scale = Math.min(1, maxWidth / video.videoWidth)
        const width = Math.max(1, Math.round(video.videoWidth * scale))
        const height = Math.max(1, Math.round(video.videoHeight * scale))
        if (canvas.width !== width) canvas.width = width
        if (canvas.height !== height) canvas.height = height
        const ctx = canvas.getContext('2d')
        if (!ctx) return
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height)

        canvas.toBlob(
          (blob) => {
            if (!blob) return
            const socketNow = socketRef.current
            if (socketNow !== socket || socketNow.readyState !== WebSocket.OPEN) return
            try {
              socketNow.send(blob)
              setSentFrames((count) => count + 1)
            } catch {
              setUploadError(SOCKET_HELP)
            }
          },
          'image/jpeg',
          0.7,
        )
      }

      const startTimer = () => {
        if (timerRef.current !== null) window.clearInterval(timerRef.current)
        timerRef.current = window.setInterval(capture, Math.max(33, 1000 / targetFpsRef.current))
      }

      const sameSocket =
        socketUrlRef.current === ingestUrl &&
        socketRef.current !== null &&
        (socketRef.current.readyState === WebSocket.CONNECTING || socketRef.current.readyState === WebSocket.OPEN)
      if (sameSocket) {
        startTimer()
        return
      }
      if (socketUrlRef.current === ingestUrl && reconnectTimerRef.current !== null) {
        startTimer()
        return
      }

      clearReconnect()
      closeSocket()
      socketUrlRef.current = ingestUrl
      reconnectAttemptRef.current = 0
      setUploadError(null)
      setUploadState('connecting')

      const scheduleReconnect = () => {
        if (stoppedRef.current) return
        clearReconnect()
        const attempt = reconnectAttemptRef.current + 1
        reconnectAttemptRef.current = attempt
        const delay = Math.min(10_000, 500 * 2 ** Math.min(attempt - 1, 4))
        setUploadState('reconnecting')
        reconnectTimerRef.current = window.setTimeout(() => {
          reconnectTimerRef.current = null
          connect()
        }, delay)
      }

      const connect = () => {
        if (stoppedRef.current) return
        if (reconnectAttemptRef.current > 0) setUploadState('reconnecting')
        else setUploadState('connecting')

        let socket: WebSocket
        try {
          socket = new WebSocket(ingestUrl)
        } catch {
          setUploadError(SOCKET_HELP)
          scheduleReconnect()
          return
        }

        socketRef.current = socket
        socket.binaryType = 'arraybuffer'
        socket.onopen = () => {
          if (socketRef.current !== socket) return
          reconnectAttemptRef.current = 0
          setUploadState('open')
          setUploadError(null)
        }
        socket.onerror = () => {
          if (socketRef.current !== socket) return
          setUploadError(SOCKET_HELP)
        }
        socket.onclose = (event) => {
          if (socketRef.current !== socket) return
          socketRef.current = null
          if (stoppedRef.current) {
            setUploadState('closed')
            return
          }
          if (event.code === 4401 || event.code === 4403 || event.code === 4404) {
            setUploadState('error')
            setUploadError(
              event.code === 4401
                ? 'The server rejected the API key for frame upload. Re-enter the API key and press Start again.'
                : event.code === 4404
                  ? 'The session ended before frames could be delivered. Start a new session.'
                  : 'This API key is not allowed to send frames for this session.',
            )
            return
          }
          setUploadError(`Frame upload disconnected (code ${event.code || 'unknown'}). Reconnecting…`)
          scheduleReconnect()
        }
      }

      connect()
      startTimer()
    },
    [clearReconnect, closeSocket],
  )

  useEffect(() => () => stop(), [stop])

  return { state, uploadState, videoRef, stream, error, uploadError, start, deliver, stop, sentFrames }
}

/** Human-readable state for the camera-source UI. */
export function webcamStateMessage(state: WebcamState): string {
  switch (state) {
    case 'idle':
      return 'Camera not started.'
    case 'requesting':
      return 'Waiting for camera permission…'
    case 'live':
      return 'Camera is live.'
    case 'denied':
      return 'Camera permission was denied.'
    case 'unavailable':
      return 'No camera is available in this browser.'
    case 'busy':
      return 'The camera is in use by another app.'
    case 'error':
      return 'The camera failed to start.'
    case 'stopped':
      return 'Camera stopped.'
    default:
      return ''
  }
}
