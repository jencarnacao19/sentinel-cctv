import { screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from '@/App'
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
  window.history.replaceState(null, '', '/')
  window.sessionStorage.clear()
})

describe('App viewer entry points', () => {
  it('keeps the Home Assistant fragment long enough to authorise /embed', async () => {
    window.history.replaceState(null, '', '/embed#viewer_key=home-assistant-viewer-key')

    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : input instanceof URL ? input.href : input
      if (url.includes('/auth/status')) {
        return Promise.resolve(json({ initialised: true, setup_completed: true }))
      }
      if (url.includes('/auth/me')) return Promise.resolve(json({ detail: 'Signed out' }, 401))
      if (url.includes('/viewer/session')) return Promise.resolve(new Response(null, { status: 204 }))
      if (url.includes('/viewer/system/status')) {
        return Promise.resolve(
          json({
            status: 'healthy',
            generated_at: '2026-08-24T21:00:00Z',
            cameras_total: 1,
            cameras_online: 1,
            recorders_expected: 1,
            recorders_running: 1,
            motion_expected: 1,
            motion_running: 1,
            storage_available: true,
            storage_total_bytes: 400_000_000_000,
            storage_used_bytes: 100_000_000_000,
            storage_free_bytes: 300_000_000_000,
            storage_used_percent: 25,
            recordings_bytes: 90_000_000_000,
            saved_clips_count: 0,
            saved_clips_bytes: 0,
            oldest_recording: '2026-08-23T21:00:00Z',
            newest_recording: '2026-08-24T21:00:00Z',
            recording_rate_bytes_per_hour: 4_000_000_000,
            configured_retention_days: 14,
            estimated_retention_days: 4,
            estimated_days_remaining: 3,
          }),
        )
      }
      if (url.includes('/viewer/cameras')) {
        return Promise.resolve(
          json([
            {
              id: 'front-door',
              name: 'Front door',
              kind: 'rtsp',
              status: 'online',
              has_substream: false,
              stream: { codec: 'h264', width: 1920, height: 1080, fps: 25 },
            },
          ]),
        )
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    renderWithProviders(<App />, { route: '/embed#viewer_key=home-assistant-viewer-key' })

    expect(await screen.findByText('Stream: Front door')).toBeInTheDocument()
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/v1/viewer/session',
        expect.objectContaining({
          method: 'POST',
          body: 'home-assistant-viewer-key',
        }),
      )
    })
    expect(window.location.hash).toBe('')
  })
})

function routes(table: Record<string, unknown>) {
  return vi.fn((input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : input instanceof URL ? input.href : input
    const path = url.replace(/^\/api\/v1/, '').split('?')[0]
    if (path in table) {
      const body = table[path]
      return Promise.resolve(
        body instanceof Response ? body : json(body),
      )
    }
    return Promise.resolve(json([]))
  })
}

const ADMIN = {
  id: 'admin',
  username: 'operator',
  is_admin: true,
  is_active: true,
  last_login_at: null,
  created_at: '2026-10-17T00:00:00Z',
}

describe('App first-run routing', () => {
  it('sends an administrator back into the setup steps until they are finished', async () => {
    vi.stubGlobal(
      'fetch',
      routes({
        '/auth/status': { initialised: true, setup_completed: false },
        '/auth/me': ADMIN,
        '/storage/settings': { path: null, retention_days: 14, max_bytes: null, min_free_bytes: 0 },
      }),
    )
    renderWithProviders(<App />, { route: '/admin' })

    expect(
      await screen.findByRole('heading', { name: 'Coming from another Sentinel?' }),
    ).toBeInTheDocument()
  })

  it('keeps a viewer account out of administration', async () => {
    vi.stubGlobal(
      'fetch',
      routes({
        '/auth/status': { initialised: true, setup_completed: true },
        '/auth/me': { ...ADMIN, id: 'amy', username: 'amy', is_admin: false },
      }),
    )
    renderWithProviders(<App />, { route: '/admin' })

    expect(await screen.findByRole('heading', { name: 'Live cameras' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign out amy' })).toBeInTheDocument()
  })

  it('starts a brand-new server with the administrator account', async () => {
    vi.stubGlobal(
      'fetch',
      routes({
        '/auth/status': { initialised: false, setup_completed: false },
        '/auth/me': new Response(JSON.stringify({ detail: 'Signed out' }), { status: 401 }),
      }),
    )
    renderWithProviders(<App />, { route: '/' })

    expect(await screen.findByRole('heading', { name: 'Welcome to Sentinel' })).toBeInTheDocument()
  })
})
