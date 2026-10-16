/**
 * MSE player for continuous playback across recorded segment files.
 *
 * Unlike the live player (`features/live/mse.ts`), which appends a single
 * continuous WebSocket stream from go2rtc, playback pulls an ordered
 * manifest of discrete segment files (`GET /cameras/{id}/playback/manifest`)
 * and appends each file's bytes in turn to the same kind of MediaSource
 * `SourceBuffer` the live view uses. Two `SourceBuffer` settings are what
 * make several independent fragmented-MP4 files play as one clip:
 *
 * `mode = 'sequence'` places each append immediately after whatever was
 * appended before it, ignoring the timestamps inside the file -- every
 * segment's own muxer resets its timestamps to zero, so without this every
 * segment after the first would try to start playing at time zero again.
 *
 * `appendWindowStart` / `appendWindowEnd` clip an append to the slice the
 * manifest actually asked for. A segment file on disk is always the whole
 * recorded file; `item.offset_seconds`/`item.duration_seconds` describe how
 * much of it the requested range actually covers, and the window keeps the
 * rest out of the buffer (and out of reach of a seek) rather than only
 * hoping the operator does not scrub past the edge of what was asked for.
 *
 * A gap between two manifest items is never represented as a hole in the
 * buffer: the item after a gap is simply the next thing appended, placed
 * immediately after the one before it by `mode: 'sequence'` regardless of
 * the real wall-clock distance between them. That absence of a hole is what
 * turns a gap into a hard cut instead of the player stalling on empty
 * buffer -- seeing the picture jump is the intended, honest behaviour; a
 * frozen frame would not be.
 *
 * Playback rate is the player's business rather than the page's, because
 * every path that recreates the media -- a seek, a `MediaSource` rebuild, the
 * native fallback -- runs the browser's media load algorithm, which resets
 * `playbackRate` to `defaultPlaybackRate`. Setting both, here, in one place,
 * is what stops 16x silently becoming 1x at the next segment boundary.
 */

import { isMseSupported } from '@/features/live/mse'
import { playbackApi } from '@/features/timeline/api'
import {
  appendSegment,
  bufferTargetsFor,
  clampPlaybackRate,
  totalBufferedSeconds,
  trimHistory,
  wallClockAt,
  type BufferedSegment,
  type PlaybackSpeed,
} from '@/features/timeline/playbackBuffer'
import type { ManifestItem, PlaybackManifest } from '@/features/timeline/types'
import { viewerAuthHeaders } from '@/features/viewer/access'

export type PlaybackState = 'loading' | 'playing' | 'stalled' | 'paused' | 'ended' | 'error'

export interface PlaybackEvents {
  onStateChange?: (state: PlaybackState, detail?: string) => void
  /** Fired as playback advances, with the wall-clock instant now on screen. */
  onTimeUpdate?: (at: Date) => void
}

/**
 * Mirrors `features/live/mse.ts`'s `CANDIDATE_CODECS`. Duplicated rather than
 * imported: the two players are separate MediaSource sessions in separate
 * feature folders, but a recording carries the exact bitstream its camera's
 * live view does -- neither one re-encodes -- so the same negotiation
 * applies for the same reason.
 */
const CANDIDATE_CODECS = [
  'avc1.640029',
  'avc1.64002A',
  'avc1.640032',
  'avc1.640033',
  'avc1.640034',
  'avc1.4d002a',
  'avc1.42E01E',
  'mp4a.40.2',
  'opus',
]

/** A camera with no footage at all must not turn a seek into an endless
 * chain of "try the next segment" hops. */
const MAX_GAP_SKIP_HOPS = 20

/**
 * How many segment downloads may be in flight at once.
 *
 * One is what the player used to do, and it costs a full round trip of dead
 * air between every append -- barely noticeable at 1x, where a segment lasts
 * a minute, and the whole budget at 16x, where it lasts under four seconds.
 * Two overlaps the next download with the current append without turning a
 * fast rate into a burst of parallel requests at the recorder.
 */
