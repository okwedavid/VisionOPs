import { Link } from 'react-router-dom'

import { EmptyState } from '@/components/ui'

export function NotFound() {
  return (
    <EmptyState
      title="Page not found"
      icon="🧭"
      action={
        <Link to="/" className="btn-primary">
          Back to the dashboard
        </Link>
      }
    >
      That URL does not match any screen in VisionOps. If you followed a link from somewhere, it may point at a
      version that has moved on.
    </EmptyState>
  )
}
