import { useSyncExternalStore } from 'react'

const TOUCH_POINTER = '(pointer: coarse)'

/** Phones/tablets stay silent, even when a mouse is paired with them. */
export function isMobileInterface(): boolean {
  return /Android|iPhone|iPad|iPod/i.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1) ||
    window.matchMedia(TOUCH_POINTER).matches
}

function subscribe(onChange: () => void): () => void {
  const media = window.matchMedia(TOUCH_POINTER)
  media.addEventListener('change', onChange)
  return () => media.removeEventListener('change', onChange)
}

export function useMobileInterface(): boolean {
  return useSyncExternalStore(subscribe, isMobileInterface, () => false)
}

/** Android app-hosted players depend on the host app's native fullscreen UI. */
export function isAndroidWebView(): boolean {
  return /Android/i.test(navigator.userAgent) && /\bwv\b|Version\/4\.0|Home ?Assistant/i.test(navigator.userAgent)
}
