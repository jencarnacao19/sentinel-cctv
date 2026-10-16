import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * A checkbox and its label as one row.
 *
 * Four dialogs each hand-rolled `<label class="flex min-h-11 …"><input
 * type="checkbox" class="size-4">` with slightly different spacing, and none of
 * them had room for the explanatory sentence that always followed. This keeps
 * the whole row as the hit target -- the 16px box alone is a quarter of the
 * 44px minimum -- and gives the description a place to live that the label's
 * accessible name does not swallow.
 *
 * Still a real `<input type="checkbox">`: it is announced correctly, it toggles
 * with Space, and it participates in a form without any of its own JavaScript.
 */
export interface CheckboxFieldProps
  extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'type'> {
  label: React.ReactNode
  description?: React.ReactNode
}

export const CheckboxField = React.forwardRef<HTMLInputElement, CheckboxFieldProps>(
  function CheckboxField({ className, label, description, id, disabled, ...props }, ref) {
    const generated = React.useId()
    const inputId = id ?? generated
    const descriptionId = description ? `${inputId}-description` : undefined

    return (
      <div className={cn('flex flex-col', className)}>
        <label
          htmlFor={inputId}
          className={cn(
            'flex min-h-11 cursor-pointer items-center gap-3 text-sm font-medium',
            disabled && 'cursor-not-allowed opacity-60',
          )}
        >
          <input
            ref={ref}
            id={inputId}
            type="checkbox"
            disabled={disabled}
            aria-describedby={descriptionId}
            className={cn(
              'size-5 shrink-0 rounded-(--radius-xs) accent-(--color-accent)',
              'disabled:cursor-not-allowed',
            )}
            {...props}
          />
          <span className="min-w-0">{label}</span>
        </label>
        {description ? (
          <p id={descriptionId} className="pl-8 text-xs leading-5 text-(--color-fg-muted)">
            {description}
          </p>
        ) : null}
      </div>
    )
  },
)
