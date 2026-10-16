import { useMutation, useQueryClient, type QueryClient } from '@tanstack/react-query'

import { camerasApi, queryKeys } from '@/api/endpoints'
import type { Camera, CameraIdentity, CameraRecordingResult } from '@/types/api'

/**
 * Apply a change to one camera across every cached list that holds it.
 *
 * `useCameras` keys its query as `['cameras', { enabledOnly }]`, so writing to
 * `['cameras']` alone updates nothing the page is actually rendering — the
 * change would land on the server and the row would keep showing the old state
 * until something else refetched. `setQueriesData` matches by prefix, which is
 * what reaches those keys; the invalidation afterwards is what reconciles
 * anything this did not anticipate.
 */
function patchCameraEverywhere(
  queryClient: QueryClient,
  cameraId: string,
  apply: (camera: Camera) => Camera,
): void {
  queryClient.setQueriesData<Camera[]>({ queryKey: queryKeys.cameras }, (cameras) =>
    Array.isArray(cameras)
      ? cameras.map((camera) => (camera.id === cameraId ? apply(camera) : camera))
      : cameras,
  )
  queryClient.setQueryData<Camera>(queryKeys.camera(cameraId), (camera) =>
    camera ? apply(camera) : camera,
  )
  void queryClient.invalidateQueries({ queryKey: queryKeys.cameras })
}

/**
 * Turn one camera's recorder on or off.
 *
 * The response carries the *observed* recorder state, not an echo of the
 * request, so the cache is updated from what the server saw rather than from
 * what the click asked for. A start that failed therefore shows as failed
 * within one round trip instead of leaving an optimistic switch turned on.
 */
export function useSetCameraRecording(cameraId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ enabled, confirm }: { enabled: boolean; confirm?: boolean }) =>
      camerasApi.setRecording(cameraId, enabled, confirm ?? false),
    onSuccess: (result: CameraRecordingResult) => {
      patchCameraEverywhere(queryClient, cameraId, (camera) => ({
        ...camera,
        recording_enabled: result.recording_enabled,
        recording_state: result.state,
      }))
      // The recorder count on the dashboard moved with it.
      void queryClient.invalidateQueries({ queryKey: queryKeys.systemHealth })
      void queryClient.invalidateQueries({ queryKey: queryKeys.recorderStatus })
    },
  })
}

/**
 * Ask the camera over ONVIF which device it is.
 *
 * Read-only on the camera, and the thing that lets Sentinel decide for itself
 * whether the Tapo controls apply instead of asking the operator to type a
 * brand name.
 */
export function useIdentifyCamera(cameraId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => camerasApi.identify(cameraId),
    onSuccess: (result: CameraIdentity) => {
      patchCameraEverywhere(queryClient, cameraId, (camera) => ({
        ...camera,
        manufacturer: result.manufacturer,
        model: result.model,
        tapo_controls: result.tapo_controls,
      }))
    },
  })
}
