import { Loader2 } from 'lucide-react'
import { useEffect, useId, useState } from 'react'

import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Switch } from '@/components/ui/switch'
import { useSetCameraRecording } from '@/features/cameras/useCameraRecording'
import { cn } from '@/lib/utils'
import type { Camera, RecordingRuntimeState } from '@/types/api'
import { errorText } from '@/lib/errors'

const FAILED = 'The recording change could not be applied.'

interface Props {
  camera: Camera
  disabled?: boolean
  disabledReason?: string | null
}

/**
 * The one control that belongs on the row rather than behind Configure.
 *
 * Recording is the thing a surveillance system is for, and "is this camera
 * recording?" was previously answerable only by opening a dialog and reading a
 * dropdown. The state shown here is the recorder's, not the setting's: a camera
 * whose recorder failed to start reads *Failed*, in the danger colour, while
 * its setting is still on.
 */
export function RecordingToggle({ camera, disabled = false, disabledReason }: Props) {
  const mutation = useSetCameraRecording(camera.id)
  const [confirming, setConfirming] = useState(false)
  const labelId = useId()

  // A failure from a previous click must not outlive the row it happened in.
  useEffect(() => {
    mutation.reset()
    // Only when the camera changes identity, not on every render of the same one.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camera.id])

  const pending = mutation.isPending
  const state: RecordingRuntimeState = pending
    ? camera.recording_enabled
      ? 'off'
      : 'recording'
    : camera.recording_state
  const presentation = pending
    ? camera.recording_enabled
      ? PENDING.stopping
      : PENDING.starting
    : STATES[state]

  function change(next: boolean) {
    if (pending || disabled) return
    mutation.reset()
    if (!next) {
      setConfirming(true)
      return
    }
    mutation.mutate({ enabled: true })
  }

  function confirmStop() {
    setConfirming(false)
    mutation.reset()
    mutation.mutate({ enabled: false, confirm: true })
  }

  const failure = errorText(mutation.error, FAILED)

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2.5">
        <Switch
          checked={camera.recording_enabled}
          onCheckedChange={change}
          // Held during the round trip so a double click cannot queue a second,
          // contradictory change while the first is still being applied.
          disabled={disabled || pending}
          aria-labelledby={labelId}
          aria-describedby={`${labelId}-state`}
        />
        <span className="min-w-0">
          <span id={labelId} className="block text-sm font-medium">
            Recording
            {/* A dozen cameras with a dozen switches all named "Recording" is
                unusable with a screen reader. The camera name is added to the
                accessible name only. */}
            <span className="sr-only"> — {camera.name}</span>
          </span>
          <span
            id={`${labelId}-state`}
            className={cn('flex items-center gap-1 text-xs', presentation.className)}
          >
            {pending ? <Loader2 className="size-3 animate-spin" aria-hidden="true" /> : null}
            {disabled && disabledReason ? disabledReason : presentation.label}
          </span>
        </span>
      </div>

      {failure ? (
        <p role="alert" className="text-xs text-(--color-danger)">
          {failure}
        </p>
      ) : null}

      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Stop recording {camera.name}?</DialogTitle>
            <DialogDescription>
              Only this camera is affected. Live view, motion detection and every other camera
              carry on as they are.
            </DialogDescription>
          </DialogHeader>
          <DialogBody>
            <Alert variant="warning" title="Footage from the paused period is lost">
              Nothing is recorded for {camera.name} until you turn this back on, and that time
              cannot be recovered afterwards. Recordings already on disk are kept.
            </Alert>
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirming(false)}>
              Keep recording
            </Button>
            <Button variant="danger" onClick={confirmStop}>
              Stop recording
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

interface Presentation {
  label: string
  className: string
}

const STATES: Record<RecordingRuntimeState, Presentation> = {
  // Deliberately not just "Recording": the control is already labelled that, and
  // a state line that repeats its own label tells a screen-reader user nothing.
  // This says which of the two things is true -- the setting, and the process.
  recording: { label: 'On, recorder running', className: 'text-(--color-recording)' },
  off: { label: 'Off', className: 'text-(--color-fg-subtle)' },
  paused: { label: 'Paused by privacy mode', className: 'text-(--color-warning)' },
  failed: { label: 'Failed to start', className: 'text-(--color-danger)' },
  unsupported: { label: 'Not applicable', className: 'text-(--color-fg-subtle)' },
}

const PENDING: Record<'starting' | 'stopping', Presentation> = {
  starting: { label: 'Starting…', className: 'text-(--color-fg-muted)' },
  stopping: { label: 'Stopping…', className: 'text-(--color-fg-muted)' },
}
