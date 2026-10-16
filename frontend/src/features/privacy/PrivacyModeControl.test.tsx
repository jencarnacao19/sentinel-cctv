import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { VideoPlayer } from '@/features/live/VideoPlayer'
import { PrivacyModeControl } from '@/features/privacy/PrivacyModeControl'
import { renderWithProviders } from '@/test/utils'

const OFF = {
  enabled: false,
  reason: null,
  reason_label: null,
  message: null,
  changed_at: null,
  changed_by: null,
  reasons: {
    privacy: 'Privacy requested',
    guests: 'Guests are present',
    private_event: 'Private event in progress',
    maintenance: 'Camera maintenance in progress',
    temporary: 'Temporarily disabled by an administrator',
  },
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('PrivacyModeControl', () => {
  it('sends the selected predefined reason and exposes the active state', async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'PUT') {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              ...OFF,
              enabled: true,
              reason: 'guests',
              reason_label: 'Guests are present',
              message: 'Cameras are paused for privacy: Guests are present.',
            }),
            { headers: { 'content-type': 'application/json' } },
          ),
        )
      }
      return Promise.resolve(
        new Response(JSON.stringify(OFF), {
          headers: { 'content-type': 'application/json' },
        }),
      )
    })
    vi.stubGlobal('fetch', fetchMock)

    renderWithProviders(<PrivacyModeControl />)
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Privacy mode' }))
    await user.click(screen.getByRole('radio', { name: /Guests are present/ }))
    await user.click(screen.getByRole('button', { name: 'Pause all cameras' }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/v1/system/privacy',
        expect.objectContaining({
          method: 'PUT',
          body: JSON.stringify({ enabled: true, reason: 'guests' }),
        }),
      )
    })
    expect(await screen.findByRole('button', { name: 'Cameras paused' })).toBeInTheDocument()
  })

  it('renders the reason without constructing a live player', () => {
    const websocket = vi.fn()
    vi.stubGlobal('WebSocket', websocket)

    renderWithProviders(
      <VideoPlayer
        cameraId="front-door"
        cameraName="Front door"
        privacyMessage="Cameras are paused for privacy: Guests are present."
      />,
    )

    expect(screen.getByText('Paused for privacy')).toBeInTheDocument()
    expect(screen.getByText(/Guests are present/)).toBeInTheDocument()
    expect(websocket).not.toHaveBeenCalled()
  })
})
