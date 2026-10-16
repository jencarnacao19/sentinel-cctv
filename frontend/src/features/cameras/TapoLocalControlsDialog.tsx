import { KeyRound, Power, RefreshCw, Volume2 } from 'lucide-react'
import { useState } from 'react'

import { ApiError, NetworkError } from '@/api/client'
import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
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
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { useUpdateCamera } from '@/features/cameras/useCameras'
import {
  useRebootTapo,
  useRefreshTapo,
  useSetTapoFeature,
  useTapoCapabilities,
  useTapoSiren,
  useTapoState,
} from '@/features/cameras/useTapoControls'
import type { Camera, TapoDetectionState, TapoFeature, TapoFeaturePayload } from '@/types/api'

interface Props {
  camera: Camera
  open: boolean
  onOpenChange: (open: boolean) => void
}

const DETECTION_LABELS: Record<string, string> = {
  motion: 'Motion detection',
  person: 'Person detection',
  vehicle: 'Vehicle detection',
  pet: 'Pet detection',
  package: 'Package detection',
  line_crossing: 'Line crossing',
  tamper: 'Tamper detection',
  baby_cry: 'Baby crying detection',
  bark: 'Barking detection',
}

interface Failure {
  title: string
  message: string
  /** What the operator can do about it, when there is something. */
  hint: string | null
}

/**
 * Turn a failure into something an operator can act on.
 *
 * Never the raw body of a non-Sentinel response: a reverse proxy's error page
 * describes the proxy's opinion of the origin, which says nothing about the
 * camera and reads as though Sentinel itself is broken.
 */
function describe(error: unknown): Failure | null {
  if (!error) return null
  if (error instanceof NetworkError) {
    return {
      title: 'Sentinel is not reachable',
      message: 'The request did not reach the Sentinel server.',
      hint: 'Check that Sentinel is running, then try again.',
    }
  }
  if (error instanceof ApiError) {
    if (error.fromProxy) {
      return {
        title: 'Remote connection interrupted',
        message: error.message,
        // Deliberately not "the camera was not contacted": no Sentinel answer
        // came back, so whether it got as far as the camera is unknown, and
        // saying otherwise would be inventing a fact to sound reassuring.
        hint: 'Sentinel did not answer, so it is not known how far the request got.',
      }
    }
    if (error.status === 502) {
      return {
        title: 'Authentication rejected',
        message: error.message,
        hint: 'Enter the Tapo account password below and save it, then discover again.',
      }
    }
    if (error.status === 503) {
      return {
        title: 'Camera did not answer',
        message: error.message,
        hint: 'The camera is on the network but its local control service did not reply.',
      }
    }
    if (error.status === 409) {
      return { title: 'Not supported', message: error.message, hint: null }
    }
    if (error.isLocked) {
      return { title: 'Paused for privacy', message: error.message, hint: null }
    }
    if (error.isRateLimited) {
      return { title: 'Too many requests', message: error.message, hint: null }
    }
    return { title: 'Local Tapo control unavailable', message: error.message, hint: null }
  }
  return {
    title: 'Local Tapo control unavailable',
    message: 'The camera did not accept the local control request.',
    hint: null,
  }
}

function ToggleRow({
  label,
  checked,
  disabled,
  detail,
  onChange,
}: {
  label: string
  checked: boolean
  disabled: boolean
  detail?: string | null
  onChange: (checked: boolean) => void
}) {
  return (
    // A real switch rather than a checkbox: these are device states that take
    // effect the moment they are flipped, not choices confirmed by a Save
    // button, and the switch is the control the platform uses to say so.
    <label className="flex min-h-12 items-center justify-between gap-4 px-3 py-2">
      <span className="min-w-0">
        <span className="block text-sm font-medium">{label}</span>
        {detail ? <span className="block text-xs text-(--color-fg-muted)">{detail}</span> : null}
      </span>
      <Switch
        tone="accent"
        checked={checked}
        disabled={disabled}
        onCheckedChange={onChange}
      />
    </label>
  )
}

function detectionPayload(enabled: boolean, detection: TapoDetectionState): TapoFeaturePayload {
  if (detection.sensitivity_label) {
    return { enabled, sensitivity_label: detection.sensitivity_label }
  }
  if (detection.sensitivity !== null) return { enabled, sensitivity: detection.sensitivity }
  return { enabled }
}

