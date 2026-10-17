import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { camerasApi, queryKeys } from '@/api/endpoints'
import type { TapoBatteryTestPayload } from '@/types/api'

export function useTestTapoBattery() {
  return useMutation({
    mutationFn: (payload: TapoBatteryTestPayload) => camerasApi.testTapoBattery(payload),
  })
}

export function useTapoBatteryStatus(cameraId: string, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.cameraTapoBatteryStatus(cameraId),
    queryFn: () => camerasApi.getTapoBatteryStatus(cameraId),
    enabled: enabled && Boolean(cameraId),
    staleTime: 30_000,
    retry: false,
  })
}

export function useSyncTapoBattery(cameraId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => camerasApi.syncTapoBatteryClips(cameraId),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.recordings }),
        queryClient.invalidateQueries({ queryKey: ['timeline'] }),
        queryClient.invalidateQueries({ queryKey: queryKeys.cameraTapoBatteryStatus(cameraId) }),
        queryClient.invalidateQueries({ queryKey: queryKeys.cameras }),
      ])
    },
  })
}
