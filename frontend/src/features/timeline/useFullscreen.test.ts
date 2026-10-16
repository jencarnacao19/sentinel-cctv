import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useFullscreen } from '@/features/timeline/useFullscreen'

/**
 * The three layers exist because the three surfaces behave differently, so each
 * is driven here against a document shaped like that surface: a normal browser,
 * an iPhone, and a Home Assistant iframe whose permissions policy refuses.
 */

type Mutable = Record<string, unknown>

function makeRefs() {
  const container = document.createElement('div')
  const video = document.createElement('video')
  container.appendChild(video)
  document.body.appendChild(container)
  return {
    containerRef: { current: container },
    videoRef: { current: video },
    container,
    video,
  }
}

const originals = new Map<string, unknown>()

function stub(target: object, name: string, value: unknown): void {
  const key = `${target.constructor.name}.${name}`
  if (!originals.has(key)) originals.set(key, (target as Mutable)[name])
  ;(target as Mutable)[name] = value
}

beforeEach(() => {
  // jsdom implements none of the Fullscreen API, so every case starts from a
  // blank slate and opts into exactly the shape it is testing.
  ;(document as unknown as Mutable).fullscreenElement = null
  ;(document as unknown as Mutable).exitFullscreen = undefined
  ;(document as unknown as Mutable).webkitFullscreenElement = undefined
  ;(document as unknown as Mutable).webkitExitFullscreen = undefined
})

afterEach(() => {
  for (const [key, value] of originals) {
    const [, name] = key.split('.')
    ;(document as unknown as Mutable)[name] = value
  }
  originals.clear()
  document.body.innerHTML = ''
  document.body.style.overflow = ''
  vi.restoreAllMocks()
})

