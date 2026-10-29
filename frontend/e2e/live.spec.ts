import { expect, test as base } from './fixtures'

/** A camera, its file source and one zone, created through the real API. */
interface LiveGraph {
  cameraId: string
  sourceId: string
}

const test = base.extend<{ graph: LiveGraph }>({
  graph: async ({ page, apiKey }, use) => {
    const headers = { 'X-API-Key': apiKey, 'Content-Type': 'application/json' }
    // Site and camera names are unique per organisation, and the e2e database is
    // shared for the whole run, so every test needs its own name.
    const unique = `live-${Math.random().toString(36).slice(2, 10)}`
    const site = await page.request.post('/api/v1/sites', { headers, data: { name: `Live ${unique}` } })
    expect(site.status(), await site.text()).toBe(201)
    const siteId = (await site.json()).id

    const camera = await page.request.post('/api/v1/cameras', {
      headers,
      data: { name: `cam-${unique}`, site_id: siteId, target_fps: 30 },
    })
    expect(camera.status(), await camera.text()).toBe(201)
    const cameraId = (await camera.json()).id

    const source = await page.request.post(`/api/v1/cameras/${cameraId}/sources`, {
      headers,
      data: { name: `clip-${unique}`, kind: 'file', target: 'fixtures/zone-walkthrough.mp4' },
    })
    expect(source.status(), await source.text()).toBe(201)
    const sourceId = (await source.json()).id

    const zone = await page.request.post('/api/v1/zones', {
      headers,
      data: {
        camera_id: cameraId,
        name: `zone-${unique}`,
        kind: 'polygon',
        points: [
          [0.05, 0.05],
          [0.95, 0.05],
          [0.95, 0.95],
          [0.05, 0.95],
        ],
      },
    })
    expect(zone.status(), await zone.text()).toBe(201)

    await use({ cameraId, sourceId })
  },
})

/**
 * The live path: a running session, a WebSocket that delivers state, and a
 * webcam page that pushes real frames from a real `MediaStream`.
 *
 * Chromium is launched with `--use-fake-device-for-media-stream`, so
 * `getUserMedia` returns a genuine stream: the browser negotiation, the frame
 * encode and the WebSocket push all execute for real. What is *not* real is the
 * video content, which is why nothing here asserts on detection accuracy.
 */

