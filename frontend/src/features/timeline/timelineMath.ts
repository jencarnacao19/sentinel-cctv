/**
 * Pure geometry, zoom and tick-formatting helpers for the timeline scrub bar.
 *
 * Kept free of React and the DOM, same reasoning as `zones/zoneMath.ts`: the
 * mapping between wall-clock time and a position on the bar is what needs to
 * be exactly right, and that is far easier to get right (and keep right)
 * when it can be tested without mounting a component.
 */

export function clamp01(value: number): number {
  if (Number.isNaN(value)) return 0
  return Math.min(1, Math.max(0, value))
}

function clampNumber(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

/** Where `time` falls within `[start, end)`, as a 0-1 fraction. A window with
 * no width (or an inverted one) maps everything to 0 rather than dividing by
 * zero or returning a negative fraction. */
export function fractionForTime(time: Date, start: Date, end: Date): number {
  const total = end.getTime() - start.getTime()
  if (total <= 0) return 0
  return clamp01((time.getTime() - start.getTime()) / total)
}

export function timeForFraction(fraction: number, start: Date, end: Date): Date {
  const total = end.getTime() - start.getTime()
  return new Date(start.getTime() + clamp01(fraction) * total)
}

export interface Window {
  start: Date
  end: Date
}

/** Minimum window a zoom is allowed to reach -- below this a bar a few
 * hundred pixels wide has nothing meaningful left to show per pixel. */
export const MIN_WINDOW_SECONDS = 30
/** Maximum window a zoom is allowed to reach, matching the timeline
 * endpoint's own cap (`MAX_TIMELINE_SECONDS`) so zooming out can never ask
 * for a range the server would reject outright. */
export const MAX_WINDOW_SECONDS = 32 * 24 * 3600

/**
 * Zoom the visible window in or out around a pivot instant, keeping that
 * instant at the same fractional position it already occupies -- zooming in
 * on where the operator is looking should not also shift what they are
 * looking at.
 */
export function zoomWindow(
  window: Window,
  pivot: Date,
  factor: number,
  minSeconds: number = MIN_WINDOW_SECONDS,
  maxSeconds: number = MAX_WINDOW_SECONDS,
): Window {
  const currentSeconds = (window.end.getTime() - window.start.getTime()) / 1000
  const nextSeconds = clampNumber(currentSeconds * factor, minSeconds, maxSeconds)
  const pivotFraction = fractionForTime(pivot, window.start, window.end)
  const nextStartMs = pivot.getTime() - pivotFraction * nextSeconds * 1000
  return { start: new Date(nextStartMs), end: new Date(nextStartMs + nextSeconds * 1000) }
}

/** Slide the window by a fraction of its own width -- panning left/right
 * without changing the zoom level. */
export function panWindow(window: Window, fractionDelta: number): Window {
  const width = window.end.getTime() - window.start.getTime()
  const deltaMs = width * fractionDelta
  return {
    start: new Date(window.start.getTime() + deltaMs),
    end: new Date(window.end.getTime() + deltaMs),
  }
}

/** Recentre the window on `at`, keeping the current zoom level. Used when a
 * seek lands outside the currently visible window. */
export function recentreWindow(window: Window, at: Date): Window {
  const halfWidth = (window.end.getTime() - window.start.getTime()) / 2
  return { start: new Date(at.getTime() - halfWidth), end: new Date(at.getTime() + halfWidth) }
}

/** Round, human-meaningful tick spacings, in seconds. */
const TICK_STEPS_SECONDS = [
  1, 5, 15, 30, 60, 300, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 24 * 3600, 7 * 24 * 3600,
]

/** The smallest step from the ladder above that still leaves no more than
 * `targetTicks` marks across the window. */
export function pickTickStep(windowSeconds: number, targetTicks = 6): number {
  if (windowSeconds <= 0) return TICK_STEPS_SECONDS[0]
  const minStep = windowSeconds / targetTicks
  return (
    TICK_STEPS_SECONDS.find((step) => step >= minStep) ??
    TICK_STEPS_SECONDS[TICK_STEPS_SECONDS.length - 1]
  )
}

export interface TickMark {
  time: Date
  fraction: number
  label: string
}

/** Tick marks for the ruler beneath the bar, evenly spaced in wall-clock
 * time. Labels switch from a time to a date once the window is wide enough
 * that "14:32" alone would be ambiguous about which day it is. */
export function buildTicks(start: Date, end: Date, targetTicks = 6): TickMark[] {
  const windowSeconds = (end.getTime() - start.getTime()) / 1000
  if (windowSeconds <= 0) return []

  const step = pickTickStep(windowSeconds, targetTicks)
  const stepMs = step * 1000
  const firstTickMs = Math.ceil(start.getTime() / stepMs) * stepMs
  const showDate = windowSeconds > 36 * 3600

  const ticks: TickMark[] = []
  for (let t = firstTickMs; t <= end.getTime(); t += stepMs) {
    const time = new Date(t)
    ticks.push({
      time,
      fraction: fractionForTime(time, start, end),
      label: showDate
        ? time.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
        : time.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }),
    })
  }
  return ticks
}
