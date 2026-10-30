import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { PlayerState } from '@/features/live/mse'
import {
  directRecentlyUnreachable,
  isWebRtcSupported,
  WebRtcPlayer,
} from '@/features/live/webrtc'

class FakeMediaStream {
  private readonly tracks: MediaStreamTrack[] = []

  addTrack(track: MediaStreamTrack) {
    this.tracks.push(track)
  }

  getTracks() {
    return this.tracks
  }
}

class FakePeerConnection extends EventTarget {
  static instances: FakePeerConnection[] = []
  static deliverTrack = true
  static connects = true

  iceGatheringState: RTCIceGatheringState = 'complete'
  connectionState: RTCPeerConnectionState = 'new'
  localDescription: RTCSessionDescription | null = null

  constructor() {
    super()
    FakePeerConnection.instances.push(this)
  }

  addTransceiver() {}

  createOffer(): Promise<RTCSessionDescriptionInit> {
    return Promise.resolve({ type: 'offer', sdp: 'v=0\r\n' })
  }

  setLocalDescription(description: RTCSessionDescriptionInit): Promise<void> {
    this.localDescription = description as RTCSessionDescription
    return Promise.resolve()
  }

  setRemoteDescription(): Promise<void> {
    if (FakePeerConnection.connects) {
      this.connectionState = 'connected'
      this.dispatchEvent(new Event('connectionstatechange'))
    }
    if (FakePeerConnection.deliverTrack) {
      const trackEvent = new Event('track') as Event & { track: MediaStreamTrack }
      trackEvent.track = {
        id: `video-${FakePeerConnection.instances.indexOf(this)}`,
        stop: vi.fn(),
      } as unknown as MediaStreamTrack
      this.dispatchEvent(trackEvent)
    }
    return Promise.resolve()
  }

  close() {
    this.connectionState = 'closed'
  }
}

function fakeVideo(): HTMLVideoElement {
  const video = document.createElement('video')
  Object.defineProperty(video, 'play', { value: vi.fn(() => Promise.resolve()) })
  Object.defineProperty(video, 'load', { value: vi.fn() })
  return video
}

beforeEach(() => {
  FakePeerConnection.instances = []
  FakePeerConnection.deliverTrack = true
  FakePeerConnection.connects = true
  window.sessionStorage.clear()
  vi.stubGlobal('RTCPeerConnection', FakePeerConnection)
  vi.stubGlobal('MediaStream', FakeMediaStream)
})

afterEach(() => vi.unstubAllGlobals())