describe('useFullscreen', () => {
  it('catches a synchronous native exception without crashing the page', () => {
    const refs = makeRefs()
    stub(refs.container, 'requestFullscreen', vi.fn(() => { throw new TypeError('Unavailable') }))
    const { result } = renderHook(() => useFullscreen(refs))
    expect(() => act(() => result.current.enter())).not.toThrow()
    expect(result.current.mode).toBe('theatre')
    expect(result.current.denied).toBe(true)
  })

  it('uses the same video in Android WebView without invoking host fullscreen', () => {
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Mozilla/5.0 (Linux; Android 14; Phone; wv) AppleWebKit/537.36 Version/4.0 Chrome/140.0 Mobile')
    const refs = makeRefs()
    const request = vi.fn()
    stub(refs.container, 'requestFullscreen', request)
    const { result } = renderHook(() => useFullscreen(refs))
    act(() => result.current.enter())
    expect(result.current.mode).toBe('theatre')
    expect(request).not.toHaveBeenCalled()
    expect(refs.container.querySelector('video')).toBe(refs.video)
    act(() => result.current.exit())
    expect(result.current.isFullscreen).toBe(false)
  })

  it('does not call a fullscreen API explicitly disabled by iframe policy', () => {
    const refs = makeRefs()
    const request = vi.fn()
    stub(refs.container, 'requestFullscreen', request)
    stub(document, 'fullscreenEnabled', false)
    const { result } = renderHook(() => useFullscreen(refs))
    act(() => result.current.enter())
    expect(request).not.toHaveBeenCalled()
    expect(result.current.mode).toBe('theatre')
  })

  it('cancels a pending native request on a second tap and ignores late rejection', async () => {
    const refs = makeRefs()
    let reject: (reason: Error) => void = () => {}
    const request = vi.fn(() => new Promise<void>((_resolve, fail) => { reject = fail }))
    stub(refs.container, 'requestFullscreen', request)
    const { result } = renderHook(() => useFullscreen(refs))
    act(() => result.current.toggle())
    act(() => result.current.toggle())
    await act(async () => { reject(new Error('late')); await Promise.resolve() })
    expect(request).toHaveBeenCalledOnce()
    expect(result.current.mode).toBe('none')
    expect(document.body.style.overflow).not.toBe('hidden')
  })

  it('keeps exit safe when an Android host throws synchronously', async () => {
    const refs = makeRefs()
    stub(refs.container, 'requestFullscreen', vi.fn().mockResolvedValue(undefined))
    stub(document, 'exitFullscreen', vi.fn(() => { throw new TypeError('Native exit failed') }))
    const { result } = renderHook(() => useFullscreen(refs))
    await act(async () => { result.current.enter(); await Promise.resolve() })
    ;(document as unknown as Mutable).fullscreenElement = refs.container
    expect(() => act(() => result.current.exit())).not.toThrow()
    expect(result.current.mode).toBe('none')
  })
  it('uses the standards API on the container, never the bare video', async () => {
    const refs = makeRefs()
    const request = vi.fn().mockResolvedValue(undefined)
    stub(refs.container, 'requestFullscreen', request)
    const videoRequest = vi.fn()
    stub(refs.video, 'requestFullscreen', videoRequest)

    const { result } = renderHook(() => useFullscreen(refs))
    await act(async () => {
      result.current.enter()
      // The standards path resolves a promise before the hook settles.
      await Promise.resolve()
    })

    expect(request).toHaveBeenCalledTimes(1)
    // The custom transport bar lives beside the video, so promoting the video
    // alone would leave the viewer without controls.
    expect(videoRequest).not.toHaveBeenCalled()
    expect(result.current.mode).toBe('standard')
    expect(result.current.isFullscreen).toBe(true)
    expect(result.current.denied).toBe(false)
  })

  it('falls back to the iOS video API when the container request is refused', async () => {
    const refs = makeRefs()
    stub(refs.container, 'requestFullscreen', vi.fn().mockRejectedValue(new Error('denied')))
    const enterWebkit = vi.fn()
    stub(refs.video, 'webkitEnterFullscreen', enterWebkit)
    stub(refs.video, 'webkitSupportsFullscreen', true)

    const { result } = renderHook(() => useFullscreen(refs))
    await act(async () => {
      result.current.enter()
      await Promise.resolve()
    })

    expect(enterWebkit).toHaveBeenCalledTimes(1)
    expect(result.current.mode).toBe('webkit')
    expect(result.current.denied).toBe(false)
  })

  it('falls back to the in-document theatre layer when the iframe refuses', async () => {
    // The Home Assistant Webpage card: no `allow="fullscreen"`, and no iOS
    // video API either.
    const refs = makeRefs()
    stub(refs.container, 'requestFullscreen', vi.fn().mockRejectedValue(new Error('denied')))

    const { result } = renderHook(() => useFullscreen(refs))
    await act(async () => {
      result.current.enter()
      await Promise.resolve()
    })

    expect(result.current.mode).toBe('theatre')
    expect(result.current.isFullscreen).toBe(true)
    // The UI has to be able to say so truthfully.
    expect(result.current.denied).toBe(true)
  })

  it('uses the theatre layer when the browser has no fullscreen API at all', () => {
    const refs = makeRefs()
    stub(refs.container, 'requestFullscreen', undefined)

    const { result } = renderHook(() => useFullscreen(refs))
    act(() => {
      result.current.enter()
    })

    expect(result.current.mode).toBe('theatre')
    // Not a refusal: nothing was asked for, so nothing was denied.
    expect(result.current.denied).toBe(false)
  })

  it('never reads window.top', async () => {
    const refs = makeRefs()
    stub(refs.container, 'requestFullscreen', vi.fn().mockRejectedValue(new Error('denied')))
    const topSpy = vi.fn()
    Object.defineProperty(window, 'top', { configurable: true, get: topSpy })

    const { result } = renderHook(() => useFullscreen(refs))
    await act(async () => {
      result.current.enter()
      await Promise.resolve()
    })

    // Reading across a cross-origin boundary throws, so the fallback must not.
    expect(topSpy).not.toHaveBeenCalled()
  })

  it('closes the theatre layer on Escape and restores page scrolling', async () => {
    const refs = makeRefs()
    document.body.style.overflow = 'auto'
    stub(refs.container, 'requestFullscreen', vi.fn().mockRejectedValue(new Error('denied')))

    const { result } = renderHook(() => useFullscreen(refs))
    await act(async () => {
      result.current.enter()
      await Promise.resolve()
    })
    expect(document.body.style.overflow).toBe('hidden')

    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    })

    expect(result.current.mode).toBe('none')
    // Restored to what the document had, not to a hard-coded default.
    expect(document.body.style.overflow).toBe('auto')
  })

  it('notices the browser leaving fullscreen without going through exit', async () => {
    const refs = makeRefs()
    stub(refs.container, 'requestFullscreen', vi.fn().mockResolvedValue(undefined))
    const { result } = renderHook(() => useFullscreen(refs))
    await act(async () => {
      result.current.enter()
      // The standards path resolves a promise before the hook settles.
      await Promise.resolve()
    })
    ;(document as unknown as Mutable).fullscreenElement = refs.container
    expect(result.current.mode).toBe('standard')

    // Escape, the Android back gesture, or the browser's own control.
    ;(document as unknown as Mutable).fullscreenElement = null
    act(() => {
      document.dispatchEvent(new Event('fullscreenchange'))
    })

    expect(result.current.mode).toBe('none')
    expect(result.current.isFullscreen).toBe(false)
  })

  it('notices the iOS player being dismissed', async () => {
    const refs = makeRefs()
    stub(refs.container, 'requestFullscreen', vi.fn().mockRejectedValue(new Error('denied')))
    stub(refs.video, 'webkitEnterFullscreen', vi.fn())
    stub(refs.video, 'webkitSupportsFullscreen', true)

    const { result } = renderHook(() => useFullscreen(refs))
    await act(async () => {
      result.current.enter()
      await Promise.resolve()
    })
    expect(result.current.mode).toBe('webkit')

    act(() => {
      refs.video.dispatchEvent(new Event('webkitendfullscreen'))
    })
    expect(result.current.mode).toBe('none')
  })

  it('does not try the iOS path before the video has metadata', async () => {
    const refs = makeRefs()
    stub(refs.container, 'requestFullscreen', vi.fn().mockRejectedValue(new Error('denied')))
    const enterWebkit = vi.fn()
    stub(refs.video, 'webkitEnterFullscreen', enterWebkit)
    // Safari reports false until the first frame; a request now is ignored.
    stub(refs.video, 'webkitSupportsFullscreen', false)

    const { result } = renderHook(() => useFullscreen(refs))
    await act(async () => {
      result.current.enter()
      await Promise.resolve()
    })

    expect(enterWebkit).not.toHaveBeenCalled()
    expect(result.current.mode).toBe('theatre')
  })

  it('toggle enters and leaves', async () => {
    const refs = makeRefs()
    stub(refs.container, 'requestFullscreen', vi.fn().mockRejectedValue(new Error('denied')))

    const { result } = renderHook(() => useFullscreen(refs))
    await act(async () => {
      result.current.toggle()
      await Promise.resolve()
    })
    expect(result.current.isFullscreen).toBe(true)

    act(() => {
      result.current.toggle()
    })
    expect(result.current.isFullscreen).toBe(false)
  })

  it('leaves native fullscreen behind when the page unmounts', async () => {
    const refs = makeRefs()
    stub(refs.container, 'requestFullscreen', vi.fn().mockResolvedValue(undefined))
    const exitFullscreen = vi.fn().mockResolvedValue(undefined)
    ;(document as unknown as Mutable).exitFullscreen = exitFullscreen

    const { result, unmount } = renderHook(() => useFullscreen(refs))
    await act(async () => {
      result.current.enter()
      // The standards path resolves a promise before the hook settles.
      await Promise.resolve()
    })
    ;(document as unknown as Mutable).fullscreenElement = refs.container

    unmount()
    expect(exitFullscreen).toHaveBeenCalled()
  })
})
