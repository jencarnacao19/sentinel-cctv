import * as SwitchPrimitive from '@radix-ui/react-switch'
import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * A two-state control whose current state is legible without reading a label.
 *
 * Used for recording, where "is this camera recording right now?" has to be
 * answerable at a glance across a page of cameras. That instance uses the
 * semantic recording colour rather than the accent, for the same reason the
 * badges do: red means recording everywhere in Sentinel. Everything that is not
 * recording -- a floodlight, an LED, on-camera person detection -- takes the
 * `accent` tone, because a red switch beside "Floodlight" would claim a meaning
 * the control does not have.
 *
 * Colour is never the only signal -- the thumb position differs, and every
 * caller pairs this with a text state -- so it still reads correctly for a
 * viewer who cannot distinguish the two colours.
 */
export interface SwitchProps
  extends React.ComponentPropsWithoutRef<typeof SwitchPrimitive.Root> {
  tone?: 'recording' | 'accent'
}

export const Switch = React.forwardRef<
  React.ElementRef<typeof SwitchPrimitive.Root>,
  SwitchProps
>(({ className, tone = 'recording', ...props }, ref) => (
  <SwitchPrimitive.Root
    ref={ref}
    className={cn(
      'peer relative inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-full',
      'border-2 border-transparent transition-colors duration-(--duration-fast)',
      // The control is 24px tall, which is right visually and too small to hit
      // on a phone. The pseudo-element brings the touch target to 44px without
      // changing the layout or the look.
      "before:absolute before:-inset-x-1 before:-inset-y-2.5 before:content-['']",
      'sm:before:hidden',
      'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-(--color-accent)',
      'disabled:cursor-not-allowed disabled:opacity-50',
      'data-[state=unchecked]:bg-(--color-border-strong)',
      tone === 'recording'
        ? 'data-[state=checked]:bg-(--color-recording)'
        : 'data-[state=checked]:bg-(--color-accent)',
      className,
    )}
    {...props}
  >
    <SwitchPrimitive.Thumb
      className={cn(
        'pointer-events-none block size-5 rounded-full bg-(--color-surface) shadow-sm',
        'transition-transform duration-(--duration-fast)',
        'data-[state=checked]:translate-x-5 data-[state=unchecked]:translate-x-0',
      )}
    />
  </SwitchPrimitive.Root>
))
Switch.displayName = 'Switch'
