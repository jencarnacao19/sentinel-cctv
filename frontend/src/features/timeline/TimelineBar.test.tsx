import { fireEvent, render, screen } from '@testing-library/react'
import { useRef, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { TimelineBar } from '@/features/timeline/TimelineBar'
import type { Window as TimeWindow } from '@/features/timeline/timelineMath'
import type { Timeline } from '@/features/timeline/types'

const RANGE_START = new Date('2026-08-13T00:00:00Z')
const RANGE_END = new Date('2026-08-14T00:00:00Z')

function baseTimeline(overrides: Partial<Timeline> = {}): Timeline {
  return {
    camera_id: 'cam-1',
    start: RANGE_START.toISOString(),
    end: RANGE_END.toISOString(),
    coverage: [],
    gaps: [],
    motion: [],
    recorded_seconds: 0,
    motion_seconds: 0,
    damaged_seconds: 0,
    gap_seconds: 86400,
    segment_count: 0,
    motion_event_count: 0,
    bucket_seconds: 2,
    truncated: false,
    ...overrides,
  }
}

/** The bar normalises against its own bounding box; jsdom reports every
 * element's box as all-zero, so coordinate-sensitive tests have to give it a
 * real one. Mirrors `features/zones/ZoneCanvas.test.tsx`'s own helper. */
function mockBarRect(element: Element, width = 800, height = 64): void {
  vi.spyOn(element, 'getBoundingClientRect').mockReturnValue({
    left: 0,
    top: 0,
    right: width,
    bottom: height,
    width,
    height,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  })
}

function Harness({ timeline }: { timeline?: Timeline }) {
  const [range, setRange] = useState<TimeWindow>({ start: RANGE_START, end: RANGE_END })
  const [playheadAt, setPlayheadAt] = useState<Date | null>(new Date('2026-08-13T12:00:00Z'))
  const [selection, setSelection] = useState<TimeWindow | null>(null)
  const seeksRef = useRef<string[]>([])

  return (
    <div>
      <TimelineBar
        range={range}
        onRangeChange={setRange}
        timeline={timeline}
        playheadAt={playheadAt}
        onSeek={(at) => {
          seeksRef.current = [...seeksRef.current, at.toISOString()]
          setPlayheadAt(at)
        }}
        selection={selection}
        onSelectionChange={setSelection}
      />
      <pre data-testid="range">
        {JSON.stringify({ start: range.start.toISOString(), end: range.end.toISOString() })}
      </pre>
      <pre data-testid="selection">
        {selection
          ? JSON.stringify({ start: selection.start.toISOString(), end: selection.end.toISOString() })
          : 'null'}
      </pre>
      <pre data-testid="seeks">{JSON.stringify(seeksRef.current)}</pre>
    </div>
  )
}

function readJson<T>(testId: string): T {
  return JSON.parse(screen.getByTestId(testId).textContent ?? 'null') as T
}

describe('TimelineBar — rendering', () => {
  it('renders with no timeline data yet, rather than crashing', () => {
    render(<Harness />)
    expect(screen.getByRole('slider', { name: 'Recording timeline' })).toBeInTheDocument()
  })

  it('shows the storage-unavailable notice only when such a gap is present', () => {
    const { rerender } = render(<Harness timeline={baseTimeline()} />)
    expect(screen.queryByText(/storage was unavailable/i)).not.toBeInTheDocument()

    rerender(
      <Harness
        timeline={baseTimeline({
          gaps: [
            {
              start: RANGE_START.toISOString(),
              end: RANGE_END.toISOString(),
              duration_seconds: 86400,
              reason: 'storage_unavailable',
            },
          ],
        })}
      />,
    )
    expect(screen.getByText(/storage was unavailable/i)).toBeInTheDocument()
  })
})

describe('TimelineBar — click to seek', () => {
  it('seeks to the clicked position when the mouse does not move', () => {
    render(<Harness />)
    const bar = screen.getByRole('slider')
    mockBarRect(bar)

    // Midpoint of a 24h bar -> noon.
    fireEvent.mouseDown(bar, { clientX: 400, clientY: 10, button: 0 })
    fireEvent.mouseUp(window, { clientX: 400, clientY: 10 })

    const seeks = readJson<string[]>('seeks')
    expect(seeks).toHaveLength(1)
    expect(new Date(seeks[0]).toISOString()).toBe('2026-08-13T12:00:00.000Z')
  })

  it('does not seek when the mouse moved far enough to count as a drag', () => {
    render(<Harness />)
    const bar = screen.getByRole('slider')
    mockBarRect(bar)

    fireEvent.mouseDown(bar, { clientX: 100, clientY: 10, button: 0 })
    fireEvent.mouseMove(window, { clientX: 300, clientY: 10 })
    fireEvent.mouseUp(window, { clientX: 300, clientY: 10 })

    expect(readJson<string[]>('seeks')).toHaveLength(0)
  })
})

describe('TimelineBar — drag to select', () => {
  it('builds a selection from the drag start to its end, in order regardless of drag direction', () => {
    render(<Harness />)
    const bar = screen.getByRole('slider')
    mockBarRect(bar)

    // Drag right-to-left: end of drag is earlier on screen than the start.
    fireEvent.mouseDown(bar, { clientX: 600, clientY: 10, button: 0 })
    fireEvent.mouseMove(window, { clientX: 200, clientY: 10 })
    fireEvent.mouseUp(window, { clientX: 200, clientY: 10 })

    const selection = readJson<{ start: string; end: string }>('selection')
    expect(new Date(selection.start).getTime()).toBeLessThan(new Date(selection.end).getTime())
    // 200/800 = 25% of the day, 600/800 = 75%.
    expect(new Date(selection.start).toISOString()).toBe('2026-08-13T06:00:00.000Z')
    expect(new Date(selection.end).toISOString()).toBe('2026-08-13T18:00:00.000Z')
  })

  it('builds the same selection with a touch gesture on mobile', () => {
    render(<Harness />)
    const bar = screen.getByRole('slider')
    mockBarRect(bar)

    fireEvent.touchStart(bar, {
      touches: [{ identifier: 7, clientX: 200, clientY: 10 }],
    })
    fireEvent.touchMove(bar, {
      touches: [{ identifier: 7, clientX: 600, clientY: 10 }],
    })
    fireEvent.touchEnd(bar, {
      changedTouches: [{ identifier: 7, clientX: 600, clientY: 10 }],
    })

    const selection = readJson<{ start: string; end: string }>('selection')
    expect(new Date(selection.start).toISOString()).toBe('2026-08-13T06:00:00.000Z')
    expect(new Date(selection.end).toISOString()).toBe('2026-08-13T18:00:00.000Z')
  })

  it('clears the selection with Escape', () => {
    render(<Harness />)
    const bar = screen.getByRole('slider')
    mockBarRect(bar)

    fireEvent.mouseDown(bar, { clientX: 600, clientY: 10, button: 0 })
    fireEvent.mouseMove(window, { clientX: 200, clientY: 10 })
    fireEvent.mouseUp(window, { clientX: 200, clientY: 10 })
    expect(readJson('selection')).not.toBeNull()

    bar.focus()
    fireEvent.keyDown(bar, { key: 'Escape' })
    expect(readJson('selection')).toBeNull()
  })
})

describe('TimelineBar — keyboard access', () => {
  it('Home and End jump to the edges of the visible range', () => {
    render(<Harness />)
    const bar = screen.getByRole('slider')
    bar.focus()

    fireEvent.keyDown(bar, { key: 'End' })
    fireEvent.keyDown(bar, { key: 'Home' })

    const seeks = readJson<string[]>('seeks')
    expect(seeks[0]).toBe(RANGE_END.toISOString())
    expect(seeks[1]).toBe(RANGE_START.toISOString())
  })

  it('the arrow keys nudge the playhead forward and backward', () => {
    render(<Harness />)
    const bar = screen.getByRole('slider')
    bar.focus()

    fireEvent.keyDown(bar, { key: 'ArrowRight' })
    const afterRight = new Date(readJson<string[]>('seeks')[0])
    expect(afterRight.getTime()).toBeGreaterThan(new Date('2026-08-13T12:00:00Z').getTime())
  })

  it('+ and - zoom the visible range without moving the playhead', () => {
    render(<Harness />)
    const bar = screen.getByRole('slider')
    bar.focus()

    const before = readJson<{ start: string; end: string }>('range')
    fireEvent.keyDown(bar, { key: '+' })
    const after = readJson<{ start: string; end: string }>('range')

    const widthBefore = new Date(before.end).getTime() - new Date(before.start).getTime()
    const widthAfter = new Date(after.end).getTime() - new Date(after.start).getTime()
    expect(widthAfter).toBeLessThan(widthBefore)
  })
})
