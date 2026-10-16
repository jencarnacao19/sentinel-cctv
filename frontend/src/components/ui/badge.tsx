import { cva, type VariantProps } from 'class-variance-authority'
import type * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * A short, factual label attached to something else.
 *
 * Set in the interface face rather than in monospace uppercase. Twelve
 * lowercase words rendered as `ONLINE` `RECORDING` `EVENT CLIPS` across a page
 * of cameras read as a console readout, and letter-spaced capitals are measurably
 * slower to read than the sentence case the rest of the interface uses.
 */
const badgeVariants = cva(
  'inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] leading-4 ' +
    'font-medium first-letter:uppercase',
  {
    variants: {
      variant: {
        neutral: 'bg-(--color-bg-subtle) text-(--color-fg-muted)',
        success: 'bg-(--color-success)/15 text-(--color-success)',
        warning: 'bg-(--color-warning)/15 text-(--color-warning)',
        danger: 'bg-(--color-danger)/15 text-(--color-danger)',
        recording: 'bg-(--color-recording)/15 text-(--color-recording)',
        motion: 'bg-(--color-motion)/15 text-(--color-motion)',
        accent: 'bg-(--color-accent)/15 text-(--color-accent)',
        /** A hairline outline for a label that must not compete with a status. */
        outline: 'border border-(--color-border) text-(--color-fg-muted)',
      },
    },
    defaultVariants: { variant: 'neutral' },
  },
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />
}
