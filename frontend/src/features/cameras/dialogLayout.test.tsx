import { screen } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AddCameraDialog } from '@/features/cameras/AddCameraDialog'
import { DiscoverCameraDialog } from '@/features/cameras/DiscoverCameraDialog'
import { EditCameraDialog } from '@/features/cameras/EditCameraDialog'
import { PtzSetupDialog } from '@/features/cameras/PtzSetupDialog'
import { TapoBatteryConfigureDialog } from '@/features/cameras/TapoBatteryConfigureDialog'
import { TapoBatteryDialog } from '@/features/cameras/TapoBatteryDialog'
import { TapoLocalControlsDialog } from '@/features/cameras/TapoLocalControlsDialog'
import { batteryCamera, tapoCamera } from '@/test/cameras'
import { renderWithProviders } from '@/test/utils'

/**
 * Every shipped dialog obeys the layout contract, not only the primitive.
 *
 * The primitive's own test proves the three-region column is correct. This one
 * proves nothing has quietly opted out of it -- a dialog that forgets
 * `DialogBody`, wraps its regions in a plain `<form>`, or adds a second
 * scrolling container inside the body looks completely normal on a large
 * monitor and is unusable on a phone.
 *
 * Adding a dialog to the application and not to this list is the mistake this
 * is here to catch, so the list is deliberately explicit rather than derived.
 */

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

const DIALOGS: { name: string; element: ReactElement; hasFooter: boolean }[] = [
  { name: 'Add camera', element: <AddCameraDialog open onOpenChange={vi.fn()} />, hasFooter: true },
  {
    name: 'Discover cameras',
    element: <DiscoverCameraDialog open onOpenChange={vi.fn()} />,
    hasFooter: true,
  },
  {
    name: 'Configure camera',
    element: <EditCameraDialog camera={tapoCamera} open onOpenChange={vi.fn()} />,
    hasFooter: true,
  },
  {
    name: 'Configure battery camera',
    element: <TapoBatteryConfigureDialog camera={batteryCamera} open onOpenChange={vi.fn()} />,
    hasFooter: true,
  },
  {
    name: 'Battery camera',
    element: <TapoBatteryDialog camera={batteryCamera} open onOpenChange={vi.fn()} />,
    hasFooter: true,
  },
  {
    name: 'Positions and patrol',
    element: <PtzSetupDialog camera={tapoCamera} open onOpenChange={vi.fn()} />,
    hasFooter: true,
  },
  {
    name: 'Tapo LAN controls',
    element: <TapoLocalControlsDialog camera={tapoCamera} open onOpenChange={vi.fn()} />,
    hasFooter: true,
  },
]

beforeEach(() => {
  // Every dialog here opens against a server it cannot reach in a test. What is
  // under test is the layout, which must hold in the loading and the error
  // state too -- those are the states with the least content and the most
  // chance of a collapsed column.
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve(json({}))),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe.each(DIALOGS)('$name', ({ element, hasFooter }) => {
  it('has exactly one scrolling region, and it is the body', async () => {
    renderWithProviders(element)
    const dialog = await screen.findByRole('dialog')
    const regions = dialog.querySelectorAll('.scroll-region')
    expect(regions).toHaveLength(1)
    expect(regions[0]).toBe(screen.getByTestId('dialog-body'))
  })

  it('keeps its pinned regions outside the scrolling one', async () => {
    renderWithProviders(element)
    await screen.findByRole('dialog')
    const body = screen.getByTestId('dialog-body')
    expect(body.className).toContain('min-h-0')
    expect(body.className).toContain('flex-1')
    if (hasFooter) {
      const footer = screen.getByTestId('dialog-footer')
      expect(body.contains(footer)).toBe(false)
      expect(footer.className).toContain('shrink-0')
    }
  })

  it('bounds itself to the dynamic viewport rather than to its content', async () => {
    renderWithProviders(element)
    const content = await screen.findByTestId('dialog-content')
    expect(content.className).toContain('flex-col')
    expect(content.className).toContain('max-h-[92dvh]')
    // A dialog that overrode the height cap with its own `vh` value would be
    // wrong on exactly the devices this refit is for.
    expect(content.className).not.toMatch(/(^|\s|:)max-h-\[\d+vh\]/)
  })

  it('offers a labelled close control', async () => {
    renderWithProviders(element)
    const dialog = await screen.findByRole('dialog')
    expect(dialog.querySelector('[aria-label="Close"]')).not.toBeNull()
  })
})
