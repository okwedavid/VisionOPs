/** Stop the backend started by the global setup and clean its data directory. */
import type { FullConfig } from '@playwright/test'

import { readState } from './global-setup'

/**
 * Playwright loads global-setup and global-teardown in separate module scopes,
 * so the child process handle is not shared. The pid is written to the state
 * file by the setup for exactly this reason.
 */
export default async function globalTeardown(_config: FullConfig): Promise<void> {
  const { pid, dataDir } = readState()
  if (pid && !alreadyGone(pid)) {
    try {
      process.kill(pid, 'SIGTERM')
    } catch {
      // Already exited; nothing to do.
    }
  }
  // Windows keeps the SQLite file locked for a moment after the child dies, so
  // deleting immediately makes an otherwise green run fail in teardown.
  await waitForExit(pid)
  const { rmSync } = await import('node:fs')
  for (let attempt = 0; ; attempt += 1) {
    try {
      rmSync(dataDir, { recursive: true, force: true })
      return
    } catch (error) {
      if (attempt >= 50) throw error
      await delay(100)
    }
  }
}

async function waitForExit(pid: number): Promise<void> {
  for (let attempt = 0; attempt < 50 && !alreadyGone(pid); attempt += 1) {
    await delay(100)
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function alreadyGone(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return false
  } catch {
    return true
  }
}
