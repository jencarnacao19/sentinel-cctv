import { describe, expect, it } from 'vitest'

import {
  buildTicks,
  clamp01,
  fractionForTime,
  MAX_WINDOW_SECONDS,
  MIN_WINDOW_SECONDS,
  panWindow,
  pickTickStep,
  recentreWindow,
  timeForFraction,
  zoomWindow,
} from '@/features/timeline/timelineMath'

const START = new Date('2026-08-13T00:00:00Z')
const END = new Date('2026-08-14T00:00:00Z')

describe('clamp01', () => {
  it('clamps to the unit range', () => {
    expect(clamp01(-1)).toBe(0)
    expect(clamp01(2)).toBe(1)
    expect(clamp01(0.5)).toBe(0.5)
  })

  it('treats NaN as zero rather than propagating it', () => {
    expect(clamp01(Number.NaN)).toBe(0)
  })
})

describe('fractionForTime / timeForFraction', () => {
  it('maps the window edges to 0 and 1', () => {
    expect(fractionForTime(START, START, END)).toBe(0)
    expect(fractionForTime(END, START, END)).toBe(1)
  })

  it('maps the midpoint to 0.5', () => {
    const mid = new Date((START.getTime() + END.getTime()) / 2)
    expect(fractionForTime(mid, START, END)).toBeCloseTo(0.5, 10)
  })

  it('round-trips through timeForFraction', () => {
    const original = new Date(START.getTime() + 3 * 3600 * 1000)
    const fraction = fractionForTime(original, START, END)
    const restored = timeForFraction(fraction, START, END)
    expect(restored.getTime()).toBeCloseTo(original.getTime(), -1)
  })

  it('clamps a time before the window to 0', () => {
    const before = new Date(START.getTime() - 3600 * 1000)
    expect(fractionForTime(before, START, END)).toBe(0)
  })

  it('clamps a time after the window to 1', () => {
    const after = new Date(END.getTime() + 3600 * 1000)
    expect(fractionForTime(after, START, END)).toBe(1)
  })

  it('does not divide by zero for a window with no width', () => {
    expect(fractionForTime(START, START, START)).toBe(0)
  })
})

describe('zoomWindow', () => {
  it('halves the window width when zooming in by 0.5', () => {
    const pivot = new Date((START.getTime() + END.getTime()) / 2)
    const zoomed = zoomWindow({ start: START, end: END }, pivot, 0.5)
    const originalWidth = END.getTime() - START.getTime()
    const zoomedWidth = zoomed.end.getTime() - zoomed.start.getTime()
    expect(zoomedWidth).toBeCloseTo(originalWidth / 2, -2)
  })

  it('keeps the pivot at the same fractional position after zooming', () => {
    const pivot = new Date(START.getTime() + (END.getTime() - START.getTime()) * 0.25)
    const zoomed = zoomWindow({ start: START, end: END }, pivot, 0.4)
    const fractionBefore = fractionForTime(pivot, START, END)
    const fractionAfter = fractionForTime(pivot, zoomed.start, zoomed.end)
    expect(fractionAfter).toBeCloseTo(fractionBefore, 6)
  })

  it('never zooms in past the minimum window', () => {
    const zoomed = zoomWindow({ start: START, end: END }, START, 0.0001)
    const seconds = (zoomed.end.getTime() - zoomed.start.getTime()) / 1000
    expect(seconds).toBeCloseTo(MIN_WINDOW_SECONDS, 0)
  })

  it('never zooms out past the maximum window', () => {
    const zoomed = zoomWindow({ start: START, end: END }, START, 1_000_000)
    const seconds = (zoomed.end.getTime() - zoomed.start.getTime()) / 1000
    expect(seconds).toBeCloseTo(MAX_WINDOW_SECONDS, 0)
  })
})

describe('panWindow', () => {
  it('shifts both edges by the same amount', () => {
    const panned = panWindow({ start: START, end: END }, 0.5)
    const width = END.getTime() - START.getTime()
    expect(panned.start.getTime()).toBe(START.getTime() + width * 0.5)
    expect(panned.end.getTime()).toBe(END.getTime() + width * 0.5)
  })

  it('preserves the window width', () => {
    const panned = panWindow({ start: START, end: END }, -0.3)
    const originalWidth = END.getTime() - START.getTime()
    const pannedWidth = panned.end.getTime() - panned.start.getTime()
    expect(pannedWidth).toBe(originalWidth)
  })
})

describe('recentreWindow', () => {
  it('puts the target instant at the midpoint, keeping the zoom level', () => {
    const target = new Date('2026-08-20T12:00:00Z')
    const recentred = recentreWindow({ start: START, end: END }, target)
    const mid = new Date((recentred.start.getTime() + recentred.end.getTime()) / 2)
    expect(mid.getTime()).toBe(target.getTime())
    expect(recentred.end.getTime() - recentred.start.getTime()).toBe(END.getTime() - START.getTime())
  })
})

describe('pickTickStep', () => {
  it('picks a coarser step for a wider window', () => {
    const stepForOneHour = pickTickStep(3600)
    const stepForOneWeek = pickTickStep(7 * 24 * 3600)
    expect(stepForOneWeek).toBeGreaterThan(stepForOneHour)
  })

  it('never produces more than roughly the requested number of ticks', () => {
    const windowSeconds = 6 * 3600
    const step = pickTickStep(windowSeconds, 6)
    expect(windowSeconds / step).toBeLessThanOrEqual(6)
  })
})

describe('buildTicks', () => {
  it('returns ticks with strictly increasing fractions', () => {
    const ticks = buildTicks(START, END)
    expect(ticks.length).toBeGreaterThan(0)
    for (let i = 1; i < ticks.length; i += 1) {
      expect(ticks[i].fraction).toBeGreaterThan(ticks[i - 1].fraction)
    }
  })

  it('keeps every tick within the window', () => {
    const ticks = buildTicks(START, END)
    for (const tick of ticks) {
      expect(tick.time.getTime()).toBeGreaterThanOrEqual(START.getTime())
      expect(tick.time.getTime()).toBeLessThanOrEqual(END.getTime())
    }
  })

  it('returns nothing for a window with no width', () => {
    expect(buildTicks(START, START)).toEqual([])
  })

  it('labels a wide window with dates rather than ambiguous times', () => {
    const wideEnd = new Date(START.getTime() + 5 * 24 * 3600 * 1000)
    const ticks = buildTicks(START, wideEnd)
    expect(ticks.length).toBeGreaterThan(0)
    // Compared against what the runtime's own locale produces, rather than
    // a hard-coded string: the point being tested is which formatting
    // function ran, not what any particular locale renders it as.
    const dateLabel = ticks[0].time.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    const timeLabel = ticks[0].time.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    expect(ticks[0].label).toBe(dateLabel)
    expect(ticks[0].label).not.toBe(timeLabel)
  })

  it('labels a narrow window with times rather than dates', () => {
    const ticks = buildTicks(START, END)
    expect(ticks.length).toBeGreaterThan(0)
    const timeLabel = ticks[0].time.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    expect(ticks[0].label).toBe(timeLabel)
  })
})
