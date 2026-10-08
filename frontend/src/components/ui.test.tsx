import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { Badge, Banner, Button, DataTable, EmptyState, Field, StatCard, TextInput } from '@/components/ui'
import { cx } from '@/lib/cx'

describe('cx', () => {
  it('joins truthy class names only', () => {
    expect(cx('a', false, null, undefined, 'b', 0)).toBe('a b')
  })
})

describe('Button', () => {
  it('disables and shows a spinner while loading', () => {
    render(<Button loading>Save</Button>)
    const button = screen.getByRole('button', { name: /save/i })
    expect(button).toBeDisabled()
  })

  it('calls the handler when clicked', async () => {
    let clicked = false
    render(<Button onClick={() => (clicked = true)}>Go</Button>)
    await userEvent.click(screen.getByRole('button', { name: 'Go' }))
    expect(clicked).toBe(true)
  })
})

describe('StatCard', () => {
  it('renders a label, a value and a hint', () => {
    render(<StatCard label="Events" value={128} hint="today" state="warn" />)
    expect(screen.getByText('Events')).toBeInTheDocument()
    expect(screen.getByText('128')).toBeInTheDocument()
    expect(screen.getByText('today')).toBeInTheDocument()
  })
})

describe('Banner', () => {
  it('exposes a live region and can be dismissed', async () => {
    let dismissed = false
    render(
      <Banner tone="warn" title="Source degraded" onDismiss={() => (dismissed = true)}>
        Camera index 0 is busy.
      </Banner>,
    )
    const banner = screen.getByRole('status')
    expect(banner).toHaveTextContent('Source degraded')
    await userEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(dismissed).toBe(true)
  })

  it('can be announced assertively', () => {
    render(<Banner tone="crit" role="alert" title="Failed" />)
    expect(screen.getByRole('alert')).toBeInTheDocument()
  })
})

describe('Field', () => {
  it('shows a validation error and links the label to the input', () => {
    render(
      <Field label="Name" error="must not be blank" htmlFor="name">
        <TextInput id="name" />
      </Field>,
    )
    expect(screen.getByLabelText('Name')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('must not be blank')
  })
})

describe('EmptyState', () => {
  it('always offers a way forward', () => {
    render(<EmptyState title="No zones yet">Draw one on the video.</EmptyState>)
    expect(screen.getByText('No zones yet')).toBeInTheDocument()
    expect(screen.getByText('Draw one on the video.')).toBeInTheDocument()
  })
})

describe('DataTable', () => {
  const rows = [
    { id: 'a', name: 'restricted', severity: 'warning' },
    { id: 'b', name: 'loading bay', severity: 'critical' },
  ]
  const columns = [
    { key: 'name', header: 'Zone', render: (row: (typeof rows)[number]) => row.name },
    { key: 'severity', header: 'Severity', render: (row: (typeof rows)[number]) => row.severity },
  ]

  it('renders headers and rows', () => {
    render(<DataTable rows={rows} columns={columns} rowKey={(row) => row.id} />)
    expect(screen.getByText('Zone')).toBeInTheDocument()
    expect(screen.getByText('loading bay')).toBeInTheDocument()
  })

  it('shows an empty message instead of an empty table', () => {
    render(<DataTable rows={[]} columns={columns} rowKey={(row) => row.id} emptyMessage="Nothing here" />)
    expect(screen.getByText('Nothing here')).toBeInTheDocument()
  })

  it('notifies on row click', async () => {
    let clicked: string | null = null
    render(
      <DataTable rows={rows} columns={columns} rowKey={(row) => row.id} onRowClick={(row) => (clicked = row.id)} />,
    )
    await userEvent.click(screen.getByText('restricted'))
    expect(clicked).toBe('a')
  })
})

describe('Badge', () => {
  it('carries text, never colour alone', () => {
    render(<Badge className="bg-warn/12 text-warn">Warning</Badge>)
    expect(screen.getByText('Warning')).toBeInTheDocument()
  })
})
