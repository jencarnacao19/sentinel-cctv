/**
 * Media Source Extensions player for go2rtc's WebSocket protocol.
 *
 * Why MSE rather than WebRTC as the default: MSE plays the camera's own H.264
 * bitstream with no transcoding and works in desktop browsers. Home Assistant's
 * iOS WebView does not expose MSE consistently, so VideoPlayer automatically
 * selects the WebRTC fallback there.
 *
 * Protocol (go2rtc `/api/ws`):
 *   client → {"type":"mse","value":"<codec list>"}
 *   server → {"type":"mse","value":"video/mp4; codecs=\"…\""}
 *   server → binary fragmented-MP4 segments, appended in order
 *
 * A dropped connection reconnects on its own (see `features/live/reconnect.ts`)
 * because a live wall that goes dark until somebody reloads the page is not a
 * live wall. Every retry reopens the same relay URL this player was built with:
 * reconnecting must never become a second connection to the camera.
 */

import { Backoff } from '@/features/live/reconnect'

export type PlayerState =
  | 'connecting'
  | 'playing'
  | 'reconnecting'
  | 'stalled'
  | 'error'
  | 'closed'

export interface PlayerEvents {
  /** `attempt` is set only for 'reconnecting', counting retries since the
   *  stream was last delivering media, so the UI can say which one it is on
   *  rather than spinning anonymously. */
  onStateChange?: (state: PlayerState, detail?: string, attempt?: number) => void
  /**
   * The MSE path cannot work in this browser at all: no MediaSource, no codec
   * in common, a SourceBuffer it refuses to create, or a relay socket that
   * never once delivered playable media. Distinct from a drop, which heals
   * itself — no amount of retrying fixes any of these, so VideoPlayer answers
   * it by handing the camera to the WebRTC player instead.
   */
  onUnsupported?: () => void
}

/**
 * Codecs offered to go2rtc, most preferred first.
 *
 * H.265 is deliberately absent: only Safari and very recent Chrome builds can
 * decode it in MSE, and offering it to a browser that cannot would produce a
 * black player rather than an honest error. An H.265 camera falls back to the
 * transcode go2rtc performs on request.
 */
const CANDIDATE_CODECS = [
  'avc1.640029', // H.264 High 4.1
  'avc1.64002A', // H.264 High 4.2
  'avc1.640032', // H.264 High 5.0
  'avc1.640033', // H.264 High 5.1
  'avc1.640034', // H.264 High 5.2
  'avc1.4d002a', // H.264 Main
  'avc1.42E01E', // H.264 Baseline
  'mp4a.40.2', // AAC-LC
  'opus',
]

/** Keep a small safety window without retaining half a minute per camera. */
const MAX_BUFFER_SECONDS = 12

/** How far behind live the playhead may drift before it is nudged forward. */
const MAX_LATENCY_SECONDS = 4

/** Avoid running SourceBuffer.remove() for every fragment. */
const TRIM_BATCH_SECONDS = 4

/**
 * Hard ceiling on segments waiting for the SourceBuffer.
 *
 * A backgrounded tab throttles `updateend` to a crawl while the socket keeps
 * delivering at full rate, so without a ceiling an overnight tab is hundreds of
 * megabytes of retained ArrayBuffers. A live view wants the newest picture, not
 * a faithful backlog, so an overflow drops the oldest segments and the seek in
 * `ensurePlaying` pulls the playhead back to the live edge.
 */
const MAX_QUEUED_SEGMENTS = 24

/**
 * Retries without ever having played before the MSE path is declared hopeless.
 *
 * A stream that played and then dropped is flapping and worth retrying forever.
 * One that has never produced a frame after this many attempts is not flapping:
 * something about this browser or this camera's MSE path does not work, and
 * WebRTC is the honest next thing to try.
 */
const ESTABLISH_ATTEMPTS = 3

export function isMseSupported(): boolean {
  return typeof MediaSource !== 'undefined' && typeof MediaSource.isTypeSupported === 'function'
}

function supportedCodecs(): string {
  if (!isMseSupported()) return ''
  return CANDIDATE_CODECS.filter((codec) =>
    MediaSource.isTypeSupported(`video/mp4; codecs="${codec}"`),
  ).join(',')
}