test.describe('Live session', () => {
  test('a session reports processing state and increments its frame count', async ({
    page,
    signIn,
    apiKey,
    graph,
  }) => {
    const { cameraId, sourceId } = graph
    const headers = { 'X-API-Key': apiKey, 'Content-Type': 'application/json' }

    const start = await page.request.post(`/api/v1/cameras/${cameraId}/session`, {
      headers,
      data: { source_id: sourceId },
    })
    expect(start.status(), await start.text()).toBe(201)

    await signIn()
    await page.goto(`/cameras/${cameraId}`)

    // The live view shows the session rather than an idle page.
    await expect(page.getByText(/live|processing|running/i).first()).toBeVisible({ timeout: 20_000 })

    // Frames are actually being counted, which is the honest proof it is live.
    await expect
      .poll(
        async () => {
          const state = await page.request.get(`/api/v1/cameras/${cameraId}/session`, { headers })
          const body = await state.json()
          return Number(body.frames_processed ?? 0)
        },
        { timeout: 30_000, message: 'the session never reported a processed frame' },
      )
      .toBeGreaterThan(0)

    await page.request.delete(`/api/v1/cameras/${cameraId}/session`, { headers })
  })

  test('a live-state WebSocket delivers repeated payloads', async ({
    page,
    signIn,
    apiKey,
    graph,
  }) => {
    const { cameraId, sourceId } = graph
    const headers = { 'X-API-Key': apiKey, 'Content-Type': 'application/json' }
    const start = await page.request.post(`/api/v1/cameras/${cameraId}/session`, {
      headers,
      data: { source_id: sourceId },
    })
    const sessionId = (await start.json()).session_id

    await signIn()
    await page.goto(`/cameras/${cameraId}`)

    const frames: unknown[] = await page.evaluate(
      ({ id, key }) =>
        new Promise<unknown[]>((resolve, reject) => {
          const received: unknown[] = []
          const socket = new WebSocket(
            `${window.location.origin.replace('http', 'ws')}/api/v1/stream/sessions/${id}?key=${key}`,
          )
          const timer = window.setTimeout(() => {
            socket.close()
            resolve(received)
          }, 8000)
          socket.onmessage = (event) => {
            received.push(JSON.parse(String(event.data)))
            if (received.length >= 3) {
              window.clearTimeout(timer)
              socket.close()
              resolve(received)
            }
          }
          socket.onerror = () => {
            window.clearTimeout(timer)
            reject(new Error(`the live-state socket errored after ${received.length} messages`))
          }
        }),
      { id: sessionId, key: apiKey },
    )

    expect(frames.length).toBeGreaterThanOrEqual(3)
    for (const frame of frames as { session_id?: string; stats?: { frames_processed?: number } }[]) {
      expect(frame.session_id).toBe(sessionId)
      expect(frame.stats).toBeDefined()
      expect(typeof frame.stats?.frames_processed).toBe('number')
    }
    // Frame counts must actually advance, or the socket is replaying one payload.
    const counts = (frames as { stats?: { frames_processed?: number } }[]).map(
      (frame) => frame.stats?.frames_processed ?? 0,
    )
    expect(Math.max(...counts)).toBeGreaterThan(0)

    await page.request.delete(`/api/v1/cameras/${cameraId}/session`, { headers })
  })

  test('a WebSocket with a bad key is refused', async ({ page, signIn, apiKey, graph }) => {
    const { cameraId, sourceId } = graph
    const headers = { 'X-API-Key': apiKey, 'Content-Type': 'application/json' }
    const start = await page.request.post(`/api/v1/cameras/${cameraId}/session`, {
      headers,
      data: { source_id: sourceId },
    })
    const sessionId = (await start.json()).session_id

    await signIn()
    await page.goto(`/cameras/${cameraId}`)

    const outcome = await page.evaluate(
      (id) =>
        new Promise<{ opened: boolean; closed: boolean }>((resolve) => {
          const socket = new WebSocket(
            `${window.location.origin.replace('http', 'ws')}/api/v1/stream/sessions/${id}?key=vo_ffffffffffff_${'a'.repeat(43)}`,
          )
          socket.onopen = () => resolve({ opened: true, closed: false })
          socket.onclose = () => resolve({ opened: false, closed: true })
          socket.onerror = () => resolve({ opened: false, closed: true })
          window.setTimeout(() => resolve({ opened: false, closed: false }), 8000)
        }),
      sessionId,
    )

    expect(outcome.opened).toBe(false)

    await page.request.delete(`/api/v1/cameras/${cameraId}/session`, { headers })
  })
})

test.describe('Browser webcam', () => {
  test('the Start button opens the camera and the page pushes frames', async ({ page, signIn, apiKey }) => {
    const headers = { 'X-API-Key': apiKey, 'Content-Type': 'application/json' }
    const unique = `webcam-${Math.random().toString(36).slice(2, 10)}`
    const site = await page.request.post('/api/v1/sites', { headers, data: { name: `Webcam ${unique}` } })
    const siteId = (await site.json()).id
    const camera = await page.request.post('/api/v1/cameras', {
      headers,
      data: { name: `laptop-${unique}`, site_id: siteId, target_fps: 30 },
    })
    const cameraId = (await camera.json()).id
    const source = await page.request.post(`/api/v1/cameras/${cameraId}/sources`, {
      headers,
      data: { name: `webcam-${unique}`, kind: 'browser', target: 'browser' },
    })
    expect(source.status(), await source.text()).toBe(201)

    await signIn()
    await page.goto(`/cameras/${cameraId}`)

    // The real path: click Start, let the page call getUserMedia (Chromium's fake
    // capture device), encode canvas frames and push them over the ingest socket.
    const start = page.getByRole('button', { name: /start session/i })
    await expect(start).toBeEnabled()
    await start.click()

    await expect(page.getByRole('button', { name: /stop session/i })).toBeVisible({ timeout: 20_000 })
    await expect(page.getByText(/frames sent/i)).toBeVisible({ timeout: 20_000 })
    await expect(page.getByLabel('Webcam preview')).toBeVisible()

    await expect
      .poll(
        async () => {
          const state = await page.request.get(`/api/v1/cameras/${cameraId}/session`, { headers })
          const body = await state.json()
          return Number(body.frames_processed ?? 0)
        },
        { timeout: 30_000, message: 'frames pushed by the page were never processed' },
      )
      .toBeGreaterThan(0)

    await page.request.delete(`/api/v1/cameras/${cameraId}/session`, { headers })
  })
})
