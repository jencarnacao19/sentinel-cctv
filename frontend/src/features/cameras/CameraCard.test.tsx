import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { CameraCard } from '@/features/cameras/CameraCard'
import { batteryCamera, makeCamera, nvrCamera, tapoCamera, unknownCamera } from '@/test/cameras'
import { renderWithProviders } from '@/test/utils'

async function openOverflow(cameraName: string) {
  const trigger = screen.getByRole('button', { name: `More actions for ${cameraName}` })
  await userEvent.click(trigger)
  return screen.findByRole('menu')
}

describe('brand-specific actions', () => {
  it('offers Tapo LAN controls for a camera whose metadata says TP-Link', async () => {
    renderWithProviders(<CameraCard camera={tapoCamera} />)
    const menu = await openOverflow(tapoCamera.name)
    expect(within(menu).getByRole('menuitem', { name: /tapo lan controls/i })).toBeInTheDocument()
  })

  it('offers nothing Tapo on an NVR channel', async () => {
    renderWithProviders(<CameraCard camera={nvrCamera} />)
    const menu = await openOverflow(nvrCamera.name)
    expect(within(menu).queryByRole('menuitem', { name: /tapo/i })).not.toBeInTheDocument()
  })

  it('never shows a Tapo button anywhere on an NVR channel', () => {
    renderWithProviders(<CameraCard camera={nvrCamera} />)
    expect(screen.queryByText(/tapo/i)).not.toBeInTheDocument()
  })

  it('tells the operator how to resolve an unidentified camera', async () => {
    renderWithProviders(<CameraCard camera={unknownCamera} />)
    expect(screen.getByText(/manufacturer unknown/i)).toBeInTheDocument()
    const menu = await openOverflow(unknownCamera.name)
    expect(within(menu).getByRole('menuitem', { name: /identify camera/i })).toBeInTheDocument()
    expect(within(menu).queryByRole('menuitem', { name: /tapo lan controls/i })).not.toBeInTheDocument()
  })

  it('shows Tapo controls once an administrator has opted in', async () => {
    const camera = makeCamera({
      ...unknownCamera,
      tapo_controls_enabled: true,
      tapo_controls: {
        supported: true,
        source: 'manual',
        detected_vendor: null,
        reason: 'An administrator enabled Tapo LAN controls for this camera.',
        can_opt_in: false,
      },
    })
    renderWithProviders(<CameraCard camera={camera} />)
    const menu = await openOverflow(camera.name)
    expect(within(menu).getByRole('menuitem', { name: /tapo lan controls/i })).toBeInTheDocument()
    expect(screen.queryByText(/manufacturer unknown/i)).not.toBeInTheDocument()
  })
})

