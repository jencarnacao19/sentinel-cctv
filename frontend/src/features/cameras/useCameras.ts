import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { camerasApi, queryKeys } from '@/api/endpoints'
import type { CameraCreate, CameraUpdate } from '@/types/api'

export function useCameras(enabledOnly = false) {
  return useQuery({
    queryKey: [...queryKeys.cameras, { enabledOnly }],
    queryFn: () => camerasApi.list(enabledOnly),
  })
}

export function useCamera(id: string) {
  return useQuery({
    queryKey: queryKeys.camera(id),
    queryFn: () => camerasApi.get(id),
    enabled: Boolean(id),
  })
}

export function useCreateCamera() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: CameraCreate) => camerasApi.create(payload),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.cameras })
    },
  })
}

export function useUpdateCamera() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: CameraUpdate }) =>
      camerasApi.update(id, payload),
    onSuccess: async (camera) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.cameras })
      queryClient.setQueryData(queryKeys.camera(camera.id), camera)
    },
  })
}

export function useDeleteCamera() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => camerasApi.remove(id),
    onSuccess: async (_data, id) => {
      queryClient.removeQueries({ queryKey: queryKeys.camera(id) })
      await queryClient.invalidateQueries({ queryKey: queryKeys.cameras })
    },
  })
}

/** Probe a URL that has not been saved yet, from the add-camera wizard. */
export function useTestStream() {
  return useMutation({
    mutationFn: ({
      url,
      username,
      password,
    }: {
      url: string
      username?: string
      password?: string
    }) => camerasApi.testStream(url, username, password),
  })
}

/** Re-probe a saved camera. Refreshes the stored codec and status. */
export function useTestCamera() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => camerasApi.test(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.cameras })
    },
  })
}

/** Detect ONVIF PTZ once and persist the service/profile for instant controls. */
export function useDetectPtz() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => camerasApi.detectPtz(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.cameras })
    },
    onError: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.cameras })
    },
  })
}
