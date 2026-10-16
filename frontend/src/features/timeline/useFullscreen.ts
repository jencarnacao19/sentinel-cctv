import { useCallback, useEffect, useRef, useState } from 'react'
import { isAndroidWebView } from '@/lib/device'

/**
 * Fullscreen for recorded playback, in three layers, because one is not enough.
 *
 * Sentinel's playback surface is shown in three places that behave differently:
 * a normal desktop browser, an iPhone, and a Home Assistant Webpage card, which
 * is a cross-origin `<iframe>`. The standards Fullscreen API is refused inside
 * an iframe unless the *embedding* document opted in with `allow="fullscreen"`,
 * and Sentinel does not control that document. iOS Safari historically refuses
 * it on any element other than the `<video>` itself.
 *
 * So the request is tried in order and the first one that works wins:
 *
 * 1. **`requestFullscreen` on the container.** The container, never the bare
 *    `<video>`: playback has `controls={false}` and its transport bar, speed
 *    menu and clock are siblings inside that container. Going fullscreen on the
 *    video element alone would give the viewer a picture and no controls.
 * 2. **`webkitEnterFullscreen` on the video.** iOS only, native player chrome,
 *    and Sentinel's own controls are gone for the duration — which is the
 *    trade-off Apple offers and the only true fullscreen an iPhone will give.
 * 3. **A theatre fallback.** A fixed, full-viewport layer inside the same
 *    document. No permission is needed because nothing is being requested: the
 *    element simply covers the viewport. This is what an HA card without
 *    `allow="fullscreen"` actually gets.
 *
 * The fallback never touches `window.top`. Reading across a cross-origin
 * boundary throws, and asking the parent to do anything would be a hard
 * dependency on a document Sentinel does not own.
 *
 * Nothing here unmounts or re-parents the `<video>`. The player holds a direct
 * reference to that element together with its `MediaSource` and in-flight
 * fetches; moving it in the tree would orphan all three and silently stop
 * playback. Every layer either asks the browser to promote an existing element
 * or changes CSS on one.
 */

export type FullscreenMode = 'none' | 'standard' | 'webkit' | 'theatre'

export interface FullscreenController {
  mode: FullscreenMode
  isFullscreen: boolean
  /** True once a native request has been refused, so the UI can say so. */
  denied: boolean
  enter: () => void
  exit: () => void
  toggle: () => void
}

/**
 * Vendor-prefixed members are read through an index signature rather than an
 * interface that merely adds optional properties. Such an interface is
 * structurally identical to the base type, so TypeScript treats the assertion
 * as a no-op and the lint autofixer removes it — along with the access it was
 * there to permit.
 */
type Vendor = Record<string, unknown>

function vendorCall(target: object | null, name: string): (() => unknown) | null {
  if (!target) return null
  const candidate = (target as unknown as Vendor)[name]
  return typeof candidate === 'function' ? (candidate as () => unknown).bind(target) : null
}

function vendorFlag(target: object | null, name: string): boolean | undefined {
  if (!target) return undefined
  const candidate = (target as unknown as Vendor)[name]
  return typeof candidate === 'boolean' ? candidate : undefined
}

function nativeElement(): Element | null {
  const legacy = (document as unknown as Vendor).webkitFullscreenElement
  return document.fullscreenElement ?? (legacy as Element | null | undefined) ?? null
}

function exitNative(onFailure: () => void = () => {}): void {
  const exit = vendorCall(document, 'exitFullscreen') ?? vendorCall(document, 'webkitExitFullscreen')
  try {
    if (exit) void Promise.resolve(exit()).catch(onFailure)
    else onFailure()
  } catch { onFailure() }
}

export interface UseFullscreenOptions {
  containerRef: React.RefObject<HTMLElement | null>
  videoRef: React.RefObject<HTMLVideoElement | null>
}

