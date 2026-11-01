import { api } from '@/api/client'
import type {
  ExportCreate,
  ExportJob,
  ExportList,
  PlaybackSession,
  Timeline,
} from '@/features/timeline/types'
import { viewerApi } from '@/features/viewer/access'

function rangeParams(start: Date, end: Date): URLSearchParams {
  return new URLSearchParams({ start: start.toISOString(), end: end.toISOString() })
}

export const timelineApi = {
  get: (cameraId: string, start: Date, end: Date, viewer = false) =>
    (viewer ? viewerApi : api).get<Timeline>(
      `/${viewer ? 'viewer/cameras' : 'cameras'}/${cameraId}/timeline?${rangeParams(start, end)}`,
    ),
}

export const playbackApi = {
  session: (cameraId: string, at: Date, viewer = false) =>
    (viewer ? viewerApi : api).post<PlaybackSession>(
      `/${viewer ? 'viewer/cameras' : 'cameras'}/${cameraId}/playback/session?${new URLSearchParams({ at: at.toISOString() })}`,
    ),
}

export const exportsApi = {
  list: (cameraId?: string, viewer = false) =>
    (viewer ? viewerApi : api).get<ExportList>(
      `/${viewer ? 'viewer/exports' : 'exports'}${cameraId ? `?camera_id=${encodeURIComponent(cameraId)}` : ''}`,
    ),
  get: (id: string, viewer = false) =>
    (viewer ? viewerApi : api).get<ExportJob>(`/${viewer ? 'viewer/exports' : 'exports'}/${id}`),
  create: (payload: ExportCreate, viewer = false) =>
    (viewer ? viewerApi : api).post<ExportJob>(`/${viewer ? 'viewer/exports' : 'exports'}`, payload),
  update: (id: string, protectedClip: boolean) =>
    api.patch<ExportJob>(`/exports/${id}`, { protected: protectedClip }),
  remove: (id: string) => api.delete<void>(`/exports/${id}`),
  downloadUrl: (id: string, viewer = false) =>
    `/api/v1/${viewer ? 'viewer/exports' : 'exports'}/${id}/download`,
}

/** Query keys, centralised so invalidation cannot drift from the fetchers. */
export const queryKeys = {
  timeline: (cameraId: string, start: Date, end: Date, viewer = false) =>
    ['timeline', { viewer }, cameraId, start.toISOString(), end.toISOString()] as const,
  exports: (cameraId?: string, viewer = false) =>
    ['exports', { viewer, cameraId: cameraId ?? null }] as const,
  export: (id: string) => ['exports', 'detail', id] as const,
}
