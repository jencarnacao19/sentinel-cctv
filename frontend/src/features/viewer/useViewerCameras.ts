import { useQuery } from '@tanstack/react-query'

import { createViewerSession, getViewerKey, viewerApi } from '@/features/viewer/access'
import type { ViewerCamera } from '@/types/api'

export function useViewerCameras() {
  return useQuery({
    queryKey: ['viewer', 'cameras'],
    queryFn: async () => {
      if (getViewerKey()) await createViewerSession()
      return viewerApi.get<ViewerCamera[]>('/viewer/cameras')
    },
    staleTime: 10_000,
    refetchInterval: 30_000,
  })
}