export function websocketUrl(path: string): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}${path}`
}

/**
 * Attaches a live stream to a `<video>` element.
 *
 * Every resource it creates is released by `stop()`, which the React wrapper
 * calls on unmount — a leaked MediaSource keeps the camera connection open and,
 * with a dozen cameras on a dashboard, that adds up fast. The same teardown runs
 * between reconnect attempts, so a night of drops leaks no more than one.
 */
export class MsePlayer {
  private ws: WebSocket | null = null
  private mediaSource: MediaSource | null = null
  private sourceBuffer: SourceBuffer | null = null
  private readonly queue: ArrayBuffer[] = []
  private objectUrl: string | null = null
  private stopped = false
  private everPlayed = false
  private readonly backoff = new Backoff()
  /**
   * Bumped by every teardown, `stop()` included, so an async continuation from
   * a socket, a MediaSource or a `play()` promise we have finished with can see
   * that it no longer owns this player and abandon quietly.
   *
   * Nulling `ws.onclose` and friends cannot do this job: every listener here is
   * registered with `addEventListener`, which those properties do not touch.
   */
  private generation = 0
  // Starts as null, not 'connecting', so the very first transition is reported
  // like every other one. A consumer that only listens to the callback would
  // otherwise never learn the player had started.
  private state: PlayerState | null = null

  constructor(
    private readonly video: HTMLVideoElement,
    private readonly url: string,
    private readonly events: PlayerEvents = {},
    private readonly protocols?: string | string[],
  ) {}

  start(): void {
    if (!isMseSupported()) {
      this.setState('error', 'This browser cannot play the stream (no Media Source support).')
      this.events.onUnsupported?.()
      return
    }
    if (!supportedCodecs()) {
      this.setState('error', 'This browser supports none of the stream formats offered.')
      this.events.onUnsupported?.()
      return
    }

    this.stopped = false
    this.everPlayed = false
    this.backoff.reset()
    this.setState('connecting')
    this.open()
  }

  stop(): void {
    this.stopped = true
    this.backoff.reset()
    this.teardownTransport()
  }

  /**
   * Retry now, at the operator's request, rather than waiting out the ladder.
   *
   * A refused autoplay leaves a perfectly good stream attached to a paused
   * element, and the tap that reached this method is the very gesture the
   * browser was holding out for — so try that before throwing away a working
   * socket. Only when there is nothing playable to resume is the connection
   * rebuilt.
   */
  retry(): void {
    if (this.stopped) return
    this.backoff.reset()

    const buffered = this.sourceBuffer?.buffered
    if (this.ws?.readyState === WebSocket.OPEN && buffered && buffered.length > 0) {
      void this.ensurePlaying()
      return
    }

    this.teardownTransport()
    this.setState('connecting')
    this.open()
  }

  /** Opens the one relay socket this player ever uses. Every reconnect comes
   *  back through here with the same URL and the same subprotocol. */
  private open(): void {
    if (this.stopped) return
    const generation = this.generation
    const codecs = supportedCodecs()

    let socket: WebSocket
    try {
      socket = this.protocols
        ? new WebSocket(this.url, this.protocols)
        : new WebSocket(this.url)
    } catch {
      // Construction only throws on a malformed URL or a rejected subprotocol.
      // Neither is fixed by waiting, so this one stays terminal.
      this.setState('error', 'Could not open the live stream connection.')
      return
    }
    this.ws = socket
    socket.binaryType = 'arraybuffer'

    socket.addEventListener('open', () => {
      if (this.superseded(generation)) return
      socket.send(JSON.stringify({ type: 'mse', value: codecs }))
    })

    socket.addEventListener('message', (event: MessageEvent<unknown>) => {
      if (this.superseded(generation)) return
      if (typeof event.data === 'string') {
        this.handleControlMessage(event.data)
      } else if (event.data instanceof ArrayBuffer) {
        this.enqueue(event.data)
      }
    })

    // A WebSocket 'error' is always followed by 'close', so the reconnect
    // decision is taken once, below. This listener exists only so a refused
    // connection does not surface as an uncaught browser error event.
    socket.addEventListener('error', () => undefined)

    socket.addEventListener('close', (event) => {
      if (this.superseded(generation)) return
      // 1008 is the relay refusing on purpose: privacy mode, or a viewer key
      // that does not cover this camera. Retrying it would be neither useful
      // nor honest — it is the one close the operator has to see and act on.
      if (event.code === 1008) {
        this.setState('closed', event.reason || 'Not authorised to view this camera.')
        return
      }
      this.scheduleReconnect('The live stream dropped. Reconnecting…')
    })
  }

  /** True when async work belongs to a connection attempt this player has
   *  already released, and so must not touch its state. */
  private superseded(generation: number): boolean {
    return this.stopped || generation !== this.generation
  }

  private scheduleReconnect(detail: string): void {
    if (this.stopped) return
    this.teardownTransport()
    const { attempt } = this.backoff.schedule(() => this.open())
    this.setState('reconnecting', detail, attempt)

    // Told exactly once, on the attempt that proves this is not a flap.
    if (!this.everPlayed && attempt === ESTABLISH_ATTEMPTS) this.events.onUnsupported?.()
  }

  /**
   * Releases everything one connection attempt owns, without deciding whether
   * another follows.
   *
   * Both `stop()` and every reconnect go through here, so neither can leave a
   * socket or a MediaSource holding the camera open behind a player that has
   * moved on.
   */
  private teardownTransport(): void {
    this.generation += 1
    this.queue.length = 0

    if (this.ws) {
      if (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING) {
        this.ws.close()
      }
      this.ws = null
    }

    if (this.sourceBuffer) {
      try {
        if (this.mediaSource?.readyState === 'open') {
          this.mediaSource.removeSourceBuffer(this.sourceBuffer)
        }
      } catch {
        /* already detached */
      }
      this.sourceBuffer = null
    }

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
    this.video.removeAttribute('src')
    this.video.load()
  }

  private setState(state: PlayerState, detail?: string, attempt?: number): void {
    // 'reconnecting' repeats with a rising attempt number, so it is never a
    // duplicate; every other state is reported only when it actually changes.
    if (this.state === state && state !== 'reconnecting') return
    this.state = state
    this.events.onStateChange?.(state, detail, attempt)
  }

  private handleControlMessage(raw: string): void {
    let message: { type?: string; value?: string }
    try {
      message = JSON.parse(raw) as { type?: string; value?: string }
    } catch {
      return
    }

    if (message.type === 'error') {
      // The relay describing a stream it cannot serve, which is not the same as
      // a dropped connection: reported as-is, and the close that normally
      // follows is what decides whether waiting is worth it.
      this.setState('error', message.value ?? 'The stream reported an error.')
      return
    }
    if (message.type === 'mse' && message.value) {
      this.attachMediaSource(message.value)
    }
  }

  private attachMediaSource(mimeCodec: string): void {
    if (this.stopped || this.mediaSource) return
    const generation = this.generation

    const mediaSource = new MediaSource()
    this.mediaSource = mediaSource
    this.objectUrl = URL.createObjectURL(mediaSource)
    this.video.src = this.objectUrl

    mediaSource.addEventListener(
      'sourceopen',
      () => {
        if (this.superseded(generation)) return
        try {
          const buffer = mediaSource.addSourceBuffer(mimeCodec)
          buffer.mode = 'segments'
          buffer.addEventListener('updateend', () => {
            if (this.superseded(generation)) return
            // appendBuffer() marks the SourceBuffer busy synchronously. The
            // previous trim call happened immediately afterwards and therefore
            // returned every time; long-running walls kept their whole history
            // until the browser started stuttering or raised QuotaExceededError.
            this.trim(false)
            this.flush()
            void this.ensurePlaying()
          })
          this.sourceBuffer = buffer
          this.flush()
        } catch {
          // A codec the browser advertised and then refused. Retrying the same
          // negotiation would land in the same place, so hand over instead.
          this.setState('error', `This browser cannot decode ${mimeCodec}.`)
          this.events.onUnsupported?.()
        }
      },
      { once: true },
    )
  }

  private enqueue(chunk: ArrayBuffer): void {
    if (this.stopped) return
    this.queue.push(chunk)
    if (this.queue.length > MAX_QUEUED_SEGMENTS) {
      this.queue.splice(0, this.queue.length - MAX_QUEUED_SEGMENTS)
    }
    this.flush()
  }

  private flush(): void {
    const buffer = this.sourceBuffer
    if (!buffer || buffer.updating || this.queue.length === 0 || this.stopped) return

    const chunk = this.queue.shift()
    if (!chunk) return

    try {
      buffer.appendBuffer(chunk)
    } catch {
      // QuotaExceededError: the buffer is full. Preserve the newest fragment,
      // remove played media and retry it on the removal's updateend event.
      this.queue.unshift(chunk)
      this.trim(true)
      return
    }
  }

  private trim(aggressive: boolean): void {
    const buffer = this.sourceBuffer
    if (!buffer || buffer.updating || buffer.buffered.length === 0) return

    const start = buffer.buffered.start(0)
    const end = buffer.buffered.end(buffer.buffered.length - 1)
    const keepFrom = end - (aggressive ? MAX_BUFFER_SECONDS / 3 : MAX_BUFFER_SECONDS)
    const minimumRemoval = aggressive ? 0.25 : TRIM_BATCH_SECONDS
    if (keepFrom - start >= minimumRemoval) {
      try {
        buffer.remove(start, keepFrom)
      } catch {
        /* removal races with an append; the next updateend retries */
      }
    }
  }

  private async ensurePlaying(): Promise<void> {
    const buffer = this.sourceBuffer
    if (!buffer || buffer.buffered.length === 0) return
    const generation = this.generation

    const liveEdge = buffer.buffered.end(buffer.buffered.length - 1)

    // A tab that was backgrounded resumes far behind live. Seeking forward is
    // the whole point of a live view: the operator wants now, not the backlog.
    if (liveEdge - this.video.currentTime > MAX_LATENCY_SECONDS) {
      this.video.currentTime = liveEdge - 0.5
    }

    if (this.video.paused) {
      try {
        await this.video.play()
        if (this.superseded(generation)) return
        this.markPlaying()
      } catch {
        // Autoplay was refused. The element is muted, so this is rare; when it
        // happens the operator's tap on the resume control recovers it.
        if (this.superseded(generation)) return
        this.setState('stalled', 'Playback needs a tap to start.')
      }
    } else {
      this.markPlaying()
    }
  }

  /** Media is on screen, so the ladder has earned its way back to the first
   *  rung and this connection has proved the MSE path works here. */
  private markPlaying(): void {
    this.everPlayed = true
    this.backoff.reset()
    this.setState('playing')
  }
}
