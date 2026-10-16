import * as DialogPrimitive from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * One modal surface: a centred dialog on a large screen, a bottom sheet on a
 * phone.
 *
 * The layout is the whole point. Previously `DialogContent` was itself the
 * scroll container, with `position: sticky` header and footer floating inside
 * it. That works until it does not: on a short window the sticky footer sat on
 * top of the last field, on iOS the sticky elements detached while momentum
 * scrolling, a `max-h` measured in `vh` disagreed with Safari's collapsing
 * toolbar, and any child that also scrolled produced two nested scroll
 * containers with no way to tell which one a gesture belonged to.
 *
 * The replacement is a flex column with exactly three regions:
 *
 *   DialogContent   flex column, height bounded by the *dynamic* viewport
 *     DialogHeader  shrink-0 -- always visible, never scrolls
 *     DialogBody    flex-1 min-h-0 -- the one and only scroll container
 *     DialogFooter  shrink-0 -- always reachable, above the home indicator
 *
 * `min-h-0` on the body is what makes this work: without it a flex child
 * refuses to shrink below its content, the column grows past the container, and
 * the footer is pushed off-screen -- which is exactly the bug this replaces.
 *
 * A form that spans body and footer must use `DialogForm`, which carries the
 * same `flex min-h-0 flex-1 flex-col`, so inserting it does not break the
 * column.
 */

export const Dialog = DialogPrimitive.Root
export const DialogTrigger = DialogPrimitive.Trigger
export const DialogClose = DialogPrimitive.Close

export const DialogContent = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content>
>(function DialogContent({ className, children, ...props }, ref) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay
        className="animate-fade-in fixed inset-0 z-50 bg-(--material-scrim)"
        data-testid="dialog-overlay"
      />
      <DialogPrimitive.Content
        ref={ref}
        data-testid="dialog-content"
        className={cn(
          // Shared: a bounded flex column. Nothing here scrolls.
          'fixed z-50 flex flex-col overflow-hidden bg-(--color-surface) ' +
            'border-(--color-border) shadow-(--shadow-sheet) outline-none',
          // Phone: a sheet attached to the bottom edge. `dvh` rather than `vh`
          // so Safari's collapsing toolbar and the on-screen keyboard both
          // shorten it instead of hiding the footer behind them.
          'inset-x-0 bottom-0 mx-auto max-h-[92dvh] rounded-t-(--radius-sheet) border-t ' +
            'animate-sheet-rise',
          // Desktop: a centred dialog, deliberately not much wider than the
          // form inside it, and never taller than a short laptop window.
          'sm:inset-x-auto sm:bottom-auto sm:top-1/2 sm:left-1/2 sm:w-[calc(100%-3rem)] ' +
            'sm:max-w-lg sm:max-h-[min(85dvh,44rem)] sm:-translate-x-1/2 sm:-translate-y-1/2 ' +
            'sm:rounded-(--radius-xl) sm:border sm:shadow-(--shadow-popover) sm:animate-fade-in',
          className,
        )}
        {...props}
      >
        {/* The grab handle is the affordance that says "this sheet came from the
            bottom edge". Purely decorative, and gone once there is room for a
            real dialog. */}
        <div className="flex shrink-0 justify-center pt-2 sm:hidden" aria-hidden="true">
          <span className="h-1 w-9 rounded-full bg-(--color-border-strong)" />
        </div>
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  )
})

/**
 * The pinned title area.
 *
 * The close control lives here rather than floating over the content, so it
 * cannot land on top of a field and it keeps a full 44px touch target on a
 * phone without overlapping the title text.
 */
export function DialogHeader({ className, children, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'flex shrink-0 items-start gap-3 border-b border-(--color-border) px-5 pt-4 pb-4 sm:pt-5',
        className,
      )}
      {...props}
    >
      <div className="flex min-w-0 flex-1 flex-col gap-1">{children}</div>
      <DialogPrimitive.Close
        className={cn(
          '-mt-1 -mr-2 flex size-11 shrink-0 items-center justify-center rounded-full sm:size-9',
          'text-(--color-fg-subtle) transition-colors duration-(--duration-fast)',
          'hover:bg-(--color-surface-hover) hover:text-(--color-fg)',
        )}
        aria-label="Close"
      >
        <X className="size-4.5" aria-hidden="true" />
      </DialogPrimitive.Close>
    </div>
  )
}

/**
 * The single scroll container.
 *
 * `scroll-region` carries `overscroll-behavior: contain`, which is what stops a
 * flick that reaches the end of this list from continuing into the page behind
 * the sheet. Nothing inside should scroll on its own.
 */
export function DialogBody({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      data-testid="dialog-body"
      className={cn('scroll-region min-h-0 flex-1 px-5 py-4', className)}
      {...props}
    />
  )
}

/**
 * The pinned action area.
 *
 * Column-reversed on a phone so the primary action is the one under the thumb,
 * and padded past the home indicator so the last button is never half-covered.
 */
export function DialogFooter({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      data-testid="dialog-footer"
      className={cn(
        'flex shrink-0 flex-col-reverse gap-2 border-t border-(--color-border) px-5 pt-4',
        'pb-safe-gutter sm:flex-row sm:justify-end sm:pb-4',
        // A phone in landscape, or a short desktop window, has no room for a
        // stack: three 44px buttons and their gaps take 150px of a 480px-tall
        // viewport, all of it out of the body. Below that height they go back
        // into a row, where they cost one button's worth.
        '[@media(max-height:40rem)]:flex-row [@media(max-height:40rem)]:justify-end',
        className,
      )}
      {...props}
    />
  )
}

/**
 * A form spanning the body and the footer.
 *
 * Inserting a plain `<form>` between `DialogContent` and its regions would make
 * the form the only flex child, collapsing the three-region column back into
 * one scrolling block. This keeps the column intact.
 */
export const DialogForm = React.forwardRef<
  HTMLFormElement,
  React.FormHTMLAttributes<HTMLFormElement>
>(function DialogForm({ className, ...props }, ref) {
  return <form ref={ref} className={cn('flex min-h-0 flex-1 flex-col', className)} {...props} />
})

export const DialogTitle = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(function DialogTitle({ className, ...props }, ref) {
  return (
    <DialogPrimitive.Title
      ref={ref}
      className={cn('text-[17px] leading-6 font-semibold tracking-[-0.01em]', className)}
      {...props}
    />
  )
})

export const DialogDescription = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(function DialogDescription({ className, ...props }, ref) {
  return (
    <DialogPrimitive.Description
      ref={ref}
      className={cn('text-sm leading-5 text-(--color-fg-muted)', className)}
      {...props}
    />
  )
})