describe('the action row stays compact', () => {
  it('keeps only the everyday actions as buttons', () => {
    renderWithProviders(<CameraCard camera={tapoCamera} />)
    const buttons = screen.getAllByRole('button').map((button) => button.textContent?.trim())
    // Configure, Test, the overflow trigger and the recording switch. Anything
    // more and a phone wraps them into rows of indistinguishable grey.
    expect(buttons.filter(Boolean)).toEqual(['Configure', 'Test'])
  })

  it('puts the occasional actions behind one labelled trigger', async () => {
    renderWithProviders(<CameraCard camera={tapoCamera} />)
    const menu = await openOverflow(tapoCamera.name)
    const items = within(menu)
      .getAllByRole('menuitem')
      .map((item) => item.textContent?.trim())
    expect(items).toContain('Identify camera')
    // The fixture has never had PTZ detected, so the action offers to do it.
    expect(items).toContain('Detect PTZ')
    expect(items).toContain('Positions & patrol')
    expect(items).toContain('Remove camera')
  })

  it('closes the menu and returns focus to its trigger on Escape', async () => {
    renderWithProviders(<CameraCard camera={tapoCamera} />)
    const trigger = screen.getByRole('button', { name: `More actions for ${tapoCamera.name}` })
    await userEvent.click(trigger)
    await screen.findByRole('menu')
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('opens the menu from the keyboard alone', async () => {
    renderWithProviders(<CameraCard camera={tapoCamera} />)
    const trigger = screen.getByRole('button', { name: `More actions for ${tapoCamera.name}` })
    trigger.focus()
    await userEvent.keyboard('{Enter}')
    expect(await screen.findByRole('menu')).toBeInTheDocument()
  })
})

describe('recording state is visible without opening anything', () => {
  it('shows the switch and the state on the card', () => {
    renderWithProviders(<CameraCard camera={tapoCamera} />)
    expect(screen.getByRole('switch', { name: /^Recording/ })).toBeChecked()
    expect(screen.getByText('On, recorder running')).toBeInTheDocument()
  })

  it('reports a recorder that failed to start rather than showing it as on', () => {
    const camera = makeCamera({ recording_enabled: true, recording_state: 'failed' })
    renderWithProviders(<CameraCard camera={camera} />)
    expect(screen.getByText('Failed to start')).toBeInTheDocument()
    expect(screen.queryByText('On, recorder running')).not.toBeInTheDocument()
  })

  it('does not repeat the recording state as a chip beside the connection state', () => {
    // They were both red and adjacent, so a fault and a healthy recorder were
    // indistinguishable at a glance. The connection state is now named rather
    // than shown as the bare word "error", but it is still the only chip.
    const failing = makeCamera({ status: 'error', recording_state: 'recording' })
    const { container } = renderWithProviders(<CameraCard camera={failing} />)
    // The connection state is named, in words rather than as the bare word
    // "error"...
    expect(screen.getAllByText('Not reachable').length).toBeGreaterThan(0)
    // ...and the recorder state is never a second chip beside it. It belongs to
    // the switch, which reports it in its own state line.
    const chips = [...container.querySelectorAll('span')]
      .map((el) => el.textContent?.trim())
      .filter((text) => text === 'Recording')
    expect(chips).toEqual([])
  })

  it('shows a battery camera from the dev build as unsupported, with only removal offered', async () => {
    renderWithProviders(<CameraCard camera={batteryCamera} />)
    expect(screen.queryByRole('switch', { name: /^Recording/ })).not.toBeInTheDocument()
    expect(screen.getByText(/not supported in this release/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Configure' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Test' })).not.toBeInTheDocument()

    const menu = await openOverflow(batteryCamera.name)
    const items = within(menu)
      .getAllByRole('menuitem')
      .map((item) => item.textContent?.trim())
    expect(items).toEqual(['Remove camera'])
  })
})

describe('privacy mode', () => {
  it('disables camera actions and says why', () => {
    renderWithProviders(
      <CameraCard camera={tapoCamera} privacyMessage="Cameras are paused for privacy: Guests are present." />,
    )
    expect(screen.getByRole('switch', { name: /^Recording/ })).toBeDisabled()
    expect(screen.getByText('Paused by privacy mode')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /test/i })).toBeDisabled()
  })

  it('replaces the thumbnail rather than requesting a snapshot', () => {
    renderWithProviders(<CameraCard camera={tapoCamera} privacyMessage="Paused." />)
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.getByText('Paused for privacy')).toBeInTheDocument()
  })
})

describe('removing a camera', () => {
  it('confirms in a modal, like the less destructive action already did', async () => {
    renderWithProviders(<CameraCard camera={tapoCamera} />)
    const menu = await openOverflow(tapoCamera.name)
    await userEvent.click(within(menu).getByRole('menuitem', { name: /remove camera/i }))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(`Remove ${tapoCamera.name}?`)).toBeInTheDocument()
    expect(within(dialog).getByText(/its recordings are deleted too/i)).toBeInTheDocument()
    expect(
      within(dialog).getByRole('button', { name: /remove camera and its recordings/i }),
    ).toBeInTheDocument()
  })

  it('can be cancelled without removing anything', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    renderWithProviders(<CameraCard camera={tapoCamera} />)
    const menu = await openOverflow(tapoCamera.name)
    await userEvent.click(within(menu).getByRole('menuitem', { name: /remove camera/i }))
    await userEvent.click(await screen.findByRole('button', { name: 'Keep camera' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})
