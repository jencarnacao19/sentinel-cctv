import { useQuery } from '@tanstack/react-query'

import type { ViewerSystemStatus } from '@/features/storage/types'
import { viewerApi } from '@/features/viewer/access'

export function useViewerSystemStatus() {
  return useQuery({
    queryKey: ['viewer', 'system-status'],
    queryFn: () => viewerApi.get<ViewerSystemStatus>('/viewer/system/status'),
    staleTime: 3_000,
    refetchInterval: 5_000,
  })
}
