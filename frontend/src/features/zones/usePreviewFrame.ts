/**
 * The live frame the canvas draws over.
 *
 * Fetched as a blob rather than pointed to directly from an `<img src>`: a
 * failure here (camera unreachable, no stream configured, go2rtc down) comes
 * back from `GET /cameras/{id}/motion-preview` as JSON with a `detail`
 * explaining why (see `app/motion/router.py`), and the only way to read that
 * is to fetch it ourselves -- an `<img>` tag just fires a content-less
 * `onError` with no message to show the operator.
 */

import { useQuery } from '@tanstack/react-query'
import { useEffect } from 'react'

import { ApiError, NetworkError } from '@/api/client'
import { motionPreviewUrl } from '@/features/zones/api'

async function loadPreview(cameraId: string): Promise<string> {
  let response: Response
  try {
    response = await fetch(motionPreviewUrl(cameraId), { credentials: 'same-origin' })
  } catch {
    throw new NetworkError()
  }

  if (!response.ok) {
    const contentType = response.headers.get('content-type') ?? ''
    const payload: unknown = contentType.includes('application/json')
      ? await response.json().catch(() => null)
      : null
    const detail =
      payload && typeof payload === 'object' && 'detail' in payload
        ? String(payload.detail)
        : `Request failed with status ${response.status}.`
    throw new ApiError(response.status, detail, payload)
  }

  const blob = await response.blob()
  return URL.createObjectURL(blob)
}

export function usePreviewFrame(cameraId: string) {
  const query = useQuery({
    queryKey: ['zones', 'preview', cameraId],
    queryFn: () => loadPreview(cameraId),
    enabled: Boolean(cameraId),
    retry: false,
    // A stale frame is actively misleading once it might have scrolled off
    // screen for a while -- never serve a cached one across remounts.
    staleTime: 0,
    gcTime: 0,
  })

  // Object URLs are not garbage-collected on their own. Revoking the one this
  // render held, exactly when it stops being current (a fresh frame replaced
  // it, or the editor for this camera unmounted), is what keeps a session of
  // "Refresh frame" clicks from leaking a blob per click.
  useEffect(() => {
    const url = query.data
    if (!url) return
    return () => URL.revokeObjectURL(url)
  }, [query.data])

  return query
}
