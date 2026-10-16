import { BatteryCharging, Plus, Radio, Video } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { PageHeader } from '@/components/layout/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Segmented } from '@/components/ui/segmented'
import { TapoBatteryDialog } from '@/features/cameras/TapoBatteryDialog'
import { useCameras } from '@/features/cameras/useCameras'
import { CameraTile, CameraTileSkeleton } from '@/features/live/CameraTile'
import { columnsFor, LAYOUT_OPTIONS, type Layout } from '@/features/live/layout'
import { VideoPlayer } from '@/features/live/VideoPlayer'
import { usePrivacyMode } from '@/features/privacy/usePrivacyMode'
import { cn } from '@/lib/utils'
import type { Camera } from '@/types/api'

/** What the caption says under a feed, when the resolution is not yet known. */
function streamLabel(camera: Camera): string {
  if (camera.stream.width && camera.stream.height) {
    return `${camera.stream.width}×${camera.stream.height}`
  }
  return camera.status === 'online' ? 'Resolution pending' : STATUS_TEXT[camera.status]
}

const STATUS_TEXT: Record<Camera['status'], string> = {
  online: 'Online',
  offline: 'Offline',
  connecting: 'Connecting',
  error: 'Not reachable',
  disabled: 'Disabled',
}

export function LivePage() {
  const cameras = useCameras(true)
  const privacy = usePrivacyMode()
  const [layout, setLayout] = useState<Layout>('auto')
  const [batteryCamera, setBatteryCamera] = useState<Camera | null>(null)

  const list = cameras.data ?? []
  const rtspCount = list.filter((camera) => camera.kind === 'rtsp').length
  const columns = columnsFor(layout, list.length)
  const paused = privacy.data?.enabled === true
  // A pause with no stored reason must still pause. Reading the message
  // directly meant a null reason produced a falsy `privacyMessage`, and the
  // player connected to a camera the operator had just switched off.
  const privacyMessage = paused
    ? (privacy.data?.message ?? 'Cameras are paused by privacy mode.')
    : null

  // A wall of cameras uses each one's substream: nine 2K decodes in one tab
  // would melt the browser, and at grid size nobody can see the difference.
  const useSubstream = layout !== '1' && rtspCount > 1

  return (
    <div className="mx-auto max-w-[110rem] space-y-5">
      <PageHeader
        title="Live"
        description="Real-time view of every enabled camera."
        aside={
          list.length > 1 ? (
            <Segmented
              ariaLabel="Grid layout"
              value={layout}
              onChange={setLayout}
              options={LAYOUT_OPTIONS}
            />
          ) : null
        }
      />

      {cameras.isError ? (
        <Alert variant="danger" title="Could not load the camera list">
          Live view needs the Sentinel server. Recording continues on the server regardless of
          this page, and it retries automatically.
        </Alert>
      ) : null}

      {cameras.isPending ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 3 }, (_, index) => (
            <CameraTileSkeleton key={index} />
          ))}
        </div>
      ) : list.length > 0 ? (
        <div className={cn('grid gap-3', columns)}>
          {list.map((camera) =>
            camera.kind === 'tapo_battery' ? (
              <CameraTile key={camera.id} name={camera.name} meta="Tapo battery · on demand">
                <button
                  type="button"
                  onClick={() => setBatteryCamera(camera)}
                  disabled={paused}
                  className={cn(
                    'absolute inset-0 flex flex-col items-center justify-center gap-3 px-6',
                    'bg-(--color-surface) text-center transition-colors',
                    'hover:bg-(--color-surface-hover) disabled:cursor-not-allowed disabled:opacity-70',
                  )}
                >
                  <span className="flex size-11 items-center justify-center rounded-full bg-(--color-bg-subtle) text-(--color-fg-muted)">
                    <BatteryCharging className="size-5" aria-hidden="true" />
                  </span>
                  <span>
                    <span className="block text-sm font-medium">
                      {privacyMessage ?? 'Open Direct Stream'}
                    </span>
                    <span className="mt-1 flex items-center justify-center gap-1.5 text-xs text-(--color-fg-subtle)">
                      <Radio className="size-3.5" aria-hidden="true" />
                      No continuous connection
                    </span>
                  </span>
                </button>
              </CameraTile>
            ) : (
              <CameraTile key={camera.id} name={camera.name} meta={streamLabel(camera)}>
                <VideoPlayer
                  flush
                  cameraId={camera.id}
                  cameraName={camera.name}
                  sub={useSubstream && Boolean(camera.sub_stream_url)}
                  ptz={camera.ptz_supported === true}
                  ptzZoom={camera.ptz_zoom_supported}
                  privacyMessage={privacyMessage}
                />
              </CameraTile>
            ),
          )}
        </div>
      ) : (
        <EmptyState
          icon={Video}
          title="No enabled cameras"
          description="Add a camera, or enable one you have already added, to see it here."
          action={
            <Button asChild>
              <Link to="/admin/cameras">
                <Plus aria-hidden="true" />
                Add camera
              </Link>
            </Button>
          }
        />
      )}

      {batteryCamera ? (
        <TapoBatteryDialog
          camera={batteryCamera}
          open
          onOpenChange={(next) => {
            if (!next) setBatteryCamera(null)
          }}
        />
      ) : null}
    </div>
  )
}
