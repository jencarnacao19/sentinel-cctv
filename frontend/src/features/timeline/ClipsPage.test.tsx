import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ClipsPage } from '@/features/timeline/ClipsPage'
import { renderWithProviders } from '@/test/utils'

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ClipsPage', () => {
  it('shows a permanent clip, lets it be downloaded and can make it temporary', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      if (url.endsWith('/cameras')) {
        return Promise.resolve(json([{ id: 'front-door', name: 'Front door' }]))
      }
      if (url.endsWith('/exports') && (!init?.method || init.method === 'GET')) {
        return Promise.resolve(
          json({
            exports: [
              {
                id: 'clip-1',
                camera_id: 'front-door',
                start_at: '2026-08-23T14:00:00Z',
                end_at: '2026-08-23T14:01:00Z',
                status: 'complete',
                progress: 1,
                error: null,
                size_bytes: 1048576,
                protected: true,
                created_at: '2026-08-23T14:01:01Z',
                started_at: '2026-08-23T14:01:02Z',
                finished_at: '2026-08-23T14:01:05Z',
                expires_at: null,
                download_url: '/api/v1/exports/clip-1/download',
                note: 'Keyframe-accurate export.',
              },
            ],
          }),
        )
      }
      if (url.endsWith('/exports/clip-1') && init?.method === 'PATCH') {
        return Promise.resolve(
          json({
            id: 'clip-1',
            camera_id: 'front-door',
            start_at: '2026-08-23T14:00:00Z',
            end_at: '2026-08-23T14:01:00Z',
            status: 'complete',
            progress: 1,
            size_bytes: 1048576,
            error: null,
            created_at: '2026-08-23T14:01:01Z',
            started_at: '2026-08-23T14:01:02Z',
            finished_at: '2026-08-23T14:01:05Z',
            expires_at: '2026-08-24T14:01:05Z',
            protected: false,
            download_url: '/api/v1/exports/clip-1/download',
            note: 'Keyframe-accurate export.',
          }),
        )
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderWithProviders(<ClipsPage />)

    expect(await screen.findByText('Front door', { exact: false })).toBeInTheDocument()
    expect(screen.getByText('Saved permanently')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Download' })).toHaveAttribute(
      'href',
      '/api/v1/exports/clip-1/download',
    )

    await user.click(screen.getByRole('button', { name: 'Permanent' }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/v1/exports/clip-1',
        expect.objectContaining({
          method: 'PATCH',
          body: JSON.stringify({ protected: false }),
        }),
      )
    })
  })
})
