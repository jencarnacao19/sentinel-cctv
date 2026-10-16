/** Native HLS fallback for networks that cannot reach Sentinel's WebRTC port. */

import { Backoff } from '@/features/live/reconnect'
import type { PlayerEvents } from '@/features/live/mse'

const STALL_RETRY_MS = 12_000
const ESTABLISH_RETRY_MS = 12_000
const MAX_COLD_ATTEMPTS = 4

interface HlsSession {
  url: string
}

export function isHlsSupported(video?: HTMLVideoElement): boolean {
  const probe = video ?? document.createElement('video')
  return probe.canPlayType('application/vnd.apple.mpegurl') !== ''
}

/**
 * Apple mobile browsers expose enough of MSE/WebRTC to pass a capability
 * check, but those transports are still the least reliable choices inside
 * Safari and Home Assistant's iOS WebView. Native HLS stays on the same HTTPS
 * origin, survives Wi-Fi/cellular handovers and does not depend on the private
 * WebRTC candidate being reachable.
 *
 * Keep desktop Safari on MSE: it has the resources and network path for the
 * lower-latency wall. The MacIntel/touch check covers iPadOS when it requests a
 * desktop user agent.
 */
export function prefersNativeHls(video?: HTMLVideoElement): boolean {
  if (!isHlsSupported(video)) return false

  const appleMobile = /iPad|iPhone|iPod/i.test(navigator.userAgent)
  const desktopModeIpad = navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1
  return appleMobile || desktopModeIpad
}

export class HlsPlayer {
  private readonly backoff = new Backoff()
  private abort: AbortController | null = null
  private stallTimer: number | null = null
  private establishTimer: number | null = null
  private stopped = false
  private generation = 0
  private everPlayed = false

  constructor(
    private readonly video: HTMLVideoElement,
    private readonly sessionUrl: string,
    private readonly events: PlayerEvents = {},
    private readonly headers: HeadersInit = {},
  ) {}

  start(): void {
    if (!isHlsSupported(this.video)) {
      this.events.onStateChange?.('error', 'This browser cannot play the HTTPS live stream.')
      return
    }
    this.stopped = false
    this.backoff.reset()
    this.everPlayed = false
    this.events.onStateChange?.('connecting', 'Opening the HTTPS live stream…')
    void this.connect()
  }

  stop(): void {
    this.stopped = true
    this.generation += 1
    this.backoff.cancel()
    this.teardown()
  }

  private teardown(): void {
    this.abort?.abort()
    this.abort = null
    if (this.stallTimer !== null) {
      window.clearTimeout(this.stallTimer)
      this.stallTimer = null
    }
    if (this.establishTimer !== null) {
      window.clearTimeout(this.establishTimer)
      this.establishTimer = null
    }
    this.video.removeEventListener('playing', this.onPlaying)
    this.video.removeEventListener('waiting', this.onWaiting)
    this.video.removeEventListener('stalled', this.onWaiting)
    this.video.removeEventListener('error', this.onError)
    this.video.removeEventListener('ended', this.onEnded)
    this.video.pause()
    this.video.removeAttribute('src')
    this.video.load()
  }

  private readonly onPlaying = () => {
    if (this.stopped) return
    if (this.stallTimer !== null) {
      window.clearTimeout(this.stallTimer)
      this.stallTimer = null
    }
    if (this.establishTimer !== null) {
      window.clearTimeout(this.establishTimer)
      this.establishTimer = null
    }
    this.everPlayed = true
    this.backoff.reset()
    this.events.onStateChange?.('playing')
  }

  private readonly onWaiting = () => {
    if (this.stopped || !this.everPlayed || this.stallTimer !== null) return
    this.events.onStateChange?.('stalled', 'The HTTPS live stream is buffering.')
    this.stallTimer = window.setTimeout(() => {
      this.stallTimer = null
      this.scheduleReconnect('The HTTPS live stream stalled. Reconnecting…')
    }, STALL_RETRY_MS)
  }

  private readonly onError = () => {
    if (!this.stopped) this.scheduleReconnect('The HTTPS live stream failed. Reconnecting…')
  }

  private readonly onEnded = () => {
    if (!this.stopped) this.scheduleReconnect('The HTTPS live stream ended. Reconnecting…')
  }

  private scheduleReconnect(detail: string): void {
    if (this.stopped) return
    if (!this.everPlayed && this.backoff.attempt >= MAX_COLD_ATTEMPTS) {
      this.teardown()
      this.events.onStateChange?.('error', 'The HTTPS live stream could not be opened.')
      return
    }
    this.teardown()
    const { attempt } = this.backoff.schedule(() => {
      if (!this.stopped) void this.connect()
    })
    this.events.onStateChange?.('reconnecting', detail, attempt)
  }

  private async connect(): Promise<void> {
    this.generation += 1
    const generation = this.generation
    this.abort = new AbortController()
    try {
      const response = await fetch(this.sessionUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { Accept: 'application/json', ...this.headers },
        signal: this.abort.signal,
      })
      if (!response.ok) throw new Error(`HTTPS live session failed (${response.status}).`)
      const session = (await response.json()) as HlsSession
      if (!session.url.startsWith('/api/v1/streams/')) {
        throw new Error('HTTPS live session returned an invalid address.')
      }
      if (this.stopped || generation !== this.generation) return

      this.video.addEventListener('playing', this.onPlaying)
      this.video.addEventListener('waiting', this.onWaiting)
      this.video.addEventListener('stalled', this.onWaiting)
      this.video.addEventListener('error', this.onError)
      this.video.addEventListener('ended', this.onEnded)
      this.video.src = session.url
      this.video.load()
      void this.video.play().catch(() => undefined)
      this.establishTimer = window.setTimeout(() => {
        this.establishTimer = null
        if (!this.stopped && generation === this.generation && !this.everPlayed) {
          this.scheduleReconnect('The HTTPS live stream did not start. Reconnecting…')
        }
      }, ESTABLISH_RETRY_MS)
    } catch (error) {
      if (this.stopped || generation !== this.generation) return
      if (error instanceof DOMException && error.name === 'AbortError') return
      this.scheduleReconnect(
        error instanceof Error ? error.message : 'Could not open the HTTPS live stream.',
      )
    }
  }
}
