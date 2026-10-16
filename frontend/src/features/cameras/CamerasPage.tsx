import { EyeOff, Plus, Radar, Video } from 'lucide-react'
import { useState } from 'react'

import { PageHeader } from '@/components/layout/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { FailureNotice, RecordersMissingNotice } from '@/components/system/StateNotice'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { AddCameraDialog } from '@/features/cameras/AddCameraDialog'
import { CameraCard } from '@/features/cameras/CameraCard'
import { DiscoverCameraDialog } from '@/features/cameras/DiscoverCameraDialog'
import { useCameras } from '@/features/cameras/useCameras'
import { usePrivacyMode } from '@/features/privacy/usePrivacyMode'

export function CamerasPage() {
  const cameras = useCameras()
  const privacy = usePrivacyMode()
  const [addOpen, setAddOpen] = useState(false)
  const [discoverOpen, setDiscoverOpen] = useState(false)

  const privacyMessage = privacy.data?.enabled ? privacy.data.message : null
  const paused = privacy.data?.enabled === true
  // Only cameras that have a continuous recorder at all. Counting the battery
  // cameras made the total one a healthy installation could never reach.
  const recordable = cameras.data?.filter((camera) => camera.recording_state !== 'unsupported')
  const recording = recordable?.filter((camera) => camera.recording_state === 'recording').length
  const faulty = cameras.data?.filter((camera) => camera.recording_state === 'failed').length ?? 0

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <PageHeader
        title="Cameras"
        description={
          recordable && recordable.length > 0
            ? `${recording} of ${recordable.length} recording.`
            : 'Continuous RTSP sources and event-based local battery cameras.'
        }
        actions={
          <>
            <Button onClick={() => setDiscoverOpen(true)} disabled={paused}>
              <Radar aria-hidden="true" />
              Discover cameras
            </Button>
            <Button variant="secondary" onClick={() => setAddOpen(true)}>
              <Plus aria-hidden="true" />
              Add manually
            </Button>
          </>
        }
      />

      {privacyMessage ? (
        <Alert variant="warning" title="Privacy mode is on" icon={<EyeOff />}>
          {privacyMessage} Camera controls are unavailable until it is switched off. Recordings
          already on disk remain available, and nothing is deleted.
        </Alert>
      ) : null}

      {/* A paused camera is not a failing camera, so this stays quiet while
          privacy mode is on rather than reporting every camera as broken. */}
      {!privacyMessage ? <RecordersMissingNotice count={faulty} /> : null}

      {cameras.isError ? (
        <FailureNotice error={cameras.error} action="loading the camera list" />
      ) : null}

      {cameras.isPending ? (
        <div className="space-y-3">
          {Array.from({ length: 3 }, (_, index) => (
            <Skeleton key={index} className="h-32 rounded-(--radius-card)" />
          ))}
        </div>
      ) : cameras.data && cameras.data.length > 0 ? (
        <div className="space-y-3">
          {cameras.data.map((camera) => (
            <CameraCard key={camera.id} camera={camera} privacyMessage={privacyMessage} />
          ))}
        </div>
      ) : (
        <EmptyState
          icon={Video}
          title="No cameras yet"
          description="Add your first RTSP camera to start recording, or scan the local network for cameras that answer ONVIF."
          action={
            <div className="flex flex-wrap justify-center gap-2">
              <Button onClick={() => setDiscoverOpen(true)}>
                <Radar aria-hidden="true" />
                Discover cameras
              </Button>
              <Button variant="secondary" onClick={() => setAddOpen(true)}>
                <Plus aria-hidden="true" />
                Add manually
              </Button>
            </div>
          }
        />
      )}

      <AddCameraDialog open={addOpen} onOpenChange={setAddOpen} />
      <DiscoverCameraDialog open={discoverOpen} onOpenChange={setDiscoverOpen} />
    </div>
  )
}
