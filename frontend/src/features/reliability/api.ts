import { api } from '@/api/client'
import type { CameraRecoveryResult, IncidentList, ReliabilityResponse } from './types'

/** How many rows the cross-camera history section asks for. */
export const RECENT_INCIDENT_LIMIT = 20

export const reliabilityApi = {
  cameras: () => api.get<ReliabilityResponse>('/reliability/cameras'),
  incidents: (limit: number = RECENT_INCIDENT_LIMIT) =>
    api.get<IncidentList>(`/reliability/incidents?limit=${limit}`),
  retry: (cameraId: string) =>
    api.post<CameraRecoveryResult>(
      `/reliability/cameras/${encodeURIComponent(cameraId)}/retry`,
      undefined,
      // Restarting a recorder means stopping an FFmpeg process with a SIGTERM
      // grace period, which can outlast the client's default ceiling.
      { timeoutMs: 30_000 },
    ),
}

export const reliabilityKeys = {
  cameras: ['reliability', 'cameras'] as const,
  incidents: ['reliability', 'incidents'] as const,
}
