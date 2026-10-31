import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ViewerPage } from '@/features/viewer/ViewerPage'
import { renderWithProviders } from '@/test/utils'

vi.mock('@/features/live/VideoPlayer', () => ({
  VideoPlayer: ({ cameraName }: { cameraName: string }) => <div>Stream: {cameraName}</div>,
}))

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
  window.sessionStorage.clear()
})

describe('ViewerPage', () => {
  it('renders the operator camera wall, playback and a locked admin link', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url =
          input instanceof Request ? input.url : input instanceof URL ? input.href : input
        if (url.includes('/viewer/system/status')) {
          return Promise.resolve(
            json({
              status: 'healthy',
              generated_at: '2026-08-24T17:00:00Z',
              cameras_total: 1,
              cameras_online: 1,
              recorders_expected: 1,
              recorders_running: 1,
              motion_expected: 1,
              motion_running: 1,
              storage_available: true,
              storage_total_bytes: 400_000_000_000,
              storage_used_bytes: 80_000_000_000,
              storage_free_bytes: 320_000_000_000,
              storage_used_percent: 20,
              recordings_bytes: 78_000_000_000,
              saved_clips_count: 2,
              saved_clips_bytes: 1_000_000_000,
              oldest_recording: '2026-08-23T19:00:00Z',
              newest_recording: '2026-08-24T17:00:00Z',
              recording_rate_bytes_per_hour: 4_200_000_000,
              configured_retention_days: 14,
              estimated_retention_days: 3.8,
              estimated_days_remaining: 3,
            }),
          )
        }
        if (url.includes('/viewer/wall')) {
          return Promise.resolve(json({ columns: 'auto', order: [], hidden: [] }))
        }
        return Promise.resolve(
          json([
            {
              id: 'front-door',
              name: 'Front door',
              kind: 'rtsp',
              status: 'online',
              has_substream: true,
              stream: {
                codec: 'h264',
                width: 1920,
                height: 1080,
                fps: 25,
                audio_codec: 'aac',
                bitrate_kbps: 2400,
              },
            },
            {
              id: 'tool-shed',
              name: 'Tool shed',
              kind: 'tapo_battery',
              status: 'online',
              has_substream: false,
              ptz_supported: false,
              ptz_zoom_supported: false,
              stream: {
                codec: null,
                width: null,
                height: null,
                fps: null,
                audio_codec: null,
                bitrate_kbps: null,
              },
            },
          ]),
        )
      }),
    )

    renderWithProviders(<ViewerPage />)

    expect(await screen.findByText('Stream: Front door')).toBeInTheDocument()
    expect(screen.getByText('Event-based battery camera')).toBeInTheDocument()
    expect(
      screen.getByText(
        'View and export footage here. Configuration requires the administrator login.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Playback' })).toHaveAttribute('href', '/playback')
    const adminLink = screen.getByRole('link', { name: /admin/i })
    expect(adminLink).toHaveAttribute('href', '/admin/login')
    expect(adminLink).toHaveAttribute('target', '_blank')
    expect(adminLink).toHaveAttribute('rel', 'noopener noreferrer')
    expect(screen.queryByText('Settings')).not.toBeInTheDocument()
    expect(await screen.findByText(/GB free$/)).toBeInTheDocument()
  })

  it('asks for an account when nobody is signed in on this browser', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(json({ detail: 'Denied' }, 401))))
    renderWithProviders(<ViewerPage />)

    expect(await screen.findByRole('heading', { name: 'Sign in to watch' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeDisabled()
  })

  it('shows the cameras once a viewer account signs in', async () => {
    let signedIn = false
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      if (url.endsWith('/auth/login') && init?.method === 'POST') {
        signedIn = true
        return Promise.resolve(
          json({ access_token: 't', token_type: 'bearer', expires_at: '2026-10-18T00:00:00Z', is_admin: false }),
        )
      }
      if (!signedIn) return Promise.resolve(json({ detail: 'Denied' }, 401))
      if (url.includes('/viewer/cameras')) {
        return Promise.resolve(
          json([
            {
              id: 'front-door',
              name: 'Front door',
              kind: 'rtsp',
              status: 'online',
              has_substream: false,
              ptz_supported: false,
              ptz_zoom_supported: false,
              stream: { codec: 'h264', width: 1920, height: 1080, fps: 25, audio_codec: null, bitrate_kbps: null },
            },
          ]),
        )
      }
      if (url.endsWith('/auth/me')) {
        return Promise.resolve(
          json({ id: 'amy', username: 'amy', is_admin: false, is_active: true, last_login_at: null, created_at: '2026-10-17T00:00:00Z' }),
        )
      }
      return Promise.resolve(json({ detail: 'Not found' }, 404))
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderWithProviders(<ViewerPage />)

    await user.type(await screen.findByLabelText('Username'), 'amy')
    await user.type(screen.getByLabelText('Password'), 'family-password-1')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(await screen.findByText('Stream: Front door')).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Sign out amy' })).toBeInTheDocument()
  })
})
