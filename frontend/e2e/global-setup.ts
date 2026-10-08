/**
 * Global setup: build a throwaway database, start the real backend, and hand
 * the API key to the test workers.
 *
 * The backend is the genuine article — `uvicorn` with the production app
 * factory, a temp SQLite file, and the SPA built from `frontend/dist`. Only the
 * *data directory* is temporary.
 */
import { execFileSync, spawn, type ChildProcess } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import type { FullConfig } from '@playwright/test'

const HERE = dirname(fileURLToPath(import.meta.url))
export const FRONTEND_DIR = resolve(HERE, '..')
export const REPO_ROOT = resolve(FRONTEND_DIR, '..')

const PORT = Number(process.env.E2E_PORT ?? 8399)
export const BASE_URL = `http://127.0.0.1:${PORT}`
export const STATE_FILE = join(REPO_ROOT, '.e2e-state.json')

let child: ChildProcess | null = null

export default async function globalSetup(config: FullConfig): Promise<void> {
  const dataDir = join(REPO_ROOT, '.e2e-data')
  rmSync(dataDir, { recursive: true, force: true })
  mkdirSync(dataDir, { recursive: true })

  const python = join(REPO_ROOT, '.venv', 'bin', 'python')
  if (!existsSync(python)) {
    throw new Error(`${python} not found — run \`make install\` before the e2e suite`)
  }

  // A key minted here, never in .env: the e2e database is disposable, but the
  // key must still be a real `vo_<id>_<secret>` so the UI's format check and
  // the server's parser are both genuinely exercised.
  const generated = execFileSync(
    python,
    [
      '-c',
      [
        'import sys; sys.path.insert(0, "backend")',
        'from visionops.core.security import generate_api_key',
        'print(generate_api_key().full_key)',
      ].join('; '),
    ],
    { cwd: REPO_ROOT, encoding: 'utf8' },
  ).trim()
  const apiKey = generated.split('\n').at(-1) ?? ''

  const env = {
    ...process.env,
    // 'development' is a valid Settings value; there is no 'test' env, and
    // silently falling back would hide a misconfiguration.
    VISIONOPS_ENV: 'development',
    VISIONOPS_API_KEY: apiKey,
    VISIONOPS_DATABASE_URL: `sqlite:///${join(dataDir, 'visionops.db')}`,
    VISIONOPS_DATA_DIR: dataDir,
    VISIONOPS_MEDIA_ROOT: join(REPO_ROOT, 'samples'),
    VISIONOPS_LOG_LEVEL: 'WARNING',
    // A session needs a model. The e2e run uses the real one if it is present
    // and otherwise refuses to start sessions — the tests assert that honestly
    // rather than pretending detections happened.
    VISIONOPS_MAX_SESSIONS: '2',
  }

  // The real entry point (`visionops serve`), not a bare uvicorn invocation:
  // it builds the app through the same factory and lifespan the docs tell
  // operators to use, so the e2e run cannot pass on a configuration nobody ships.
  child = spawn(python, ['-m', 'visionops.cli', 'serve', '--host', '127.0.0.1', '--port', String(PORT)], {
    cwd: REPO_ROOT,
    env: { ...env, PYTHONPATH: join(REPO_ROOT, 'backend') },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  child.stdout?.on('data', (chunk: Buffer) => process.stdout.write(`[api] ${chunk}`))
  child.stderr?.on('data', (chunk: Buffer) => process.stderr.write(`[api] ${chunk}`))

  await waitForHealth(`${BASE_URL}/api/v1/healthz`, 60_000)

  if (child.pid === undefined) throw new Error('the backend process has no pid')
  writeFileSync(
    STATE_FILE,
    JSON.stringify({ apiKey, baseUrl: BASE_URL, dataDir, pid: child.pid }, null, 2),
    'utf8',
  )
  config.metadata.baseURL = BASE_URL
}

async function waitForHealth(url: string, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs
  let lastError = 'no attempt made'
  while (Date.now() < deadline) {
    if (child?.exitCode !== null && child?.exitCode !== undefined) {
      throw new Error(`the backend exited with code ${child.exitCode} during startup`)
    }
    try {
      const response = await fetch(url)
      if (response.ok) return
      lastError = `status ${response.status}`
    } catch (error) {
      lastError = String(error)
    }
    await new Promise((resolve_) => setTimeout(resolve_, 250))
  }
  throw new Error(`the backend never became healthy at ${url}: ${lastError}`)
}

export interface E2EState {
  apiKey: string
  baseUrl: string
  dataDir: string
  pid: number
}

/** Read what the global setup recorded: the key, the URL and the backend pid. */
export function readState(): E2EState {
  return JSON.parse(readFileSync(STATE_FILE, 'utf8')) as E2EState
}
