import { describe, expect, it } from 'vitest'

import {
  appendSegment,
  bufferTimeFor,
  totalBufferedSeconds,
  trimHistory,
  wallClockAt,
  type BufferedSegment,
} from '@/features/timeline/playbackBuffer'
import type { ManifestItem } from '@/features/timeline/types'

function item(overrides: Partial<ManifestItem> = {}): ManifestItem {
  return {
    segment_id: 'seg-1',
    started_at: '2026-08-13T00:00:00Z',
    ended_at: '2026-08-13T00:01:00Z',
    offset_seconds: 0,
    duration_seconds: 60,
    url: '/api/v1/recordings/seg-1/download',
    ...overrides,
  }
}

describe('appendSegment', () => {
  it('places the first item at buffer position zero', () => {
    const segments = appendSegment([], item())
    expect(segments[0].bufferStart).toBe(0)
    expect(segments[0].bufferEnd).toBe(60)
  })

  it('places each item immediately after the previous one', () => {
    let segments: BufferedSegment[] = []
    segments = appendSegment(segments, item({ segment_id: 'a', duration_seconds: 60 }))
    segments = appendSegment(segments, item({ segment_id: 'b', duration_seconds: 45 }))
    expect(segments[1].bufferStart).toBe(60)
    expect(segments[1].bufferEnd).toBe(105)
  })

  it('places the item after a wall-clock gap contiguously too -- a gap never becomes a hole', () => {
    let segments: BufferedSegment[] = []
    segments = appendSegment(
      segments,
      item({ segment_id: 'a', started_at: '2026-08-13T00:00:00Z', ended_at: '2026-08-13T00:01:00Z' }),
    )
    // The next item starts a full hour later in wall-clock time...
    segments = appendSegment(
      segments,
      item({
        segment_id: 'b',
        started_at: '2026-08-13T01:00:00Z',
        ended_at: '2026-08-13T01:01:00Z',
        duration_seconds: 60,
      }),
    )
    // ...but is still placed right after the first in buffer time.
    expect(segments[1].bufferStart).toBe(60)
    expect(segments[1].bufferEnd).toBe(120)
  })
})

describe('trimHistory', () => {
  it('leaves a short history untouched', () => {
    let segments: BufferedSegment[] = []
    for (let i = 0; i < 10; i += 1) {
      segments = appendSegment(segments, item({ segment_id: `s${i}` }))
    }
    expect(trimHistory(segments, 500)).toHaveLength(10)
  })

  it('keeps absolute buffer positions valid after trimming the oldest entries', () => {
    let segments: BufferedSegment[] = []
    for (let i = 0; i < 1200; i += 1) {
      segments = appendSegment(segments, item({ segment_id: `s${i}`, duration_seconds: 10 }))
    }
    const trimmed = trimHistory(segments, 500)
    expect(trimmed.length).toBeLessThan(segments.length)
    // The kept entries' positions must be unchanged -- they describe where
    // that media actually is in the real SourceBuffer, which trimming the
    // bookkeeping array does not move.
    expect(trimmed[0]).toEqual(segments[segments.length - trimmed.length])
    expect(totalBufferedSeconds(trimmed)).toBe(totalBufferedSeconds(segments))
  })
})

describe('wallClockAt', () => {
  it('maps a buffer position inside the first item back to wall-clock time', () => {
    const segments = appendSegment(
      [],
      item({ started_at: '2026-08-13T00:00:00Z', duration_seconds: 60 }),
    )
    const at = wallClockAt(segments, 15)
    expect(at?.toISOString()).toBe('2026-08-13T00:00:15.000Z')
  })

  it('maps a buffer position inside a later item using that item as the base', () => {
    let segments: BufferedSegment[] = []
    segments = appendSegment(segments, item({ started_at: '2026-08-13T00:00:00Z', duration_seconds: 60 }))
    segments = appendSegment(
      segments,
      item({ started_at: '2026-08-13T05:00:00Z', duration_seconds: 60 }),
    )
    const at = wallClockAt(segments, 70) // 10s into the second item
    expect(at?.toISOString()).toBe('2026-08-13T05:00:10.000Z')
  })

  it('returns null before anything has been appended', () => {
    expect(wallClockAt([], 0)).toBeNull()
  })

  it('returns null past everything appended so far', () => {
    const segments = appendSegment([], item({ duration_seconds: 60 }))
    expect(wallClockAt(segments, 999)).toBeNull()
  })
})

describe('bufferTimeFor', () => {
  it('is the inverse of wallClockAt for a time inside a buffered item', () => {
    const segments = appendSegment(
      [],
      item({ started_at: '2026-08-13T00:00:00Z', ended_at: '2026-08-13T00:01:00Z', duration_seconds: 60 }),
    )
    const at = new Date('2026-08-13T00:00:20Z')
    expect(bufferTimeFor(segments, at)).toBe(20)
  })

  it('returns null for an instant nothing buffered so far covers', () => {
    const segments = appendSegment(
      [],
      item({ started_at: '2026-08-13T00:00:00Z', ended_at: '2026-08-13T00:01:00Z' }),
    )
    expect(bufferTimeFor(segments, new Date('2026-08-13T05:00:00Z'))).toBeNull()
  })
})

describe('totalBufferedSeconds', () => {
  it('is zero with nothing appended', () => {
    expect(totalBufferedSeconds([])).toBe(0)
  })

  it('is the end position of the last appended item', () => {
    let segments: BufferedSegment[] = []
    segments = appendSegment(segments, item({ duration_seconds: 60 }))
    segments = appendSegment(segments, item({ duration_seconds: 30 }))
    expect(totalBufferedSeconds(segments)).toBe(90)
  })
})
