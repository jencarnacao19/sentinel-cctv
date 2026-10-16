import { useMutation } from '@tanstack/react-query'

import { api } from '@/api/client'

export interface DiscoveredDevice {
  uuid: string | null
  endpoint: string
  xaddrs: string[]
  scopes: string[]
  manufacturer: string | null
  model: string | null
  name: string | null
}

export interface DiscoveryScan {
  devices: DiscoveredDevice[]
  interfaces_probed: string[]
}

export interface MediaProfile {
  token: string
  name: string | null
  encoding: string | null
  width: number | null
  height: number | null
  fps: number | null
  stream_uri: string
}

export const discoveryApi = {
  scan: () =>
    api.post<DiscoveryScan>('/discovery/scan', { timeout_seconds: 5 }, { timeoutMs: 20_000 }),
  profiles: (xaddr: string, username: string, password: string) =>
    api.post<{ profiles: MediaProfile[] }>(
      '/discovery/profiles',
      {
        xaddr,
        username: username || null,
        password: password || null,
        timeout_seconds: 12,
      },
      { timeoutMs: 40_000 },
    ),
}

export function useDiscoveryScan() {
  return useMutation({ mutationFn: discoveryApi.scan })
}

export function useOnvifProfiles() {
  return useMutation({
    mutationFn: ({
      xaddr,
      username,
      password,
    }: {
      xaddr: string
      username: string
      password: string
    }) => discoveryApi.profiles(xaddr, username, password),
  })
}
