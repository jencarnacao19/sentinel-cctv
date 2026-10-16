/**
 * The scrubbable timeline bar: coverage, gaps (with why) and motion shading
 * for one camera's visible range, with click-to-seek, drag-to-select (for
 * export) and keyboard access.
 *
 * An SVG overlay draws the shading -- it is purely decorative
 * (`pointer-events: none`), with a single real, focusable, keyboard-handling
 * element beneath it doing the actual interaction. That split is what keeps
 * hit-testing and focus management simple: the SVG never needs to know where
 * a click landed, only how to redraw once the range or playhead changes.
 *
 * Mouse dragging uses temporary window listeners so a selection can finish
 * outside the bar. Touch has an explicit gesture path and `touch-action:none`:
 * the old mouse-only implementation made clip selection nearly impossible in
 * the Home Assistant mobile app.
 */

import { ChevronLeft, ChevronRight, Minus, Plus } from 'lucide-react'
import { useCallback, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  buildTicks,
  clamp01,
  fractionForTime,
  panWindow,
  timeForFraction,
  zoomWindow,
  type Window as TimeWindow,
} from '@/features/timeline/timelineMath'
import type { Gap, GapReason, MotionBand, TimeSpan, Timeline } from '@/features/timeline/types'
import { cn } from '@/lib/utils'

interface TimelineBarProps {
  range: TimeWindow
  onRangeChange: (range: TimeWindow) => void
  timeline: Timeline | undefined
  playheadAt: Date | null
  onSeek: (at: Date) => void
  selection: TimeWindow | null
  onSelectionChange: (selection: TimeWindow | null) => void
  className?: string
}

const NUDGE_FRACTION = 0.01
const NUDGE_FRACTION_LARGE = 0.1
const ZOOM_IN_FACTOR = 0.7
const ZOOM_OUT_FACTOR = 1 / ZOOM_IN_FACTOR
/** Below this fractional drag distance, a mouse-down/up pair is a click
 * (seek), not a drag (select) -- gives a click a little slop to land. */
const DRAG_THRESHOLD_FRACTION = 0.004

const GAP_REASON_LABEL: Record<GapReason, string> = {
  not_recording: 'Not recording',
  storage_unavailable: 'Storage unavailable',
  damaged: 'Damaged segment',
}

function localTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

interface PercentRect {
  left: number
  width: number
}

function rectFor(span: { start: string; end: string }, range: TimeWindow): PercentRect {
  const start = new Date(span.start)
  const end = new Date(span.end)
  const left = fractionForTime(start, range.start, range.end) * 100
  const right = fractionForTime(end, range.start, range.end) * 100
  return { left, width: Math.max(right - left, 0.15) }
}