export function TapoLocalControlsDialog({ camera, open, onOpenChange }: Props) {
  const capabilities = useTapoCapabilities(camera.id, open)
  // Enabled on any answer, including a partial one. Gating it on complete
  // success meant one failed probe left the state query disabled -- which
  // react-query reports as `pending` for ever, so the dialog showed loading
  // skeletons above an error message that would never resolve.
  const state = useTapoState(camera.id, open && capabilities.data !== undefined)
  const refresh = useRefreshTapo(camera.id)
  const change = useSetTapoFeature(camera.id)
  const updateCamera = useUpdateCamera()
  const siren = useTapoSiren(camera.id)
  const reboot = useRebootTapo(camera.id)
  const [accountPassword, setAccountPassword] = useState('')
  const [confirmAction, setConfirmAction] = useState<
    'siren_start' | 'siren_stop' | 'reboot' | null
  >(null)
  const supported = new Set(capabilities.data?.supported ?? [])
  const pending =
    refresh.isPending ||
    change.isPending ||
    updateCamera.isPending ||
    siren.isPending ||
    reboot.isPending
  const error =
    capabilities.error ??
    state.error ??
    refresh.error ??
    change.error ??
    updateCamera.error ??
    siren.error ??
    reboot.error
  const failure = describe(error)
  // A skeleton means "an answer is coming". A query that is disabled, or one
  // that has already failed, is not going to produce one.
  const loading =
    (capabilities.isPending && capabilities.fetchStatus !== 'idle') ||
    (state.isPending && state.fetchStatus !== 'idle')

  function apply(feature: TapoFeature, payload: TapoFeaturePayload) {
    void change.mutateAsync({ feature, payload }).catch(() => undefined)
  }

  async function saveAccountPassword() {
    if (!accountPassword) return
    await updateCamera.mutateAsync({
      id: camera.id,
      payload: { tapo_password: accountPassword },
    })
    setAccountPassword('')
    await refresh.mutateAsync()
  }

  async function runConfirmedAction() {
    const action = confirmAction
    if (!action) return
    if (action === 'reboot') {
      await reboot.mutateAsync()
      setConfirmAction(null)
      handleOpenChange(false)
      return
    }
    await siren.mutateAsync(action === 'siren_start')
    setConfirmAction(null)
  }

  function handleOpenChange(next: boolean) {
    if (!next) {
      setAccountPassword('')
      setConfirmAction(null)
      // Otherwise a failure from this visit greets the next one.
      change.reset()
      siren.reset()
      reboot.reset()
      updateCamera.reset()
      refresh.reset()
    }
    onOpenChange(next)
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Local controls — {camera.name}</DialogTitle>
          <DialogDescription>
            Direct LAN controls confirmed by this camera. No TP-Link cloud connection is used.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-5">
          {loading ? (
            <div className="space-y-3" aria-label="Reading the camera">
              <Skeleton className="h-16" />
              <Skeleton className="h-40" />
            </div>
          ) : null}

          {failure ? (
            <Alert variant="danger" title={failure.title}>
              <div className="space-y-1">
                <p>{failure.message}</p>
                {failure.hint ? (
                  <p className="text-(--color-fg-subtle)">{failure.hint}</p>
                ) : null}
              </div>
            </Alert>
          ) : null}

          {capabilities.data?.partial ? (
            <Alert variant="warning" title="Some checks did not answer">
              The controls below were confirmed by this camera and work normally.{' '}
              {capabilities.data.unchecked.length} other check
              {capabilities.data.unchecked.length === 1 ? '' : 's'} did not answer, so anything
              they cover is not shown. Use Refresh from camera to try those again.
            </Alert>
          ) : null}

          <section className="space-y-3 rounded-lg border border-(--color-border) p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h3 className="text-sm font-semibold">Tapo LAN authentication</h3>
                <p className="mt-1 text-xs text-(--color-fg-muted)">
                  RTSP/ONVIF keeps using the Camera Account. Newer Tapo control firmware uses the
                  fixed username <code>admin</code> plus your Tapo account password against this
                  camera's private address. Email is not sent and Sentinel does not contact the
                  TP-Link cloud.
                </p>
              </div>
              <Badge variant={camera.has_tapo_credentials ? 'success' : 'neutral'}>
                {camera.has_tapo_credentials
                  ? 'Separate control password stored'
                  : 'Trying Camera Account credentials'}
              </Badge>
            </div>
            <div className="space-y-2">
              <Label htmlFor={`tapo-account-password-${camera.id}`}>Tapo account password</Label>
              <div className="flex flex-col gap-2 sm:flex-row">
                <Input
                  id={`tapo-account-password-${camera.id}`}
                  type="password"
                  value={accountPassword}
                  onChange={(event) => setAccountPassword(event.target.value)}
                  placeholder={
                    camera.has_tapo_credentials
                      ? 'Enter only to replace the stored password'
                      : 'Password used to sign in to the Tapo app'
                  }
                  autoComplete="new-password"
                />
                <Button
                  type="button"
                  variant="secondary"
                  disabled={!accountPassword || pending}
                  loading={updateCamera.isPending || refresh.isPending}
                  onClick={() => void saveAccountPassword().catch(() => undefined)}
                >
                  <KeyRound aria-hidden="true" />
                  Save & discover
                </Button>
              </div>
            </div>
          </section>

          {capabilities.data ? (
            <div className="flex flex-wrap items-center gap-2 rounded-lg border border-(--color-border) p-3">
              <span className="text-sm font-medium">
                {capabilities.data.device.model ?? camera.model ?? 'Tapo camera'}
              </span>
              {capabilities.data.device.hardware_version ? (
                <Badge>HW {capabilities.data.device.hardware_version}</Badge>
              ) : null}
              {capabilities.data.device.firmware_version ? (
                <Badge>FW {capabilities.data.device.firmware_version}</Badge>
              ) : null}
              <Badge variant="success">LAN only</Badge>
            </div>
          ) : null}

          {state.data ? (
            <>
              <section className="space-y-2">
                <h3 className="text-sm font-semibold">Camera hardware</h3>
                {supported.has('led') && state.data.led_enabled !== null ? (
                  <ToggleRow
                    label="Status LED"
                    checked={state.data.led_enabled}
                    disabled={pending}
                    onChange={(enabled) => apply('led', { enabled })}
                  />
                ) : null}
                {supported.has('privacy_mask') && state.data.privacy_mask_enabled !== null ? (
                  <ToggleRow
                    label="Physical camera privacy mask"
                    detail="This closes the camera lens electronically; it is separate from Sentinel privacy mode."
                    checked={state.data.privacy_mask_enabled}
                    disabled={pending}
                    onChange={(enabled) => apply('privacy_mask', { enabled })}
                  />
                ) : null}
                {supported.has('smart_track') && state.data.smart_track_enabled !== null ? (
                  <ToggleRow
                    label="Local subject tracking"
                    checked={state.data.smart_track_enabled}
                    disabled={pending}
                    onChange={(enabled) => apply('smart_track', { enabled })}
                  />
                ) : null}
                {supported.has('floodlight') && state.data.floodlight_enabled !== null ? (
                  <ToggleRow
                    label="Floodlight"
                    checked={state.data.floodlight_enabled}
                    disabled={pending}
                    onChange={(enabled) => apply('floodlight', { enabled })}
                  />
                ) : null}
                {supported.has('day_night') && state.data.day_night_mode ? (
                  <div className="space-y-2 px-3 py-2 text-sm">
                    <Label htmlFor={`tapo-day-night-${camera.id}`}>Day/night mode</Label>
                    <Select
                      id={`tapo-day-night-${camera.id}`}
                      value={state.data.day_night_mode}
                      disabled={pending}
                      onChange={(event) =>
                        apply('day_night', {
                          mode: event.target.value as 'day' | 'night' | 'auto',
                        })
                      }
                    >
                      <option value="auto">Automatic</option>
                      <option value="day">Day</option>
                      <option value="night">Night</option>
                    </Select>
                  </div>
                ) : null}
              </section>

              {Object.entries(state.data.detections).some(
                ([feature, detection]) => supported.has(feature) && detection.supported,
              ) ? (
                <section className="space-y-2">
                  <h3 className="text-sm font-semibold">On-camera detection</h3>
                  {Object.entries(state.data.detections).map(([feature, detection]) =>
                    supported.has(feature) && detection.supported && detection.enabled !== null ? (
                      <ToggleRow
                        key={feature}
                        label={DETECTION_LABELS[feature] ?? feature}
                        detail={
                          detection.sensitivity_label
                            ? `Sensitivity: ${detection.sensitivity_label}`
                            : detection.sensitivity !== null
                              ? `Sensitivity: ${detection.sensitivity}%`
                              : null
                        }
                        checked={detection.enabled}
                        disabled={pending}
                        onChange={(enabled) =>
                          apply(feature as TapoFeature, detectionPayload(enabled, detection))
                        }
                      />
                    ) : null,
                  )}
                </section>
              ) : null}

              {state.data.sd_card ? (
                <section className="rounded-lg border border-(--color-border) p-3 text-sm">
                  <h3 className="font-semibold">Camera SD card</h3>
                  <p className="mt-1 text-(--color-fg-muted)">
                    {state.data.sd_card.present
                      ? `${state.data.sd_card.status}${
                          state.data.sd_card.free_gb !== null
                            ? ` · ${state.data.sd_card.free_gb.toFixed(1)} GB free`
                            : ''
                        }`
                      : 'No card detected'}
                  </p>
                </section>
              ) : null}

              {supported.has('cruise') || supported.has('presets') ? (
                <section className="rounded-lg border border-(--color-border) p-3 text-sm">
                  <h3 className="font-semibold">Local movement</h3>
                  <p className="mt-1 text-(--color-fg-muted)">
                    {supported.has('cruise')
                      ? `Camera patrol: ${state.data.cruise_enabled ? 'active' : 'stopped'}`
                      : 'Camera patrol was not confirmed'}
                    {supported.has('presets') && state.data.preset_count !== null
                      ? ` · ${state.data.preset_count} camera presets`
                      : ''}
                  </p>
                  <p className="mt-1 text-xs text-(--color-fg-subtle)">
                    Positions and patrol routes are managed from the separate Positions & patrol
                    panel so manual PTZ safety remains unchanged.
                  </p>
                </section>
              ) : null}

              {supported.has('alarm') || supported.has('reboot') ? (
                <section className="space-y-3 rounded-lg border border-(--color-border) p-3">
                  <div>
                    <h3 className="text-sm font-semibold">Physical camera actions</h3>
                    <p className="mt-1 text-xs text-(--color-fg-muted)">
                      These affect the real camera and always require a second confirmation.
                    </p>
                  </div>
                  {confirmAction ? (
                    <Alert
                      variant="warning"
                      title={
                        confirmAction === 'reboot'
                          ? 'Confirm camera reboot'
                          : confirmAction === 'siren_start'
                            ? 'Confirm audible siren'
                            : 'Confirm siren stop'
                      }
                    >
                      <div className="mt-2 flex flex-wrap gap-2">
                        <Button
                          type="button"
                          variant={confirmAction === 'siren_stop' ? 'secondary' : 'danger'}
                          loading={siren.isPending || reboot.isPending}
                          disabled={pending}
                          onClick={() => void runConfirmedAction().catch(() => undefined)}
                        >
                          Confirm action
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          disabled={pending}
                          onClick={() => setConfirmAction(null)}
                        >
                          Cancel
                        </Button>
                      </div>
                    </Alert>
                  ) : (
                    <div className="flex flex-wrap gap-2">
                      {supported.has('alarm') ? (
                        <Button
                          type="button"
                          variant={state.data.siren_active ? 'secondary' : 'danger'}
                          disabled={pending}
                          onClick={() =>
                            setConfirmAction(
                              state.data?.siren_active ? 'siren_stop' : 'siren_start',
                            )
                          }
                        >
                          <Volume2 aria-hidden="true" />
                          {state.data.siren_active ? 'Stop siren' : 'Sound siren'}
                        </Button>
                      ) : null}
                      {supported.has('reboot') ? (
                        <Button
                          type="button"
                          variant="danger"
                          disabled={pending}
                          onClick={() => setConfirmAction('reboot')}
                        >
                          <Power aria-hidden="true" />
                          Reboot camera
                        </Button>
                      ) : null}
                    </div>
                  )}
                </section>
              ) : null}
            </>
          ) : null}

          {capabilities.data && supported.size === 0 && !failure ? (
            <Alert variant="info" title="This camera confirmed no controls">
              It answered Sentinel but reported none of the settings Sentinel can change. That is
              normal for older firmware and for cameras that expose only RTSP; recording, live
              view and ONVIF are unaffected.
            </Alert>
          ) : null}

          <Alert variant="info">
            Firmware updates, SD-card formatting and factory reset remain excluded. Unsupported
            controls stay hidden until this individual camera confirms them.
          </Alert>
        </DialogBody>

        <DialogFooter>
          <Button
            type="button"
            variant="secondary"
            loading={refresh.isPending}
            disabled={pending}
            onClick={() => void refresh.mutateAsync().catch(() => undefined)}
          >
            <RefreshCw aria-hidden="true" />
            Refresh from camera
          </Button>
          <Button type="button" variant="ghost" onClick={() => handleOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
