import { useEffect, useState } from 'react'

/**
 * Whether the screen's own large title is still on show.
 *
 * The mobile navigation bar carries a copy of the page title so the current
 * screen is named even after the content has scrolled. Rendering both at once
 * says "Cameras" twice in the first 90 pixels, which is the giveaway that a
 * bar was bolted on rather than designed with the page. So the bar's copy is
 * revealed only once the page's own heading has left the top of the viewport,
 * which is the behaviour the platform's own large titles have.
 *
 * Purely presentational: the bar's copy is `aria-hidden`, and the real heading
 * is the `<h1>` this watches, so nothing here changes what a screen reader is
 * told.
 */
export function useLargeTitleVisible(pathname: string): boolean {
  const [visible, setVisible] = useState(true)

  useEffect(() => {
    // A screen with no large title (there are none today, but a future one may
    // omit `PageHeader`) leaves the bar's title showing rather than blank.
    if (typeof IntersectionObserver === 'undefined') return

    setVisible(true)
    let observer: IntersectionObserver | null = null

    // One frame, so the route being navigated to has committed its own DOM.
    const frame = requestAnimationFrame(() => {
      const target = document.querySelector('[data-page-title]')
      if (!target) {
        setVisible(false)
        return
      }
      observer = new IntersectionObserver(
        (entries) => {
          const entry = entries[0]
          if (entry) setVisible(entry.isIntersecting)
        },
        // The heading counts as gone once it is behind the bar, not once it is
        // off the screen entirely. `rootMargin` only accepts pixels or
        // percentages, so the bar's 3.5rem height is written out.
        { rootMargin: '-56px 0px 0px 0px', threshold: 0 },
      )
      observer.observe(target)
    })

    return () => {
      cancelAnimationFrame(frame)
      observer?.disconnect()
    }
  }, [pathname])

  return visible
}
