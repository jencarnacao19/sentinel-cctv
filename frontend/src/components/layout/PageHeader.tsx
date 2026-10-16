import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'

interface PageHeaderProps {
  title: string
  description?: ReactNode
  /** Primary and secondary actions for this screen. */
  actions?: ReactNode
  /** A control that belongs to the content rather than to the title, such as a
   *  density switch. Placed under the actions on a phone so it never competes
   *  with them for the same row. */
  aside?: ReactNode
  className?: string
}

/**
 * The top of a screen.
 *
 * Every page had grown its own header markup with a different heading size,
 * a different gap and a different rule for where the buttons wrap. On a phone
 * the result was a 24px title, a paragraph and two full-width buttons all
 * fighting for the first screenful before any camera appeared.
 *
 * The title is large on desktop and one step down on a phone -- the mobile
 * navigation bar above it already names the screen, so a second 30px repetition
 * of the same word is wasted height rather than hierarchy.
 */
export function PageHeader({ title, description, actions, aside, className }: PageHeaderProps) {
  return (
    <header className={cn('flex flex-col gap-3', className)}>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
        <div className="min-w-0">
          {/* `data-page-title` is what the mobile navigation bar watches: its
              own copy of the title fades in only once this one has scrolled
              away, so the two are never on screen together. */}
          <h1
            data-page-title
            className="text-xl leading-tight font-semibold tracking-[-0.02em] sm:text-2xl"
          >
            {title}
          </h1>
          {description ? (
            <p className="mt-1 max-w-prose text-sm leading-5 text-(--color-fg-muted)">
              {description}
            </p>
          ) : null}
        </div>
        {actions ? <div className="flex shrink-0 flex-wrap gap-2">{actions}</div> : null}
      </div>
      {aside ? <div className="flex min-w-0 flex-wrap items-center gap-2">{aside}</div> : null}
    </header>
  )
}
