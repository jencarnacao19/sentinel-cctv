import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError } from '@/api/client'
import { authApi, queryKeys } from '@/api/endpoints'

/** Whether an administrator account exists. Decides setup wizard vs sign-in. */
export function useAuthStatus() {
  return useQuery({
    queryKey: queryKeys.authStatus,
    queryFn: authApi.status,
    staleTime: 30_000,
    retry: 1,
  })
}

/**
 * The signed-in user, or null.
 *
 * A 401 here is the expected "not signed in" answer, not a failure, so it
 * resolves to null instead of retrying and flooding the log.
 */
export function useCurrentUser() {
  return useQuery({
    queryKey: queryKeys.me,
    queryFn: async () => {
      try {
        return await authApi.me()
      } catch (error) {
        if (error instanceof ApiError && error.isUnauthorized) return null
        throw error
      }
    },
    staleTime: 60_000,
    retry: (failureCount, error) =>
      !(error instanceof ApiError && error.isUnauthorized) && failureCount < 2,
  })
}

export function useLogin() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ username, password }: { username: string; password: string }) =>
      authApi.login(username, password),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.me })
      await queryClient.invalidateQueries({ queryKey: queryKeys.systemInfo })
    },
  })
}

export function useLogout() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: authApi.logout,
    onSuccess: () => {
      // Drop every cached response: the next user must not see the previous
      // one's camera names or tokens.
      queryClient.clear()
    },
  })
}

export function useCreateAdmin() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ username, password }: { username: string; password: string }) =>
      authApi.createAdmin(username, password),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.authStatus })
    },
  })
}