describe('WebRtcPlayer', () => {
  it('posts an SDP offer with restricted viewer headers and starts playback', async () => {
    const request = vi
      .fn()
      .mockResolvedValue(
        new Response('v=0', {
          status: 200,
          headers: { 'Content-Type': 'application/sdp' },
        }),
      )
    vi.stubGlobal('fetch', request)
    const states: PlayerState[] = []

    const player = new WebRtcPlayer(
      fakeVideo(),
      '/api/v1/streams/camera-1/viewer-webrtc?sub=true',
      { onStateChange: (state) => states.push(state) },
      { 'X-Sentinel-Viewer-Key': 'restricted-key' },
    )
    player.start()

    await vi.waitFor(() => expect(states).toContain('playing'))
    expect(isWebRtcSupported()).toBe(true)
    expect(request).toHaveBeenCalledWith(
      '/api/v1/streams/camera-1/viewer-webrtc?sub=true',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Sentinel-Viewer-Key': 'restricted-key' }),
        body: 'v=0\r\n',
      }),
    )
    player.stop()
    expect(FakePeerConnection.instances[0].connectionState).toBe('closed')
  })

  it('reports a useful error when WebRTC is unavailable', () => {
    vi.stubGlobal('RTCPeerConnection', undefined)
    let detail: string | undefined
    new WebRtcPlayer(fakeVideo(), '/webrtc', {
      onStateChange: (_state, message) => {
        detail = message
      },
    }).start()
    expect(detail).toMatch(/cannot play/i)
  })

  it('hands off to HTTPS when negotiation succeeds but no media can cross the network', async () => {
    vi.useFakeTimers()
    try {
      FakePeerConnection.deliverTrack = false
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('v=0', { status: 200 })))
      const handoff = vi.fn()
      const player = new WebRtcPlayer(fakeVideo(), '/webrtc', { onUnsupported: handoff })
      player.start()

      await vi.advanceTimersByTimeAsync(8_000)
      expect(handoff).toHaveBeenCalledOnce()
      expect(FakePeerConnection.instances[0].connectionState).toBe('closed')
      player.stop()
    } finally {
      vi.useRealTimers()
    }
  })

  it('hands off within seconds when the direct path never connects, and remembers it', async () => {
    // Away from home the answer and its tracks arrive, but the private media
    // address is unreachable; the wall must not wait for media timeouts.
    vi.useFakeTimers()
    try {
      FakePeerConnection.connects = false
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('v=0', { status: 200 })))
      const handoff = vi.fn()
      const player = new WebRtcPlayer(fakeVideo(), '/webrtc', { onUnsupported: handoff })
      player.start()

      await vi.advanceTimersByTimeAsync(2_499)
      expect(handoff).not.toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(1)
      expect(handoff).toHaveBeenCalledOnce()
      expect(directRecentlyUnreachable()).toBe(true)
      player.stop()
    } finally {
      vi.useRealTimers()
    }
  })

  it('reconnects after a failed session instead of ending, on the iOS path', async () => {
    // This is the path an iPhone and the Home Assistant WebView take, so a
    // terminal failure here is the one users can least easily recover from.
    vi.useFakeTimers()
    try {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue(
          new Response('v=0', {
            status: 200,
            headers: { 'Content-Type': 'application/sdp' },
          }),
        ),
      )
      const states: PlayerState[] = []
      const player = new WebRtcPlayer(fakeVideo(), '/webrtc', {
        onStateChange: (state) => states.push(state),
      })
      player.start()
      await vi.waitFor(() => expect(states).toContain('playing'))

      const first = FakePeerConnection.instances[0]
      first.connectionState = 'failed'
      first.dispatchEvent(new Event('connectionstatechange'))
      expect(states).toContain('reconnecting')

      await vi.advanceTimersByTimeAsync(60_000)
      // A second peer connection was built rather than the player giving up.
      expect(FakePeerConnection.instances.length).toBeGreaterThan(1)
      player.stop()
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not reconnect after stop, and leaves no peer connection open', async () => {
    vi.useFakeTimers()
    try {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue(
          new Response('v=0', {
            status: 200,
            headers: { 'Content-Type': 'application/sdp' },
          }),
        ),
      )
      const player = new WebRtcPlayer(fakeVideo(), '/webrtc', {})
      player.start()
      await vi.waitFor(() => expect(FakePeerConnection.instances.length).toBe(1))

      const peer = FakePeerConnection.instances[0]
      peer.connectionState = 'failed'
      peer.dispatchEvent(new Event('connectionstatechange'))
      player.stop()

      await vi.advanceTimersByTimeAsync(120_000)
      // The pending retry was cancelled with the player, so nothing woke up and
      // opened a socket to a camera nobody is watching.
      expect(FakePeerConnection.instances.length).toBe(1)
      expect(peer.connectionState).toBe('closed')
    } finally {
      vi.useRealTimers()
    }
  })

  it('reconnects when disconnected never advances to failed', async () => {
    vi.useFakeTimers()
    try {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue(new Response('v=0', { status: 200 })),
      )
      const states: PlayerState[] = []
      const player = new WebRtcPlayer(fakeVideo(), '/webrtc', {
        onStateChange: (state) => states.push(state),
      })
      player.start()
      await vi.waitFor(() => expect(states).toContain('playing'))

      const first = FakePeerConnection.instances[0]
      first.connectionState = 'disconnected'
      first.dispatchEvent(new Event('connectionstatechange'))
      await vi.advanceTimersByTimeAsync(60_000)

      expect(FakePeerConnection.instances.length).toBeGreaterThan(1)
      player.stop()
    } finally {
      vi.useRealTimers()
    }
  })

  it('gives up on a network it has never crossed rather than retrying for ever', async () => {
    // No STUN is configured and only host candidates are offered, so a network
    // WebRTC cannot cross will never start working; retrying it indefinitely
    // would only drain a phone battery.
    vi.useFakeTimers()
    try {
      vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('negotiation refused')))
      const states: PlayerState[] = []
      const player = new WebRtcPlayer(fakeVideo(), '/webrtc', {
        onStateChange: (state) => states.push(state),
      })
      player.start()

      await vi.advanceTimersByTimeAsync(10 * 60_000)
      expect(states).toContain('error')
      player.stop()
    } finally {
      vi.useRealTimers()
    }
  })
})
