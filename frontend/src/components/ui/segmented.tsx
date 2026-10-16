import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * A segmented control: a small set of mutually exclusive choices, all visible.
 *
 * Three screens had grown their own version of this -- the live grid density,
 * the viewer's copy of it, and the theme switcher -- each with a different
 * height, radius and active treatment, and none of them reachable with the
 * keyboard beyond tabbing through every option in turn.
 *
 * The keyboard behaviour is the one a radio group is supposed to have: one tab
 * stop for the whole control, arrow keys move *and* select, Home and End jump
 * to the ends. That is why it is a `radiogroup` of `radio`s rather than a row
 * of buttons.
 */

export interface SegmentedOption<T extends string> {
  value: T
  /** Visible text. Omit for an icon-only segment and give `label` instead. */
  children?: React.ReactNode
  /** Accessible name. Required when there is no visible text. */
  label: string
  icon?: React.ReactNode
}

interface SegmentedProps<T extends string> {
  value: T
  onChange: (value: T) => void
  options: SegmentedOption<T>[]
  /** Names the whole control for assistive technology. */
  ariaLabel: string
  /** Compact is for controls that sit in dense chrome, such as a sidebar. */
  size?: 'md' | 'compact'
  className?: string
}

export function Segmented<T extends string>({
  value,
  onChange,
  options,
  ariaLabel,
  size = 'md',
  className,
}: SegmentedProps<T>) {
  const refs = React.useRef(new Map<T, HTMLButtonElement | null>())

  function move(delta: number): void {
    const index = options.findIndex((option) => option.value === value)
    if (index < 0) return
    const next = options[(index + delta + options.length) % options.length]
    onChange(next.value)
    refs.current.get(next.value)?.focus()
  }

  function jump(to: 0 | -1): void {
    const next = to === 0 ? options[0] : options[options.length - 1]
    onChange(next.value)
    refs.current.get(next.value)?.focus()
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>): void {
    switch (event.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        event.preventDefault()
        move(1)
        break
      case 'ArrowLeft':
      case 'ArrowUp':
        event.preventDefault()
        move(-1)
        break
      case 'Home':
        event.preventDefault()
        jump(0)
        break
      case 'End':
        event.preventDefault()
        jump(-1)
        break
    }
  }

  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      onKeyDown={onKeyDown}
      className={cn(
        'inline-flex max-w-full items-center gap-0.5 overflow-x-auto rounded-(--radius-md)',
        'bg-(--color-bg-subtle) p-0.5',
        className,
      )}
    >
      {options.map((option) => {
        const active = option.value === value
        return (
          <button
            key={option.value}
            ref={(node) => {
              refs.current.set(option.value, node)
            }}
            type="button"
            role="radio"
            aria-checked={active}
            aria-label={option.children ? undefined : option.label}
            // One tab stop for the group: only the selected segment is
            // reachable with Tab, and the arrows move within it.
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(option.value)}
            className={cn(
              'flex shrink-0 items-center justify-center gap-1.5 rounded-[calc(var(--radius-md)-2px)]',
              'text-xs font-medium transition-colors duration-(--duration-fast)',
              'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-(--color-accent)',
              size === 'compact' ? 'min-h-7 px-2.5' : 'min-h-9 px-3 sm:min-h-8',
              active
                ? 'bg-(--color-surface) text-(--color-fg) shadow-(--shadow-raised)'
                : 'text-(--color-fg-subtle) hover:text-(--color-fg)',
              '[&_svg]:size-3.5 [&_svg]:shrink-0',
            )}
          >
            {option.icon}
            {option.children}
          </button>
        )
      })}
    </div>
  )
}