export function TimelineBar({
  range,
  onRangeChange,
  timeline,
  playheadAt,
  onSeek,
  selection,
  onSelectionChange,
  className,
}: TimelineBarProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{ startFraction: number; moved: boolean } | null>(null)
  const touchRef = useRef<{ identifier: number; startFraction: number; moved: boolean } | null>(null)
  const lastTouchAtRef = useRef(0)
  const [hoverFraction, setHoverFraction] = useState<number | null>(null)
  const [announcement, setAnnouncement] = useState('')

  const fractionAtClientX = useCallback((clientX: number): number => {
    const rect = containerRef.current?.getBoundingClientRect()
    if (!rect || rect.width === 0) return 0
    return clamp01((clientX - rect.left) / rect.width)
  }, [])

  const handleMouseDown = useCallback(
    (event: React.MouseEvent<HTMLDivElement>) => {
      if (event.button !== 0) return
      if (Date.now() - lastTouchAtRef.current < 600) return
      const startFraction = fractionAtClientX(event.clientX)
      dragRef.current = { startFraction, moved: false }

      const handleMove = (moveEvent: MouseEvent) => {
        const drag = dragRef.current
        if (!drag) return
        const current = fractionAtClientX(moveEvent.clientX)
        if (Math.abs(current - drag.startFraction) > DRAG_THRESHOLD_FRACTION) drag.moved = true
        if (drag.moved) {
          const a = timeForFraction(Math.min(drag.startFraction, current), range.start, range.end)
          const b = timeForFraction(Math.max(drag.startFraction, current), range.start, range.end)
          onSelectionChange({ start: a, end: b })
        }
      }
      const handleUp = (upEvent: MouseEvent) => {
        window.removeEventListener('mousemove', handleMove)
        window.removeEventListener('mouseup', handleUp)
        const drag = dragRef.current
        dragRef.current = null
        if (!drag) return
        if (!drag.moved) {
          const at = timeForFraction(fractionAtClientX(upEvent.clientX), range.start, range.end)
          onSeek(at)
          setAnnouncement(`Seeking to ${at.toLocaleTimeString()}`)
        }
      }
      window.addEventListener('mousemove', handleMove)
      window.addEventListener('mouseup', handleUp)
    },
    [fractionAtClientX, onSeek, onSelectionChange, range.end, range.start],
  )

  const updateTouch = useCallback(
    (touch: React.Touch) => {
      const gesture = touchRef.current
      if (!gesture || touch.identifier !== gesture.identifier) return
      const current = fractionAtClientX(touch.clientX)
      if (Math.abs(current - gesture.startFraction) > DRAG_THRESHOLD_FRACTION) {
        gesture.moved = true
      }
      if (gesture.moved) {
        const a = timeForFraction(
          Math.min(gesture.startFraction, current),
          range.start,
          range.end,
        )
        const b = timeForFraction(
          Math.max(gesture.startFraction, current),
          range.start,
          range.end,
        )
        onSelectionChange({ start: a, end: b })
      }
    },
    [fractionAtClientX, onSelectionChange, range.end, range.start],
  )

  const handleTouchStart = useCallback(
    (event: React.TouchEvent<HTMLDivElement>) => {
      if (event.touches.length !== 1) return
      const touch = event.touches[0]
      lastTouchAtRef.current = Date.now()
      touchRef.current = {
        identifier: touch.identifier,
        startFraction: fractionAtClientX(touch.clientX),
        moved: false,
      }
    },
    [fractionAtClientX],
  )

  const handleTouchMove = useCallback(
    (event: React.TouchEvent<HTMLDivElement>) => {
      const gesture = touchRef.current
      if (!gesture) return
      const touch = Array.from(event.touches).find(
        (candidate) => candidate.identifier === gesture.identifier,
      )
      if (!touch) return
      event.preventDefault()
      lastTouchAtRef.current = Date.now()
      updateTouch(touch)
    },
    [updateTouch],
  )

  const handleTouchEnd = useCallback(
    (event: React.TouchEvent<HTMLDivElement>) => {
      const gesture = touchRef.current
      if (!gesture) return
      const touch = Array.from(event.changedTouches).find(
        (candidate) => candidate.identifier === gesture.identifier,
      )
      touchRef.current = null
      lastTouchAtRef.current = Date.now()
      if (!touch || gesture.moved) return
      const at = timeForFraction(fractionAtClientX(touch.clientX), range.start, range.end)
      onSeek(at)
      setAnnouncement(`Seeking to ${at.toLocaleTimeString()}`)
    },
    [fractionAtClientX, onSeek, range.end, range.start],
  )

  const handleWheel = useCallback(
    (event: React.WheelEvent<HTMLDivElement>) => {
      event.preventDefault()
      const pivot = timeForFraction(fractionAtClientX(event.clientX), range.start, range.end)
      const factor = event.deltaY < 0 ? ZOOM_IN_FACTOR : ZOOM_OUT_FACTOR
      onRangeChange(zoomWindow(range, pivot, factor))
    },
    [fractionAtClientX, onRangeChange, range],
  )

  const zoom = useCallback(
    (factor: number) => {
      const pivot = playheadAt ?? new Date((range.start.getTime() + range.end.getTime()) / 2)
      onRangeChange(zoomWindow(range, pivot, factor))
    },
    [onRangeChange, playheadAt, range],
  )

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      const step = event.shiftKey ? NUDGE_FRACTION_LARGE : NUDGE_FRACTION
      const at = playheadAt ?? range.start
      switch (event.key) {
        case 'ArrowLeft': {
          event.preventDefault()
          const fraction = fractionForTime(at, range.start, range.end) - step
          onSeek(timeForFraction(fraction, range.start, range.end))
          break
        }
        case 'ArrowRight': {
          event.preventDefault()
          const fraction = fractionForTime(at, range.start, range.end) + step
          onSeek(timeForFraction(fraction, range.start, range.end))
          break
        }
        case 'Home':
          event.preventDefault()
          onSeek(range.start)
          break
        case 'End':
          event.preventDefault()
          onSeek(range.end)
          break
        case '+':
        case '=':
          event.preventDefault()
          zoom(ZOOM_IN_FACTOR)
          break
        case '-':
        case '_':
          event.preventDefault()
          zoom(ZOOM_OUT_FACTOR)
          break
        case 'Escape':
          if (selection) {
            event.preventDefault()
            onSelectionChange(null)
          }
          break
        default:
          break
      }
    },
    [onSeek, onSelectionChange, playheadAt, range, selection, zoom],
  )

  const ticks = buildTicks(range.start, range.end)
  const playheadFraction = playheadAt ? fractionForTime(playheadAt, range.start, range.end) : null
  const selectionRect = selection ? rectFor(toIso(selection), range) : null

  const hoverTime = hoverFraction !== null ? timeForFraction(hoverFraction, range.start, range.end) : null
  const hoverGap = hoverTime
    ? timeline?.gaps.find((gap) => {
        const hoverMs = hoverTime.getTime()
        return new Date(gap.start).getTime() <= hoverMs && hoverMs < new Date(gap.end).getTime()
      })
    : undefined

  return (
    <div className={cn('space-y-1.5', className)}>
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-(--color-fg-subtle)">
          {range.start.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}
          {' – '}
          {range.end.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}
        </p>
        <div className="flex gap-1">
          <Button
            variant="ghost"
            size="icon"
            className="size-7"
            onClick={() => onRangeChange(panWindow(range, -0.25))}
            aria-label="Pan timeline earlier"
          >
            <ChevronLeft aria-hidden="true" />
          </Button>
          <Button
            variant="secondary"
            size="icon"
            className="size-7"
            onClick={() => zoom(ZOOM_OUT_FACTOR)}
            aria-label="Zoom out"
          >
            <Minus aria-hidden="true" />
          </Button>
          <Button
            variant="secondary"
            size="icon"
            className="size-7"
            onClick={() => zoom(ZOOM_IN_FACTOR)}
            aria-label="Zoom in"
          >
            <Plus aria-hidden="true" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="size-7"
            onClick={() => onRangeChange(panWindow(range, 0.25))}
            aria-label="Pan timeline later"
          >
            <ChevronRight aria-hidden="true" />
          </Button>
        </div>
      </div>

      <div
        ref={containerRef}
        role="slider"
        tabIndex={0}
        aria-label="Recording timeline"
        aria-valuemin={range.start.getTime()}
        aria-valuemax={range.end.getTime()}
        aria-valuenow={(playheadAt ?? range.start).getTime()}
        aria-valuetext={(playheadAt ?? range.start).toLocaleString()}
        className={cn(
          'relative h-20 cursor-pointer touch-none overflow-hidden rounded-lg border border-(--color-border) sm:h-16',
          'bg-(--color-bg-subtle) select-none focus-visible:outline-2 focus-visible:outline-(--color-accent)',
        )}
        onMouseDown={handleMouseDown}
        onWheel={handleWheel}
        onKeyDown={handleKeyDown}
        onMouseMove={(event) => setHoverFraction(fractionAtClientX(event.clientX))}
        onMouseLeave={() => setHoverFraction(null)}
        onTouchStart={handleTouchStart}
        onTouchMove={handleTouchMove}
        onTouchEnd={handleTouchEnd}
        onTouchCancel={() => {
          touchRef.current = null
          lastTouchAtRef.current = Date.now()
        }}
      >
        <svg
          viewBox="0 0 1000 100"
          preserveAspectRatio="none"
          className="pointer-events-none absolute inset-0 size-full"
          aria-hidden="true"
        >
          {/* Base: everything starts out looking like a gap: an operator
              should never read silence as "definitely fine". */}
          <rect x={0} y={0} width={1000} height={100} className="fill-(--color-offline)/25" />

          {timeline?.coverage.map((span: TimeSpan, index: number) => {
            const { left, width } = rectFor(span, range)
            return (
              <rect
                key={`coverage-${index}`}
                x={left * 10}
                y={20}
                width={width * 10}
                height={60}
                className="fill-(--color-recording)/70"
              />
            )
          })}

          {timeline?.gaps
            .filter((gap: Gap) => gap.reason === 'damaged')
            .map((gap: Gap, index: number) => {
              const { left, width } = rectFor(gap, range)
              return (
                <rect
                  key={`damaged-${index}`}
                  x={left * 10}
                  y={20}
                  width={width * 10}
                  height={60}
                  className="fill-(--color-danger)/60"
                />
              )
            })}

          {timeline?.motion.map((band: MotionBand, index: number) => {
            const { left, width } = rectFor(band, range)
            return (
              <rect
                key={`motion-${index}`}
                x={left * 10}
                y={5}
                width={width * 10}
                height={14}
                className="fill-(--color-motion)"
                opacity={0.5 + band.peak_score * 0.5}
              />
            )
          })}

          {ticks.map((tick, index) => (
            <line
              key={index}
              x1={tick.fraction * 1000}
              x2={tick.fraction * 1000}
              y1={0}
              y2={100}
              className="stroke-(--color-border-strong)"
              strokeWidth={1}
            />
          ))}

          {selectionRect ? (
            <rect
              x={selectionRect.left * 10}
              y={0}
              width={selectionRect.width * 10}
              height={100}
              className="fill-(--color-accent)/20 stroke-(--color-accent)"
              strokeWidth={2}
            />
          ) : null}

          {playheadFraction !== null ? (
            <line
              x1={playheadFraction * 1000}
              x2={playheadFraction * 1000}
              y1={0}
              y2={100}
              className="stroke-(--color-fg)"
              strokeWidth={2}
            />
          ) : null}

          {hoverFraction !== null ? (
            <line
              x1={hoverFraction * 1000}
              x2={hoverFraction * 1000}
              y1={0}
              y2={100}
              className="stroke-(--color-fg-subtle)"
              strokeWidth={1}
              strokeDasharray="4 4"
            />
          ) : null}
        </svg>
      </div>

      <div className="relative h-4 text-[10px] text-(--color-fg-subtle)">
        {ticks.map((tick, index) => (
          <span
            key={index}
            className="tabular absolute -translate-x-1/2 first:translate-x-0 last:-translate-x-full"
            style={{ left: `${tick.fraction * 100}%` }}
          >
            {tick.label}
          </span>
        ))}
      </div>

      <p className="h-4 text-xs text-(--color-fg-subtle)">
        {hoverTime ? (
          <>
            {localTime(hoverTime.toISOString())}
            {hoverGap ? ` — ${GAP_REASON_LABEL[hoverGap.reason]}` : ''}
          </>
        ) : (
          ' '
        )}
      </p>

      {timeline ? (
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-(--color-fg-subtle)">
          <LegendSwatch className="bg-(--color-recording)/70" label="Recorded" />
          <LegendSwatch className="bg-(--color-motion)" label="Motion" />
          <LegendSwatch className="bg-(--color-danger)/60" label="Damaged" />
          <LegendSwatch className="bg-(--color-offline)/25" label="No footage" />
          {timeline.gaps.some((gap) => gap.reason === 'storage_unavailable') ? (
            <span className="text-(--color-warning)">Storage was unavailable during part of this range</span>
          ) : null}
        </div>
      ) : null}

      <p className="sr-only" role="status" aria-live="polite">
        {announcement}
      </p>
    </div>
  )
}

function LegendSwatch({ className, label }: { className: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={cn('inline-block size-2.5 rounded-sm', className)} aria-hidden="true" />
      {label}
    </span>
  )
}

function toIso(bounds: TimeWindow): TimeSpan {
  return {
    start: bounds.start.toISOString(),
    end: bounds.end.toISOString(),
    duration_seconds: (bounds.end.getTime() - bounds.start.getTime()) / 1000,
  }
}
