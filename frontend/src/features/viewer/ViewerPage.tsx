import { BatteryCharging, Video } from 'lucide-react'
import { useState } from 'react'

import { ApiError } from '@/api/client'
import { EmptyState } from '@/components/EmptyState'
import { Alert } from '@/components/ui/alert'
import { Segmented } from '@/components/ui/segmented'
import { CameraTile, CameraTileSkeleton } from '@/features/live/CameraTile'
import { columnsFor, LAYOUT_OPTIONS, type Layout } from '@/features/live/layout'
import { VideoPlayer } from '@/features/live/VideoPlayer'
import { ViewerNav } from '@/features/viewer/ViewerNav'
import { ViewerSignIn } from '@/features/viewer/ViewerSignIn'
import { ViewerSystemStrip } from '@/features/viewer/ViewerSystemStrip'
import { useViewerCameras } from '@/features/viewer/useViewerCameras'
import { useViewerSystemStatus } from '@/features/viewer/useViewerSystemStatus'
import { cn } from '@/lib/utils'

export function ViewerPage() {
  const cameras = useViewerCameras()
  const system = useViewerSystemStatus()
  const [layout, setLayout] = useState<Layout>('auto')
  const list = cameras.data ?? []
  const rtspCount = list.filter((camera) => camera.kind === 'rtsp').length
  const columns = columnsFor(layout, list.length)
  const useSubstream = layout !== '1' && rtspCount > 1
  const denied = cameras.error instanceof ApiError && cameras.error.isUnauthorized
  const privacyMessage = system.data?.privacy_enabled
    ? (system.data.privacy_message ?? 'Cameras are paused by privacy mode.')
    : null

  return (
    <div className="min-h-dvh overflow-x-hidden bg-(--color-bg)">
      <ViewerNav current="live" />

      <main className="mx-auto max-w-[110rem] space-y-5 px-4 py-5 pb-[max(env(safe-area-inset-bottom,0px),1.5rem)] sm:px-6 lg:px-8">
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
          <div className="min-w-0">
            <h1 className="text-xl font-semibold tracking-[-0.02em] sm:text-2xl">Live cameras</h1>
            <p className="mt-1 text-sm leading-5 text-(--color-fg-muted)">
              View and export footage here. Configuration requires the administrator login.
            </p>
          </div>

          {list.length > 1 ? (
            <Segmented
              ariaLabel="Grid layout"
              value={layout}
              onChange={setLayout}
              options={LAYOUT_OPTIONS}
            />
          ) : null}
        </div>

        {!cameras.isError ? <ViewerSystemStrip /> : null}

        {privacyMessage ? (
          <Alert variant="warning" title="All cameras are paused for privacy">
            {privacyMessage} Recorded footage from before the pause is still available under
            Playback.
          </Alert>
        ) : null}

        {denied ? <ViewerSignIn /> : null}
        {cameras.isError && !denied ? (
          <Alert variant="danger" title="Could not load cameras">
            The recorder continues running on the server.
          </Alert>
        ) : null}

        {denied ? null : cameras.isPending ? (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {Array.from({ length: 3 }, (_, index) => (
              <CameraTileSkeleton key={index} />
            ))}
          </div>
        ) : list.length > 0 ? (
          <div className={cn('grid gap-3', columns)}>
            {list.map((camera) =>
              camera.kind === 'tapo_battery' ? (
                <CameraTile key={camera.id} name={camera.name} meta="Tapo battery · event clips">
                  <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-(--color-surface) px-6 text-center">
                    <span className="flex size-11 items-center justify-center rounded-full bg-(--color-bg-subtle) text-(--color-fg-muted)">
                      <BatteryCharging className="size-5" aria-hidden="true" />
                    </span>
                    <div>
                      <p className="text-sm font-medium">Event-based battery camera</p>
                      <p className="mt-1 text-xs leading-4 text-(--color-fg-subtle)">
                        Recorded events are under Playback. On-demand live requires an
                        administrator.
                      </p>
                    </div>
                  </div>
                </CameraTile>
              ) : (
                <CameraTile
                  key={camera.id}
                  name={camera.name}
                  meta={
                    camera.stream.width && camera.stream.height
                      ? `${camera.stream.width}×${camera.stream.height}`
                      : camera.status
                  }
                >
                  <VideoPlayer
                    flush
                    cameraId={camera.id}
                    cameraName={camera.name}
                    sub={useSubstream && camera.has_substream}
                    viewer
                    ptz={camera.ptz_supported}
                    ptzZoom={camera.ptz_zoom_supported}
                    privacyMessage={privacyMessage}
                  />
                </CameraTile>
              ),
            )}
          </div>
        ) : !cameras.isError ? (
          <EmptyState
            icon={Video}
            title="No enabled cameras"
            description="An administrator can add or enable cameras in Sentinel settings."
          />
        ) : null}
      </main>
    </div>
  )
}
