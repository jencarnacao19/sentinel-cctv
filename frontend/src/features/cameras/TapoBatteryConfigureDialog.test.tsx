import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TapoBatteryConfigureDialog } from '@/features/cameras/TapoBatteryConfigureDialog'
import { batteryCamera } from '@/test/cameras'
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

describe('TapoBatteryConfigureDialog', () => {
  it('proves a replacement Tapo password before saving it', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      if (url === '/api/v1/cameras/tapo-battery/test') {
        return Promise.resolve(
          json({
            model: 'TC82',
            hardware_version: '1.0',
            firmware_version: '1.2.3',
            battery_percent: 73,
            charging: true,
            charging_source: 'solar',
            read_at: '2026-08-28T20:00:00Z',
          }),
        )
      }
      if (url === `/api/v1/cameras/${batteryCamera.id}` && init?.method === 'PATCH') {
        return Promise.resolve(json(batteryCamera))
      }
      return Promise.reject(new Error(`Unexpected request to ${url}`))
    })
    vi.stubGlobal('fetch', fetchMock)
    const onOpenChange = vi.fn()
    const user = userEvent.setup()

    renderWithProviders(
      <TapoBatteryConfigureDialog
        camera={batteryCamera}
        open
        onOpenChange={onOpenChange}
      />,
    )

    expect(screen.getByLabelText('Connection type')).toHaveValue('standalone')
    expect(screen.queryByLabelText(/H200 username/i)).not.toBeInTheDocument()
    await user.type(screen.getByLabelText('New Tapo account password'), 'correct-app-password')
    expect(screen.getByRole('button', { name: 'Save battery camera' })).toBeDisabled()
    expect(screen.getByText(/not the Camera Account or RTSP password/i)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Test connection' }))
    expect(await screen.findByText('Local connection confirmed')).toBeInTheDocument()
    expect(screen.getByText(/73% battery/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save battery camera' })).toBeEnabled()

    await user.click(screen.getByRole('button', { name: 'Save battery camera' }))
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))

    const saveCall = fetchMock.mock.calls.find(([input, init]) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      return url === `/api/v1/cameras/${batteryCamera.id}` && init?.method === 'PATCH'
    })
    const body = saveCall?.[1]?.body
    expect(JSON.parse(typeof body === 'string' ? body : '{}')).toMatchObject({
      host: batteryCamera.host,
      tapo_child_id: null,
      tapo_password: 'correct-app-password',
    })
  })

  it('displays discovered hub cameras and selects child ID', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      if (url === '/api/v1/cameras/tapo-battery/test') {
        return Promise.resolve(
          json({
            model: 'H200',
            hardware_version: '1.0',
            firmware_version: '1.2.3',
            battery_percent: null,
            charging: null,
            charging_source: null,
            read_at: '2026-08-28T20:00:00Z',
            children: [
              { child_id: 'CHILD_HUB_1', name: 'Garden TC82', model: 'TC82' },
            ],
          }),
        )
      }
      return Promise.reject(new Error(`Unexpected request to ${url}`))
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderWithProviders(
      <TapoBatteryConfigureDialog
        camera={batteryCamera}
        open
        onOpenChange={vi.fn()}
      />,
    )

    await user.selectOptions(screen.getByLabelText('Connection type'), 'hub')
    expect(screen.getByLabelText(/H200 username/i)).toBeInTheDocument()
    await user.type(screen.getByLabelText('New Tapo account password'), 'hub-password')
    await user.click(screen.getByRole('button', { name: 'Test connection' }))

    expect(await screen.findByText(/1 hub camera\(s\) detected/i)).toBeInTheDocument()
    const select = await screen.findByLabelText('Discovered hub cameras')
    expect(select).toBeInTheDocument()

    await user.selectOptions(select, 'CHILD_HUB_1')
    expect(screen.getByLabelText(/H200 child ID/)).toHaveValue('CHILD_HUB_1')
  })

  it('surfaces actionable guidance on 422 authentication failure without proxy errors', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      if (url === '/api/v1/cameras/tapo-battery/test') {
        return Promise.resolve(
          json(
            {
              detail:
                'The Tapo battery camera or local hub rejected the credentials. Enable Third-Party Compatibility in the Tapo app, then use the password you use to sign in to the Tapo app, not the Camera Account or RTSP password.',
            },
            422,
          ),
        )
      }
      return Promise.reject(new Error(`Unexpected request to ${url}`))
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderWithProviders(
      <TapoBatteryConfigureDialog
        camera={batteryCamera}
        open
        onOpenChange={vi.fn()}
      />,
    )

    await user.type(screen.getByLabelText('New Tapo account password'), 'bad-password')
    await user.click(screen.getByRole('button', { name: 'Test connection' }))

    const alert = await screen.findByText(/rejected the credentials/)
    expect(alert).toHaveTextContent('Third-Party Compatibility')
    expect(alert).not.toHaveTextContent(/cloudflare/i)
  })
})