const MAX_CONCURRENT_SEGMENT_FETCHES = 2

/**
 * Above this rate the audio is a squeal two octaves up that no browser
 * resamples usefully, so the element is muted from 4x on.
 *
 * The operator's own mute choice is remembered on the way up and handed back
 * on the way down: coming out of 16x must not leave someone who had chosen to
 * listen staring at a muted player, and must not un-mute a player they had
 * deliberately silenced either.
 */
const MUTE_ABOVE_RATE = 2

function supportedCodecs(): string {
  if (!isMseSupported()) return ''
  return CANDIDATE_CODECS.filter((codec) =>
    MediaSource.isTypeSupported(`video/mp4; codecs="${codec}"`),
  ).join(',')
}

export class PlaybackPlayer {
  private mediaSource: MediaSource | null = null
  private sourceBuffer: SourceBuffer | null = null
  private objectUrl: string | null = null
  private segments: BufferedSegment[] = []
  private pendingItems: ManifestItem[] = []
  private manifestCursor: Date | null = null
  private pendingSeekSeconds: number | null = null
  private fallbackNextAt: Date | null = null
  private appending = false
  private loadingManifest = false
  private stopped = false
  /** Incremented on every seek(); async work from a superseded call checks
   * this and quietly stops rather than mutating state a newer call owns. */
  private token = 0
  private state: PlaybackState | null = null
  private rate: PlaybackSpeed = 1
  /** The operator's own `muted` choice, parked here while a fast rate forces
   * the element muted. `null` means nothing is being held. */
  private mutedBeforeFastRate: boolean | null = null
  /** Downloads in flight or waiting to be appended, oldest first. Appends
   * come off the head, so several may be downloading at once yet still reach
   * the SourceBuffer in the order the manifest listed them -- `mode:
   * 'sequence'` places each append after the last, so out-of-order arrival
   * would silently reorder the footage. */
  private fetches: { item: ManifestItem; bytes: Promise<ArrayBuffer | null> }[] = []
  /** Aborts every download belonging to the current token. Replaced, not
   * reused, on each seek. */
  private controller: AbortController | null = null

  constructor(
    private readonly video: HTMLVideoElement,
    private readonly cameraId: string,
    private readonly events: PlaybackEvents = {},
    private readonly viewer = false,
  ) {
    this.video.addEventListener('timeupdate', this.handleTimeUpdate)
    this.video.addEventListener('ended', this.handleEnded)
    // Belt and braces beside `defaultPlaybackRate`: a few WebView builds do
    // not restore it themselves, and this fires on every load, whoever
    // started it.
    this.video.addEventListener('loadedmetadata', this.handleLoadedMetadata)
  }

  /** The speed playback runs at, and the speed it returns to after every
   * seek, segment transition and fallback. */
  get playbackRate(): PlaybackSpeed {
    return this.rate
  }

  /**
   * Change the playback speed.
   *
   * Both `playbackRate` and `defaultPlaybackRate` are written: the first is
   * what plays now, the second is what the browser restores the first to
   * every time the media element loads -- which is every seek, because each
   * one builds a fresh `MediaSource`, and both native-fallback paths besides.
   */
  setPlaybackRate(rate: number): void {
    this.rate = clampPlaybackRate(rate)
    this.applyRate()
    // A faster rate empties the buffer faster; ask for the next segment now
    // rather than waiting for the timeupdate that a stall would prevent.
    void this.pump(this.token)
  }

  private applyRate(): void {
    this.video.defaultPlaybackRate = this.rate
    this.video.playbackRate = this.rate
    this.applyFastRateMute()
  }

  private applyFastRateMute(): void {
    if (this.rate > MUTE_ABOVE_RATE) {
      if (this.mutedBeforeFastRate === null) this.mutedBeforeFastRate = this.video.muted
      this.video.muted = true
      return
    }
    if (this.mutedBeforeFastRate !== null) {
      this.video.muted = this.mutedBeforeFastRate
      this.mutedBeforeFastRate = null
    }
  }

