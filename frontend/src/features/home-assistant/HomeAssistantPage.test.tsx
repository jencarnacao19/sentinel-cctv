import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { HomeAssistantPage } from '@/features/home-assistant/HomeAssistantPage'
import { renderWithProviders } from '@/test/utils'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function serve(linkExists: boolean) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = input instanceof Request ? input.url : input instanceof URL ? input.href : input
    if (url.endsWith('/panel/link') && init?.method === 'POST') {
      return Promise.resolve(json({ key: 'fresh-key' }))
    }
    if (url.endsWith('/panel/origins')) {
      return Promise.resolve(
        json({ link_exists: linkExists, env_key: false, origins: ['https://ha.example.net'] }),
      )
    }
    if (url.endsWith('/integrations/home-assistant/panel')) {
      return Promise.resolve(json({ link_exists: linkExists, env_key: false, origins: [] }))
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => vi.unstubAllGlobals())

describe('HomeAssistantPage', () => {
  it('creates the panel link from the address Home Assistant opens', async () => {
    serve(false)
    renderWithProviders(<HomeAssistantPage />)

    const address = screen.getByLabelText('1. The address Home Assistant opens')
    await userEvent.clear(address)
    await userEvent.type(address, 'https://cctv.example.net/')
    await userEvent.click(await screen.findByRole('button', { name: 'Create link' }))

    expect(
      await screen.findByText('https://cctv.example.net/embed#viewer_key=fresh-key'),
    ).toBeInTheDocument()
  })

  it('asks before replacing an existing link', async () => {
    const fetchMock = serve(true)
    renderWithProviders(<HomeAssistantPage />)

    await userEvent.click(await screen.findByRole('button', { name: 'Replace the link' }))
    expect(fetchMock).not.toHaveBeenCalledWith(
      expect.stringContaining('/panel/link'),
      expect.anything(),
    )
    await userEvent.click(
      screen.getByRole('button', { name: 'Create a new link and stop the old one' }),
    )
    expect(await screen.findByText(/#viewer_key=fresh-key$/)).toBeInTheDocument()
  })

  it('warns that an http or home-network address will not work everywhere', async () => {
    serve(false)
    renderWithProviders(<HomeAssistantPage />)
    const address = screen.getByLabelText('1. The address Home Assistant opens')

    await userEvent.clear(address)
    await userEvent.type(address, 'http://192.168.1.27:8090')
    expect(screen.getByText(/cannot show an http:\/\/ page/)).toBeInTheDocument()

    await userEvent.clear(address)
    await userEvent.type(address, 'https://192.168.1.27')
    expect(screen.getByText('This address only works on your home network.')).toBeInTheDocument()

    await userEvent.clear(address)
    await userEvent.type(address, 'https://cctv.example.net')
    expect(screen.queryByText(/home network|http:\/\/ page/)).not.toBeInTheDocument()
  })

  it('saves the Home Assistant addresses one per line', async () => {
    const fetchMock = serve(false)
    renderWithProviders(<HomeAssistantPage />)

    await userEvent.type(
      screen.getByLabelText('2. Your Home Assistant addresses'),
      'https://ha.example.net{enter}http://192.168.1.10:8123',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Save addresses' }))

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/v1/integrations/home-assistant/panel/origins',
        expect.objectContaining({
          method: 'PUT',
          body: JSON.stringify({
            origins: ['https://ha.example.net', 'http://192.168.1.10:8123'],
          }),
        }),
      ),
    )
  })
})
