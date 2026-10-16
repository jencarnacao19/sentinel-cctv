/**
 * Pure bookkeeping for where each manifest item lands inside a
 * `mode: 'sequence'` `SourceBuffer`'s own timeline, and the mapping back to
 * wall-clock time.
 *
 * A segment file is always the *whole* recorded file; `item.offset_seconds`
 * and `item.duration_seconds` describe the slice actually wanted, clipped
 * into the buffer with `appendWindowStart`/`appendWindowEnd` by the player.
 * Once clipped, `mode: 'sequence'` places each append immediately after the
 * previous one -- so a wall-clock gap between two items never becomes a hole
 * in the buffer: the item after the gap is simply the next thing appended.
 * That is what turns a gap into a hard cut instead of a stall.
 *
 * Kept free of any MediaSource/SourceBuffer reference so the position
 * bookkeeping can be tested without a real (or faked) browser media stack.
 * The rate-dependent buffering targets live here for the same reason: how
 * much to hold ahead of a playhead running at 16x is arithmetic, and
 * arithmetic is worth testing without a media stack in the way.
 */

import type { ManifestItem } from '@/features/timeline/types'

export interface BufferedSegment {
  item: ManifestItem
  /** Seconds into the SourceBuffer's own timeline where this item's wanted
   * slice begins. Unrelated to wall-clock time or to any timestamp inside
   * the file itself. */
  bufferStart: number
  bufferEnd: number
}

/** Record one more appended item, placed right after whatever was appended
 * last. O(1): a long playback session appends thousands of these, and
 * recomputing the whole history on every one would make the session itself
 * slow down over time. */
export function appendSegment(
  segments: readonly BufferedSegment[],
  item: ManifestItem,
): BufferedSegment[] {
  const bufferStart = segments.length ? segments[segments.length - 1].bufferEnd : 0
  const bufferEnd = bufferStart + item.duration_seconds
  return [...segments, { item, bufferStart, bufferEnd }]
}

/** Bound how much bookkeeping history is kept -- old entries are only ever
 * looked up near the current playhead, which is always at or past the most
 * recent append. `bufferStart`/`bufferEnd` are absolute positions from the
 * very first append, so dropping the oldest entries never invalidates the
 * ones that remain. */
export function trimHistory(segments: BufferedSegment[], keep = 500): BufferedSegment[] {
  return segments.length > keep * 2 ? segments.slice(-keep) : segments
}

/** The wall-clock instant `bufferTime` (seconds into the SourceBuffer's own
 * timeline) corresponds to, or `null` if it falls outside everything
 * recorded so far. */
export function wallClockAt(segments: readonly BufferedSegment[], bufferTime: number): Date | null {
  const segment = segments.find((s) => bufferTime >= s.bufferStart && bufferTime <= s.bufferEnd)
  if (!segment) return null
  const intoItem = bufferTime - segment.bufferStart
  return new Date(new Date(segment.item.started_at).getTime() + intoItem * 1000)
}

/** The buffer-relative position (seconds) that plays at wall-clock instant
 * `at`, or `null` if nothing appended so far covers it. */
export function bufferTimeFor(segments: readonly BufferedSegment[], at: Date): number | null {
  const atMs = at.getTime()
  for (const segment of segments) {
    const startMs = new Date(segment.item.started_at).getTime()
    const endMs = new Date(segment.item.ended_at).getTime()
    if (atMs >= startMs && atMs <= endMs) {
      return segment.bufferStart + (atMs - startMs) / 1000
    }
  }
  return null
}

export function totalBufferedSeconds(segments: readonly BufferedSegment[]): number {
  return segments.length ? segments[segments.length - 1].bufferEnd : 0
}

/**
 * The speeds playback offers. Fixed rather than free-form: every one is a
 * power of two around 1x (plus a half-speed step for reading a number plate),
 * which is what a browser's audio and video decoders resample cleanly, and
 * six of them fit a control bar without a popover that a fullscreen element
 * would render behind.
 */
