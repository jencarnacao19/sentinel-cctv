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
 * to a live `<video>` and scales the player's buffer, so neither a stray zero
 * from a caller nor a NaN may reach either. */
export function clampPlaybackRate(rate: number): PlaybackSpeed {
  if (!Number.isFinite(rate)) return 1
  // The nearest offered speed rather than a refusal: a rounding error in a
  // caller must not silently drop playback back to 1x.
  return PLAYBACK_SPEEDS.reduce((best, speed) =>
    Math.abs(speed - rate) < Math.abs(best - rate) ? speed : best,
  )
}
