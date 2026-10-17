import { fireEvent, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { CameraThumbnail } from '@/features/cameras/CameraThumbnail'
import { batteryCamera, makeCamera, tapoCamera } from '@/test/cameras'
import { renderWithProviders } from '@/test/utils'

describe('the fallback', () => {
  it('replaces a broken snapshot with a neutral placeholder', () => {
    const { container } = renderWithProviders(<CameraThumbnail camera={tapoCamera} />)
    const image = container.querySelector('img')
    expect(image).not.toBeNull()

    fireEvent.error(image as HTMLImageElement)

    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByText('No recent frame')).toBeInTheDocument()
  })

  it('offers a retry rather than blanking the preview for the life of the page', () => {
    const { container } = renderWithProviders(<CameraThumbnail camera={tapoCamera} />)
    fireEvent.error(container.querySelector('img') as HTMLImageElement)

    const retry = screen.getByRole('button', {
      name: `Retry the preview for ${tapoCamera.name}`,
    })
    fireEvent.click(retry)

    expect(container.querySelector('img')).not.toBeNull()
  })

  it('tries again by itself once the camera has been seen again', () => {
    const { container, rerender } = renderWithProviders(
      <CameraThumbnail camera={tapoCamera} />,
    )
    fireEvent.error(container.querySelector('img') as HTMLImageElement)
    expect(container.querySelector('img')).toBeNull()

    // What Test does when it succeeds.
    rerender(
      <CameraThumbnail camera={{ ...tapoCamera, last_seen_at: '2026-08-28T09:00:00Z' }} />,
    )
    expect(container.querySelector('img')).not.toBeNull()
  })

  it('never exposes alt text as visible page content', () => {
    // The failure this prevents: a broken image whose alt text renders beside
    // the browser's broken-image glyph, which reads as a broken product.
    const { container } = renderWithProviders(<CameraThumbnail camera={tapoCamera} />)
    const image = container.querySelector('img') as HTMLImageElement
    expect(image.getAttribute('alt')).toBe('')
    expect(image).toHaveAttribute('aria-hidden', 'true')
    expect(screen.queryByText(/latest frame/i)).not.toBeInTheDocument()
  })

  it('does not request a snapshot for a camera that is not online', () => {
    const offline = makeCamera({ status: 'offline' })
    const { container } = renderWithProviders(<CameraThumbnail camera={offline} />)
    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByText('Offline')).toBeInTheDocument()
  })

  it('names each non-online state rather than showing one grey box', () => {
    for (const [status, label] of [
      ['error', 'Not reachable'],
      ['connecting', 'Connecting'],
      ['disabled', 'Disabled'],
    ] as const) {
      const { unmount } = renderWithProviders(
        <CameraThumbnail camera={makeCamera({ status })} />,
      )
      expect(screen.getByText(label)).toBeInTheDocument()
      unmount()
    }
  })

  it('does not request a snapshot for a battery camera', () => {
    const { container } = renderWithProviders(<CameraThumbnail camera={batteryCamera} />)
    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByText('Dev build only')).toBeInTheDocument()
  })

  it('does not request a snapshot while privacy mode is on', () => {
    const { container } = renderWithProviders(
      <CameraThumbnail camera={tapoCamera} privacyMessage="Paused." />,
    )
    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByText('Paused for privacy')).toBeInTheDocument()
  })
})

describe('request behaviour', () => {
  it('keeps one stable URL across re-renders of the same camera', () => {
    const { container, rerender } = renderWithProviders(<CameraThumbnail camera={tapoCamera} />)
    const first = container.querySelector('img')?.getAttribute('src')
    rerender(<CameraThumbnail camera={tapoCamera} className="mt-2" />)
    expect(container.querySelector('img')?.getAttribute('src')).toBe(first)
  })

  it('loads lazily so a page of cameras does not fetch every frame at once', () => {
    const { container } = renderWithProviders(<CameraThumbnail camera={tapoCamera} />)
    expect(container.querySelector('img')).toHaveAttribute('loading', 'lazy')
  })
})
