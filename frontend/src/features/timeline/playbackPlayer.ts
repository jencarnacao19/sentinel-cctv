/**
 * Recorded-footage player: an HLS playlist the server builds from the files on
 * disk (`POST .../playback/session`).
 *
 * The playlist addresses each recorded file by byte range, a few seconds of
 * video at a time, so the first frame needs one small request instead of a
 * whole minute of footage. Apple devices -- including Home Assistant's iOS
 * app, which has no Media Source -- play it natively; everything else plays it
 * through hls.js. Neither re-encodes anything.
 *
 * One playlist covers up to an hour. When it ends, the next hour is opened
 * from where it stopped; a stretch with no footage is skipped to the next
 * recording, so a gap is a cut rather than a frozen frame.
 */

import type Hls from 'hls.js'
import type { ErrorData } from 'hls.js'

import { isHlsSupported, prefersNativeHls } from '@/features/live/hls'
import { isMseSupported } from '@/features/live/mse'
import { playbackApi } from '@/features/timeline/api'
import { clampPlaybackRate, type PlaybackSpeed } from '@/features/timeline/playbackSpeed'
import type { PlaybackSession, PlaybackSessionItem } from '@/features/timeline/types'

export type PlaybackState = 'loading' | 'playing' | 'stalled' | 'paused' | 'ended' | 'error'

export interface PlaybackEvents {
  onStateChange?: (state: PlaybackState, detail?: string) => void
  /** Fired as playback advances, with the wall-clock instant now on screen. */
  onTimeUpdate?: (at: Date) => void
}

/** A camera with no footage at all must not turn a seek into an endless
 * chain of "try the next recording" hops. */
const MAX_GAP_SKIP_HOPS = 20

/** Above this rate the audio is a squeal no browser resamples usefully. */
const MUTE_ABOVE_RATE = 2

/** Seconds of footage hls.js keeps ahead at 1x; scaled by the rate so 16x
 * does not drain it in two seconds. */
const BUFFER_AHEAD_SECONDS = 30

/** The wall-clock instant at `position` seconds into a playlist. */
export function wallClockAt(items: readonly PlaybackSessionItem[], position: number): Date | null {
  let current: PlaybackSessionItem | null = null
  for (const item of items) {
    if (item.position > position) break
    current = item
  }
  if (!current) return null
  const into = Math.min(position - current.position, current.duration)
  return new Date(new Date(current.started_at).getTime() + into * 1000)
}

/** Safari and the iOS WebView play HLS natively and better than through MSE. */
function prefersNative(video: HTMLVideoElement): boolean {
  if (!isHlsSupported(video)) return false
  return prefersNativeHls(video) || /Apple/.test(navigator.vendor) || !isMseSupported()
}

export class PlaybackPlayer {
  private hls: Hls | null = null
  private session: PlaybackSession | null = null
  private stopped = false
  /** Incremented on every seek(); async work from a superseded call checks
   * this and quietly stops rather than mutating state a newer call owns. */
  private token = 0
  private state: PlaybackState | null = null
  private rate: PlaybackSpeed = 1
  /** The operator's own `muted` choice, parked while a fast rate forces mute. */
  private mutedBeforeFastRate: boolean | null = null
  private recovered = false

  constructor(
    private readonly video: HTMLVideoElement,
    private readonly cameraId: string,
    private readonly events: PlaybackEvents = {},
    private readonly viewer = false,
  ) {
    video.addEventListener('timeupdate', this.handleTimeUpdate)
    video.addEventListener('ended', this.handleEnded)
    video.addEventListener('loadedmetadata', this.applyRate)
    video.addEventListener('playing', this.handlePlaying)
    video.addEventListener('pause', this.handlePause)
    video.addEventListener('waiting', this.handleWaiting)
  }

  get playbackRate(): PlaybackSpeed {
    return this.rate
  }

  /** Change the speed. Written to both rates: loading new media resets
   * `playbackRate` to `defaultPlaybackRate`. */
  setPlaybackRate(rate: number): void {
    this.rate = clampPlaybackRate(rate)
    this.applyRate()
  }

  private applyRate = (): void => {
    this.video.defaultPlaybackRate = this.rate
    this.video.playbackRate = this.rate
    if (this.hls) {
      this.hls.config.maxBufferLength = BUFFER_AHEAD_SECONDS * Math.max(this.rate, 1)
      this.hls.config.maxMaxBufferLength = 4 * BUFFER_AHEAD_SECONDS * Math.max(this.rate, 1)
    }
    if (this.rate > MUTE_ABOVE_RATE) {
      if (this.mutedBeforeFastRate === null) this.mutedBeforeFastRate = this.video.muted
      this.video.muted = true
    } else if (this.mutedBeforeFastRate !== null) {
      this.video.muted = this.mutedBeforeFastRate
      this.mutedBeforeFastRate = null
    }
  }

