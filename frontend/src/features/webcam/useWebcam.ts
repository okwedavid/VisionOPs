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

export type WebcamState = 'idle' | 'requesting' | 'live' | 'denied' | 'unavailable' | 'error' | 'stopped'

export interface UseWebcamResult {
  state: WebcamState
  videoRef: React.RefObject<HTMLVideoElement | null>
  stream: MediaStream | null
  error: string | null
  /** Request camera access. Returns false (with `state` set) when it fails. */
  start: () => Promise<boolean>
  /** Connect the ingest socket and start delivering frames. */
  deliver: (ingestUrl: string, targetFps: number) => void
  stop: () => void
  sentFrames: number
}

const PERMISSION_HELP =
  'Allow camera access in your browser, then press Start again. If the prompt is gone, click the camera icon in the address bar, or use a device/file source instead.'

export function useWebcam(): UseWebcamResult {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const socketRef = useRef<WebSocket | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const timerRef = useRef<number | null>(null)
  const targetFpsRef = useRef(5)
  const [state, setState] = useState<WebcamState>('idle')
  const [error, setError] = useState<string | null>(null)
  const [stream, setStream] = useState<MediaStream | null>(null)
  const [sentFrames, setSentFrames] = useState(0)

  const stop = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current)
      timerRef.current = null
    }
    socketRef.current?.close()
    socketRef.current = null
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    setStream(null)
    setState((current) => (current === 'live' ? 'stopped' : current))
  }, [])

  const start = useCallback(async (): Promise<boolean> => {
    if (!navigator.mediaDevices?.getUserMedia) {
      setState('unavailable')
      setError(
        'This browser does not expose camera access over a secure context. Use https:// or http://localhost, or choose a device or file source instead.',
      )
      return false
    }
    setState('requesting')
    setError(null)
    try {
      const media = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'environment' },
        audio: false,
      })
      streamRef.current = media
      setStream(media)
      if (videoRef.current) {
        videoRef.current.srcObject = media
        await videoRef.current.play().catch(() => {
          /* autoplay can be blocked; the user gesture that started us usually allows it */
        })
      }
      setState('live')
      return true
    } catch (cause) {
      const name = (cause as DOMException)?.name
      if (name === 'NotAllowedError' || name === 'SecurityError') {
        setState('denied')
        setError(PERMISSION_HELP)
      } else if (name === 'NotFoundError' || name === 'OverconstrainedError') {
        setState('unavailable')
        setError('No camera was found on this device. Add a device or file source instead.')
      } else {
        setState('error')
        setError(`The camera could not be started: ${(cause as Error)?.message ?? String(cause)}`)
      }
      return false
    }
  }, [])

  const deliver = useCallback(
    (ingestUrl: string, targetFps: number) => {
      targetFpsRef.current = Math.max(0.5, Math.min(30, targetFps))
      const socket = new WebSocket(ingestUrl)
      socket.binaryType = 'arraybuffer'
      socketRef.current = socket
      socket.onerror = () => {
        setError('The frame-upload connection failed. Check that the API key is set and the server is reachable.')
      }

      if (timerRef.current !== null) window.clearInterval(timerRef.current)
      timerRef.current = window.setInterval(() => {
        const video = videoRef.current
        const socketNow = socketRef.current
        if (!video || !socketNow || socketNow.readyState !== WebSocket.OPEN) return
        if (video.readyState < 2 || video.videoWidth === 0) return

        const canvas = (canvasRef.current ??= document.createElement('canvas'))
        // Cap the capture size: the model input is far smaller, and the network
        // is a LAN hop — there is no reason to send 4K.
        const maxWidth = 960
        const scale = Math.min(1, maxWidth / video.videoWidth)
        canvas.width = Math.round(video.videoWidth * scale)
        canvas.height = Math.round(video.videoHeight * scale)
        const ctx = canvas.getContext('2d')
        if (!ctx) return
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height)

        canvas.toBlob(
          (blob) => {
            if (!blob) return
            if (socketNow.readyState !== WebSocket.OPEN) return
            socketNow.send(blob)
            setSentFrames((count) => count + 1)
          },
          'image/jpeg',
          0.7,
        )
      }, 1000 / targetFpsRef.current)
    },
    [],
  )

  useEffect(() => () => stop(), [stop])

  return { state, videoRef, stream, error, start, deliver, stop, sentFrames }
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
    case 'error':
      return 'The camera failed to start.'
    case 'stopped':
      return 'Camera stopped.'
    default:
      return ''
  }
}
