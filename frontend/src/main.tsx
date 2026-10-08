import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createBrowserRouter, createRoutesFromElements, Route } from 'react-router-dom'

import './index.css'
import { AppLayout, ErrorBoundary } from './App'
import { Dashboard } from './pages/Dashboard'
import { CameraView } from './pages/CameraView'
import { Events } from './pages/Events'
import { EventDetail } from './pages/EventDetail'
import { Zones } from './pages/Zones'
import { SystemStatus } from './pages/SystemStatus'
import { Setup } from './pages/Setup'
import { NotFound } from './pages/NotFound'
import { ToastProvider } from './components/Toast'
import { RequireApiKey } from './pages/RequireApiKey'

/**
 * Query defaults. Live endpoints poll; configuration is cached; history keeps
 * its data until something invalidates it. `retry: 1` so a transient failure
 * does not hammer the API, and a 401 is never retried.
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (failureCount, error) => {
        const status = (error as { status?: number }).status
        if (status === 401 || status === 403 || status === 404) return false
        return failureCount < 1
      },
      refetchOnWindowFocus: true,
      staleTime: 5000,
    },
    mutations: { retry: 0 },
  },
})

const router = createBrowserRouter(
  createRoutesFromElements(
    <Route element={<AppLayout />}>
      <Route path="/" element={<Dashboard />} />
      <Route path="/setup" element={<Setup />} />
      <Route path="/cameras" element={<CameraView />} />
      <Route path="/cameras/:cameraId" element={<CameraView />} />
      <Route path="/zones" element={<Zones />} />
      <Route path="/zones/:cameraId" element={<Zones />} />
      <Route path="/events" element={<Events />} />
      <Route path="/events/:eventId" element={<EventDetail />} />
      <Route path="/system" element={<SystemStatus />} />
      <Route path="*" element={<NotFound />} />
    </Route>,
  ),
)

const container = document.getElementById('root')
if (!container) throw new Error('#root is missing from index.html')

createRoot(container).render(
  <StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>
          <RequireApiKey>
            <RouterProvider router={router} />
          </RequireApiKey>
        </ToastProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
)
