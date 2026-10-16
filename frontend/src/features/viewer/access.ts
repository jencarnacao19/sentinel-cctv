/**
 * Read-only viewer key handling.
 *
 * Home Assistant receives the key in the URL fragment. Fragments never reach
 * the web server, reverse proxy or Cloudflare logs. We move it to session
 * storage and immediately remove it from the visible address. A visit through
 * the configured sign-in hostname has no key because Cloudflare Access
 * protects that hostname at the edge.
 */

import { API_BASE, ApiError, apiFetch } from '@/api/client'

const STORAGE_KEY = 'sentinel.viewer-key'
const VIEWER_KEY_HEADER = 'X-Sentinel-Viewer-Key'
const VIEWER_SUBPROTOCOL_PREFIX = 'sentinel.viewer.'

let memoryKey: string | null | undefined

function readSessionKey(): string | null {
  try {
    return window.sessionStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

function saveSessionKey(value: string): void {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, value)
  } catch {
    // Private browsing may disable storage. The in-memory copy still works for
    // the lifetime of this page.
  }
}

export function getViewerKey(): string | null {
  if (memoryKey !== undefined) return memoryKey

  const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ''))
  const supplied = fragment.get('viewer_key')?.trim() ?? ''
  if (supplied) {
    memoryKey = supplied
    saveSessionKey(supplied)
    window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`)
    return memoryKey
  }

  memoryKey = readSessionKey()
  return memoryKey
}

export async function createViewerSession(): Promise<void> {
  const key = getViewerKey()
  if (!key) return

  // text/plain is CORS-safelisted. This avoids a Cloudflare Access preflight
  // while keeping the secret out of URLs and proxy access logs.
  await apiFetch<void>('/viewer/session', {
    method: 'POST',
    rawBody: key,
    headers: { 'Content-Type': 'text/plain;charset=UTF-8' },
  })
}

export function viewerAuthHeaders(initial?: HeadersInit): Record<string, string> {
  const headers = new Headers(initial)
  const key = getViewerKey()
  if (key) headers.set(VIEWER_KEY_HEADER, key)
  return Object.fromEntries(headers.entries())
}

/** Viewer-only JSON client that does not rely on third-party cookie support. */
export const viewerApi = {
  get: <T>(path: string) =>
    apiFetch<T>(path, { method: 'GET', headers: viewerAuthHeaders() }),
  post: <T>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: 'POST', body, headers: viewerAuthHeaders() }),
}

function encodeBase64Url(value: string): string {
  const bytes = new TextEncoder().encode(value)
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return window.btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/**
 * Authenticate a browser WebSocket without putting the viewer key in its URL.
 * The server selects this exact protocol only after validating the key.
 */
export function viewerWebsocketProtocols(): string[] | undefined {
  const key = getViewerKey()
  return key ? [`${VIEWER_SUBPROTOCOL_PREFIX}${encodeBase64Url(key)}`] : undefined
}

function downloadFilename(contentDisposition: string | null, fallback: string): string {
  const encoded = contentDisposition?.match(/filename\*=UTF-8''([^;]+)/i)?.[1]
  if (encoded) {
    try {
      return decodeURIComponent(encoded)
    } catch {
      // Fall through to a conservative local filename.
    }
  }
  const plain = contentDisposition?.match(/filename="?([^";]+)"?/i)?.[1]
  return plain?.trim() || fallback
}

export async function downloadViewerExport(id: string): Promise<void> {
  const response = await fetch(`${API_BASE}/viewer/exports/${encodeURIComponent(id)}/download`, {
    credentials: 'same-origin',
    headers: viewerAuthHeaders(),
  })
  if (!response.ok) {
    throw new ApiError(response.status, `Download failed with status ${response.status}.`)
  }

  const blobUrl = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = blobUrl
  link.download = downloadFilename(response.headers.get('content-disposition'), `sentinel-${id}.mp4`)
  document.body.append(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(blobUrl), 0)
}

export function viewerWebsocketPath(cameraId: string, sub: boolean): string {
  const params = new URLSearchParams()
  if (sub) params.set('sub', 'true')
  const query = params.toString()
  return `/api/v1/streams/${cameraId}/viewer-ws${query ? `?${query}` : ''}`
}
