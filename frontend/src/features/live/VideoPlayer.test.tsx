import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const calls = vi.hoisted(() => ({
  mseStarts: 0,
  mseStops: 0,
  webRtcStarts: 0,
  webRtcStops: 0,
  hlsStarts: 0,
  hlsStops: 0,
  preferHls: false,
  webRtcFallsBack: false,
}))

vi.mock('@/features/live/mse', () => ({
  isMseSupported: () => true,
  websocketUrl: (path: string) => `ws://sentinel${path}`,
  MsePlayer: class {
    private readonly events: { onUnsupported?: () => void }

    constructor(
      _video: HTMLVideoElement,
      _url: string,
      events: { onUnsupported?: () => void },
    ) {
      this.events = events
    }

    start() {
      calls.mseStarts += 1
      this.events.onUnsupported?.()
    }

    stop() {
      calls.mseStops += 1
    }
  },
}))

vi.mock('@/features/live/webrtc', () => ({
  isWebRtcSupported: () => true,
  WebRtcPlayer: class {
    private readonly events: { onUnsupported?: () => void }

    constructor(
      _video: HTMLVideoElement,
      _url: string,
      events: { onUnsupported?: () => void },
    ) {
      this.events = events
    }

    start() {
      calls.webRtcStarts += 1
      if (calls.webRtcFallsBack) this.events.onUnsupported?.()
    }

    stop() {
      calls.webRtcStops += 1
    }
  },
}))

vi.mock('@/features/live/hls', () => ({
  prefersNativeHls: () => calls.preferHls,
  HlsPlayer: class {
    start() {
      calls.hlsStarts += 1
    }

    stop() {
      calls.hlsStops += 1
    }
  },
}))

import { VideoPlayer } from '@/features/live/VideoPlayer'

describe('VideoPlayer transport fallback', () => {
  beforeEach(() => {
    calls.mseStarts = 0
    calls.mseStops = 0
    calls.webRtcStarts = 0
    calls.webRtcStops = 0
    calls.hlsStarts = 0
    calls.hlsStops = 0
    calls.preferHls = false
    calls.webRtcFallsBack = false
  })

  it('uses same-origin native HLS first on Apple mobile browsers', async () => {
    calls.preferHls = true
    const view = render(<VideoPlayer cameraId="camera-1" cameraName="Front door" viewer />)

    await waitFor(() => expect(calls.hlsStarts).toBe(1))
    expect(calls.mseStarts).toBe(0)
    expect(calls.webRtcStarts).toBe(0)

    view.unmount()
    expect(calls.hlsStops).toBe(1)
  })

  it('moves from WebRTC to same-origin HTTPS when direct media cannot arrive', async () => {
    calls.webRtcFallsBack = true
    const view = render(
      <VideoPlayer cameraId="camera-1" cameraName="Front door" viewer />,
    )

    await waitFor(() => expect(calls.hlsStarts).toBe(1))
    expect(calls.webRtcStarts).toBe(1)
    expect(calls.webRtcStops).toBe(1)

    view.unmount()
    expect(calls.hlsStops).toBe(1)
  })

  it('hands an unsupported MSE stream to WebRTC and cleans both players up', async () => {
    const view = render(<VideoPlayer cameraId="camera-1" cameraName="Front door" />)

    await waitFor(() => expect(calls.webRtcStarts).toBe(1))
    expect(calls.mseStarts).toBe(1)
    expect(calls.mseStops).toBe(1)

    view.unmount()
    expect(calls.webRtcStops).toBe(1)
  })

  it('falls back to a full-viewport theatre layer when mobile fullscreen is unavailable', () => {
    const view = render(<VideoPlayer cameraId="camera-1" cameraName="Front door" />)
    const button = screen.getByRole('button', { name: 'Show Front door fullscreen' })
    const video = view.container.querySelector('video')
    const player = video?.parentElement
    const starts = calls.webRtcStarts
    const stops = calls.webRtcStops

    fireEvent.click(button)

    expect(player).toHaveClass('fixed', 'inset-0', 'h-[100dvh]', 'w-[100dvw]')
    expect(screen.getByRole('button', { name: 'Exit fullscreen for Front door' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )

    fireEvent.click(screen.getByRole('button', { name: 'Exit fullscreen for Front door' }))
    expect(player).not.toHaveClass('fixed')
    expect(view.container.querySelector('video')).toBe(video)
    expect(calls.webRtcStarts).toBe(starts)
    expect(calls.webRtcStops).toBe(stops)
  })
})
