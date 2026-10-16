import type * as React from 'react'

import { cn } from '@/lib/utils'

/** Placeholder block shown while real data loads. Never used to fake content. */
export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      role="status"
      aria-busy="true"
      aria-live="polite"
      className={cn('skeleton rounded-sm', className)}
      {...props}
    >
      <span className="sr-only">Loading…</span>
    </div>
  )
}
