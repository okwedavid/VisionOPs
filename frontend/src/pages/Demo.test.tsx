import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { Demo } from './Demo'

describe('Demo', () => {
  it('labels illustrative values as demo content and gives an exit to camera setup', () => {
    render(<MemoryRouter><Demo /></MemoryRouter>)
    expect(screen.getAllByText(/DEMO/i).length).toBeGreaterThan(0)
    expect(screen.getByText(/not a camera feed, model output, or a record from your site/i)).toBeInTheDocument()
    expect(screen.getAllByRole('link', { name: /connect your own camera/i }).length).toBeGreaterThan(0)
    expect(screen.getByText(/sample only/i)).toBeInTheDocument()
  })
})
