import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { CamerasPage } from '@/features/cameras/CamerasPage'
import { batteryCamera, makeCamera, nvrCamera, tapoCamera, unknownCamera } from '@/test/cameras'
import { requestedUrl, sentJson } from '@/test/requests'
import { renderWithProviders } from '@/test/utils'
import type { Camera } from '@/types/api'

/**
 * The page, not the card.
 *
 * These exist because a bug lived exactly here and nowhere else: the recording
 * mutation wrote to the `['cameras']` cache key while `useCameras` reads
 * `['cameras', { enabledOnly }]`. Every component test passed, the server did
 * the right thing, and the row on screen never changed. A test that renders a
 * card with a fixed prop cannot see that.
 */

const CAMERAS: Camera[] = [tapoCamera, nvrCamera, unknownCamera, batteryCamera]

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

let cameras: Camera[]

beforeEach(() => {
  cameras = CAMERAS.map((camera) => ({ ...camera }))
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestedUrl(input)
      if (url.includes('/system/privacy')) {
        return Promise.resolve(json({ enabled: false, reason: null, message: null, reasons: {} }))
      }
      if (url.includes('/recording') && init?.method === 'PUT') {
        const body = sentJson(init) as { enabled: boolean }
        const id = /cameras\/([^/]+)\/recording/.exec(url)?.[1] ?? ''
        // Mirror the server: persist, then report the observed state.
        cameras = cameras.map((camera) =>
          camera.id === id
            ? {
                ...camera,
                recording_enabled: body.enabled,
                recording_state: body.enabled ? 'recording' : 'off',
              }
            : camera,
        )
        return Promise.resolve(
          json({
            camera_id: id,
            recording_enabled: body.enabled,
            state: body.enabled ? 'recording' : 'off',
            message: body.enabled ? 'This camera is recording.' : 'Recording is off.',
          }),
        )
      }
      if (url.includes('/cameras')) return Promise.resolve(json(cameras))
      return Promise.resolve(json({}))
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the camera list', () => {
  it('summarises how many cameras are recording', async () => {
    renderWithProviders(<CamerasPage />)
    // Three RTSP cameras, all recording. The battery camera is excluded from
    // both sides: it has no continuous recorder, so counting it would make the
    // total one a healthy installation could never reach.
    expect(await screen.findByText('3 of 3 recording.')).toBeInTheDocument()
  })

  it('shows every camera once', async () => {
    renderWithProviders(<CamerasPage />)
    for (const camera of CAMERAS) {
      expect(await screen.findByRole('heading', { name: camera.name })).toBeInTheDocument()
    }
  })

  it('offers Tapo controls only where they apply', async () => {
    renderWithProviders(<CamerasPage />)
    await screen.findByRole('heading', { name: nvrCamera.name })

    await userEvent.click(
      screen.getByRole('button', { name: `More actions for ${nvrCamera.name}` }),
    )
    let menu = await screen.findByRole('menu')
    expect(within(menu).queryByRole('menuitem', { name: /tapo/i })).not.toBeInTheDocument()
    await userEvent.keyboard('{Escape}')

    await userEvent.click(
      screen.getByRole('button', { name: `More actions for ${tapoCamera.name}` }),
    )
    menu = await screen.findByRole('menu')
    expect(within(menu).getByRole('menuitem', { name: /tapo lan controls/i })).toBeInTheDocument()
  })
})

describe('turning a camera off from the list', () => {
  it("updates that row, and only that row", async () => {
    renderWithProviders(<CamerasPage />)
    await screen.findByText('3 of 3 recording.')

    const switches = screen.getAllByRole('switch', { name: /^Recording/ })
    expect(switches).toHaveLength(3)
    await userEvent.click(switches[0])
    await userEvent.click(await screen.findByRole('button', { name: 'Stop recording' }))

    // The regression: the server was updated but the row was not.
    await waitFor(() => expect(screen.getByText('2 of 3 recording.')).toBeInTheDocument())
    const after = screen.getAllByRole('switch', { name: /^Recording/ })
    expect(after[0]).not.toBeChecked()
    expect(after[1]).toBeChecked()
    expect(after[2]).toBeChecked()
    expect(screen.getByText('Off')).toBeInTheDocument()
  })

  it('turns it back on again', async () => {
    cameras = cameras.map((camera) =>
      camera.id === tapoCamera.id
        ? { ...camera, recording_enabled: false, recording_state: 'off' }
        : camera,
    )
    renderWithProviders(<CamerasPage />)
    await screen.findByText('2 of 3 recording.')

    const control = screen.getAllByRole('switch', { name: /^Recording/ })[0]
    expect(control).not.toBeChecked()
    await userEvent.click(control)

    await waitFor(() => expect(screen.getByText('3 of 3 recording.')).toBeInTheDocument())
    expect(screen.getAllByRole('switch', { name: /^Recording/ })[0]).toBeChecked()
  })
})

describe('cameras that are not recording but should be', () => {
  it('says so once, at the top, rather than only per row', async () => {
    cameras = cameras.map((camera) =>
      camera.kind === 'rtsp' && camera.id !== nvrCamera.id
        ? { ...camera, recording_enabled: true, recording_state: 'failed' }
        : camera,
    )
    renderWithProviders(<CamerasPage />)
    expect(await screen.findByText('2 cameras not recording')).toBeInTheDocument()
    expect(screen.getByText(/set to record but no recorder is running/i)).toBeInTheDocument()
  })

  it('says nothing when every camera is fine', async () => {
    renderWithProviders(<CamerasPage />)
    await screen.findByText('3 of 3 recording.')
    expect(screen.queryByText(/not recording/i)).not.toBeInTheDocument()
  })
})

describe('privacy mode', () => {
  it('explains itself once and disables the camera controls', async () => {
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const url = requestedUrl(input)
      if (url.includes('/system/privacy')) {
        return Promise.resolve(
          json({
            enabled: true,
            reason: 'guests',
            message: 'Cameras are paused for privacy: Guests are present.',
            reasons: {},
          }),
        )
      }
      if (url.includes('/cameras')) return Promise.resolve(json(cameras))
      return Promise.resolve(json({}))
    })

    renderWithProviders(<CamerasPage />)
    expect(await screen.findByText('Privacy mode is on')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /discover cameras/i })).toBeDisabled()
    for (const control of screen.getAllByRole('switch', { name: /^Recording/ })) {
      expect(control).toBeDisabled()
    }
    // A paused camera is not a failing camera.
    expect(screen.queryByText(/not recording$/i)).not.toBeInTheDocument()
  })
})

describe('an empty installation', () => {
  it('invites the operator to add a camera', async () => {
    cameras = []
    renderWithProviders(<CamerasPage />)
    expect(await screen.findByText('No cameras yet')).toBeInTheDocument()
  })
})

describe('a camera that has never been identified', () => {
  it('is not silently treated as a Tapo camera', async () => {
    cameras = [makeCamera({ ...unknownCamera })]
    renderWithProviders(<CamerasPage />)
    await screen.findByRole('heading', { name: unknownCamera.name })
    expect(screen.getByText(/manufacturer unknown/i)).toBeInTheDocument()
  })
})