export const PLAYBACK_SPEEDS = [0.5, 1, 2, 4, 8, 16] as const

export type PlaybackSpeed = (typeof PLAYBACK_SPEEDS)[number]

/** Clamp anything to a speed the player actually supports. A rate is applied
 * to a live `<video>` and used as a divisor below, so neither a stray zero
 * from a caller nor a NaN may reach either. */
export function clampPlaybackRate(rate: number): PlaybackSpeed {
  if (!Number.isFinite(rate)) return 1
  // The nearest offered speed rather than a refusal: a rounding error in a
  // caller must not silently drop playback back to 1x.
  return PLAYBACK_SPEEDS.reduce((best, speed) =>
    Math.abs(speed - rate) < Math.abs(best - rate) ? speed : best,
  )
}

/** What 1x has always used, and what every rate is derived from. */
const BASE_LOOKAHEAD_SECONDS = 5 * 60
const BASE_REFILL_SECONDS = 2 * 60
const BASE_KEEP_BEHIND_SECONDS = 5 * 60
const BASE_MANIFEST_WINDOW_SECONDS = 30 * 60

/**
 * Hard ceilings, in media seconds, on everything the rate scales.
 *
 * Without them 16x would ask for sixteen times the media in the same viewing
 * time, and a phone left running at 16x would climb until the tab was killed.
 * These are the memory and request budget: whatever the rate, the player
 * never holds more than `MAX_LOOKAHEAD_SECONDS + MAX_KEEP_BEHIND_SECONDS` of
 * media, and never asks for a manifest page wider than
 * `MAX_MANIFEST_WINDOW_SECONDS`.
 */
export const MAX_LOOKAHEAD_SECONDS = 20 * 60
export const MAX_REFILL_SECONDS = 8 * 60
export const MAX_KEEP_BEHIND_SECONDS = 10 * 60
export const MAX_MANIFEST_WINDOW_SECONDS = 2 * 3600

export interface BufferTargets {
  /** Seconds of *viewing time* to keep buffered ahead of the playhead before
   * appending stops. Viewing time, not media time, is what starving is
   * measured in: at 16x five minutes of media is nineteen seconds of watching. */
  lookaheadSeconds: number
  /** Fetch the next manifest page once viewing time ahead falls below this.
   * Always below `lookaheadSeconds`, so the manifest stays ahead of the
   * appends rather than being asked for only once the buffer has run dry. */
  refillSeconds: number
  /** Media seconds behind the playhead to keep before trimming. Scaled by the
   * rate because at 16x the playhead covers that ground in moments, and
   * trimming on every append would be pure churn. */
  keepBehindSeconds: number
  /** Media seconds of manifest to ask for at a time. */
  manifestWindowSeconds: number
}

/**
 * The buffering targets for a given playback rate.
 *
 * Lookahead and refill are expressed in viewing time so the comparison in the
 * player is simply "measured media ahead, divided by the rate". Each ceiling
 * is a media-second budget, so dividing it by the rate turns it back into the
 * viewing time that budget is worth -- which is why the fast rates buy less
 * viewing time than 1x does, and why they cannot buy unbounded media.
 */
export function bufferTargetsFor(rate: number): BufferTargets {
  const safe = clampPlaybackRate(rate)
  return {
    lookaheadSeconds: Math.min(BASE_LOOKAHEAD_SECONDS, MAX_LOOKAHEAD_SECONDS / safe),
    refillSeconds: Math.min(BASE_REFILL_SECONDS, MAX_REFILL_SECONDS / safe),
    keepBehindSeconds: Math.min(BASE_KEEP_BEHIND_SECONDS * safe, MAX_KEEP_BEHIND_SECONDS),
    manifestWindowSeconds: Math.min(
      BASE_MANIFEST_WINDOW_SECONDS * safe,
      MAX_MANIFEST_WINDOW_SECONDS,
    ),
  }
}
