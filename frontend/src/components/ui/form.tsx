import * as React from 'react'

import { Label } from '@/components/ui/label'
import { cn } from '@/lib/utils'

/**
 * Form building blocks.
 *
 * Camera setup was one flat run of twenty controls: a required stream URL sat
 * between a patrol dropdown and a segment-length dropdown with nothing to say
 * which of them an operator adding their first camera needed to read. These
 * give a form three things it did not have -- a visible grouping, a label that
 * is always present rather than a placeholder that disappears the moment you
 * type, and one place for the hint and the error so both are wired to the
 * control instead of floating beside it.
 */

export interface FormFieldRenderProps {
  id: string
  'aria-describedby': string | undefined
  'aria-invalid': true | undefined
}

interface FormFieldProps {
  label: React.ReactNode
  /** Shown after the label, in the muted colour. For genuinely optional input. */
  optional?: boolean
  /** Plain-language explanation. Announced with the control, not after it. */
  hint?: React.ReactNode
  /** A validation message. Marks the control invalid and is announced too. */
  error?: React.ReactNode
  id?: string
  className?: string
  children: (field: FormFieldRenderProps) => React.ReactNode
}

/**
 * One labelled control.
 *
 * The render prop exists so the id, `aria-describedby` and `aria-invalid` are
 * generated once and land on the real control. Passing a plain child and hoping
 * the caller repeats the same id by hand is how a label ends up pointing at
 * nothing, which is invisible until someone uses a screen reader.
 */
export function FormField({
  label,
  optional = false,
  hint,
  error,
  id,
  className,
  children,
}: FormFieldProps) {
  const generated = React.useId()
  const fieldId = id ?? generated
  const hintId = hint ? `${fieldId}-hint` : undefined
  const errorId = error ? `${fieldId}-error` : undefined
  const describedBy = [hintId, errorId].filter(Boolean).join(' ') || undefined

  return (
    <div className={cn('space-y-2', className)}>
      <Label htmlFor={fieldId}>
        {label}
        {optional ? (
          <span className="ml-1.5 font-normal text-(--color-fg-subtle)">Optional</span>
        ) : null}
      </Label>
      {children({
        id: fieldId,
        'aria-describedby': describedBy,
        'aria-invalid': error ? true : undefined,
      })}
      {hint ? (
        <p id={hintId} className="text-xs leading-5 text-(--color-fg-muted)">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} className="text-xs leading-5 text-(--color-danger)">
          {error}
        </p>
      ) : null}
    </div>
  )
}

interface FormSectionProps extends Omit<React.HTMLAttributes<HTMLElement>, 'title'> {
  title?: React.ReactNode
  description?: React.ReactNode
  /** Draw the section on its own grouped surface. Use for settings that belong
   *  together and are read as a unit, not for the first two fields of a form. */
  grouped?: boolean
}

/** A titled group of fields. */
export function FormSection({
  title,
  description,
  grouped = false,
  className,
  children,
  ...props
}: FormSectionProps) {
  return (
    <section className={cn('space-y-3', className)} {...props}>
      {title || description ? (
        <div className="space-y-1">
          {title ? (
            <h3 className="text-sm leading-5 font-semibold tracking-[-0.005em]">{title}</h3>
          ) : null}
          {description ? (
            <p className="text-xs leading-5 text-(--color-fg-muted)">{description}</p>
          ) : null}
        </div>
      ) : null}
      <div
        className={cn(
          'space-y-4',
          grouped &&
            'rounded-(--radius-card) border border-(--color-border) bg-(--color-surface-raised) p-4',
        )}
      >
        {children}
      </div>
    </section>
  )
}

interface FormDisclosureProps {
  title: React.ReactNode
  description?: React.ReactNode
  defaultOpen?: boolean
  className?: string
  children: React.ReactNode
}

/**
 * Advanced settings, closed by default.
 *
 * A native `<details>` rather than a state-driven panel: it opens with Enter or
 * Space, is announced as expanded or collapsed, is findable by the browser's
 * own in-page search even while closed, and needs no JavaScript to work.
 */
export function FormDisclosure({
  title,
  description,
  defaultOpen = false,
  className,
  children,
}: FormDisclosureProps) {
  return (
    <details
      open={defaultOpen}
      className={cn(
        'group overflow-hidden rounded-(--radius-card) border border-(--color-border)',
        'bg-(--color-surface-raised)',
        className,
      )}
    >
      <summary
        className={cn(
          'flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 px-4 py-3',
          'text-sm font-medium marker:hidden hover:bg-(--color-surface-hover)',
          '[&::-webkit-details-marker]:hidden',
        )}
      >
        <span className="min-w-0">
          <span className="block">{title}</span>
          {description ? (
            <span className="mt-0.5 block text-xs font-normal text-(--color-fg-muted)">
              {description}
            </span>
          ) : null}
        </span>
        <span
          className="shrink-0 text-(--color-fg-subtle) transition-transform duration-(--duration-fast) group-open:rotate-180"
          aria-hidden="true"
        >
          <svg viewBox="0 0 16 16" className="size-4" fill="none" stroke="currentColor">
            <path d="m4 6 4 4 4-4" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </span>
      </summary>
      <div className="space-y-4 border-t border-(--color-border) p-4">{children}</div>
    </details>
  )
}

/**
 * A grouped surface of rows separated by hairlines -- the iOS inset list.
 *
 * Used where several settings are read as one block. Prefer this to giving
 * every row its own border: a page of individually-bordered rows reads as a
 * page of unrelated boxes.
 */
export function SurfaceGroup({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'divide-y divide-(--color-border) overflow-hidden rounded-(--radius-card)',
        'border border-(--color-border) bg-(--color-surface)',
        className,
      )}
      {...props}
    />
  )
}
