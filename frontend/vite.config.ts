import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const API_TARGET = process.env.VISIONOPS_API_TARGET ?? 'http://127.0.0.1:8000'

/**
 * The dev server proxies `/api` and the WebSocket so the app is same-origin in
 * development exactly as it is in production behind nginx. No CORS, no env
 * juggling, and the browser camera flow is not affected by a cross-origin page.
 */
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(fileURLToPath(new URL('.', import.meta.url)), 'src') },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: false,
        ws: true,
      },
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
    proxy: {
      '/api': { target: API_TARGET, ws: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    target: 'es2022',
    chunkSizeWarningLimit: 700,
  },
})