  /** Start (or restart) playback at wall-clock instant `at`. */
  async seek(at: Date, hop = 0): Promise<void> {
    if (this.stopped) return
    const token = (this.token += 1)
    // Before anything else: a download started for the instant the operator
    // has just scrubbed away from is now worthless, and holding the append
    // slot until it finishes would stall the seek they actually asked for.
    this.abortInFlight()
    this.fallbackNextAt = null
    this.setState('loading')

    const target = await this.safeSeekLookup(token, at)
    if (!target || token !== this.token) return

    if (!target.found) {
      if (target.next_started_at && hop < MAX_GAP_SKIP_HOPS) {
        await this.seek(new Date(target.next_started_at), hop + 1)
        return
      }
      this.setState('ended', 'No recorded footage after this time.')
      return
    }

    this.teardownSource()
    if (!isMseSupported()) {
      this.setState('error', 'This browser cannot play recordings (no Media Source support).')
      return
    }
    const codecs = supportedCodecs()
    if (!codecs) {
      this.setState('error', 'This browser supports none of the recording formats offered.')
      return
    }

    this.segments = []
    this.pendingItems = []
    this.pendingSeekSeconds = target.offset_seconds
    this.manifestCursor = new Date(target.started_at as string)

    this.controller = new AbortController()

    const mediaSource = new MediaSource()
    this.mediaSource = mediaSource
    this.objectUrl = URL.createObjectURL(mediaSource)
    this.video.src = this.objectUrl
    // Assigning `src` runs the media load algorithm, which resets the rate.
    this.applyRate()

    mediaSource.addEventListener(
      'sourceopen',
      () => {
        if (this.stopped || token !== this.token) return
        let buffer: SourceBuffer
        try {
          buffer = mediaSource.addSourceBuffer(`video/mp4; codecs="${codecs}"`)
        } catch {
          this.setState('error', 'This browser cannot decode this recording.')
          return
        }
        buffer.mode = 'sequence'
        buffer.addEventListener('updateend', () => void this.pump(token))
        this.sourceBuffer = buffer
        void this.pump(token)
      },
      { once: true },
    )
  }

  stop(): void {
    this.stopped = true
    this.token += 1
    this.video.removeEventListener('timeupdate', this.handleTimeUpdate)
    this.video.removeEventListener('ended', this.handleEnded)
    this.video.removeEventListener('loadedmetadata', this.handleLoadedMetadata)
    this.fallbackNextAt = null
    this.teardownSource()
    this.video.removeAttribute('src')
    this.video.load()
  }

  private async safeSeekLookup(token: number, at: Date) {
    try {
      return await playbackApi.seek(this.cameraId, at, this.viewer)
    } catch {
      if (token === this.token) this.setState('error', 'Could not reach Sentinel.')
      return null
    }
  }

  private teardownSource(): void {
    if (this.sourceBuffer && this.mediaSource?.readyState === 'open') {
      try {
        this.mediaSource.removeSourceBuffer(this.sourceBuffer)
      } catch {
        /* already detached */
      }
    }
    this.sourceBuffer = null
    if (this.mediaSource?.readyState === 'open') {
      try {
        this.mediaSource.endOfStream()
      } catch {
        /* nothing appended yet */
      }
    }
    this.mediaSource = null
    if (this.objectUrl) {
      URL.revokeObjectURL(this.objectUrl)
      this.objectUrl = null
    }
    this.pendingItems = []
    this.abortInFlight()
  }

  /**
   * Drop every download belonging to the media that is going away.
   *
   * `appending` is cleared here as well, and in a `finally` around the one
   * place it is set, because a stale `true` is the worst failure this player
   * has: the new token's `pump()` refuses to run while it is set, so the
   * seek the operator just made would wait out a download nobody wants --
   * seconds of frozen picture at 16x.
   */
  private abortInFlight(): void {
    this.controller?.abort()
    this.controller = null
    this.fetches = []
    this.appending = false
    this.loadingManifest = false
  }

  private setState(state: PlaybackState, detail?: string): void {
    if (this.state === state) return
    this.state = state
    this.events.onStateChange?.(state, detail)
  }

