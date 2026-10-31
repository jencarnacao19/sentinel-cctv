import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { WallPage } from '@/features/wall/WallPage'
import { makeCamera } from '@/test/cameras'
import { renderWithProviders } from '@/test/utils'

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } })
}

afterEach(() => vi.unstubAllGlobals())

describe('WallPage', () => {
  it('saves the order, the hidden cameras and the default grid together', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = input instanceof Request ? input.url : input instanceof URL ? input.href : input
      if (url.includes('/viewer/wall')) {
        return Promise.resolve(json({ columns: 'auto', order: [], hidden: [] }))
      }
      if (url.endsWith('/wall') && init?.method === 'PUT') {
        return Promise.resolve(json(JSON.parse(init.body as string)))
      }
      return Promise.resolve(
        json([
          makeCamera({ id: 'porch', name: 'Porch' }),
          makeCamera({ id: 'patio', name: 'Patio' }),
        ]),
      )
    })
    vi.stubGlobal('fetch', fetchMock)
    renderWithProviders(<WallPage />)

    await userEvent.click(await screen.findByRole('button', { name: 'Move Patio up' }))
    await userEvent.click(screen.getByRole('checkbox', { name: 'Porch' }))
    await userEvent.selectOptions(screen.getByLabelText('Cameras per row'), '2x2')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/v1/wall',
        expect.objectContaining({
          method: 'PUT',
          body: JSON.stringify({ columns: '2x2', order: ['patio', 'porch'], hidden: ['porch'] }),
        }),
      ),
    )
  })
})
