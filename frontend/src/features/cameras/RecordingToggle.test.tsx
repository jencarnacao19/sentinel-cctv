import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { RecordingToggle } from '@/features/cameras/RecordingToggle'
import { makeCamera } from '@/test/cameras'
import { sentJson } from '@/test/requests'
import { renderWithProviders } from '@/test/utils'

const camera = makeCamera()

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn())
})

afterEach(() => {
  vi.unstubAllGlobals()
})

async function stopRecording(name = camera.name) {
  await userEvent.click(screen.getByRole('switch', { name: /^Recording/ }))
  await screen.findByText(`Stop recording ${name}?`)
  await userEvent.click(screen.getByRole('button', { name: 'Stop recording' }))
}

describe('confirmation', () => {
  it('does not stop recording without an explicit confirmation', async () => {
    renderWithProviders(<RecordingToggle camera={camera} />)
    await userEvent.click(screen.getByRole('switch', { name: /^Recording/ }))
    expect(await screen.findByText(`Stop recording ${camera.name}?`)).toBeInTheDocument()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('says what is lost before it is lost', async () => {
    renderWithProviders(<RecordingToggle camera={camera} />)
    await userEvent.click(screen.getByRole('switch', { name: /^Recording/ }))
    expect(await screen.findByText(/cannot be recovered afterwards/i)).toBeInTheDocument()
    expect(screen.getByText(/only this camera is affected/i)).toBeInTheDocument()
  })

  it('leaves the camera recording when the confirmation is dismissed', async () => {
    renderWithProviders(<RecordingToggle camera={camera} />)
    await userEvent.click(screen.getByRole('switch', { name: /^Recording/ }))
    await userEvent.click(await screen.findByRole('button', { name: 'Keep recording' }))
    await waitFor(() =>
      expect(screen.queryByText(`Stop recording ${camera.name}?`)).not.toBeInTheDocument(),
    )
    expect(fetch).not.toHaveBeenCalled()
    expect(screen.getByRole('switch', { name: /^Recording/ })).toBeChecked()
  })

  it('sends the confirmation flag once confirmed', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse({
        camera_id: camera.id,
        recording_enabled: false,
        state: 'off',
        message: 'Recording is off for this camera.',
      }),
    )
    renderWithProviders(<RecordingToggle camera={camera} />)
    await stopRecording()
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    const [, init] = vi.mocked(fetch).mock.calls[0]
    expect(init?.method).toBe('PUT')
    expect(sentJson(init)).toEqual({ enabled: false, confirm: true })
  })

  it('needs no confirmation to start recording', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse({
        camera_id: camera.id,
        recording_enabled: true,
        state: 'recording',
        message: 'This camera is recording.',
      }),
    )
    const off = makeCamera({ recording_enabled: false, recording_state: 'off' })
    renderWithProviders(<RecordingToggle camera={off} />)
    await userEvent.click(screen.getByRole('switch', { name: /^Recording/ }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    const [, init] = vi.mocked(fetch).mock.calls[0]
    expect(sentJson(init)).toEqual({ enabled: true, confirm: false })
  })
})

describe('races', () => {
  it('holds the switch disabled while a change is in flight', async () => {
    let resolve: (value: Response) => void = () => {}
    vi.mocked(fetch).mockImplementation(
      () => new Promise<Response>((done) => (resolve = done)),
    )
    renderWithProviders(<RecordingToggle camera={camera} />)
    await stopRecording()

    const control = await screen.findByRole('switch', { name: /^Recording/ })
    await waitFor(() => expect(control).toBeDisabled())
    expect(screen.getByText('Stopping…')).toBeInTheDocument()

    // A second click while the first is in flight must not reach the server.
    await userEvent.click(control)
    expect(fetch).toHaveBeenCalledTimes(1)

    resolve(
      jsonResponse({
        camera_id: camera.id,
        recording_enabled: false,
        state: 'off',
        message: 'Recording is off for this camera.',
      }),
    )
    await waitFor(() => expect(control).toBeEnabled())
  })
})

describe('honest reporting', () => {
  it('rolls back to the previous state when the request fails', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse({ detail: 'Administrator privileges are required.' }, 403),
    )
    renderWithProviders(<RecordingToggle camera={camera} />)
    await stopRecording()
    // The server's own wording wins when it sent one; the canned text is only
    // for a bare status with no body.
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Administrator privileges are required.',
    )
    // The switch still reflects the camera it was given, not the failed request.
    expect(screen.getByRole('switch', { name: /^Recording/ })).toBeChecked()
  })

  it('reports a recorder that would not start', () => {
    const failed = makeCamera({ recording_enabled: true, recording_state: 'failed' })
    renderWithProviders(<RecordingToggle camera={failed} />)
    expect(screen.getByText('Failed to start')).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: /^Recording/ })).toBeChecked()
  })

  it('distinguishes privacy mode from a fault', () => {
    const paused = makeCamera({ recording_state: 'paused' })
    renderWithProviders(<RecordingToggle camera={paused} />)
    expect(screen.getByText('Paused by privacy mode')).toBeInTheDocument()
  })

  it('never shows a proxy error page as a camera fault', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        '<html><body><h1>Error 520</h1><p>The origin web server returned an invalid or ' +
          'incomplete response to Cloudflare.</p></body></html>',
        { status: 520, headers: { 'content-type': 'text/html' } },
      ),
    )
    renderWithProviders(<RecordingToggle camera={camera} />)
    await stopRecording()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/failed before it could answer/i)
    expect(alert).not.toHaveTextContent(/cloudflare/i)
    expect(alert).not.toHaveTextContent(/origin web server/i)
  })
})

describe('accessibility', () => {
  it('labels the switch and describes its state', () => {
    renderWithProviders(<RecordingToggle camera={camera} />)
    const control = screen.getByRole('switch', { name: /^Recording/ })
    // Unique per camera, so a page of switches stays distinguishable.
    expect(control).toHaveAccessibleName(`Recording — ${camera.name}`)
    expect(control).toHaveAccessibleDescription('On, recorder running')
  })

  it('is operable from the keyboard', async () => {
    renderWithProviders(<RecordingToggle camera={camera} />)
    await userEvent.tab()
    expect(screen.getByRole('switch', { name: /^Recording/ })).toHaveFocus()
    await userEvent.keyboard(' ')
    expect(await screen.findByText(`Stop recording ${camera.name}?`)).toBeInTheDocument()
  })

  it('cannot be operated at all while privacy mode holds it', async () => {
    renderWithProviders(
      <RecordingToggle camera={camera} disabled disabledReason="Paused by privacy mode" />,
    )
    const control = screen.getByRole('switch', { name: /^Recording/ })
    expect(control).toBeDisabled()
    await userEvent.click(control)
    expect(screen.queryByText(`Stop recording ${camera.name}?`)).not.toBeInTheDocument()
  })
})
