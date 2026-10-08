import { expect, test as base } from '@playwright/test'

import { readState } from './global-setup'

/**
 * Test fixtures for the e2e suite.
 *
 * Authentication goes through the gate the operator uses. Injecting the key
 * with `addInitScript` would re-apply it on *every* navigation, which quietly
 * breaks any test about signing out — so `signIn` types it into the form and
 * `primeKey` (which seeds sessionStorage once) is only for tests that never
 * reload the page.
 */
export const test = base.extend<{
  apiKey: string
  signIn: () => Promise<void>
  primeKey: () => Promise<void>
}>({
  apiKey: async ({}, use) => {
    await use(readState().apiKey)
  },

  signIn: async ({ page, apiKey }, use) => {
    await use(async () => {
      await page.goto('/')
      const field = page.getByLabel(/api key/i)
      await expect(field).toBeVisible()
      await field.fill(apiKey)
      await page.getByRole('button', { name: /^connect$/i }).click()
      await expect(page.getByRole('navigation', { name: /main/i })).toBeVisible()
    })
  },

  primeKey: async ({ page, apiKey }, use) => {
    await use(async () => {
      await page.goto('/setup')
      await page.evaluate((key) => {
        window.sessionStorage.setItem('visionops.apiKey', key)
      }, apiKey)
    })
  },
})

export { expect }
