/**
 * Direct calls to endpoints `frontend/src/api/endpoints.ts` does not (yet)
 * cover. That file is the orchestrator's; motion zones, motion status and
 * events are all real, already-shipped backend endpoints (see
 * `backend/app/motion/router.py`) this feature is free to call directly with
 * the shared low-level client, the same way `EventsPage.tsx` and
 * `SettingsPage.tsx` already do for endpoints outside their owning feature's
 * slice of `endpoints.ts`.
 */

import { api, API_BASE, ApiError, NetworkError } from '@/api/client'
import type {
  MotionEventListResponse,
  MotionWorkerStatusResponse,
} from '@/features/zones/types'

interface RawMotionZonesResponse {
  camera_id: string
  zones: unknown
}

export function fetchMotionZones(cameraId: string): Promise<RawMotionZonesResponse> {
  return api.get<RawMotionZonesResponse>(`/cameras/${cameraId}/motion-zones`)
}

/**
 * `PUT` is the one verb the shared client does not expose (it offers
 * get/post/patch/delete). Built the same way `SettingsPage`'s backup/restore
 * calls are: a direct `fetch` carrying the session cookie, translating a
 * non-2xx response into the same `ApiError`/`NetworkError` types the rest of
 * the app already knows how to render.
 */
export async function saveMotionZones(
  cameraId: string,
  zones: [number, number][][],
): Promise<RawMotionZonesResponse> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}/cameras/${cameraId}/motion-zones`, {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ zones }),
    })
  } catch {
    throw new NetworkError()
  }

  const contentType = response.headers.get('content-type') ?? ''
  const payload: unknown = contentType.includes('application/json')
    ? await response.json().catch(() => null)
    : null

  if (!response.ok) {
    const detail =
      payload && typeof payload === 'object' && 'detail' in payload
        ? String(payload.detail)
        : `Request failed with status ${response.status}.`
    throw new ApiError(response.status, detail, payload)
  }
  return payload as RawMotionZonesResponse
}

/** A frame to draw over. `t` busts the browser cache, matching
 * `camerasApi.snapshotUrl`'s reasoning: a stale cached frame from before a
 * lighting change is actively misleading to draw zones against. */
export function motionPreviewUrl(cameraId: string): string {
  return `${API_BASE}/cameras/${cameraId}/motion-preview?t=${Date.now()}`
}

export function fetchMotionWorkerStatus(): Promise<MotionWorkerStatusResponse> {
  return api.get<MotionWorkerStatusResponse>('/events/status')
}

export function fetchRecentMotionEvents(cameraId: string, limit: number): Promise<MotionEventListResponse> {
  return api.get<MotionEventListResponse>(`/events?camera_id=${cameraId}&limit=${limit}`)
}