export function useFullscreen({
  containerRef,
  videoRef,
}: UseFullscreenOptions): FullscreenController {
  const [mode, setMode] = useState<FullscreenMode>('none')
  const [denied, setDenied] = useState(false)
  // Mirrors `mode` for use inside listeners registered once, so the effect does
  // not have to re-subscribe on every state change.
  const modeRef = useRef<FullscreenMode>('none')
  const generationRef = useRef(0)
  const pendingRef = useRef(false)
  modeRef.current = mode

  const exit = useCallback(() => {
    generationRef.current += 1
    pendingRef.current = false
    if (modeRef.current === 'standard' && nativeElement() === containerRef.current) {
      exitNative(() => setMode('none'))
      return
    }
    if (modeRef.current === 'webkit') {
      try { vendorCall(videoRef.current, 'webkitExitFullscreen')?.() } catch { /* Keep exit usable. */ }
    }
    modeRef.current = 'none'
    setMode('none')
  }, [containerRef, videoRef])

  const enter = useCallback(() => {
    const container = containerRef.current
    const video = videoRef.current
    if (!container || pendingRef.current) return
    const generation = ++generationRef.current
    const selectMode = (next: FullscreenMode, refused = false) => {
      if (generation !== generationRef.current) return
      pendingRef.current = false
      modeRef.current = next
      setMode(next)
      setDenied(refused)
    }
    const fallback = (refused: boolean) => {
      if (generation !== generationRef.current) return
      if (tryWebkit(video)) selectMode('webkit')
      else selectMode('theatre', refused)
    }

    // Android app-hosted WebViews hand native fullscreen to the host app.
    // Keep the existing video/decoder in place instead of entering that crash-
    // prone host transition. Standalone Android browsers retain native support.
    if (isAndroidWebView()) {
      selectMode('theatre')
      return
    }
    if (document.fullscreenEnabled === false) {
      fallback(true)
      return
    }

    // Layer 1. The promise rejects when a permissions policy forbids it, which
    // is exactly the Home Assistant iframe case, so the fallback is chosen from
    // the rejection rather than from sniffing the environment.
    const request = container.requestFullscreen?.bind(container)
    if (request) {
      pendingRef.current = true
      try {
        // Call inside the gesture and the try block. Promise.resolve(request())
        // alone cannot catch a synchronous exception from a host WebView.
        void Promise.resolve(request()).then(() => {
          if (generation !== generationRef.current) {
            if (nativeElement() === container) exitNative()
            return
          }
          selectMode('standard')
        }).catch(() => fallback(true))
      } catch { fallback(true) }
      return
    }

    const legacyRequest = vendorCall(container, 'webkitRequestFullscreen')
    if (legacyRequest) {
      try {
        void Promise.resolve(legacyRequest()).catch(() => {
          if (generation === generationRef.current) selectMode('theatre', true)
        })
        selectMode('standard')
        return
      } catch {
        // Fall through to the layers below.
      }
    }

    if (tryWebkit(video)) {
      selectMode('webkit')
      return
    }

    // Layer 3. Not a failure: on a browser with no Fullscreen API at all this
    // is simply what fullscreen means here.
    selectMode('theatre')
  }, [containerRef, videoRef])

  const toggle = useCallback(() => {
    if (pendingRef.current) exit()
    else if (modeRef.current === 'none') enter()
    else exit()
  }, [enter, exit])

  // The browser can leave fullscreen without going through `exit`: Escape, the
  // Android back gesture, or the user dismissing the iOS player. Without these
  // listeners the hook would keep claiming to be fullscreen afterwards.
  useEffect(() => {
    function onChange() {
      if (modeRef.current === 'standard' && !nativeElement()) setMode('none')
    }
    document.addEventListener('fullscreenchange', onChange)
    document.addEventListener('webkitfullscreenchange', onChange)
    return () => {
      document.removeEventListener('fullscreenchange', onChange)
      document.removeEventListener('webkitfullscreenchange', onChange)
    }
  }, [])

  useEffect(() => {
    const video = videoRef.current
    if (!video) return
    function onEnd() {
      if (modeRef.current === 'webkit') setMode('none')
    }
    function onBegin() {
      setMode('webkit')
    }
    video.addEventListener('webkitbeginfullscreen', onBegin)
    video.addEventListener('webkitendfullscreen', onEnd)
    return () => {
      video.removeEventListener('webkitbeginfullscreen', onBegin)
      video.removeEventListener('webkitendfullscreen', onEnd)
    }
  }, [videoRef])

  // The theatre layer is inside the document, so nothing dismisses it for us.
  useEffect(() => {
    if (mode !== 'theatre') return
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.preventDefault()
        setMode('none')
      }
    }
    document.addEventListener('keydown', onKeyDown)
    // Stop the page behind the layer scrolling under it, and restore whatever
    // the document had rather than assuming it was the default.
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.body.style.overflow = previousOverflow
    }
  }, [mode])

  // Leaving the page while fullscreen would otherwise strand the document with
  // `overflow: hidden` and a promoted element.
  useEffect(
    () => () => {
      generationRef.current += 1
      pendingRef.current = false
      if (modeRef.current === 'standard' && nativeElement()) exitNative()
    },
    [],
  )

  return { mode, isFullscreen: mode !== 'none', denied, enter, exit, toggle }
}

function tryWebkit(video: HTMLVideoElement | null): boolean {
  const request = vendorCall(video, 'webkitEnterFullscreen')
  if (!request) return false
  // `webkitSupportsFullscreen` is false until metadata has loaded, so a request
  // before the first frame would be silently ignored.
  if (vendorFlag(video, 'webkitSupportsFullscreen') === false) return false
  try {
    request()
    return true
  } catch {
    return false
  }
}
