import { ChevronDown } from 'lucide-react'
import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * A native `<select>` that matches the rest of the form.
 *
 * Deliberately native rather than a custom listbox: on iOS this opens the
 * system wheel picker, which is the control a person already knows how to use
 * one-handed, and it inherits VoiceOver behaviour, keyboard typeahead and the
 * platform's own scrolling for free. Nine separate screens were each restyling
 * a bare `<select>` with a slightly different height, radius and background --
 * two of them against a `--radius-input` token that did not exist, so those two
 * rendered with square corners.
 *
 * Only the chrome is ours: the arrow is drawn as an overlay because the
 * built-in one cannot be recoloured for a dark surface, and it is
 * `pointer-events-none` so every click still reaches the real control.
 */
export const Select = React.forwardRef<
  HTMLSelectElement,
  React.SelectHTMLAttributes<HTMLSelectElement>
>(function Select({ className, children, ...props }, ref) {
  return (
    <div className="relative">
      <select
        ref={ref}
        className={cn(
          'h-11 w-full appearance-none rounded-(--radius-input) border border-(--color-border) sm:h-9',
          'bg-(--color-surface-raised) py-1 pr-10 pl-3 text-sm text-(--color-fg)',
          'transition-colors duration-(--duration-fast)',
          'focus-visible:border-(--color-accent) focus-visible:outline-2',
          'focus-visible:outline-offset-1 focus-visible:outline-(--color-accent)',
          'disabled:cursor-not-allowed disabled:opacity-50',
          'aria-invalid:border-(--color-danger)',
          className,
        )}
        {...props}
      >
        {children}
      </select>
      <ChevronDown
        className="pointer-events-none absolute top-1/2 right-3 size-4 -translate-y-1/2 text-(--color-fg-subtle)"
        aria-hidden="true"
      />
    </div>
  )
})
