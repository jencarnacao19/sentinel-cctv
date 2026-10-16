import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TapoBatteryDialog } from '@/features/cameras/TapoBatteryDialog'
import { batteryCamera } from '@/test/cameras'
import { renderWithProviders } from '@/test/utils'

const mpegtsMock = vi.hoisted(() => {
  const player = {
    attachMediaElement: vi.fn(),
    detachMediaElement: vi.fn(),
    destroy: vi.fn(),
    load: vi.fn(),
    on: vi.fn(),
    pause: vi.fn(),
    play: vi.fn(() => Promise.reject(new DOMException('autoplay blocked', 'NotAllowedError'))),
    unload: vi.fn(),
  }
  return { player, createPlayer: vi.fn(() => player) }
})

vi.mock('mpegts.js', () => ({
  default: {
    Events: { ERROR: 'error' },
    createPlayer: mpegtsMock.createPlayer,
    isSupported: () => true,
  },
}))

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function requestUrl(input: RequestInfo | URL): string {
  return typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('TapoBatteryDialog', () => {
  it('renders the redesigned dialog with tabs and telemetry', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      if (url.includes('/tapo-battery/status')) {
        return Promise.resolve(
          json({
            model: 'TC82',
            hardware_version: '1.0',
            firmware_version: '1.2.3',
            battery_percent: 84,
            charging: true,
            charging_source: 'solar',
            read_at: '2026-08-28T20:00:00Z',
          }),
        )
      }
      if (url.includes('/timed-recording/options')) {
        return Promise.resolve(
          json({
            presets_minutes: [5, 10, 15, 30, 60],
            max_minutes: 60,
            hard_max_minutes: 120,
            min_minutes: 1,
            battery_percent: 84,
            warning: 'Recording keeps this battery camera awake.',
          }),
        )
      }
      if (url.includes('/timed-recording')) {
        return Promise.resolve(json(null))
      }
      return Promise.reject(new Error(`Unexpected request to ${url}`))
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderWithProviders(
      <TapoBatteryDialog
        camera={batteryCamera}
        open
        onOpenChange={vi.fn()}
      />,
    )

    // Check header and tabs
    expect(screen.getByText(batteryCamera.name)).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Live' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: /timed recording/i })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /event clips/i })).toBeInTheDocument()

    // Check fixed 16:9 canvas hero action
    expect(screen.getByText('Camera in standby')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /start live view/i })).toBeInTheDocument()

    // Check telemetry data once status resolves
    await waitFor(() => {
      expect(screen.getAllByText(/84/).length).toBeGreaterThan(0)
      expect(screen.getAllByText(/Charging/i).length).toBeGreaterThan(0)
    })

    // Switch to Timed Recording tab
    await user.click(screen.getByRole('tab', { name: /timed recording/i }))
    expect(screen.getByRole('region', { name: 'Timed recording' })).toBeInTheDocument()

    // Switch to Event Clips tab
    await user.click(screen.getByRole('tab', { name: /event clips/i }))
    expect(screen.getByText('Import event clips')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /sync last 24 hours/i })).toBeInTheDocument()
  })

  it('displays a clean contextual banner when battery status fails', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      if (url.includes('/tapo-battery/status')) {
        return Promise.resolve(
          json(
            {
              detail: 'The Tapo battery camera or local hub did not respond.',
            },
            424,
          ),
        )
      }
      return Promise.resolve(json(null))
    })
    vi.stubGlobal('fetch', fetchMock)

    renderWithProviders(
      <TapoBatteryDialog
        camera={batteryCamera}
        open
        onOpenChange={vi.fn()}
      />,
    )

    await waitFor(() => {
      expect(screen.getByText(/The Tapo battery camera or local hub did not respond/)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    })
  })

  it('keeps a valid Direct Stream open when autoplay needs a real tap', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
        if (url.includes('/tapo-battery/status')) {
          return Promise.resolve(
            json({
              model: 'TC82',
              battery_percent: null,
              charging: false,
              read_at: '2026-09-01T12:00:00Z',
            }),
          )
        }
        return Promise.resolve(json(null))
      }),
    )
    const nativePlay = vi
      .spyOn(HTMLMediaElement.prototype, 'play')
      .mockResolvedValue(undefined)
    const user = userEvent.setup()

    renderWithProviders(
      <TapoBatteryDialog camera={batteryCamera} open onOpenChange={vi.fn()} />,
    )

    await user.click(screen.getByRole('button', { name: /start live view/i }))
    expect(await screen.findByRole('button', { name: /tap to play/i })).toBeInTheDocument()
    expect(mpegtsMock.player.destroy).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: /tap to play/i }))
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /tap to play/i })).not.toBeInTheDocument()
    })
    expect(nativePlay).toHaveBeenCalled()
    expect(mpegtsMock.player.destroy).not.toHaveBeenCalled()
  })

  it('does not mislabel a decoder failure as a browser permission problem', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(json(null))))
    mpegtsMock.player.play.mockRejectedValueOnce(new DOMException('bad format', 'NotSupportedError'))
    const user = userEvent.setup()
    renderWithProviders(<TapoBatteryDialog camera={batteryCamera} open onOpenChange={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: /start live view/i }))
    expect(await screen.findByText(/received video could not be played/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /tap to play/i })).not.toBeInTheDocument()
    expect(mpegtsMock.player.destroy).toHaveBeenCalledOnce()
  })

  it('closes the camera session when leaving the live tab', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(json(null))))
    const user = userEvent.setup()
    renderWithProviders(<TapoBatteryDialog camera={batteryCamera} open onOpenChange={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: /start live view/i }))
    await screen.findByRole('button', { name: /tap to play/i })
    await user.click(screen.getByRole('tab', { name: /event clips/i }))
    expect(mpegtsMock.player.destroy).toHaveBeenCalledOnce()
  })

  it('uses native HLS on iPhone and stops the server session when leaving live', async () => {
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('iPhone')
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably')
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
    const base = `/api/v1/cameras/${batteryCamera.id}/tapo-battery/hls/test-grant`
    const fetchMock = vi.fn((input: RequestInfo | URL) => Promise.resolve(
      requestUrl(input).endsWith('hls-session')
        ? json({ url: `${base}/playlist.m3u8`, stop_url: base }) : json(null),
    ))
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderWithProviders(<TapoBatteryDialog camera={batteryCamera} open onOpenChange={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: /start live view/i }))
    await waitFor(() => expect(document.querySelector('video')?.getAttribute('src')).toBe(`${base}/playlist.m3u8`))
    expect(mpegtsMock.createPlayer).not.toHaveBeenCalled()
    fireEvent.playing(document.querySelector('video')!)
    expect(screen.queryByText('Waking camera')).not.toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: /event clips/i }))
    expect(fetchMock).toHaveBeenCalledWith(base, expect.objectContaining({ method: 'DELETE' }))
  })

  it('discards a native session that arrives after the operator cancels', async () => {
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('iPhone')
    vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably')
    const base = `/api/v1/cameras/${batteryCamera.id}/tapo-battery/hls/cancelled-grant`
    let finish: (response: Response) => void = () => {}
    const pending = new Promise<Response>((resolve) => { finish = resolve })
    const fetchMock = vi.fn((input: RequestInfo | URL) => requestUrl(input).endsWith('hls-session')
      ? pending : Promise.resolve(json(null)))
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderWithProviders(<TapoBatteryDialog camera={batteryCamera} open onOpenChange={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: /start live view/i }))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    finish(json({ url: `${base}/playlist.m3u8`, stop_url: base }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(base, expect.objectContaining({ method: 'DELETE' })))
    expect(document.querySelector('video')).not.toHaveAttribute('src')
    expect(mpegtsMock.createPlayer).not.toHaveBeenCalled()
  })
})
