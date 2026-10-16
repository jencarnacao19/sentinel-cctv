import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AddCameraDialog } from '@/features/cameras/AddCameraDialog'
import { renderWithProviders } from '@/test/utils'

type Handler = (url: string, init?: RequestInit) => Response

function stubRoutes(routes: Record<string, Handler>) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const path = url.split('?')[0] ?? url
    const handler = routes[path]
    if (!handler) return Promise.reject(new Error(`Unexpected request to ${url}`))
    return Promise.resolve(handler(url, init))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

const OK_TEST = {
  ok: true,
  error: null,
  codec: 'h264',
  resolution: '2560x1440',
  width: 2560,
  height: 1440,
  fps: 25,
  audio_codec: 'aac',
  bitrate_kbps: 4200,
  can_stream_copy: true,
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('AddCameraDialog', () => {
  it('disables saving until a name and a URL are present', async () => {
    const user = userEvent.setup()
    renderWithProviders(<AddCameraDialog open onOpenChange={vi.fn()} />)

    const save = screen.getByRole('button', { name: 'Add camera' })
    expect(save).toBeDisabled()

    await user.type(screen.getByLabelText('Name'), 'Front door')
    expect(save).toBeDisabled()

    await user.type(
      screen.getByLabelText('Main stream (recording)'),
      'rtsp://192.168.1.50:554/stream1',
    )
    expect(save).toBeEnabled()
  })

  it('shows the probed stream details after a successful test', async () => {
    stubRoutes({ '/api/v1/cameras/test-stream': () => json(OK_TEST) })
    const user = userEvent.setup()
    renderWithProviders(<AddCameraDialog open onOpenChange={vi.fn()} />)

    await user.type(
      screen.getByLabelText('Main stream (recording)'),
      'rtsp://192.168.1.50:554/stream1',
    )
    await user.click(screen.getByRole('button', { name: /test connection/i }))

    expect(await screen.findByText('Connection OK')).toBeInTheDocument()
    expect(screen.getByText('2560x1440')).toBeInTheDocument()
    expect(screen.getByText('25 fps')).toBeInTheDocument()
    expect(screen.getByText('4.2 Mbps')).toBeInTheDocument()
    expect(screen.getByText('Records without re-encoding')).toBeInTheDocument()
  })

  it('tests and saves a Tapo battery camera as a local event source', async () => {
    const fetchMock = stubRoutes({
      '/api/v1/cameras/tapo-battery/test': () =>
        json({
          model: 'TC82',
          hardware_version: '1.0',
          firmware_version: '1.2.3',
          battery_percent: 84,
          charging: true,
          charging_source: 'solar',
          read_at: '2026-08-27T12:00:00Z',
        }),
      '/api/v1/cameras': () => json({ id: 'tc82', name: 'Solar garden' }, 201),
    })
    const onOpenChange = vi.fn()
    const user = userEvent.setup()
    renderWithProviders(<AddCameraDialog open onOpenChange={onOpenChange} />)

    await user.click(screen.getByRole('radio', { name: /Tapo battery/ }))
    await user.selectOptions(screen.getByLabelText('Connection type'), 'hub')
    await user.type(screen.getByLabelText('Name'), 'Solar garden')
    await user.type(screen.getByLabelText('Camera or H200 local address'), '192.168.1.60')
    await user.type(screen.getByLabelText(/Hub child ID/), 'child-82')
    expect(screen.getByLabelText(/Tapo username/i)).toBeInTheDocument()
    await user.type(screen.getByLabelText('Tapo account password'), 'local-secret')
    expect(screen.getByRole('button', { name: 'Add camera' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: /test connection/i }))

    expect(await screen.findByText('Local connection OK')).toBeInTheDocument()
    expect(screen.getByText('84%')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add camera' })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: 'Add camera' }))
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))

    const createCall = fetchMock.mock.calls.find(([input]) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      return url.endsWith('/api/v1/cameras')
    })
    const body = createCall?.[1]?.body
    expect(JSON.parse(typeof body === 'string' ? body : '{}')).toMatchObject({
      name: 'Solar garden',
      kind: 'tapo_battery',
      host: '192.168.1.60',
      tapo_child_id: 'child-82',
      tapo_password: 'local-secret',
      recording_mode: 'events',
      motion_enabled: false,
    })
    expect(JSON.parse(typeof body === 'string' ? body : '{}')).not.toHaveProperty('username')
  })

  it('warns when the codec would force re-encoding', async () => {
    stubRoutes({
      '/api/v1/cameras/test-stream': () =>
        json({ ...OK_TEST, codec: 'mpeg4', can_stream_copy: false }),
    })
    const user = userEvent.setup()
    renderWithProviders(<AddCameraDialog open onOpenChange={vi.fn()} />)

    await user.type(screen.getByLabelText('Main stream (recording)'), 'rtsp://10.0.0.5/s1')
    await user.click(screen.getByRole('button', { name: /test connection/i }))

    expect(await screen.findByText(/cannot be copied directly/i)).toBeInTheDocument()
  })

  it('reports a failed probe with the server message', async () => {
    stubRoutes({
      '/api/v1/cameras/test-stream': () =>
        json({ ...OK_TEST, ok: false, error: 'The camera rejected the username or password.' }),
    })
    const user = userEvent.setup()
    renderWithProviders(<AddCameraDialog open onOpenChange={vi.fn()} />)

    await user.type(screen.getByLabelText('Main stream (recording)'), 'rtsp://10.0.0.5/s1')
    await user.click(screen.getByRole('button', { name: /test connection/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The camera rejected the username or password.',
    )
  })

  it('sends the typed credentials and closes on success', async () => {
    const fetchMock = stubRoutes({
      '/api/v1/cameras': () => json({ id: 'abc', name: 'Front door' }, 201),
    })
    const onOpenChange = vi.fn()
    const user = userEvent.setup()
    renderWithProviders(<AddCameraDialog open onOpenChange={onOpenChange} />)

    await user.type(screen.getByLabelText('Name'), 'Front door')
    await user.type(
      screen.getByLabelText('Main stream (recording)'),
      'rtsp://192.168.1.50:554/stream1',
    )
    await user.type(screen.getByLabelText('Username'), 'admin')
    await user.type(screen.getByLabelText('Password'), 'hunter2')
    await user.click(screen.getByRole('button', { name: 'Add camera' }))

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))

    const body = fetchMock.mock.calls[0]?.[1]?.body
    expect(JSON.parse(typeof body === 'string' ? body : '{}')).toEqual({
      name: 'Front door',
      kind: 'rtsp',
      main_stream_url: 'rtsp://192.168.1.50:554/stream1',
      sub_stream_url: null,
      username: 'admin',
      password: 'hunter2',
    })
  })

  it('surfaces a duplicate-name conflict instead of closing', async () => {
    stubRoutes({
      '/api/v1/cameras': () =>
        json({ detail: "A camera named 'Front door' already exists." }, 409),
    })
    const onOpenChange = vi.fn()
    const user = userEvent.setup()
    renderWithProviders(<AddCameraDialog open onOpenChange={onOpenChange} />)

    await user.type(screen.getByLabelText('Name'), 'Front door')
    await user.type(screen.getByLabelText('Main stream (recording)'), 'rtsp://10.0.0.5/s1')
    await user.click(screen.getByRole('button', { name: 'Add camera' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('already exists')
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
  })

  it('displays discovered hub cameras in dropdown and auto-populates child ID and name', async () => {
    stubRoutes({
      '/api/v1/cameras/tapo-battery/test': () =>
        json({
          model: 'H200',
          hardware_version: '1.0',
          firmware_version: '1.2.3',
          battery_percent: null,
          charging: null,
          charging_source: null,
          read_at: '2026-08-27T12:00:00Z',
          children: [
            { child_id: 'CHILD_01', name: 'Backyard TC82', model: 'TC82' },
            { child_id: 'CHILD_02', name: 'Driveway TC82', model: 'TC82' },
          ],
        }),
    })
    const user = userEvent.setup()
    renderWithProviders(<AddCameraDialog open onOpenChange={vi.fn()} />)

    await user.click(screen.getByRole('radio', { name: /Tapo battery/ }))
    await user.selectOptions(screen.getByLabelText('Connection type'), 'hub')
    await user.type(screen.getByLabelText('Camera or H200 local address'), '192.168.1.60')
    await user.type(screen.getByLabelText('Tapo account password'), 'hub-password')
    await user.click(screen.getByRole('button', { name: /test connection/i }))

    expect(await screen.findByText('Local connection OK')).toBeInTheDocument()
    expect(screen.getByText(/2 camera\(s\) detected/i)).toBeInTheDocument()

    const select = await screen.findByLabelText('Discovered hub cameras')
    expect(select).toBeInTheDocument()
    await user.selectOptions(select, 'CHILD_01')

    expect(screen.getByLabelText(/Hub child ID/)).toHaveValue('CHILD_01')
    expect(screen.getByLabelText('Name')).toHaveValue('Backyard TC82')
  })

  it('surfaces actionable guidance on 422 authentication failure without leaking proxy error', async () => {
    stubRoutes({
      '/api/v1/cameras/tapo-battery/test': () =>
        json(
          {
            detail:
              'The Tapo battery camera or local hub rejected the credentials. Enable Third-Party Compatibility in the Tapo app, then use the password you use to sign in to the Tapo app, not the Camera Account or RTSP password.',
          },
          422,
        ),
    })
    const user = userEvent.setup()
    renderWithProviders(<AddCameraDialog open onOpenChange={vi.fn()} />)

    await user.click(screen.getByRole('radio', { name: /Tapo battery/ }))
    expect(screen.getByLabelText('Connection type')).toHaveValue('standalone')
    expect(screen.queryByLabelText(/Tapo username/i)).not.toBeInTheDocument()
    await user.type(screen.getByLabelText('Camera or H200 local address'), '192.168.1.60')
    await user.type(screen.getByLabelText('Tapo account password'), 'wrong-password')
    await user.click(screen.getByRole('button', { name: /test connection/i }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('rejected the credentials')
    expect(alert).toHaveTextContent('Third-Party Compatibility')
    expect(alert).toHaveTextContent('not the Camera Account or RTSP password')
    expect(alert).not.toHaveTextContent(/cloudflare/i)
  })
})
