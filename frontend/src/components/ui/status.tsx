import type * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * The one way Sentinel says whether something is working.
 *
 * A dot plus a word, in that order, everywhere. The colour is a second signal
 * rather than the only one -- a red dot and a green dot are the same dot to a
 * viewer who cannot tell them apart -- so the label is never optional and never
 * repeats what the colour already says ("online", not "green").
 */

export type StatusTone = 'ok' | 'warning' | 'danger' | 'idle' | 'recording' | 'active'

const DOT: Record<StatusTone, string> = {
  ok: 'bg-(--color-success)',
  warning: 'bg-(--color-warning)',
  danger: 'bg-(--color-danger)',
  idle: 'bg-(--color-offline)',
  recording: 'bg-(--color-recording)',
  active: 'bg-(--color-accent)',
}

const TEXT: Record<StatusTone, string> = {
  ok: 'text-(--color-fg)',
  warning: 'text-(--color-warning)',
  danger: 'text-(--color-danger)',
  idle: 'text-(--color-fg-muted)',
  recording: 'text-(--color-recording)',
  active: 'text-(--color-fg)',
}

export interface StatusProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone: StatusTone
  /** Pulse the dot. Only for something genuinely happening right now. */
  live?: boolean
  children: React.ReactNode
}

export function Status({ tone, live = false, className, children, ...props }: StatusProps) {
  return (
    <span
      className={cn('inline-flex min-w-0 items-center gap-1.5 text-xs', TEXT[tone], className)}
      {...props}
    >
      <span
        className={cn('size-1.5 shrink-0 rounded-full', DOT[tone], live && 'pulse-recording')}
        aria-hidden="true"
      />
      <span className="truncate">{children}</span>
    </span>
  )
}
