import { Plus, Radar } from 'lucide-react'
import { useState } from 'react'

import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Status } from '@/components/ui/status'
import { AddCameraDialog } from '@/features/cameras/AddCameraDialog'
import { STATUS_LABEL, STATUS_TONE } from '@/features/cameras/cameraKind'
import { DiscoverCameraDialog } from '@/features/cameras/DiscoverCameraDialog'
import { useCameras } from '@/features/cameras/useCameras'

/** The same two ways in as the Cameras page, with the cameras added so far. */
export function CamerasStep() {
  const cameras = useCameras()
  const [discoverOpen, setDiscoverOpen] = useState(false)
  const [addOpen, setAddOpen] = useState(false)
  const list = cameras.data ?? []

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => setDiscoverOpen(true)}>
          <Radar aria-hidden="true" />
          Find cameras on the network
        </Button>
        <Button variant="secondary" onClick={() => setAddOpen(true)}>
          <Plus aria-hidden="true" />
          Add by RTSP address
        </Button>
      </div>

      {cameras.isPending ? (
        <Skeleton className="h-16" />
      ) : cameras.isError ? (
        <Alert variant="danger">Could not load the cameras.</Alert>
      ) : list.length > 0 ? (
        <ul
          aria-label="Cameras added"
          className="divide-y divide-(--color-border) rounded-lg border border-(--color-border)"
        >
          {list.map((camera) => (
            <li key={camera.id} className="flex items-center justify-between gap-3 px-4 py-3">
              <span className="min-w-0 truncate text-sm font-medium">{camera.name}</span>
              <Status tone={STATUS_TONE[camera.status]}>{STATUS_LABEL[camera.status]}</Status>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-(--color-fg-muted)">
          No cameras yet. Add at least one to continue.
        </p>
      )}

      <DiscoverCameraDialog open={discoverOpen} onOpenChange={setDiscoverOpen} />
      <AddCameraDialog open={addOpen} onOpenChange={setAddOpen} />
    </div>
  )
}
