import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  fetchMotionWorkerStatus,
  fetchMotionZones,
  fetchRecentMotionEvents,
  saveMotionZones,
} from '@/features/zones/api'
import type { WireZone } from '@/features/zones/types'
import { sanitizeServerZones, type SanitizedZones } from '@/features/zones/zoneMath'

const zonesQueryKey = (cameraId: string) => ['zones', 'motion-zones', cameraId] as const

export function useMotionZones(cameraId: string) {
  return useQuery<SanitizedZones>({
    queryKey: zonesQueryKey(cameraId),
    queryFn: async () => {
      const response = await fetchMotionZones(cameraId)
      return sanitizeServerZones(response.zones)
    },
    enabled: Boolean(cameraId),
  })
}

export function useSaveMotionZones(cameraId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (zones: WireZone[]) => saveMotionZones(cameraId, zones),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: zonesQueryKey(cameraId) })
      // Saving restarts the camera's motion worker (it holds masks compiled
      // from the old zones), so the status the tuning panel shows is about to
      // change too.
      await queryClient.invalidateQueries({ queryKey: ['zones', 'worker-status'] })
    },
  })
}

/** Polled rather than pushed: phase 9's WebSocket event stream does not exist
 * yet (see PROJECT_STATUS.md), so this is the closest thing to "live" the
 * current API allows. Every worker's status comes back in one call; this
 * feature only ever displays the one row for the camera being edited. */
export function useMotionWorkerStatus() {
  return useQuery({
    queryKey: ['zones', 'worker-status'],
    queryFn: fetchMotionWorkerStatus,
    refetchInterval: 3_000,
  })
}

/** Recent events for one camera, so tuning a zone can be checked against real
 * detections instead of trusting the shape of the polygon alone. */
export function useRecentMotionEvents(cameraId: string, limit = 5) {
  return useQuery({
    queryKey: ['zones', 'recent-events', cameraId, limit],
    queryFn: () => fetchRecentMotionEvents(cameraId, limit),
    enabled: Boolean(cameraId),
    refetchInterval: 15_000,
  })
}
