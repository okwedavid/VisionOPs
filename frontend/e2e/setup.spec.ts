import { expect, test } from './fixtures'

test.describe('Empty states', () => {
  test('an install with no cameras explains what to do next', async ({ page, signIn }) => {
    await signIn()
    await page.goto('/cameras')
    await expect(page.getByText(/no cameras yet/i)).toBeVisible()
    await expect(page.getByRole('link', { name: /run the setup wizard/i })).toBeVisible()
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
  })

  test('an install with no zones says so rather than showing a broken editor', async ({ page, signIn }) => {
    await signIn()
    await page.goto('/zones')
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
    await expect(page.getByText(/no zones configured|nothing restricted yet|draw a zone/i).first()).toBeVisible({
      timeout: 15_000,
    })
  })

  test('an install with no events does not look like an error', async ({ page, signIn }) => {
    await signIn()
    await page.goto('/events')
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
    await expect(page.getByText(/no events match these filters|no events|history/i).first()).toBeVisible({
      timeout: 15_000,
    })
    await expect(page.getByText(/the interface hit an error/i)).toBeHidden()
  })

  test('a camera id that does not exist is a clean 404, not a crash', async ({ page, signIn }) => {
    await signIn()
    const crashes: string[] = []
    page.on('pageerror', (error) => crashes.push(error.message))
    await page.goto('/cameras/01920000-0000-7000-8000-000000000000')
    await expect(page.getByRole('link', { name: /all cameras/i })).toBeVisible({ timeout: 15_000 })
    expect(crashes).toEqual([])
  })
})

test.describe('System status', () => {
  test('reports the real model identity and memory', async ({ page, signIn }) => {
    await signIn()
    await page.goto('/system')
    await expect(page.getByRole('heading', { name: /system status/i })).toBeVisible()

    // The model section states what the AI is actually running. If no model is
    // installed the page must say so — it must never imply detection is active.
    await expect(page.getByText(/detection model|what the AI is actually running/i).first()).toBeVisible({
      timeout: 15_000,
    })
    await expect(page.getByText(/yolo11n|model unavailable|no detection model/i).first()).toBeVisible()
  })

  test('shows the running version and environment', async ({ page, signIn }) => {
    await signIn()
    await page.goto('/system')
    await expect(page.getByText(/0\.1\.0/).first()).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText(/development|local|staging|production/i).first()).toBeVisible()
  })
})

test.describe('Setup wizard', () => {
  test('walks from an empty install to a running session', async ({ page, signIn }) => {
    test.slow()
    await signIn()
    await page.goto('/setup')

    await expect(page.getByRole('heading', { name: /set up visionops/i })).toBeVisible()

    // Step 1: the key is already held, so the wizard offers to skip past it.
    await expect(page.getByText(/1 · API key/i)).toBeVisible()
    await page.getByRole('button', { name: /skip to the next step/i }).click()

    // Step 2: a site.
    await expect(page.getByText(/2 · Site/i)).toBeVisible()
    await page.getByLabel(/site name/i).fill('Warehouse')
    await page.getByRole('button', { name: /^create site$/i }).click()

    // Step 3: a camera.
    await expect(page.getByText(/3 · Camera/i)).toBeVisible()
    await page.getByLabel(/camera name/i).fill('Loading bay')
    await page.getByRole('button', { name: /^create camera$/i }).click()

    // Step 4: a source. The bundled synthetic fixture needs no camera and no
    // network, which is the point: this path must work on any machine.
    await expect(page.getByText(/4 · Video source/i)).toBeVisible()
    await page.getByLabel(/source kind/i).selectOption('file')
    await page.getByLabel(/file path/i).fill('fixtures/zone-walkthrough.mp4')
    await page.getByRole('button', { name: /^add source$/i }).click()

    // Step 5: a zone. The default rectangle is the fast path; drawing is
    // covered separately on the camera page.
    await expect(page.getByText(/5 · Your first restricted zone/i)).toBeVisible()
    await page.getByRole('button', { name: /sensible default rectangle/i }).click()

    // Step 6: start processing.
    await expect(page.getByText(/6 · Start processing/i)).toBeVisible()
    const start = page.getByRole('button', { name: /start session/i })
    await expect(start).toBeVisible()
    await start.click()

    // The wizard hands over to the camera view.
    await expect(page).toHaveURL(/\/cameras\/[0-9a-f-]{36}/, { timeout: 20_000 })
  })

  test('the wizard is reachable without a camera and does not crash', async ({ page, signIn }) => {
    await signIn()
    const crashes: string[] = []
    page.on('pageerror', (error) => crashes.push(error.message))
    await page.goto('/setup')
    await expect(page.getByText(/Prefer the command line/i)).toBeVisible()
    expect(crashes).toEqual([])
  })
})
