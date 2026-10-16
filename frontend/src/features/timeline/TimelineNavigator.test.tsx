import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { TimelineNavigator } from '@/features/timeline/TimelineNavigator'
import type { Window as TimeWindow } from '@/features/timeline/timelineMath'
import type { Timeline } from '@/features/timeline/types'

const END = new Date('2026-08-24T18:00:00Z')
const START = new Date(END.getTime() - 6 * 3600 * 1000)

function timeline(): Timeline {
  return {
    camera_id: 'cam-1',
    start: START.toISOString(),
    end: END.toISOString(),
    coverage: [{ start: START.toISOString(), end: END.toISOString(), duration_seconds: 21_600 }],
    gaps: [],
    motion: [
      {
        start: '2026-08-24T16:30:00Z',
        end: '2026-08-24T16:31:00Z',
        peak_score: 0.8,
        event_count: 2,
      },
    ],
    recorded_seconds: 21_600,
    motion_seconds: 60,
    damaged_seconds: 0,
    gap_seconds: 0,
    segment_count: 360,
    motion_event_count: 2,
    bucket_seconds: 10,
    truncated: false,
  }
}

function Harness({ onSeek = vi.fn() }: { onSeek?: (at: Date) => void }) {
  const [range, setRange] = useState<TimeWindow>({ start: START, end: END })
  return (
    <>
      <TimelineNavigator
        range={range}
        playheadAt={new Date('2026-08-24T17:00:00Z')}
        timeline={timeline()}
        onRangeChange={setRange}
        onSeek={onSeek}
      />
      <output data-testid="width">
        {(range.end.getTime() - range.start.getTime()) / 1000}
      </output>
    </>
  )
}

describe('TimelineNavigator', () => {
  it('switches between useful time windows without a date calculation form', () => {
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: '24h' }))
    expect(screen.getByTestId('width')).toHaveTextContent('86400')
  })

  it('turns motion bands into one-tap playback targets', () => {
    const onSeek = vi.fn<(at: Date) => void>()
    render(<Harness onSeek={onSeek} />)

    fireEvent.click(screen.getByRole('button', { name: /2 events/i }))
    expect(onSeek).toHaveBeenCalledTimes(1)
    expect(onSeek.mock.calls[0][0].toISOString()).toBe('2026-08-24T16:30:00.000Z')
  })
})
