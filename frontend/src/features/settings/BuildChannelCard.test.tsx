import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { BuildChannelCard } from '@/features/settings/BuildChannelCard'
import { renderWithProviders } from '@/test/utils'

function json(body: unknown, status = 202): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function serve(helperSeenAt: string | null, lastResult: unknown = null) {
  const state = {
    current: 'stable',
    requested: null,
    last_result: lastResult,
    helper_seen_at: helperSeenAt,
  }
  const fetchMock = vi.fn((input: string, init?: RequestInit) => {
    if (init?.method !== 'POST') return Promise.resolve(json(state, 200))
    const update = input.endsWith('/update')
    return Promise.resolve(json({ ...state, requested: update ? 'stable' : 'dev' }))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => vi.unstubAllGlobals())

describe('BuildChannelCard', () => {
  it('switches only after the build name is typed in capitals', async () => {
    const fetchMock = serve(new Date().toISOString())
    renderWithProviders(<BuildChannelCard />)

    await userEvent.click(await screen.findByRole('button', { name: 'Switch to the dev build' }))
    const confirm = screen.getByRole('button', { name: 'Switch to dev' })
    expect(confirm).toBeDisabled()

    const field = screen.getByLabelText('Type DEV to confirm')
    await userEvent.type(field, 'dev')
    expect(confirm).toBeDisabled()
    await userEvent.clear(field)
    await userEvent.type(field, 'DEV')
    expect(confirm).toBeEnabled()

    await userEvent.click(confirm)
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/v1/system/channel',
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({ channel: 'dev', confirm: 'DEV' }),
        }),
      ),
    )
    expect(await screen.findByText(/Switching to the dev build/)).toBeInTheDocument()
  })

  it('updates the running build in one click and says what it found', async () => {
    const fetchMock = serve(new Date().toISOString())
    renderWithProviders(<BuildChannelCard />)

    await userEvent.click(await screen.findByRole('button', { name: 'Update' }))
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/v1/system/channel/update',
        expect.objectContaining({ method: 'POST' }),
      ),
    )
    expect(await screen.findByText(/Looking for a newer stable build/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Switch to the dev build' })).toBeDisabled()
  })

  it('shows a recent result, and not one from long ago', async () => {
    const message = 'Sentinel already runs the newest stable build.'
    serve(new Date().toISOString(), { state: 'done', message, finished_at: new Date().toISOString() })
    const { unmount } = renderWithProviders(<BuildChannelCard />)
    expect(await screen.findByText(message)).toBeInTheDocument()
    unmount()

    serve(new Date().toISOString(), { state: 'done', message, finished_at: '2026-01-01T00:00:00Z' })
    renderWithProviders(<BuildChannelCard />)
    await screen.findByRole('button', { name: 'Update' })
    expect(screen.queryByText(message)).not.toBeInTheDocument()
  })

  it('reports a missing helper instead of offering buttons that would wait for ever', async () => {
    serve(null)
    renderWithProviders(<BuildChannelCard />)

    expect(
      await screen.findByText(/helper that applies an update or switch is not running/),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Switch to the dev build' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Update' })).toBeDisabled()
  })
})
