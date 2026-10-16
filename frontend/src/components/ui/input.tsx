import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * A text field.
 *
 * The size step is not cosmetic: iOS Safari zooms the whole page in when a
 * field with a computed font size below 16px receives focus, and then leaves
 * the page zoomed. So the control is 16px on a phone and drops to 14px from the
 * `sm` breakpoint, where the zoom rule does not apply and density is worth more.
 *
 * `scroll-comfort` gives the browser somewhere to put the field when it scrolls
 * it into view, so a focused input lands below the pinned header rather than
 * underneath it, and above the on-screen keyboard rather than behind it.
 */
export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, type = 'text', ...props }, ref) {
    return (
      <input
        ref={ref}
        type={type}
        className={cn(
          'scroll-comfort flex h-11 w-full rounded-(--radius-input) border sm:h-9 ' +
            'border-(--color-border) bg-(--color-surface-raised) px-3 py-1 ' +
            'text-base text-(--color-fg) transition-colors duration-(--duration-fast) sm:text-sm ' +
            'placeholder:text-(--color-fg-subtle) ' +
            'focus-visible:border-(--color-accent) focus-visible:outline-2 ' +
            'focus-visible:outline-offset-1 focus-visible:outline-(--color-accent) ' +
            'disabled:cursor-not-allowed disabled:opacity-50 ' +
            'aria-invalid:border-(--color-danger)',
          className,
        )}
        {...props}
      />
    )
  },
)
