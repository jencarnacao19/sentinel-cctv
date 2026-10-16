import {
  CalendarClock,
  ChevronLeft,
  ChevronRight,
  CircleDot,
  Crosshair,
  Film,
  Gauge,
  Radio,
  TriangleAlert,
} from 'lucide-react'
import { useMemo } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { panWindow, recentreWindow, type Window as TimeWindow } from '@/features/timeline/timelineMath'
import type { MotionBand, Timeline } from '@/features/timeline/types'
import { cn } from '@/lib/utils'

const RANGE_PRESETS = [
  { label: '1h', seconds: 3600 },
  { label: '6h', seconds: 6 * 3600 },
  { label: '24h', seconds: 24 * 3600 },
  { label: '7d', seconds: 7 * 24 * 3600 },
]

function localInputValue(value: Date): string {
  const offset = value.getTimezoneOffset() * 60_000
  return new Date(value.getTime() - offset).toISOString().slice(0, 16)
}

function durationLabel(seconds: number): string {
  if (seconds >= 3600) {
    const hours = seconds / 3600
    return `${hours >= 10 ? Math.round(hours) : hours.toFixed(1)}h`
  }
  if (seconds >= 60) return `${Math.round(seconds / 60)}m`
  return `${Math.round(seconds)}s`
}

function percent(part: number, total: number): string {
  if (total <= 0) return '0%'
  return `${Math.round((part / total) * 100)}%`
}

