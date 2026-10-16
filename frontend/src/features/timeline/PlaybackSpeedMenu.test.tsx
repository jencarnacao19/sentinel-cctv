import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { PlaybackSpeedMenu } from '@/features/timeline/PlaybackSpeedMenu'
import { PLAYBACK_SPEEDS } from '@/features/timeline/playbackBuffer'

describe('PlaybackSpeedMenu', () => {
  it('offers exactly the six supported speeds', async () => {
    render(<PlaybackSpeedMenu value={1} onChange={vi.fn()} />)
    await userEvent.click(screen.getByRole('button', { name: /Playback speed/ }))

    const options = within(screen.getByRole('listbox')).getAllByRole('option')
    expect(options.map((option) => option.textContent?.trim().split('×')[0])).toEqual([
      '0.5',
      '1',
      '2',
      '4',
      '8',
      '16',
    ])
    expect(PLAYBACK_SPEEDS).toHaveLength(6)
  })

  it('shows the current speed without opening anything', () => {
    render(<PlaybackSpeedMenu value={16} onChange={vi.fn()} />)
    // The whole point of replacing the cycler: the speed is legible at a glance.
    expect(screen.getByRole('button', { name: /Playback speed/ })).toHaveTextContent('16×')
  })

  it('selects a speed in one press rather than by cycling', async () => {
    const onChange = vi.fn()
    render(<PlaybackSpeedMenu value={1} onChange={onChange} />)

    await userEvent.click(screen.getByRole('button', { name: /Playback speed/ }))
    await userEvent.click(screen.getByRole('option', { name: /16×/ }))

    // The cycler this replaces needed five presses to get from 1x to 16x.
    expect(onChange).toHaveBeenCalledExactlyOnceWith(16)
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('is fully operable from the keyboard', async () => {
    const onChange = vi.fn()
    render(<PlaybackSpeedMenu value={1} onChange={onChange} />)

    const trigger = screen.getByRole('button', { name: /Playback speed/ })
    trigger.focus()
    await userEvent.keyboard('{Enter}')
    expect(screen.getByRole('listbox')).toBeInTheDocument()

    // The ladder is [0.5, 1, 2, 4, 8, 16]; two steps down from 1x is 4x.
    await userEvent.keyboard('{ArrowDown}{ArrowDown}{Enter}')
    expect(onChange).toHaveBeenCalledExactlyOnceWith(4)
  })

  it('jumps to the ends with Home and End', async () => {
    const onChange = vi.fn()
    render(<PlaybackSpeedMenu value={2} onChange={onChange} />)

    await userEvent.click(screen.getByRole('button', { name: /Playback speed/ }))
    await userEvent.keyboard('{End}{Enter}')
    expect(onChange).toHaveBeenCalledExactlyOnceWith(16)
  })

  it('closes on Escape and returns focus to the trigger', async () => {
    render(<PlaybackSpeedMenu value={1} onChange={vi.fn()} />)
    const trigger = screen.getByRole('button', { name: /Playback speed/ })

    await userEvent.click(trigger)
    await userEvent.keyboard('{Escape}')

    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    // A keyboard user must not be dropped at the top of the document.
    expect(trigger).toHaveFocus()
  })

  it('marks the playing speed as selected for assistive technology', async () => {
    render(<PlaybackSpeedMenu value={4} onChange={vi.fn()} />)
    await userEvent.click(screen.getByRole('button', { name: /Playback speed/ }))

    expect(screen.getByRole('option', { selected: true })).toHaveTextContent('4×')
  })

  it('says which speeds are silent instead of leaving it a surprise', async () => {
    render(<PlaybackSpeedMenu value={1} onChange={vi.fn()} />)
    await userEvent.click(screen.getByRole('button', { name: /Playback speed/ }))

    const listbox = within(screen.getByRole('listbox'))
    expect(listbox.getByRole('option', { name: /8×/ })).toHaveTextContent('Silent')
    expect(listbox.getByRole('option', { name: /0.5×/ })).not.toHaveTextContent('Silent')
  })

  it('cannot be opened while there is nothing to play', async () => {
    render(<PlaybackSpeedMenu value={1} onChange={vi.fn()} disabled />)
    const trigger = screen.getByRole('button', { name: /Playback speed/ })

    expect(trigger).toBeDisabled()
    await userEvent.click(trigger)
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('reopens on the speed that is playing, not where it was left', async () => {
    const onChange = vi.fn()
    const { rerender } = render(<PlaybackSpeedMenu value={1} onChange={onChange} />)

    await userEvent.click(screen.getByRole('button', { name: /Playback speed/ }))
    await userEvent.keyboard('{ArrowDown}')
    await userEvent.keyboard('{Escape}')

    rerender(<PlaybackSpeedMenu value={8} onChange={onChange} />)
    await userEvent.click(screen.getByRole('button', { name: /Playback speed/ }))
    await userEvent.keyboard('{Enter}')

    expect(onChange).toHaveBeenCalledExactlyOnceWith(8)
  })
})
