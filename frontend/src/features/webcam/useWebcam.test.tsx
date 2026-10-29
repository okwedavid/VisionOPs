import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useWebcam } from '@/features/webcam/useWebcam'

class FakeWebSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  static instances: FakeWebSocket[] = []

  readonly url: string
  readyState = FakeWebSocket.CONNECTING
  binaryType = 'blob'
  onopen: ((event: Event) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  send = vi.fn()
  close = vi.fn(() => {
    this.readyState = FakeWebSocket.CLOSED
  })

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }

  open() {
    this.readyState = FakeWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  serverClose(code = 1006) {
    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.({ code } as CloseEvent)
  }
}

interface FakeMedia {
  stream: MediaStream
  track: MediaStreamTrack & { stop: ReturnType<typeof vi.fn> }
}

function fakeMedia(): FakeMedia {
  const track = {
    kind: 'video',
    readyState: 'live',
    stop: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  } as unknown as MediaStreamTrack & { stop: ReturnType<typeof vi.fn> }
  const stream = {
    active: true,
    getTracks: () => [track],
    getVideoTracks: () => [track],
  } as unknown as MediaStream
  return { stream, track }
}

function makeVideo({ width = 640, height = 480, readyState = 2 } = {}) {
  const video = document.createElement('video')
  Object.defineProperty(video, 'readyState', { configurable: true, get: () => readyState })
  Object.defineProperty(video, 'videoWidth', { configurable: true, get: () => width })
  Object.defineProperty(video, 'videoHeight', { configurable: true, get: () => height })
  Object.defineProperty(video, 'srcObject', { configurable: true, writable: true, value: null })
  video.play = vi.fn().mockResolvedValue(undefined)
  return video
}

const originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
let getUserMedia: ReturnType<typeof vi.fn>

function installMediaDevices(value: unknown) {
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value })
}

function attachVideo(result: { current: ReturnType<typeof useWebcam> }, video: HTMLVideoElement) {
  ;(result.current.videoRef as { current: HTMLVideoElement | null }).current = video
}

beforeEach(() => {
  vi.useFakeTimers()
  FakeWebSocket.instances = []
  vi.stubGlobal('WebSocket', FakeWebSocket)
  getUserMedia = vi.fn()
  installMediaDevices({ getUserMedia })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    () => ({ drawImage: vi.fn() }) as unknown as CanvasRenderingContext2D,
  )
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation((callback: BlobCallback) => {
    callback(new Blob(['jpeg'], { type: 'image/jpeg' }))
  })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  if (originalMediaDevices) Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices)
  else delete (navigator as { mediaDevices?: unknown }).mediaDevices
})

