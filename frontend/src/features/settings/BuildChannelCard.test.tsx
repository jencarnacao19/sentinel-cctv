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

function serve(helperSeenAt: string | null) {
  const state = { current: 'stable', requested: null, last_result: null, helper_seen_at: helperSeenAt }
  const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
    Promise.resolve(
      init?.method === 'POST' ? json({ ...state, requested: 'dev' }) : json(state, 200),
    ),
  )
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

  it('reports a missing helper instead of offering a switch that would wait for ever', async () => {
    serve(null)
    renderWithProviders(<BuildChannelCard />)

    expect(await screen.findByText(/helper that applies a switch is not running/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Switch to the dev build' })).toBeDisabled()
  })
})
