import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it } from 'vitest'

import { Onboarding } from './Onboarding'

describe('Onboarding', () => {
  beforeEach(() => sessionStorage.clear())

  it('shows the welcome on a fresh browser tab and can skip it for this session', () => {
    render(<MemoryRouter><Onboarding /></MemoryRouter>)
    expect(screen.getByText(/Camera footage, turned into actionable intelligence/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Explore dashboard' }))
    expect(screen.queryByText(/Camera footage, turned into actionable intelligence/)).not.toBeInTheDocument()
    expect(sessionStorage.getItem('visionops.onboarding.complete')).toBe('1')
  })

  it('supports back and next controls and can be skipped', () => {
    render(<MemoryRouter><Onboarding /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: 'Start guided demo' }))
    expect(screen.getByText('Dashboard', { selector: 'h2' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    expect(screen.getByText('Cameras', { selector: 'h2' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(screen.getByText('Dashboard', { selector: 'h2' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Skip tour' }))
    expect(sessionStorage.getItem('visionops.onboarding.complete')).toBe('1')
  })

  it('restores the current tour step after a remount and supports Escape', () => {
    const view = render(<MemoryRouter><Onboarding /></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: 'Start guided demo' }))
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    view.unmount()

    render(<MemoryRouter><Onboarding /></MemoryRouter>)
    expect(screen.getByRole('progressbar', { name: 'Tour progress' })).toHaveAttribute('aria-valuenow', '2')
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})
