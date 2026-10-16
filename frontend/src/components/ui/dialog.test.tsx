import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogForm,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'

/**
 * The layout contract, not the appearance.
 *
 * These exist because the failure they guard against is invisible in a unit
 * test that only checks text: a dialog whose footer scrolls away, or whose
 * body cannot shrink, still renders every string correctly. It is only wrong
 * on a short window or a phone — the two places nobody runs a test.
 *
 * So the assertions are about structure and about the specific classes that
 * make the three-region column work. `min-h-0` in particular is load-bearing
 * and easy to delete by accident: without it the flex child refuses to shrink
 * below its content, the column grows past the container, and the action
 * buttons end up below the bottom of the screen.
 */

function Fixture({ withForm = false }: { withForm?: boolean } = {}) {
  const body = (
    <DialogBody>
      <p>A great deal of configuration.</p>
      <input aria-label="Something" />
    </DialogBody>
  )
  const footer = (
    <DialogFooter>
      <Button variant="ghost">Cancel</Button>
      <Button>Save</Button>
    </DialogFooter>
  )

  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button>Open</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Configure something</DialogTitle>
          <DialogDescription>What this dialog is for.</DialogDescription>
        </DialogHeader>
        {withForm ? (
          <DialogForm>
            {body}
            {footer}
          </DialogForm>
        ) : (
          <>
            {body}
            {footer}
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

async function open(withForm = false) {
  render(<Fixture withForm={withForm} />)
  await userEvent.click(screen.getByRole('button', { name: 'Open' }))
  const dialog = await screen.findByRole('dialog')
  return dialog
}

describe('the dialog has exactly one scroll container', () => {
  it('puts it on the body, and nowhere else', async () => {
    const dialog = await open()
    const scrollable = dialog.querySelectorAll('.scroll-region')
    expect(scrollable).toHaveLength(1)
    expect(scrollable[0]).toBe(screen.getByTestId('dialog-body'))
  })

  it('keeps it on the body when a form spans the body and the footer', async () => {
    const dialog = await open(true)
    expect(dialog.querySelectorAll('.scroll-region')).toHaveLength(1)
    expect(screen.getByTestId('dialog-body').className).toContain('scroll-region')
  })

  it('never lets a scroll gesture continue into the page behind it', async () => {
    await open()
    // `overscroll-behavior: contain` is what stops a flick that reaches the end
    // of the body from dragging the whole site underneath the sheet on iOS.
    // It rides on `.scroll-region`, so asserting the class is asserting the
    // behaviour; a rule change would have to go through that one utility.
    expect(screen.getByTestId('dialog-body').className).toContain('scroll-region')
  })

  it('does not scroll the surface itself', async () => {
    const content = await open()
    expect(content.className).toContain('overflow-hidden')
    expect(content.className).not.toContain('overflow-y-auto')
  })
})

describe('the action buttons stay reachable', () => {
  it('keeps the footer outside the scrolling region', async () => {
    await open()
    const body = screen.getByTestId('dialog-body')
    const footer = screen.getByTestId('dialog-footer')
    expect(body.contains(footer)).toBe(false)
    expect(footer.className).toContain('shrink-0')
  })

  it('keeps the header outside the scrolling region', async () => {
    await open()
    expect(screen.getByTestId('dialog-body').contains(screen.getByRole('heading'))).toBe(false)
  })

  it('pads the footer past the home indicator', async () => {
    await open()
    // A phone's gesture bar overlaps the bottom of the viewport; without this
    // the primary action sits half underneath it.
    expect(screen.getByTestId('dialog-footer').className).toContain('pb-safe-gutter')
  })

  it('lets the body shrink so the column cannot outgrow the viewport', async () => {
    await open()
    const body = screen.getByTestId('dialog-body')
    // Both are required. `flex-1` alone still refuses to shrink below the
    // content's own height, which is exactly how a footer gets pushed off a
    // short window.
    expect(body.className).toContain('min-h-0')
    expect(body.className).toContain('flex-1')
  })

  it('keeps the column intact when a form is inserted between the regions', async () => {
    await open(true)
    const form = screen.getByTestId('dialog-body').closest('form')
    expect(form).not.toBeNull()
    expect(form?.className).toContain('min-h-0')
    expect(form?.className).toContain('flex-1')
    expect(form?.className).toContain('flex-col')
    // The footer is the form's child, so submit still works from it.
    expect(form?.contains(screen.getByTestId('dialog-footer'))).toBe(true)
  })
})

describe('the surface is bounded by the viewport', () => {
  it('is limited by the dynamic viewport height on a phone and a short window', async () => {
    const content = await open()
    // `dvh`, not `vh`: Safari's collapsing toolbar and the on-screen keyboard
    // both change the usable height, and `vh` ignores each of them.
    expect(content.className).toContain('max-h-[92dvh]')
    expect(content.className).toContain('sm:max-h-[min(85dvh,44rem)]')
    expect(content.className).not.toMatch(/(^|\s|:)max-h-\[\d+vh\]/)
  })

  it('does not grow wider than the viewport on a desktop', async () => {
    const content = await open()
    expect(content.className).toContain('sm:w-[calc(100%-3rem)]')
    expect(content.className).toContain('sm:max-w-lg')
  })
})

describe('dismissal and focus', () => {
  it('closes on Escape', async () => {
    await open()
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('returns focus to whatever opened it', async () => {
    render(<Fixture />)
    const trigger = screen.getByRole('button', { name: 'Open' })
    await userEvent.click(trigger)
    await screen.findByRole('dialog')
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('traps focus inside the dialog', async () => {
    const dialog = await open()
    // Cycle past the last control; focus must come back round rather than
    // landing on the page behind the overlay.
    for (let i = 0; i < 8; i += 1) {
      await userEvent.tab()
      expect(dialog.contains(document.activeElement)).toBe(true)
    }
  })

  it('offers a labelled close control', async () => {
    const dialog = await open()
    const close = within(dialog).getByRole('button', { name: 'Close' })
    await userEvent.click(close)
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('is announced with a name and a description', async () => {
    const dialog = await open()
    expect(dialog).toHaveAccessibleName('Configure something')
    expect(dialog).toHaveAccessibleDescription('What this dialog is for.')
  })
})

describe('the page behind the dialog', () => {
  it('is locked while the dialog is open and released afterwards', async () => {
    expect(document.body).not.toHaveAttribute('data-scroll-locked')
    await open()
    // Radix locks the document rather than relying on the overlay swallowing
    // wheel events. That is what stops the page scrolling underneath a sheet,
    // and it is the half of the fix that `overscroll-contain` on the body
    // cannot do on its own.
    await waitFor(() => expect(document.body).toHaveAttribute('data-scroll-locked'))
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await waitFor(() => expect(document.body).not.toHaveAttribute('data-scroll-locked'))
  })
})
