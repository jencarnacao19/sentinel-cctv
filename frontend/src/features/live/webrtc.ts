/**
 * The first live player every browser tries.
 *
 * On the home network the browser reaches go2rtc directly and the picture
 * starts in a fraction of a second. A remote peer that cannot reach the private
 * media candidate hands off to the same-origin HTTPS players instead of
 * spinning, and later players skip it for a while.
 */

import { Backoff } from '@/features/live/reconnect'
import type { PlayerEvents } from '@/features/live/mse'

const ICE_GATHER_TIMEOUT_MS = 2_000
/** A LAN peer connects in well under a second; past this it is not reachable. */
const ICE_CONNECT_TIMEOUT_MS = 2_500
const DISCONNECTED_RETRY_MS = 5_000
const MEDIA_ESTABLISH_TIMEOUT_MS = 8_000

const UNREACHABLE_KEY = 'sentinel.webrtc.unreachable-at'
/** Long enough that one page of cameras pays the wait once, short enough that
 *  a phone carried back home goes direct again without a reload. */
const UNREACHABLE_FOR_MS = 5 * 60_000

/** True when a direct session recently failed to connect on this network. */
export function directRecentlyUnreachable(): boolean {
  try {
    const at = Number(window.sessionStorage.getItem(UNREACHABLE_KEY))
    return at > 0 && Date.now() - at < UNREACHABLE_FOR_MS
  } catch {
    return false
  }
}

function markDirectUnreachable(): void {
  try {
    window.sessionStorage.setItem(UNREACHABLE_KEY, String(Date.now()))
  } catch {
    // Storage blocked: every player simply tries direct first.
  }
}

/**
 * Consecutive signalling failures after which HTTPS takes over.
 *
 * A session that has never connected is usually a network WebRTC cannot cross
 * at all -- there is no STUN server configured and only host candidates are
 * offered -- and retrying that for ever would drain a phone battery to reach
 * the same answer. A successful SDP exchange that delivers no media takes the
 * faster eight-second handoff; this limit covers fast failures of signalling.
 */
const MAX_COLD_ATTEMPTS = 6

export function isWebRtcSupported(): boolean {
  return typeof RTCPeerConnection !== 'undefined'
}

async function waitForIceGathering(peer: RTCPeerConnection): Promise<void> {
  if (peer.iceGatheringState === 'complete') return

  await new Promise<void>((resolve) => {
    const timeout = window.setTimeout(done, ICE_GATHER_TIMEOUT_MS)
    function done() {
      window.clearTimeout(timeout)
      peer.removeEventListener('icegatheringstatechange', changed)
      resolve()
    }
    function changed() {
      if (peer.iceGatheringState === 'complete') done()
    }
    peer.addEventListener('icegatheringstatechange', changed)
  })
}

export class WebRtcPlayer {
  private peer: RTCPeerConnection | null = null
  private stream: MediaStream | null = null
  private abort: AbortController | null = null
  private disconnectedTimer: number | null = null
  private establishTimer: number | null = null
  private connectTimer: number | null = null
  private stopped = false
  private readonly backoff = new Backoff()
  /** Bumped on every stop and every reconnect, so a late async continuation
   *  from a superseded attempt cannot touch the current one. */
  private generation = 0
  /** True once media has actually arrived on this player, which is what makes a
   *  later failure worth retrying for ever rather than giving up. */
  private everConnected = false

  constructor(
    private readonly video: HTMLVideoElement,
    private readonly offerUrl: string,
    private readonly events: PlayerEvents = {},
    private readonly headers: HeadersInit = {},
  ) {}

  start(): void {
    if (!isWebRtcSupported()) {
      this.events.onStateChange?.('error', 'This browser cannot play live video.')
      return
    }
    this.stopped = false
    this.backoff.reset()
    this.everConnected = false
    this.events.onStateChange?.('connecting')
    void this.connect()
  }

  stop(): void {
    this.stopped = true
    this.generation += 1
    // Cancels the pending retry as well as the in-flight negotiation: without
    // this a stopped player would still wake up and open a peer connection.
    this.backoff.cancel()
    this.teardown()
  }

  /** Drops the current session without ending the player, so a reconnect never
   *  leaves an orphaned peer connection or a live track behind it. */
  private teardown(): void {
    if (this.disconnectedTimer !== null) {
      window.clearTimeout(this.disconnectedTimer)
      this.disconnectedTimer = null
    }
    if (this.establishTimer !== null) {
      window.clearTimeout(this.establishTimer)
      this.establishTimer = null
    }
    if (this.connectTimer !== null) {
      window.clearTimeout(this.connectTimer)
      this.connectTimer = null
    }
    this.abort?.abort()
    this.abort = null
    this.peer?.close()
    this.peer = null
    for (const track of this.stream?.getTracks() ?? []) track.stop()
    this.stream = null
    this.video.srcObject = null
    this.video.load()
  }

  private superseded(generation: number): boolean {
    return this.stopped || generation !== this.generation
  }