  private async loadManifest(token: number): Promise<void> {
    if (!this.manifestCursor || this.loadingManifest) return
    const start = this.manifestCursor
    const end = new Date(
      start.getTime() + bufferTargetsFor(this.rate).manifestWindowSeconds * 1000,
    )

    let manifest: PlaybackManifest
    // Guarded, and released in `finally`, so the several `pump()` calls a
    // busy `timeupdate` fires cannot each ask for the same page: at 16x that
    // is one wasted request per frame rather than one per page.
    this.loadingManifest = true
    try {
      manifest = await playbackApi.manifest(this.cameraId, start, end, this.viewer)
    } catch {
      if (token === this.token) this.setState('error', 'Could not reach Sentinel.')
      return
    } finally {
      this.loadingManifest = false
    }
    if (token !== this.token || this.stopped) return

    if (manifest.items.length === 0) {
      if (this.segments.length === 0) {
        // Nothing has played yet and this page is empty too. Re-enter the
        // normal seek flow rather than paging through more empty windows
        // here as well -- it already knows how to hop to real footage, or
        // report plainly that there is none.
        await this.seek(end)
        return
      }
      // Something is already playing; an empty follow-up page just means
      // playback has caught up to the edge of what is recorded right now.
      this.setState('paused', 'No further recorded footage yet.')
      return
    }

    this.manifestCursor = new Date(manifest.items[manifest.items.length - 1].ended_at)
    this.pendingItems.push(...manifest.items)
    void this.pump(token)
  }

  private async pump(token: number): Promise<void> {
    if (token !== this.token || this.stopped) return
    const buffer = this.sourceBuffer
    if (!buffer || buffer.updating || this.appending) return

    const targets = bufferTargetsFor(this.rate)
    // Measured in media seconds, divided by the rate to get what it is worth
    // in viewing time. A five-minute buffer is five minutes of cover at 1x
    // and nineteen seconds of it at 16x, and it is the viewing time that
    // decides whether the picture is about to freeze.
    const viewingAhead =
      (totalBufferedSeconds(this.segments) - this.video.currentTime) / this.rate

    if (this.fetches.length === 0 && this.pendingItems.length === 0) {
      if (viewingAhead >= targets.refillSeconds) {
        return // enough is buffered; handleTimeUpdate calls again once it is not
      }
      await this.loadManifest(token)
      return
    }
    if (viewingAhead >= targets.lookaheadSeconds) {
      return
    }

    this.startFetches()
    const next = this.fetches[0]
    if (!next) return

    this.appending = true
    let bytes: ArrayBuffer | null
    try {
      bytes = await next.bytes
    } finally {
      // Always, even for a download a newer seek has abandoned: see
      // `abortInFlight`.
      this.appending = false
    }
    if (token !== this.token || this.stopped || !this.sourceBuffer) return
    // Only this token's pump consumes the queue, and only after the token
    // check -- an abort empties `fetches` outright.
    const item = this.fetches.shift()?.item
    if (!item) return
    if (!bytes) {
      this.setState('error', 'Could not fetch a recording segment.')
      return
    }

    try {
      // Clip to exactly the slice this item represents before sequence mode
      // places it -- the file on disk is the whole segment, not just this.
      this.sourceBuffer.appendWindowStart = item.offset_seconds
      this.sourceBuffer.appendWindowEnd = item.offset_seconds + item.duration_seconds
      this.sourceBuffer.appendBuffer(bytes)
    } catch {
      if (token === this.token) this.playNativeFallback(token, item, bytes)
      return
    }

    this.segments = trimHistory(appendSegment(this.segments, item))

    if (this.pendingSeekSeconds !== null) {
      const seekTo = this.pendingSeekSeconds
      this.pendingSeekSeconds = null
      this.video.currentTime = seekTo
      this.video.play().then(
        () => this.setState('playing'),
        () => this.setState('stalled', 'Playback needs a tap to start.'),
      )
    }

    this.trimPlayedBuffer()
    void this.pump(token)
  }

