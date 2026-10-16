import { useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { PLAYBACK_SPEEDS, type PlaybackSpeed } from '@/features/timeline/playbackBuffer'
import { cn } from '@/lib/utils'

/**
 * Direct selection of a playback speed.
 *
 * The control this replaces cycled 0.5 → 1 → 2 on every click. With six speeds
 * that would mean up to five presses to reach one of them, on a control bar
 * where every other button does its whole job in one — so this opens a list and
 * the speed you want is one press away wherever you are.
 *
 * It is a listbox rather than a `<select>` because the bar sits over video on a
 * dark surface, and a native dropdown cannot be styled to match it on every
 * platform. Everything a native one gives for free is therefore provided
 * explicitly: roles, `aria-activedescendant`, arrow/Home/End/Escape keys, focus
 * return, outside-click dismissal and 44px touch targets.
 */

const LABELS: Record<PlaybackSpeed, string> = {
  0.5: 'Half speed',
  1: 'Normal speed',
  2: 'Double speed',
  4: 'Four times speed',
  8: 'Eight times speed',
  16: 'Sixteen times speed',
}

/** Speeds at and above which the browser stops producing audible sound. */
const SILENT_FROM: PlaybackSpeed = 4

export interface PlaybackSpeedMenuProps {
  value: PlaybackSpeed
  onChange: (speed: PlaybackSpeed) => void
  disabled?: boolean
}

export function PlaybackSpeedMenu({ value, onChange, disabled = false }: PlaybackSpeedMenuProps) {
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState<PlaybackSpeed>(value)
  const containerRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)

  // Reopening should land on what is playing now, not on wherever the previous
  // visit left the highlight.
  useEffect(() => {
    if (open) setActive(value)
  }, [open, value])

  useEffect(() => {
    if (!open) return
    function onPointerDown(event: MouseEvent | TouchEvent) {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('touchstart', onPointerDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('touchstart', onPointerDown)
    }
  }, [open])

  function choose(speed: PlaybackSpeed): void {
    onChange(speed)
    setOpen(false)
    // Focus goes back to the trigger so a keyboard user is not dropped at the
    // top of the document after choosing.
    triggerRef.current?.focus()
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>): void {
    if (!open) return
    const index = PLAYBACK_SPEEDS.indexOf(active)
    if (event.key === 'Escape') {
      event.preventDefault()
      setOpen(false)
      triggerRef.current?.focus()
      return
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      const step = event.key === 'ArrowDown' ? 1 : -1
      const next = Math.min(Math.max(index + step, 0), PLAYBACK_SPEEDS.length - 1)
      setActive(PLAYBACK_SPEEDS[next])
      return
    }
    if (event.key === 'Home') {
      event.preventDefault()
      setActive(PLAYBACK_SPEEDS[0])
      return
    }
    if (event.key === 'End') {
      event.preventDefault()
      setActive(PLAYBACK_SPEEDS[PLAYBACK_SPEEDS.length - 1])
      return
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      choose(active)
    }
  }

  return (
    <div ref={containerRef} className="relative" onKeyDown={onKeyDown}>
      <Button
        ref={triggerRef}
        variant="ghost"
        size="sm"
        className="min-h-11 min-w-14 text-(--color-media-fg) hover:bg-white/10 sm:min-h-10"
        onClick={() => setOpen((current) => !current)}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`Playback speed: ${LABELS[value]}`}
      >
        <span className="tabular font-semibold">{value}×</span>
      </Button>

      {open ? (
        <div
          role="listbox"
          aria-label="Playback speed"
          aria-activedescendant={`playback-speed-${active}`}
          tabIndex={-1}
          className={cn(
            'material-media absolute right-0 bottom-full z-30 mb-2 min-w-40 overflow-hidden rounded-(--radius-md) border',
            'border-(--color-media-border) py-1 shadow-(--shadow-popover)',
          )}
        >
          {PLAYBACK_SPEEDS.map((speed) => (
            <button
              key={speed}
              id={`playback-speed-${speed}`}
              type="button"
              role="option"
              aria-selected={speed === value}
              onClick={() => choose(speed)}
              onMouseEnter={() => setActive(speed)}
              className={cn(
                'flex min-h-11 w-full items-center justify-between gap-4 px-3.5 text-left text-sm',
                'text-(--color-media-fg-muted) transition-colors sm:min-h-9',
                speed === active && 'bg-white/10 text-(--color-media-fg)',
                speed === value && 'font-semibold text-(--color-media-fg)',
              )}
            >
              <span className="tabular">{speed}×</span>
              <span className="text-xs text-(--color-media-fg-subtle)">
                {speed === value ? 'Playing' : speed >= SILENT_FROM ? 'Silent' : ''}
              </span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  )
}