describe('useWebcam', () => {
  it('requests the camera from the click, waits for video, and pushes JPEG frames', async () => {
    const media = fakeMedia()
    getUserMedia.mockResolvedValue(media.stream)
    const video = makeVideo()
    const { result, unmount } = renderHook(() => useWebcam())
    attachVideo(result, video)

    let started = false
    await act(async () => {
      started = await result.current.start()
    })

    expect(started).toBe(true)
    expect(getUserMedia).toHaveBeenCalledWith({
      video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'environment' },
      audio: false,
    })
    expect(video.srcObject).toBe(media.stream)
    expect(video.play).toHaveBeenCalled()
    expect(result.current.state).toBe('live')

    act(() => result.current.deliver('ws://example.test/api/v1/stream/ingest?session_id=s1', 10))
    expect(FakeWebSocket.instances).toHaveLength(1)
    const socket = FakeWebSocket.instances[0]!
    expect(result.current.uploadState).toBe('connecting')

    act(() => socket.open())
    expect(result.current.uploadState).toBe('open')

    await act(async () => {
      vi.advanceTimersByTime(120)
    })

    expect(socket.send).toHaveBeenCalledTimes(1)
    expect(socket.send.mock.calls[0]![0]).toBeInstanceOf(Blob)
    expect(result.current.sentFrames).toBe(1)

    act(() => result.current.stop())
    expect(media.track.stop).toHaveBeenCalled()
    expect(socket.close).toHaveBeenCalled()
    expect(result.current.state).toBe('stopped')
    expect(result.current.uploadState).toBe('closed')

    unmount()
  })

  it('does not open a second ingest socket when delivery is requested twice', async () => {
    const media = fakeMedia()
    getUserMedia.mockResolvedValue(media.stream)
    const { result, unmount } = renderHook(() => useWebcam())
    attachVideo(result, makeVideo())

    await act(async () => {
      await result.current.start()
    })

    act(() => {
      result.current.deliver('ws://example.test/ingest', 5)
      result.current.deliver('ws://example.test/ingest', 5)
    })

    expect(FakeWebSocket.instances).toHaveLength(1)
    unmount()
  })

  it('explains a denied permission instead of leaving a blank video', async () => {
    getUserMedia.mockRejectedValue(new DOMException('denied', 'NotAllowedError'))
    const { result, unmount } = renderHook(() => useWebcam())
    attachVideo(result, makeVideo())

    let started = true
    await act(async () => {
      started = await result.current.start()
    })

    expect(started).toBe(false)
    expect(result.current.state).toBe('denied')
    expect(result.current.error).toMatch(/allow camera access/i)
    unmount()
  })

  it('reports a camera that is already in use as busy', async () => {
    getUserMedia.mockRejectedValue(new DOMException('in use', 'NotReadableError'))
    const { result, unmount } = renderHook(() => useWebcam())
    attachVideo(result, makeVideo())

    await act(async () => {
      await result.current.start()
    })

    expect(result.current.state).toBe('busy')
    expect(result.current.error).toMatch(/in use by another app/i)
    unmount()
  })

  it('reports an unsupported secure context when mediaDevices is missing', async () => {
    installMediaDevices(undefined)
    const { result, unmount } = renderHook(() => useWebcam())

    let started = true
    await act(async () => {
      started = await result.current.start()
    })

    expect(started).toBe(false)
    expect(result.current.state).toBe('unavailable')
    expect(result.current.error).toMatch(/secure context/i)
    unmount()
  })

  it('fails with a useful message when the stream never produces a frame', async () => {
    const media = fakeMedia()
    getUserMedia.mockResolvedValue(media.stream)
    const { result, unmount } = renderHook(() => useWebcam())
    attachVideo(result, makeVideo({ width: 0, height: 0, readyState: 0 }))

    let started = true
    let startPromise!: Promise<boolean>
    act(() => {
      startPromise = result.current.start().then((value) => {
        started = value
        return value
      })
    })

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(result.current.state).toBe('requesting')

    await act(async () => {
      vi.advanceTimersByTime(5000)
      await startPromise
    })

    expect(started).toBe(false)
    expect(result.current.state).toBe('error')
    expect(result.current.error).toMatch(/did not produce video frames/i)
    expect(media.track.stop).toHaveBeenCalled()
    unmount()
  })

  it('reconnects after an unexpected ingest disconnect', async () => {
    const media = fakeMedia()
    getUserMedia.mockResolvedValue(media.stream)
    const { result, unmount } = renderHook(() => useWebcam())
    attachVideo(result, makeVideo())

    await act(async () => {
      await result.current.start()
    })
    act(() => result.current.deliver('ws://example.test/ingest', 5))

    const first = FakeWebSocket.instances[0]!
    act(() => {
      first.open()
      first.serverClose(1006)
    })
    expect(result.current.uploadState).toBe('reconnecting')

    await act(async () => {
      vi.advanceTimersByTime(500)
    })

    expect(FakeWebSocket.instances).toHaveLength(2)
    const second = FakeWebSocket.instances[1]!
    act(() => second.open())
    expect(result.current.uploadState).toBe('open')
    unmount()
  })

  it('does not retry forever after the server rejects the API key', async () => {
    const media = fakeMedia()
    getUserMedia.mockResolvedValue(media.stream)
    const { result, unmount } = renderHook(() => useWebcam())
    attachVideo(result, makeVideo())

    await act(async () => {
      await result.current.start()
    })
    act(() => result.current.deliver('ws://example.test/ingest', 5))

    const socket = FakeWebSocket.instances[0]!
    act(() => {
      socket.open()
      socket.serverClose(4401)
    })

    expect(result.current.uploadState).toBe('error')
    expect(result.current.uploadError).toMatch(/rejected the API key/i)

    await act(async () => {
      vi.advanceTimersByTime(30_000)
    })
    expect(FakeWebSocket.instances).toHaveLength(1)
    unmount()
  })
})
