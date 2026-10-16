import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { exportsApi, playbackApi, queryKeys, timelineApi } from '@/features/timeline/api'
import { isExportActive, type ExportCreate } from '@/features/timeline/types'

export function useTimeline(cameraId: string, start: Date, end: Date, viewer = false) {
  return useQuery({
    queryKey: queryKeys.timeline(cameraId, start, end, viewer),
    queryFn: () => timelineApi.get(cameraId, start, end, viewer),
    enabled: Boolean(cameraId),
    // The timeline is a view of what has already happened; a stretch that
    // is already loaded does not need refetching every render, only when
    // the visible window itself changes.
    staleTime: 15_000,
  })
}

export function usePlaybackManifest(
  cameraId: string,
  start: Date | null,
  end: Date | null,
  viewer = false,
) {
  return useQuery({
    queryKey:
      start && end
        ? queryKeys.manifest(cameraId, start, end, viewer)
        : (['playback-manifest', 'idle', { viewer }] as const),
    queryFn: () => playbackApi.manifest(cameraId, start as Date, end as Date, viewer),
    enabled: Boolean(cameraId) && start !== null && end !== null,
  })
}

export function useExports(cameraId?: string, viewer = false) {
  return useQuery({
    queryKey: queryKeys.exports(cameraId, viewer),
    queryFn: () => exportsApi.list(cameraId, viewer),
    enabled: !viewer || Boolean(cameraId),
    refetchInterval: (query) => {
      const jobs = query.state.data?.exports ?? []
      return jobs.some(isExportActive) ? 2_000 : false
    },
  })
}

export function useCreateExport(viewer = false) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: ExportCreate) => exportsApi.create(payload, viewer),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['exports'] })
    },
  })
}

export function useDeleteExport() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => exportsApi.remove(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['exports'] })
    },
  })
}

export function useUpdateExport() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, protectedClip }: { id: string; protectedClip: boolean }) =>
      exportsApi.update(id, protectedClip),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['exports'] })
    },
  })
}
