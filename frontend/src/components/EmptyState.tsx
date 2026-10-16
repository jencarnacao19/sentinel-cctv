import type { LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'

interface EmptyStateProps {
  icon?: LucideIcon
  title: string
  description?: string
  action?: ReactNode
  className?: string
}

/**
 * Shown when a list genuinely has nothing in it. Sentinel never renders sample
 * or placeholder data to fill a screen -- an empty dashboard says so plainly and
 * offers the one action that fixes it.
 *
 * A quiet grouped surface rather than a dashed outline: a dashed box reads as a
 * drop target or an unfinished region, and there is nothing to drop here.
 */
export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        'flex flex-col items-center justify-center rounded-(--radius-card) border ' +
          'border-(--color-border) bg-(--color-surface) px-6 py-14 text-center',
        className,
      )}
    >
      {Icon ? (
        <div className="mb-4 rounded-full bg-(--color-bg-subtle) p-3.5">
          <Icon className="size-6 text-(--color-fg-subtle)" aria-hidden="true" />
        </div>
      ) : null}
      <h3 className="text-base font-semibold text-(--color-fg)">{title}</h3>
      {description ? (
        <p className="mt-1.5 max-w-sm text-sm leading-5 text-(--color-fg-muted)">{description}</p>
      ) : null}
      {action ? <div className="mt-5">{action}</div> : null}
    </div>
  )
}
