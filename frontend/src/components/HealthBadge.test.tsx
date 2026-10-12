import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { HealthBadge } from '@/components/HealthBadge'
import type { HealthSnapshot, UseHealthResult } from '@/hooks/useHealth'

const { useHealthStatusMock } = vi.hoisted(() => ({ useHealthStatusMock: vi.fn() }))

vi.mock('@/hooks/useHealth', () => ({
  useHealthStatus: useHealthStatusMock,
}))

function snapshot(overrides: Partial<HealthSnapshot> = {}): HealthSnapshot {
  return {
    state: 'healthy',
    checks: {
      api: { state: 'ok', reason_code: null, detail: 'API responding', required: true },
      database: { state: 'ok', reason_code: null, detail: '1.2 ms', required: true },
      model: { state: 'ok', reason_code: null, detail: 'yolo11n ready', required: false },
    },
    reachable: true,
    authError: false,
    stale: false,
    lastSuccessAt: Date.now(),
    timestamp: new Date().toISOString(),
    ...overrides,
  }
}

function setHealth(overrides: Partial<HealthSnapshot> = {}, extra: Partial<UseHealthResult> = {}) {
  useHealthStatusMock.mockReturnValue({
    ...snapshot(overrides),
    isRefreshing: false,
    refresh: vi.fn(),
    ...extra,
  })
}

function renderBadge() {
  return render(
    <MemoryRouter>
      <HealthBadge />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  setHealth()
})

describe('HealthBadge', () => {
  it('shows the verdict word with an accessible label', () => {
    renderBadge()
    const button = screen.getByRole('button', { name: /healthy/i })
    expect(button).toHaveAttribute('aria-expanded', 'false')
  })

  it('opens a diagnostics panel with each subsystem and its required flag', async () => {
    renderBadge()
    await userEvent.click(screen.getByRole('button', { name: /healthy/i }))
    const panel = screen.getByRole('dialog', { name: /system health/i })
    expect(within(panel).getByText('database')).toBeInTheDocument()
    expect(within(panel).getAllByText('required').length).toBeGreaterThan(0)
    expect(within(panel).getByRole('link', { name: /open system status/i })).toBeInTheDocument()
  })

  it('closes when the toggle is clicked again and on Escape', async () => {
    renderBadge()
    const button = screen.getByRole('button', { name: /healthy/i })
    await userEvent.click(button)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await userEvent.click(button)
    await userEvent.click(button)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('warns when the data is stale', async () => {
    setHealth({ state: 'offline', stale: true })
    renderBadge()
    await userEvent.click(screen.getByRole('button', { name: /offline/i }))
    expect(screen.getByText(/last known state/i)).toBeInTheDocument()
  })

  it('renders the checking and degraded verdicts', () => {
    setHealth({ state: 'checking', checks: {} })
    const { unmount } = renderBadge()
    expect(screen.getByRole('button', { name: /checking/i })).toBeInTheDocument()
    unmount()
    setHealth({ state: 'degraded' })
    renderBadge()
    expect(screen.getByRole('button', { name: /degraded/i })).toBeInTheDocument()
  })
})
