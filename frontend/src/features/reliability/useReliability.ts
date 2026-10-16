import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { reliabilityApi, reliabilityKeys } from '@/features/reliability/api'

/** The backend evaluates every 30 s, so polling faster only repeats an answer. */
const REFRESH_MS = 20_000

export function useCameraReliability() {
  return useQuery({
    queryKey: reliabilityKeys.cameras,
    queryFn: reliabilityApi.cameras,
    refetchInterval: REFRESH_MS,
  })
}

export function useRecentIncidents() {
  return useQuery({
    queryKey: reliabilityKeys.incidents,
    queryFn: () => reliabilityApi.incidents(),
    refetchInterval: REFRESH_MS,
  })
}

export function useRetryCameraServices() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (cameraId: string) => reliabilityApi.retry(cameraId),
    onSettled: async () => {
      // Refetched whether or not the request succeeded: a refused retry still
      // means the page should show the current state rather than a stale one.
      await queryClient.invalidateQueries({ queryKey: reliabilityKeys.cameras })
    },
  })
}
