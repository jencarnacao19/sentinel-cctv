import {
  BatteryCharging,
  Fingerprint,
  Loader2,
  MapPinned,
  MoreHorizontal,
  Move,
  RefreshCw,
  Settings2,
  SlidersHorizontal,
  Trash2,
} from 'lucide-react'
import { useState } from 'react'

import { ApiError, NetworkError } from '@/api/client'
import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Status } from '@/components/ui/status'
import { describeKind, STATUS_LABEL, STATUS_TONE } from '@/features/cameras/cameraKind'
import { CameraThumbnail } from '@/features/cameras/CameraThumbnail'
import { EditCameraDialog } from '@/features/cameras/EditCameraDialog'
import { PtzSetupDialog } from '@/features/cameras/PtzSetupDialog'
import { RecordingToggle } from '@/features/cameras/RecordingToggle'
import { TapoBatteryConfigureDialog } from '@/features/cameras/TapoBatteryConfigureDialog'
import { TapoBatteryDialog } from '@/features/cameras/TapoBatteryDialog'
import { TapoLocalControlsDialog } from '@/features/cameras/TapoLocalControlsDialog'
import { useIdentifyCamera } from '@/features/cameras/useCameraRecording'
import { useDeleteCamera, useDetectPtz, useTestCamera } from '@/features/cameras/useCameras'
import type { Camera } from '@/types/api'

interface Props {
  camera: Camera
  privacyMessage?: string | null
}

/**
 * One camera.
 *
 * The layout is a deliberate three-way split: what the camera looks like, what
 * it is, and what you can do to it. Only the actions that get used on a normal
 * day stay as buttons -- recording, Configure, Test -- and everything
 * occasional moves into the overflow menu. Before this, every camera carried
 * six or seven equal-weight buttons, including a *Tapo controls* button on
 * Hikvision NVR channels, and on a phone they wrapped into three rows of
 * indistinguishable grey.
 *
 * The identity line answers the three questions an operator scans a list of
 * cameras for, in the order they ask them: is it up, what kind of thing is it,
 * and what can it do. Those used to be a coloured chip, a chain of dot-joined
 * fragments and three more chips of the same weight, so a camera that had
 * stopped responding looked much like one with PTZ.
 *
 * Brand-specific actions appear only when the camera's own metadata supports
 * them; a camera nothing identifies is offered the explicit opt-in instead of a
 * button that would fail.
 */
