import * as DropdownMenuPrimitive from '@radix-ui/react-dropdown-menu'
import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * The overflow menu for actions that do not deserve a permanent button.
 *
 * A camera row had six or seven buttons side by side, which on a phone wrapped
 * into three rows and made the two that matter -- recording and live view --
 * indistinguishable from PTZ redetection. Everything occasional moves in here.
 *
 * Radix handles the parts that are easy to get wrong by hand: focus moves into
 * the menu on open and back to the trigger on close, arrow keys and typeahead
 * work, Escape and outside clicks dismiss, and the trigger carries the right
 * `aria-expanded`/`aria-haspopup`.
 */

export const DropdownMenu = DropdownMenuPrimitive.Root
export const DropdownMenuTrigger = DropdownMenuPrimitive.Trigger
export const DropdownMenuGroup = DropdownMenuPrimitive.Group

export const DropdownMenuContent = React.forwardRef<
  React.ElementRef<typeof DropdownMenuPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Content>
>(({ className, sideOffset = 6, align = 'end', ...props }, ref) => (
  <DropdownMenuPrimitive.Portal>
    <DropdownMenuPrimitive.Content
      ref={ref}
      align={align}
      sideOffset={sideOffset}
      className={cn(
        'z-50 min-w-56 overflow-hidden rounded-(--radius-card) border border-(--color-border)',
        'bg-(--color-surface) p-1 shadow-(--shadow-popover)',
        // Bounded by the viewport rather than by a guessed height, so a long
        // menu scrolls on a phone instead of running off the bottom.
        'max-h-(--radix-dropdown-menu-content-available-height) overflow-y-auto',
        className,
      )}
      {...props}
    />
  </DropdownMenuPrimitive.Portal>
))
DropdownMenuContent.displayName = 'DropdownMenuContent'

export const DropdownMenuItem = React.forwardRef<
  React.ElementRef<typeof DropdownMenuPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Item> & { danger?: boolean }
>(({ className, danger = false, ...props }, ref) => (
  <DropdownMenuPrimitive.Item
    ref={ref}
    className={cn(
      'relative flex min-h-11 cursor-default items-center gap-2.5 rounded-sm px-2.5 text-sm sm:min-h-9',
      'outline-none select-none',
      'focus:bg-(--color-surface-hover) data-highlighted:bg-(--color-surface-hover)',
      'data-disabled:pointer-events-none data-disabled:opacity-50',
      '[&_svg]:size-4 [&_svg]:shrink-0 [&_svg]:text-(--color-fg-subtle)',
      danger
        ? 'text-(--color-danger) [&_svg]:text-(--color-danger)'
        : 'text-(--color-fg)',
      className,
    )}
    {...props}
  />
))
DropdownMenuItem.displayName = 'DropdownMenuItem'

export function DropdownMenuLabel({
  className,
  ...props
}: React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Label>) {
  return (
    <DropdownMenuPrimitive.Label
      className={cn(
        'px-2.5 py-1.5 text-[11px] font-semibold text-(--color-fg-subtle)',
        className,
      )}
      {...props}
    />
  )
}

export function DropdownMenuSeparator({
  className,
  ...props
}: React.ComponentPropsWithoutRef<typeof DropdownMenuPrimitive.Separator>) {
  return (
    <DropdownMenuPrimitive.Separator
      className={cn('my-1 h-px bg-(--color-border)', className)}
      {...props}
    />
  )
}
