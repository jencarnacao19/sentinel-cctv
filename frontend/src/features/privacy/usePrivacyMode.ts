import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { queryKeys, systemApi } from '@/api/endpoints'
import type { PrivacyReason } from '@/types/api'

export function usePrivacyMode() {
  return useQuery({
    queryKey: queryKeys.privacy,
    queryFn: systemApi.privacy,
    staleTime: 3_000,
    refetchInterval: 5_000,
  })
}

export function useSetPrivacyMode() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ enabled, reason }: { enabled: boolean; reason?: PrivacyReason }) =>
      systemApi.setPrivacy(enabled, reason),
    onSuccess: async (state) => {
      queryClient.setQueryData(queryKeys.privacy, state)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.systemHealth }),
        queryClient.invalidateQueries({ queryKey: queryKeys.cameras }),
        queryClient.invalidateQueries({ queryKey: ['viewer', 'system-status'] }),
      ])
    },
    onError: async () => {
      // Enabling is fail-safe: the backend may have activated privacy even if
      // one service reported a dirty stop. Always refresh the authoritative state.
      await queryClient.invalidateQueries({ queryKey: queryKeys.privacy })
    },
  })
}
