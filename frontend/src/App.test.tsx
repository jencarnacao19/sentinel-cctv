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
      if (url.includes('/auth/status')) return Promise.resolve(json({ initialised: true }))
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
