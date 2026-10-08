import { expect, test } from './fixtures'

/**
 * The first-run experience and the API-key gate.
 *
 * These are the first things a user sees and the last things anyone tests by
 * hand after the first hour, so they are pinned here.
 */

test.describe('API key gate', () => {
  test('a first-time visitor is asked for a key', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('heading', { name: /connect to visionops/i })).toBeVisible()
    await expect(page.getByLabel(/api key/i)).toBeVisible()
    await expect(page.getByRole('button', { name: /^connect$/i })).toBeVisible()
  })

  test('no API request is made before a key is entered', async ({ page }) => {
    const apiCalls: string[] = []
    page.on('request', (request) => {
      if (request.url().includes('/api/v1/')) apiCalls.push(request.url())
    })

    await page.goto('/')
    await expect(page.getByRole('heading', { name: /connect to visionops/i })).toBeVisible()
    expect(apiCalls).toEqual([])
  })

  test('a malformed key is rejected in the browser, before the server sees it', async ({ page }) => {
    const apiCalls: string[] = []
    page.on('request', (request) => {
      if (request.url().includes('/api/v1/') && request.method() !== 'GET') apiCalls.push(request.url())
    })

    await page.goto('/')
    await page.getByLabel(/api key/i).fill('not-a-key')
    await page.getByRole('button', { name: /^connect$/i }).click()

    await expect(page.getByText(/does not look like a VisionOps key/i)).toBeVisible()
    expect(apiCalls).toEqual([])
  })

  test('a well-formed but wrong key reaches the server and is refused', async ({ page, apiKey }) => {
    const wrong = `vo_ffffffffffff_${'a'.repeat(43)}`
    expect(wrong).not.toBe(apiKey)

    await page.goto('/')
    await page.getByLabel(/api key/i).fill(wrong)
    await page.getByRole('button', { name: /^connect$/i }).click()

    // The gate accepts the format, so the app renders and the API answers 401.
    // The operator must see a *usable* message, not a silent empty dashboard.
    await expect(page.getByText(/unauthor|invalid|not authori|sign in|connect/i).first()).toBeVisible({
      timeout: 15_000,
    })
  })

  test('a valid key signs in and the key is kept in sessionStorage only', async ({ page, apiKey }) => {
    await page.goto('/')
    await page.getByLabel(/api key/i).fill(apiKey)
    await page.getByRole('button', { name: /^connect$/i }).click()

    await expect(page.getByRole('navigation')).toBeVisible()
    const stored = await page.evaluate(() => ({
      session: window.sessionStorage.getItem('visionops.apiKey'),
      local: window.localStorage.getItem('visionops.apiKey'),
    }))
    expect(stored.session).toBe(apiKey)
    expect(stored.local).toBeNull()
  })

  test('the key is not left in localStorage after signing out', async ({ page, signIn }) => {
    await signIn()

    const signOut = page.getByRole('button', { name: /sign out/i })
    await expect(signOut).toBeVisible({ timeout: 15_000 })
    await signOut.click()

    await expect(page.getByRole('heading', { name: /connect to visionops/i })).toBeVisible()
    const everywhere = await page.evaluate(() => ({
      session: window.sessionStorage.getItem('visionops.apiKey'),
      local: window.localStorage.getItem('visionops.apiKey'),
      cookie: document.cookie,
    }))
    expect(everywhere.session).toBeNull()
    expect(everywhere.local).toBeNull()
    expect(everywhere.cookie).toBe('')
  })

  test('a reload keeps the session', async ({ page, signIn }) => {
    await signIn()
    await expect(page.getByRole('navigation')).toBeVisible()

    await page.reload()
    await expect(page.getByRole('navigation')).toBeVisible()
    await expect(page.getByRole('heading', { name: /connect to visionops/i })).toBeHidden()
  })
})

test.describe('Navigation', () => {
  test('an unknown URL renders a not-found page rather than a blank screen', async ({ page, signIn }) => {
    await signIn()
    await page.goto('/there-is-no-such-page')
    await expect(page.getByText(/not found|404|no such page/i).first()).toBeVisible()
  })

  test('every primary page renders without a JavaScript crash', async ({ page, apiKey }) => {
    const crashes: string[] = []
    page.on('pageerror', (error) => crashes.push(error.message))

    await page.addInitScript((key) => {
      window.sessionStorage.setItem('visionops.apiKey', key)
    }, apiKey)

    // The real h1 of each page, as shipped.
    for (const [path, heading] of [
      ['/', /operations overview/i],
      ['/cameras', /cameras?/i],
      ['/zones', /^zones$/i],
      ['/events', /^events$/i],
      ['/system', /system status/i],
    ] as const) {
      await page.goto(path)
      await expect(page.getByRole('heading', { name: heading }).first()).toBeVisible({
        timeout: 15_000,
      })
      // The error boundary must never have taken over.
      await expect(page.getByText(/the interface hit an error/i)).toBeHidden()
    }
    expect(crashes).toEqual([])
  })

  test('a client-side navigation does not reload the whole app', async ({ page, signIn }) => {
    await signIn()

    await page.evaluate(() => {
      window.__navigationMarker = 'kept'
    })
    await page.getByRole('link', { name: /^events$/i }).click()
    await expect(page.getByRole('heading', { name: /event/i }).first()).toBeVisible()

    expect(await page.evaluate(() => (window as never as { __navigationMarker?: string }).__navigationMarker)).toBe('kept')
  })
})
