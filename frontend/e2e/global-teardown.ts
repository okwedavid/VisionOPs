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
  const { rmSync } = await import('node:fs')
  rmSync(dataDir, { recursive: true, force: true })
}

function alreadyGone(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return false
  } catch {
    return true
  }
}