export function CameraCard({ camera, privacyMessage }: Props) {
  const testCamera = useTestCamera()
  const deleteCamera = useDeleteCamera()
  const detectPtz = useDetectPtz()
  const identify = useIdentifyCamera(camera.id)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [editing, setEditing] = useState(false)
  const [ptzSetupOpen, setPtzSetupOpen] = useState(false)
  const [tapoControlsOpen, setTapoControlsOpen] = useState(false)
  const [batteryOpen, setBatteryOpen] = useState(false)

  const battery = camera.kind === 'tapo_battery'
  const paused = Boolean(privacyMessage)
  const tapo = camera.tapo_controls
  const kind = describeKind(camera)
  const KindIcon = kind.icon

  const details = [
    kind.label,
    camera.stream.width && camera.stream.height
      ? `${camera.stream.width}×${camera.stream.height}`
      : null,
    camera.model,
    camera.host,
    camera.manufacturer,
    camera.stream.codec?.toUpperCase(),
    camera.stream.fps ? `${Math.round(camera.stream.fps)} fps` : null,
  ].filter(Boolean)

  // A failure from one action must not sit on the card for ever, and must not
  // be reported by whichever action happens to be first in a chain.
  // Both run from the overflow menu, which closes on select, so without this
  // they would produce no visible feedback at all until they finished.
  const busyLabel = identify.isPending
    ? 'Asking the camera which device it is…'
    : detectPtz.isPending
      ? 'Checking for ONVIF PTZ support…'
      : testCamera.isPending
        ? 'Testing the stream…'
        : null

  const actionError =
    errorText(testCamera.error) ??
    errorText(detectPtz.error) ??
    errorText(identify.error) ??
    errorText(deleteCamera.error)

  return (
    <Card className="overflow-hidden">
      <div className="flex flex-col gap-3 p-3.5 sm:p-4 lg:flex-row lg:items-start lg:gap-5">
        <div className="flex min-w-0 flex-1 gap-3 sm:gap-4">
          <CameraThumbnail camera={camera} privacyMessage={privacyMessage} />

          <div className="min-w-0 flex-1 space-y-1.5">
            <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
              {/* `h2`, not `h3`: these rows sit directly under the page's `h1`
                  with no intervening section heading, and skipping a level
                  breaks heading navigation for a screen-reader user. */}
              <h2 className="min-w-0 truncate text-[15px] leading-5 font-semibold">
                {camera.name}
              </h2>
              <Status tone={STATUS_TONE[camera.status]}>{STATUS_LABEL[camera.status]}</Status>
            </div>

            <p className="flex items-start gap-1.5 text-xs leading-4 text-(--color-fg-muted)">
              <KindIcon className="mt-px size-3.5 shrink-0" aria-hidden="true" />
              {/* Capped at two lines: the full chain of seven fragments wrapped
                  to four on a phone and pushed the recording switch below the
                  fold. The most identifying values come first, so what is
                  clipped is always the least useful. */}
              <span className="line-clamp-2 min-w-0" title={details.join(' · ')}>
                {details.join(' · ')}
              </span>
            </p>

            {/* Capabilities, not statuses. Kept visually lighter than the state
                above so a camera with PTZ never reads as a camera with a fault. */}
            {battery || camera.ptz_supported === true || camera.onvif_patrol_enabled ? (
              <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
                {battery ? <Badge variant="outline">Event clips</Badge> : null}
                {camera.ptz_supported === true ? <Badge variant="outline">PTZ</Badge> : null}
                {camera.onvif_patrol_enabled ? <Badge variant="outline">Local patrol</Badge> : null}
              </div>
            ) : null}

            {camera.status_message ? (
              <p className="text-xs leading-4 text-(--color-danger)">{camera.status_message}</p>
            ) : null}
            {camera.ptz_status_message ? (
              <p className="text-xs leading-4 text-(--color-fg-subtle)">
                {camera.ptz_status_message}
              </p>
            ) : null}
            {busyLabel ? (
              <p className="flex items-center gap-1.5 text-xs text-(--color-fg-muted)">
                <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
                {busyLabel}
              </p>
            ) : null}
            {identify.data && !identify.isPending ? (
              <p className="text-xs leading-4 text-(--color-fg-muted)">{identify.data.message}</p>
            ) : null}
            {actionError ? (
              <p role="alert" className="text-xs leading-4 text-(--color-danger)">
                {actionError}
              </p>
            ) : null}
          </div>
        </div>

        <div className="flex shrink-0 flex-col gap-2.5 border-t border-(--color-border) pt-3 lg:items-end lg:border-t-0 lg:pt-0">
          {battery ? (
            <p className="text-xs leading-4 text-(--color-fg-muted) lg:max-w-64 lg:text-right">
              Records event clips and timed recordings only.
            </p>
          ) : (
            <RecordingToggle
              camera={camera}
              disabled={paused || !camera.enabled}
              disabledReason={
                paused ? 'Paused by privacy mode' : !camera.enabled ? 'Camera is disabled' : null
              }
            />
          )}

          <div className="flex flex-wrap items-center gap-2">
            {battery ? (
              <Button variant="secondary" size="sm" onClick={() => setBatteryOpen(true)} disabled={paused}>
                <BatteryCharging aria-hidden="true" />
                Battery camera
              </Button>
            ) : (
              <>
                <Button variant="secondary" size="sm" onClick={() => setEditing(true)}>
                  <Settings2 aria-hidden="true" />
                  Configure
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => void testCamera.mutateAsync(camera.id).catch(() => undefined)}
                  loading={testCamera.isPending}
                  disabled={paused}
                >
                  <RefreshCw aria-hidden="true" />
                  Test
                </Button>
              </>
            )}

            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`More actions for ${camera.name}`}
                >
                  <MoreHorizontal aria-hidden="true" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent>
                <DropdownMenuLabel>{camera.name}</DropdownMenuLabel>

                {tapo.supported && !battery ? (
                  <DropdownMenuItem onSelect={() => setTapoControlsOpen(true)} disabled={paused}>
                    <SlidersHorizontal aria-hidden="true" />
                    Tapo LAN controls
                  </DropdownMenuItem>
                ) : null}

                {!battery ? (
                  <>
                    <DropdownMenuItem
                      onSelect={() => void identify.mutateAsync().catch(() => undefined)}
                      disabled={paused || identify.isPending}
                    >
                      <Fingerprint aria-hidden="true" />
                      {identify.isPending ? 'Identifying…' : 'Identify camera'}
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      onSelect={() => void detectPtz.mutateAsync(camera.id).catch(() => undefined)}
                      disabled={paused || detectPtz.isPending}
                    >
                      <Move aria-hidden="true" />
                      {camera.ptz_last_checked_at ? 'Recheck PTZ' : 'Detect PTZ'}
                    </DropdownMenuItem>
                  </>
                ) : null}

                {camera.ptz_supported === true ? (
                  <DropdownMenuItem onSelect={() => setPtzSetupOpen(true)} disabled={paused}>
                    <MapPinned aria-hidden="true" />
                    Positions &amp; patrol
                  </DropdownMenuItem>
                ) : null}

                {battery ? (
                  <DropdownMenuItem onSelect={() => setEditing(true)}>
                    <Settings2 aria-hidden="true" />
                    Configure
                  </DropdownMenuItem>
                ) : null}

                <DropdownMenuSeparator />
                <DropdownMenuItem danger onSelect={() => setConfirmingDelete(true)}>
                  <Trash2 aria-hidden="true" />
                  Remove camera
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>

          {!battery && !tapo.supported && tapo.can_opt_in ? (
            <p className="text-xs leading-4 text-(--color-fg-subtle) lg:max-w-64 lg:text-right">
              Manufacturer unknown. Use{' '}
              <span className="font-medium text-(--color-fg-muted)">Identify camera</span> or
              enable the vendor LAN controls in Configure.
            </p>
          ) : null}
        </div>
      </div>

      <Dialog open={confirmingDelete} onOpenChange={setConfirmingDelete}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Remove {camera.name}?</DialogTitle>
            <DialogDescription>
              This cannot be undone from Sentinel.
            </DialogDescription>
          </DialogHeader>
          <DialogBody>
            <Alert variant="danger" title="Its recordings are deleted too">
              The camera is removed, its recorder stops, and every recording and motion event
              Sentinel holds for it is deleted with it. Saved clips you exported are kept.
            </Alert>
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmingDelete(false)}>
              Keep camera
            </Button>
            <Button
              variant="danger"
              loading={deleteCamera.isPending}
              onClick={() => void deleteCamera.mutateAsync(camera.id).catch(() => undefined)}
            >
              Remove camera and its recordings
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {battery ? (
        <TapoBatteryConfigureDialog camera={camera} open={editing} onOpenChange={setEditing} />
      ) : (
        <EditCameraDialog camera={camera} open={editing} onOpenChange={setEditing} />
      )}
      {camera.ptz_supported === true ? (
        <PtzSetupDialog camera={camera} open={ptzSetupOpen} onOpenChange={setPtzSetupOpen} />
      ) : null}
      {tapo.supported && !battery ? (
        <TapoLocalControlsDialog
          camera={camera}
          open={tapoControlsOpen}
          onOpenChange={setTapoControlsOpen}
        />
      ) : null}
      {battery ? (
        <TapoBatteryDialog camera={camera} open={batteryOpen} onOpenChange={setBatteryOpen} />
      ) : null}
    </Card>
  )
}

function errorText(error: unknown): string | null {
  if (error instanceof NetworkError) return 'Could not reach the Sentinel server.'
  if (error instanceof ApiError) return error.message
  return error ? 'That camera action did not complete.' : null
}