function eventLabel(event: MotionBand): string {
  const at = new Date(event.start)
  return at.toLocaleString(undefined, {
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

interface TimelineNavigatorProps {
  range: TimeWindow
  playheadAt: Date | null
  timeline: Timeline | undefined
  loading?: boolean
  onRangeChange: (range: TimeWindow) => void
  onSeek: (at: Date) => void
}

export function TimelineNavigator({
  range,
  playheadAt,
  timeline,
  loading = false,
  onRangeChange,
  onSeek,
}: TimelineNavigatorProps) {
  const windowSeconds = (range.end.getTime() - range.start.getTime()) / 1000
  const now = new Date()
  const isAtNow = Math.abs(now.getTime() - range.end.getTime()) < 90_000
  const events = useMemo(
    () => [...(timeline?.motion ?? [])].sort((a, b) => b.start.localeCompare(a.start)).slice(0, 10),
    [timeline],
  )

  const setPreset = (seconds: number) => {
    const end = isAtNow ? new Date() : (playheadAt ?? range.end)
    onRangeChange({ start: new Date(end.getTime() - seconds * 1000), end })
  }

  const move = (direction: -1 | 1) => {
    let next = panWindow(range, direction * 0.85)
    const currentNow = new Date()
    if (next.end > currentNow) {
      const width = next.end.getTime() - next.start.getTime()
      next = { start: new Date(currentNow.getTime() - width), end: currentNow }
    }
    onRangeChange(next)
  }

  const goNow = () => {
    const end = new Date()
    onRangeChange({ start: new Date(end.getTime() - windowSeconds * 1000), end })
    onSeek(new Date(end.getTime() - 5_000))
  }

  const totalSeconds = Math.max(windowSeconds, 1)

  return (
    <section className="space-y-3" aria-label="Timeline navigation">
      <div className="flex flex-col gap-2 rounded-xl border border-(--color-border) bg-(--color-surface-raised) p-2.5 lg:flex-row lg:items-center">
        <div className="flex min-w-0 items-center gap-1 overflow-x-auto">
          <Button
            variant="ghost"
            size="icon"
            className="size-9 shrink-0"
            onClick={() => move(-1)}
            aria-label="Earlier footage"
          >
            <ChevronLeft aria-hidden />
          </Button>
          {RANGE_PRESETS.map((preset) => {
            const active = Math.abs(windowSeconds - preset.seconds) / preset.seconds < 0.02
            return (
              <Button
                key={preset.seconds}
                variant={active ? 'primary' : 'ghost'}
                size="sm"
                className="min-h-9 shrink-0"
                onClick={() => setPreset(preset.seconds)}
              >
                {preset.label}
              </Button>
            )
          })}
          <Button
            variant="ghost"
            size="icon"
            className="size-9 shrink-0"
            onClick={() => move(1)}
            disabled={isAtNow}
            aria-label="Later footage"
          >
            <ChevronRight aria-hidden />
          </Button>
        </div>

        <div className="flex min-w-0 flex-1 items-center gap-2 lg:justify-end">
          <label htmlFor="timeline-jump-at" className="sr-only">
            Jump to date and time
          </label>
          <div className="relative min-w-0 flex-1 lg:max-w-60">
            <CalendarClock
              className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-(--color-fg-subtle)"
              aria-hidden
            />
            <Input
              id="timeline-jump-at"
              type="datetime-local"
              className="min-h-10 w-full pl-9 text-xs"
              value={localInputValue(playheadAt ?? new Date((range.start.getTime() + range.end.getTime()) / 2))}
              onChange={(event) => {
                const at = new Date(event.target.value)
                if (Number.isNaN(at.getTime())) return
                onRangeChange(recentreWindow(range, at))
                onSeek(at)
              }}
            />
          </div>
          <Button variant={isAtNow ? 'secondary' : 'primary'} size="sm" className="min-h-10" onClick={goNow}>
            <Radio className={cn('size-4', isAtNow && 'text-(--color-recording)')} aria-hidden />
            Now
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        <div className="rounded-lg border border-(--color-border) bg-(--color-surface-subtle) p-3">
          <p className="flex items-center gap-1.5 text-[11px] font-medium text-(--color-fg-subtle)">
            <Film className="size-3.5" aria-hidden /> Coverage
          </p>
          <p className="mt-1 text-lg font-semibold tabular-nums">
            {loading ? '…' : percent(timeline?.recorded_seconds ?? 0, totalSeconds)}
          </p>
          <p className="text-xs text-(--color-fg-subtle)">
            {durationLabel(timeline?.recorded_seconds ?? 0)} recorded
          </p>
        </div>
        <div className="rounded-lg border border-(--color-border) bg-(--color-surface-subtle) p-3">
          <p className="flex items-center gap-1.5 text-[11px] font-medium text-(--color-fg-subtle)">
            <Gauge className="size-3.5" aria-hidden /> Motion
          </p>
          <p className="mt-1 text-lg font-semibold tabular-nums">
            {loading ? '…' : (timeline?.motion_event_count ?? 0).toLocaleString()}
          </p>
          <p className="text-xs text-(--color-fg-subtle)">
            {durationLabel(timeline?.motion_seconds ?? 0)} active
          </p>
        </div>
        <div className="rounded-lg border border-(--color-border) bg-(--color-surface-subtle) p-3">
          <p className="flex items-center gap-1.5 text-[11px] font-medium text-(--color-fg-subtle)">
            <TriangleAlert className="size-3.5" aria-hidden /> Gaps
          </p>
          <p className="mt-1 text-lg font-semibold tabular-nums">
            {loading ? '…' : (timeline?.gaps.length ?? 0).toLocaleString()}
          </p>
          <p className="text-xs text-(--color-fg-subtle)">
            {durationLabel((timeline?.gap_seconds ?? 0) + (timeline?.damaged_seconds ?? 0))}
          </p>
        </div>
        <div className="rounded-lg border border-(--color-border) bg-(--color-surface-subtle) p-3">
          <p className="flex items-center gap-1.5 text-[11px] font-medium text-(--color-fg-subtle)">
            <Crosshair className="size-3.5" aria-hidden /> Window
          </p>
          <p className="mt-1 text-lg font-semibold tabular-nums">{durationLabel(windowSeconds)}</p>
          <p className="truncate text-xs text-(--color-fg-subtle)">
            {range.start.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
          </p>
        </div>
      </div>

      {events.length > 0 ? (
        <div className="rounded-xl border border-(--color-border) bg-(--color-surface-raised) p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <h2 className="flex items-center gap-2 text-sm font-semibold">
              <CircleDot className="size-4 text-(--color-motion)" aria-hidden /> Motion activity
            </h2>
            <span className="text-[11px] text-(--color-fg-subtle)">Tap an event to play</span>
          </div>
          <div className="flex gap-2 overflow-x-auto pb-1">
            {events.map((event, index) => (
              <button
                key={`${event.start}-${index}`}
                type="button"
                className="min-h-11 shrink-0 rounded-lg border border-(--color-border) bg-(--color-surface-subtle) px-3 py-2 text-left transition-colors hover:border-(--color-motion)/60 hover:bg-(--color-surface-hover) focus-visible:outline-2 focus-visible:outline-(--color-accent)"
                onClick={() => onSeek(new Date(event.start))}
              >
                <span className="block text-xs font-semibold tabular-nums">{eventLabel(event)}</span>
                <span className="mt-0.5 block text-[10px] text-(--color-fg-subtle)">
                  {event.event_count} event{event.event_count === 1 ? '' : 's'} ·{' '}
                  {Math.round(event.peak_score * 100)}% peak
                </span>
              </button>
            ))}
          </div>
        </div>
      ) : null}
    </section>
  )
}
