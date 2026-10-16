import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { camerasApi, queryKeys } from '@/api/endpoints'
import type { TimedRecordingStatus } from '@/types/api'

/**
 * What durations this installation allows, and the charge before confirming.
 *
 * This endpoint is configuration-only. Battery telemetry is fetched once by
 * the parent dialog and passed into the panel, avoiding concurrent LAN probes.
 */
export function useTimedRecordingOptions(cameraId: string, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.cameraTimedRecordingOptions(cameraId),
    queryFn: () => camerasApi.getTimedRecordingOptions(cameraId),
    enabled: enabled && Boolean(cameraId),
    staleTime: 60_000,
    retry: false,
  })
}

/**
 * Progress of a running session.
 *
 * Polled once a second while one is active and not at all otherwise. The
 * endpoint answers from memory and never touches the camera, which is what
 * makes a one-second poll acceptable against a device on a battery.
 */
export function useTimedRecording(cameraId: string, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.cameraTimedRecording(cameraId),
    queryFn: () => camerasApi.getTimedRecording(cameraId),
    enabled: enabled && Boolean(cameraId),
    refetchInterval: (query) => {
      const value = query.state.data
      if (!value || value.finished) return false
      return 1_000
    },
    staleTime: 0,
    retry: 2,
  })
}

export function useStartTimedRecording(cameraId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (minutes: number) => camerasApi.startTimedRecording(cameraId, minutes),
    onSuccess: (value: TimedRecordingStatus) => {
      queryClient.setQueryData(queryKeys.cameraTimedRecording(cameraId), value)
    },
  })
}

export function useStopTimedRecording(cameraId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => camerasApi.stopTimedRecording(cameraId),
    onSuccess: async (value: TimedRecordingStatus) => {
      queryClient.setQueryData(queryKeys.cameraTimedRecording(cameraId), value)
      // The clip is in the timeline now, so anything showing footage is stale.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.recordings }),
        queryClient.invalidateQueries({ queryKey: ['timeline'] }),
      ])
    },
  })
}
