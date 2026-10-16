import { afterEach, describe, expect, it, vi } from 'vitest'

import type { PlayerState } from '@/features/live/mse'
import { HlsPlayer, isHlsSupported, prefersNativeHls } from '@/features/live/hls'

function fakeVideo(): HTMLVideoElement {
  const video = document.createElement('video')
  vi.spyOn(video, 'canPlayType').mockReturnValue('maybe')
  vi.spyOn(video, 'load').mockImplementation(() => undefined)
  vi.spyOn(video, 'play').mockResolvedValue(undefined)
  vi.spyOn(video, 'pause').mockImplementation(() => undefined)
  return video
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('HlsPlayer', () => {
  it('detects native HLS support', () => {
    const video = fakeVideo()
    expect(isHlsSupported(video)).toBe(true)
  })

  it('prefers native HLS on an iPhone even when other transports exist', () => {
    vi.stubGlobal('navigator', {
      userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X)',
      platform: 'iPhone',
      maxTouchPoints: 5,
    })

    expect(prefersNativeHls(fakeVideo())).toBe(true)
  })

  it('keeps desktop Safari on the lower-latency transport', () => {
    vi.stubGlobal('navigator', {
      userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
      platform: 'MacIntel',
      maxTouchPoints: 0,
    })

    expect(prefersNativeHls(fakeVideo())).toBe(false)
  })

  it('covers iPadOS desktop user-agent mode', () => {
    vi.stubGlobal('navigator', {
      userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
      platform: 'MacIntel',
      maxTouchPoints: 5,
    })

    expect(prefersNativeHls(fakeVideo())).toBe(true)
  })

  it('exchanges the viewer header for an opaque media URL and plays it', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          url: '/api/v1/streams/camera-1/hls/opaque-ticket/playlist.m3u8',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)
    const video = fakeVideo()
    const states: PlayerState[] = []
    const player = new HlsPlayer(
      video,
      '/api/v1/streams/camera-1/viewer-hls-session?sub=true',
      { onStateChange: (state) => states.push(state) },
      { 'X-Sentinel-Viewer-Key': 'secret' },
    )

    player.start()
    await vi.waitFor(() => expect(video.src).toContain('opaque-ticket'))
    video.dispatchEvent(new Event('playing'))

    expect(states).toContain('playing')
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/streams/camera-1/viewer-hls-session?sub=true',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Sentinel-Viewer-Key': 'secret' }),
      }),
    )
    player.stop()
  })

  it('refuses a media address outside Sentinel', async () => {
    vi.useFakeTimers()
    try {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue(
          new Response(JSON.stringify({ url: 'https://example.invalid/video.m3u8' }), {
            status: 200,
          }),
        ),
      )
      const states: PlayerState[] = []
      const player = new HlsPlayer(fakeVideo(), '/session', {
        onStateChange: (state) => states.push(state),
      })
      player.start()
      await vi.advanceTimersByTimeAsync(60_000)
      expect(states).toContain('error')
      player.stop()
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not remain on Connecting forever when native playback never starts', async () => {
    vi.useFakeTimers()
    try {
      const request = vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ url: '/api/v1/streams/camera-1/hls/ticket/playlist.m3u8' }),
          { status: 200 },
        ),
      )
      vi.stubGlobal('fetch', request)
      const player = new HlsPlayer(fakeVideo(), '/session')
      player.start()

      await vi.advanceTimersByTimeAsync(15_000)
      expect(request.mock.calls.length).toBeGreaterThan(1)
      player.stop()
    } finally {
      vi.useRealTimers()
    }
  })
})
