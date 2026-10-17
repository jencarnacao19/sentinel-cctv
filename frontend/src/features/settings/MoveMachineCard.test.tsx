import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { MoveMachineCard } from '@/features/settings/MoveMachineCard'
import { renderWithProviders } from '@/test/utils'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('MoveMachineCard', () => {
  it('only exports passwords once two matching passphrases are long enough', async () => {
    const user = userEvent.setup()
    renderWithProviders(<MoveMachineCard />)
    const download = screen.getByRole('button', { name: 'Download configuration' })
    expect(download).toBeEnabled()

    await user.click(screen.getByLabelText('Include camera passwords'))
    expect(download).toBeDisabled()

    await user.type(screen.getByLabelText('Passphrase'), 'long enough')
    expect(screen.getByText('The two passphrases are different.')).toBeInTheDocument()
    await user.type(screen.getByLabelText('Passphrase again'), 'long enough')
    expect(download).toBeEnabled()
  })

  it('sends the chosen file and passphrase and reports what was added and skipped', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        json({
          cameras_added: ['Front door'],
          cameras_skipped: [{ name: 'Garage', reason: 'A camera with this name exists.' }],
          settings_applied: ['timezone'],
          settings_skipped: [],
          warnings: [],
          passwords_restored: true,
        }),
      ),
    )
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderWithProviders(<MoveMachineCard />)

    await user.type(
      screen.getByLabelText('Passphrase (only if the file has passwords)'),
      'correct horse',
    )
    const file = new File(['format: sentinel-config\n'], 'sentinel-config.yaml', {
      type: 'application/yaml',
    })
    await user.upload(screen.getByLabelText('Configuration file to import'), file)

    expect(await screen.findByText('1 camera added')).toBeInTheDocument()
    expect(screen.getByText('Not added, Garage: A camera with this name exists.')).toBeInTheDocument()

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toContain('/system/config/import')
    const form = init.body as FormData
    expect(form.get('passphrase')).toBe('correct horse')
    expect((form.get('file') as File).name).toBe('sentinel-config.yaml')
  })

  it('shows why a file was refused', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(json({ detail: 'That passphrase does not open this file.' }, 422)),
      ),
    )
    const user = userEvent.setup()
    renderWithProviders(<MoveMachineCard />)

    await user.upload(
      screen.getByLabelText('Configuration file to import'),
      new File(['x'], 'x.yaml', { type: 'application/yaml' }),
    )
    expect(await screen.findByText('That passphrase does not open this file.')).toBeInTheDocument()
  })
})
