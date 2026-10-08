import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

/**
 * Test configuration is kept separate from `vite.config.ts` because Vitest
 * ships its own Vite copy; sharing one file makes the two plugin types clash.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(fileURLToPath(new URL('.', import.meta.url)), 'src') },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html'],
      include: ['src/lib/**', 'src/hooks/**'],
      // PRD §48 gate: the frontend must stay at or above 70%. Raise it, never
      // lower it (docs/AGENT_WORKFLOW.md §"Never weaken a gate").
      thresholds: { lines: 70, functions: 70, statements: 70, branches: 70 },
    },
  },
})