  private scheduleReconnect(detail: string): void {
    if (this.stopped) return
    if (!this.everConnected && this.backoff.attempt >= MAX_COLD_ATTEMPTS) {
      this.handoffToHttps()
      return
    }
    this.teardown()
    const { attempt } = this.backoff.schedule(() => {
      if (this.stopped) return
      void this.connect()
    })
    this.events.onStateChange?.('reconnecting', detail, attempt)
  }

  private handoffToHttps(): void {
    if (this.stopped) return
    markDirectUnreachable()
    this.teardown()
    if (this.events.onUnsupported) {
      this.events.onStateChange?.(
        'reconnecting',
        'The direct mobile stream is blocked on this network. Switching to HTTPS…',
        1,
      )
      this.events.onUnsupported()
    } else {
      this.events.onStateChange?.(
        'error',
        'The mobile live stream could not be established on this network.',
      )
    }
  }

  private async connect(): Promise<void> {
    this.generation += 1
    const generation = this.generation
    try {
      const peer = new RTCPeerConnection({ iceServers: [] })
      this.peer = peer
      this.stream = new MediaStream()
      this.video.srcObject = this.stream
      this.establishTimer = window.setTimeout(() => {
        this.establishTimer = null
        if (!this.superseded(generation) && !this.everConnected) this.handoffToHttps()
      }, MEDIA_ESTABLISH_TIMEOUT_MS)

      // A track alone arrives with the answer, before any packet crosses the
      // network, and a connected transport alone may carry no video. Only both
      // together earn a cold network unlimited reconnects.
      const established = () => {
        if (this.everConnected || this.superseded(generation)) return
        if (peer.connectionState !== 'connected' || !this.stream?.getTracks().length) return
        this.everConnected = true
        for (const timer of [this.establishTimer, this.connectTimer]) {
          if (timer !== null) window.clearTimeout(timer)
        }
        this.establishTimer = null
        this.connectTimer = null
        this.backoff.reset()
      }

      peer.addTransceiver('video', { direction: 'recvonly' })
      peer.addTransceiver('audio', { direction: 'recvonly' })
      peer.addEventListener('track', (event) => {
        const stream = this.stream
        if (!stream || this.superseded(generation)) return
        if (!stream.getTracks().some((track) => track.id === event.track.id)) {
          stream.addTrack(event.track)
        }
        established()
        void this.video.play().then(
          () => this.events.onStateChange?.('playing'),
          () =>
            this.events.onStateChange?.(
              'stalled',
              'The mobile live stream is ready and needs a tap to start.',
            ),
        )
      })
      peer.addEventListener('connectionstatechange', () => {
        if (this.superseded(generation)) return
        if (peer.connectionState === 'connected') {
          if (this.disconnectedTimer !== null) {
            window.clearTimeout(this.disconnectedTimer)
            this.disconnectedTimer = null
          }
          established()
        } else if (peer.connectionState === 'failed') {
          this.scheduleReconnect('The mobile live stream failed. Reconnecting…')
        } else if (peer.connectionState === 'disconnected') {
          // `disconnected` is often transient. Give ICE a short recovery grace,
          // then rebuild if the browser remains stuck there; some WebViews
          // never advance this state to `failed` after a Wi-Fi/cell handover.
          this.events.onStateChange?.('stalled', 'The mobile live stream was interrupted.')
          if (this.disconnectedTimer === null) {
            this.disconnectedTimer = window.setTimeout(() => {
              this.disconnectedTimer = null
              if (
                !this.superseded(generation) &&
                peer.connectionState === 'disconnected'
              ) {
                this.scheduleReconnect('The mobile live stream stayed disconnected. Reconnecting…')
              }
            }, DISCONNECTED_RETRY_MS)
          }
        }
      })

      const offer = await peer.createOffer()
      await peer.setLocalDescription(offer)
      await waitForIceGathering(peer)
      if (this.superseded(generation)) return

      this.abort = new AbortController()
      const response = await fetch(this.offerUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { Accept: 'application/sdp', 'Content-Type': 'application/sdp', ...this.headers },
        body: peer.localDescription?.sdp ?? offer.sdp,
        signal: this.abort.signal,
      })
      if (!response.ok) throw new Error(`WebRTC negotiation failed (${response.status}).`)

      const answer = await response.text()
      if (this.superseded(generation)) return
      if (!this.everConnected) {
        this.connectTimer = window.setTimeout(() => {
          this.connectTimer = null
          if (!this.superseded(generation) && peer.connectionState !== 'connected') {
            this.handoffToHttps()
          }
        }, ICE_CONNECT_TIMEOUT_MS)
      }
      await peer.setRemoteDescription({ type: 'answer', sdp: answer })
    } catch (error) {
      if (this.superseded(generation)) return
      if (error instanceof DOMException && error.name === 'AbortError') return
      this.scheduleReconnect(
        error instanceof Error ? error.message : 'Could not start the mobile live stream.',
      )
    }
  }
}
