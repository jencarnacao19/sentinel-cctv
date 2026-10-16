import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { camerasApi, queryKeys } from '@/api/endpoints'
import type { TapoFeature, TapoFeaturePayload } from '@/types/api'

export function useTapoCapabilities(cameraId: string, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.cameraTapoCapabilities(cameraId),
    queryFn: () => camerasApi.getTapoCapabilities(cameraId),
    enabled,
    staleTime: 15 * 60_000,
    retry: false,
  })
}

export function useTapoState(cameraId: string, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.cameraTapoState(cameraId),
    queryFn: () => camerasApi.getTapoState(cameraId),
    enabled,
    staleTime: 5_000,
    retry: false,
  })
}

export function useRefreshTapo(cameraId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      const capabilities = await camerasApi.getTapoCapabilities(cameraId, true)
      // Capability discovery also fills the backend's short-lived state cache;
      // reuse it instead of forcing an immediate second getMost round trip.
      const state = await camerasApi.getTapoState(cameraId)
      return { capabilities, state }
    },
    onSuccess: ({ capabilities, state }) => {
      queryClient.setQueryData(queryKeys.cameraTapoCapabilities(cameraId), capabilities)
      queryClient.setQueryData(queryKeys.cameraTapoState(cameraId), state)
    },
  })
}

export function useSetTapoFeature(cameraId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ feature, payload }: { feature: TapoFeature; payload: TapoFeaturePayload }) =>
      camerasApi.setTapoFeature(cameraId, feature, payload),
    onSuccess: (result) => {
      if (result.state) queryClient.setQueryData(queryKeys.cameraTapoState(cameraId), result.state)
      else void queryClient.invalidateQueries({ queryKey: queryKeys.cameraTapoState(cameraId) })
    },
  })
}

export function useTapoSiren(cameraId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (active: boolean) => camerasApi.setTapoSiren(cameraId, active),
    onSuccess: (result) => {
      if (result.state) queryClient.setQueryData(queryKeys.cameraTapoState(cameraId), result.state)
      else void queryClient.invalidateQueries({ queryKey: queryKeys.cameraTapoState(cameraId) })
    },
  })
}

export function useRebootTapo(cameraId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => camerasApi.rebootTapo(cameraId),
    onSuccess: () => {
      queryClient.removeQueries({ queryKey: queryKeys.cameraTapoCapabilities(cameraId) })
      queryClient.removeQueries({ queryKey: queryKeys.cameraTapoState(cameraId) })
      void queryClient.invalidateQueries({ queryKey: queryKeys.cameras })
    },
  })
}