  /** Start (or restart) playback at wall-clock instant `at`. */
  async seek(at: Date, hop = 0): Promise<void> {
    if (this.stopped) return
    const token = (this.token += 1)
    this.detach()
    this.setState('loading')

    let session: PlaybackSession
    try {
      session = await playbackApi.session(this.cameraId, at, this.viewer)
    } catch {
      if (token === this.token) this.setState('error', 'Could not reach Sentinel.')
      return
    }
    if (token !== this.token || this.stopped) return

    if (!session.url) {
      if (session.next_at && hop < MAX_GAP_SKIP_HOPS) {
        await this.seek(new Date(session.next_at), hop + 1)
        return
      }
      this.setState('ended', 'No recorded footage after this time.')
      return
    }

    this.session = session
    this.recovered = false
    if (prefersNative(this.video)) {
      this.playNative(token, session.url, session.start_position)
    } else if (isMseSupported()) {
      await this.playWithHlsJs(token, session.url, session.start_position)
    } else {
      this.setState('error', 'This browser cannot play recordings.')
    }
  }

  stop(): void {
    this.stopped = true
    this.token += 1
    this.video.removeEventListener('timeupdate', this.handleTimeUpdate)
    this.video.removeEventListener('ended', this.handleEnded)
    this.video.removeEventListener('loadedmetadata', this.applyRate)
    this.video.removeEventListener('playing', this.handlePlaying)
    this.video.removeEventListener('pause', this.handlePause)
    this.video.removeEventListener('waiting', this.handleWaiting)
    this.detach()
  }

  private detach(): void {
    this.hls?.destroy()
    this.hls = null
    this.session = null
    this.video.removeAttribute('src')
    this.video.load()
  }

  private playNative(token: number, url: string, startPosition: number): void {
    this.video.addEventListener(
      'loadedmetadata',
      () => {
        if (token === this.token && !this.stopped) this.video.currentTime = startPosition
      },
      { once: true },
    )
    this.video.preload = 'auto'
    this.video.src = url
    // iOS loads nothing until play() is called: ask now, not once the metadata is in.
    this.play(token)
  }

  private async playWithHlsJs(token: number, url: string, startPosition: number): Promise<void> {
    // Loaded on demand: the camera wall never pays for it.
    const { default: HlsJs } = await import('hls.js')
    if (token !== this.token || this.stopped) return
    const hls = new HlsJs({ startPosition, backBufferLength: 60 })
    this.hls = hls
    this.applyRate()
    hls.on(HlsJs.Events.MANIFEST_PARSED, () => this.play(token))
    hls.on(HlsJs.Events.ERROR, (_event, data) => this.handleHlsError(token, HlsJs, hls, data))
    hls.loadSource(url)
    hls.attachMedia(this.video)
  }

  private handleHlsError(token: number, HlsJs: typeof Hls, hls: Hls, data: ErrorData): void {
    if (token !== this.token || !data.fatal) return
    if (data.type === HlsJs.ErrorTypes.MEDIA_ERROR && !this.recovered) {
      this.recovered = true
      hls.recoverMediaError()
      return
    }
    if (data.type === HlsJs.ErrorTypes.NETWORK_ERROR && data.response?.code === 404) {
      // The playlist's grant expired during a long pause: open a fresh one here.
      const at = wallClockAt(this.session?.items ?? [], this.video.currentTime)
      if (at) void this.seek(at)
      return
    }
    const codec =
      data.details === HlsJs.ErrorDetails.MANIFEST_INCOMPATIBLE_CODECS_ERROR ||
      data.details === HlsJs.ErrorDetails.BUFFER_ADD_CODEC_ERROR
    this.setState(
      'error',
      codec
        ? "This browser cannot decode this camera's video. Safari, Edge or the iPhone app can."
        : 'Playback failed. Try again.',
    )
  }

  private play(token: number): void {
    this.applyRate()
    this.video.play().then(
      () => {
        if (token === this.token) this.setState('playing')
      },
      (error: unknown) => {
        if (token !== this.token) return
        if (error instanceof DOMException && error.name === 'NotAllowedError' && !this.video.muted) {
          // Phones only start playback by themselves without sound; the
          // speaker button turns it back on.
          this.video.muted = true
          this.play(token)
          return
        }
        this.setState('stalled', 'Playback needs a tap to start.')
      },
    )
  }

  private setState(state: PlaybackState, detail?: string): void {
    if (this.state === state) return
    this.state = state
    this.events.onStateChange?.(state, detail)
  }

  private handleTimeUpdate = (): void => {
    const at = wallClockAt(this.session?.items ?? [], this.video.currentTime)
    if (at) this.events.onTimeUpdate?.(at)
  }

  /** The hour ran out: carry on from where it stopped. */
  private handleEnded = (): void => {
    const end = this.session?.end
    if (end && !this.stopped) void this.seek(new Date(end))
  }

  private handlePlaying = (): void => this.setState('playing')

  private handlePause = (): void => {
    if (!this.video.ended && this.session) this.setState('paused')
  }

  private handleWaiting = (): void => {
    if (this.session) this.setState('loading')
  }
}