  /**
   * Top the in-flight window up from the head of the manifest queue.
   *
   * Started in manifest order and appended in the same order from the head of
   * `fetches`, so the concurrency only ever overlaps the downloads -- never
   * the appends, which `mode: 'sequence'` would happily reorder into
   * nonsense.
   */
  private startFetches(): void {
    const controller = this.controller
    if (!controller) return
    while (this.fetches.length < MAX_CONCURRENT_SEGMENT_FETCHES && this.pendingItems.length > 0) {
      const item = this.pendingItems.shift()
      if (!item) return
      this.fetches.push({ item, bytes: this.fetchSegment(item, controller.signal) })
    }
  }

  /**
   * Download one segment. Resolves to `null` rather than rejecting for any
   * failure, abort included: these promises sit in a queue an abort may
   * discard before anyone awaits them, and a rejection nobody is left to
   * catch is an unhandled error in the console at best. An abandoned `null`
   * is never mistaken for a broken download, because the pump only reads one
   * after confirming the token still owns the queue it came from.
   */
  private async fetchSegment(item: ManifestItem, signal: AbortSignal): Promise<ArrayBuffer | null> {
    try {
      const response = await fetch(item.url, {
        credentials: 'same-origin',
        headers: this.viewer ? viewerAuthHeaders() : undefined,
        signal,
      })
      if (!response.ok) throw new Error(`status ${response.status}`)
      return await response.arrayBuffer()
    } catch {
      return null
    }
  }

  private trimPlayedBuffer(): void {
    const buffer = this.sourceBuffer
    if (!buffer || buffer.updating || buffer.buffered.length === 0) return
    const start = buffer.buffered.start(0)
    const keepFrom = this.video.currentTime - bufferTargetsFor(this.rate).keepBehindSeconds
    if (keepFrom > start) {
      try {
        buffer.remove(start, keepFrom)
      } catch {
        /* removal races with an append; the next trim retries */
      }
    }
  }

  /**
   * Some Safari/WebView builds detach a SourceBuffer while a fragmented MP4
   * append is in flight. The bytes are still a complete, independently
   * playable MP4 segment, so fall back to the browser's native file player and
   * continue at the following segment when it ends.
   */
  private playNativeFallback(token: number, item: ManifestItem, bytes: ArrayBuffer): void {
    const seekTo = this.pendingSeekSeconds ?? item.offset_seconds
    this.teardownSource()
    if (token !== this.token || this.stopped) return

    this.segments = appendSegment([], item)
    this.pendingSeekSeconds = null
    this.fallbackNextAt = new Date(new Date(item.ended_at).getTime() + 1)
    const blobUrl = URL.createObjectURL(new Blob([bytes], { type: 'video/mp4' }))
    this.objectUrl = blobUrl
    this.video.src = blobUrl
    this.applyRate()

    const start = () => {
      if (token !== this.token || this.stopped || this.objectUrl !== blobUrl) return
      // This path can run before the element ever fires `loadedmetadata`
      // (the readyState branch below), and the page's own handler for that
      // event is therefore not a place the rate can be restored from.
      this.applyRate()
      this.video.currentTime = seekTo
      this.video.play().then(
        () => this.setState('playing'),
        () => this.setState('stalled', 'Playback needs a tap to start.'),
      )
    }
    if (this.video.readyState >= HTMLMediaElement.HAVE_METADATA) start()
    else this.video.addEventListener('loadedmetadata', start, { once: true })
    this.video.load()
    this.applyRate()
  }

  private handleEnded = (): void => {
    const next = this.fallbackNextAt
    if (!next || this.stopped) return
    this.fallbackNextAt = null
    void this.seek(next)
  }

  private handleLoadedMetadata = (): void => {
    if (this.stopped) return
    this.applyRate()
  }

  private handleTimeUpdate = (): void => {
    const at = wallClockAt(this.segments, this.video.currentTime)
    if (at) this.events.onTimeUpdate?.(at)
    void this.pump(this.token)
  }
}
