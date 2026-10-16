import { useEffect, useState } from 'react'

import { ApiError, NetworkError } from '@/api/client'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { CheckboxField } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogForm,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { FormDisclosure, FormField, FormSection } from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useUpdateCamera } from '@/features/cameras/useCameras'
import type {
  Camera,
  CameraUpdate,
  RecordingMode,
  RecordingProfile,
  RecordingSourceMode,
  TapoPatrolMode,
} from '@/types/api'

interface Props {
  camera: Camera
  open: boolean
  onOpenChange: (open: boolean) => void
}

function errorText(error: unknown): string | null {
  if (error instanceof NetworkError) return 'Could not reach the Sentinel server.'
  if (error instanceof ApiError) return error.message
  if (error) return 'The camera settings could not be saved.'
  return null
}

/**
 * Three answers, not two.
 *
 * "Automatic" is not "off" -- it means Sentinel decides from the manufacturer
 * and model the camera reports, which is right for almost every camera. The
 * explicit settings exist for the ones whose firmware reports nothing, where a
 * two-state control would force the operator to state a preference Sentinel
 * would then have to distinguish from silence.
 */
type TapoControlChoice = 'auto' | 'on' | 'off'

function choiceFor(value: boolean | null): TapoControlChoice {
  if (value === true) return 'on'
  if (value === false) return 'off'
  return 'auto'
}

/**
 * Everything a camera can be configured to do.
 *
 * The form is grouped and the rarely-correct settings are folded away. It used
 * to be one flat column of twenty controls in which the two an operator
 * normally changes -- the name and whether the camera is enabled -- sat between
 * a patrol direction and a recording segment length, all at the same visual
 * weight. The disclosures are open by default whenever they hold something
 * other than the default, so a camera that has been tuned never hides that fact.
 */
export function EditCameraDialog({ camera, open, onOpenChange }: Props) {
  const update = useUpdateCamera()
  const resetUpdate = update.reset
  const [name, setName] = useState(camera.name)
  const [mainUrl, setMainUrl] = useState(camera.main_stream_url ?? '')
  const [subUrl, setSubUrl] = useState(camera.sub_stream_url ?? '')
  const [username, setUsername] = useState(camera.username ?? '')
  const [onvifXaddr, setOnvifXaddr] = useState(camera.onvif_device_xaddr ?? '')
  const [password, setPassword] = useState('')
  const [clearPassword, setClearPassword] = useState(false)
  const [enabled, setEnabled] = useState(camera.enabled)
  const [recordingMode, setRecordingMode] = useState<RecordingMode>(camera.recording_mode)
  const [recordingProfile, setRecordingProfile] = useState<RecordingProfile>(
    camera.recording_profile,
  )
  const [recordingSourceMode, setRecordingSourceMode] = useState<RecordingSourceMode>(
    camera.recording_source_mode,
  )
  const [recordingSegmentSeconds, setRecordingSegmentSeconds] = useState(
    camera.recording_segment_seconds != null ? String(camera.recording_segment_seconds) : '',
  )
  const [retentionDays, setRetentionDays] = useState(
    camera.retention_days != null ? String(camera.retention_days) : '',
  )
  const [motionEnabled, setMotionEnabled] = useState(camera.motion_enabled)
  const [motionSensitivity, setMotionSensitivity] = useState(camera.motion_sensitivity)
  const [tapoPatrolMode, setTapoPatrolMode] = useState<TapoPatrolMode | ''>(
    camera.tapo_patrol_mode ?? '',
  )
  const [tapoPassword, setTapoPassword] = useState('')
  const [clearTapoPassword, setClearTapoPassword] = useState(false)
  const [tapoControls, setTapoControls] = useState<TapoControlChoice>(
    choiceFor(camera.tapo_controls_enabled),
  )
  const tapoSectionAvailable = camera.tapo_controls.source !== 'other_vendor'
  const tapoSettingsActive =
    tapoSectionAvailable &&
    (tapoControls === 'on' || (tapoControls === 'auto' && camera.tapo_controls.supported))

  // Anything already moved off its default is worth showing on open, so a
  // folded section can never hide a setting someone deliberately changed.
  const engineTuned =
    camera.recording_profile !== 'auto' ||
    camera.recording_source_mode !== 'auto' ||
    camera.recording_segment_seconds != null ||
    Boolean(camera.onvif_device_xaddr)
  const vendorTuned =
    camera.tapo_controls_enabled !== null ||
    Boolean(camera.tapo_patrol_mode) ||
    camera.has_tapo_credentials

  useEffect(() => {
    if (!open) return
    setName(camera.name)
    setMainUrl(camera.main_stream_url ?? '')
    setSubUrl(camera.sub_stream_url ?? '')
    setUsername(camera.username ?? '')
    setOnvifXaddr(camera.onvif_device_xaddr ?? '')
    setPassword('')
    setClearPassword(false)
    setEnabled(camera.enabled)
    setRecordingMode(camera.recording_mode)
    setRecordingProfile(camera.recording_profile)
    setRecordingSourceMode(camera.recording_source_mode)
    setRecordingSegmentSeconds(
      camera.recording_segment_seconds != null ? String(camera.recording_segment_seconds) : '',
    )
    setRetentionDays(camera.retention_days != null ? String(camera.retention_days) : '')
    setMotionEnabled(camera.motion_enabled)
    setMotionSensitivity(camera.motion_sensitivity)
    setTapoPatrolMode(camera.tapo_patrol_mode ?? '')
    setTapoPassword('')
    setClearTapoPassword(false)
    setTapoControls(choiceFor(camera.tapo_controls_enabled))
    resetUpdate()
  }, [camera, open, resetUpdate])

  async function save(event: React.FormEvent) {
    event.preventDefault()
    const payload: CameraUpdate = {
      name: name.trim(),
      main_stream_url: mainUrl.trim(),
      sub_stream_url: subUrl.trim() || null,
      username: username.trim() || null,
      enabled,
      recording_mode: recordingMode,
      recording_profile: recordingProfile,
      recording_source_mode: recordingSourceMode,
      recording_segment_seconds: recordingSegmentSeconds
        ? Number(recordingSegmentSeconds)
        : null,
      retention_days: retentionDays.trim() ? Number(retentionDays) : null,
      motion_enabled: motionEnabled,
      motion_sensitivity: motionSensitivity,
    }
    if (camera.kind === 'rtsp' && tapoSectionAvailable) {
      payload.tapo_controls_enabled =
        tapoControls === 'auto' ? null : tapoControls === 'on'
      if (tapoSettingsActive) {
        payload.tapo_patrol_mode = tapoPatrolMode || null
        if (clearTapoPassword) payload.tapo_password = ''
        else if (tapoPassword) payload.tapo_password = tapoPassword
      }
    }
    if (clearPassword) payload.password = ''
    else if (password) payload.password = password
    if (onvifXaddr.trim() !== (camera.onvif_device_xaddr ?? '')) {
      payload.onvif_device_xaddr = onvifXaddr.trim() || null
    }

    await update.mutateAsync({ id: camera.id, payload })
    onOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Configure {camera.name}</DialogTitle>
          <DialogDescription>
            Streams, recording, retention and motion. Stored passwords are never shown.
          </DialogDescription>
        </DialogHeader>

        <DialogForm onSubmit={(event) => void save(event).catch(() => undefined)}>
          <DialogBody className="space-y-6">
            <FormSection>
              <FormField label="Name" id={`edit-camera-name-${camera.id}`}>
                {(field) => (
                  <Input
                    {...field}
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                    required
                  />
                )}
              </FormField>
              <CheckboxField
                label="Camera enabled"
                description="A disabled camera keeps its settings and recordings, but Sentinel stops connecting to it."
                checked={enabled}
                onChange={(event) => setEnabled(event.target.checked)}
              />
            </FormSection>

            <FormSection
              title="Streams"
              description="Sentinel records the main stream and detects motion on the substream."
            >
              <FormField
                label="Main stream (recording)"
                id={`edit-camera-main-${camera.id}`}
              >
                {(field) => (
                  <Input
                    {...field}
                    value={mainUrl}
                    onChange={(event) => setMainUrl(event.target.value)}
                    spellCheck={false}
                    autoComplete="off"
                    required
                  />
                )}
              </FormField>
              <FormField
                label="Substream (motion/live grid)"
                id={`edit-camera-sub-${camera.id}`}
                optional
              >
                {(field) => (
                  <Input
                    {...field}
                    value={subUrl}
                    onChange={(event) => setSubUrl(event.target.value)}
                    spellCheck={false}
                    autoComplete="off"
                  />
                )}
              </FormField>
            </FormSection>

            <FormSection
              title="Camera sign-in"
              description="The account created on the camera itself. Sentinel encrypts it and never displays it again."
            >
              <div className="grid gap-4 sm:grid-cols-2">
                <FormField label="Camera username" id={`edit-camera-user-${camera.id}`}>
                  {(field) => (
                    <Input
                      {...field}
                      value={username}
                      onChange={(event) => setUsername(event.target.value)}
                      autoComplete="off"
                    />
                  )}
                </FormField>
                <FormField label="New camera password" id={`edit-camera-password-${camera.id}`}>
                  {(field) => (
                    <Input
                      {...field}
                      type="password"
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                      placeholder={
                        camera.has_credentials ? 'Leave blank to keep stored password' : ''
                      }
                      autoComplete="new-password"
                      disabled={clearPassword}
                    />
                  )}
                </FormField>
              </div>
              {camera.has_credentials ? (
                <CheckboxField
                  label="Remove the stored camera password"
                  checked={clearPassword}
                  onChange={(event) => setClearPassword(event.target.checked)}
                />
              ) : null}
            </FormSection>

            <FormSection title="Recording">
              <div className="grid gap-4 sm:grid-cols-2">
                <FormField label="Recording mode" id={`edit-camera-mode-${camera.id}`}>
                  {(field) => (
                    <Select
                      {...field}
                      value={recordingMode}
                      onChange={(event) => setRecordingMode(event.target.value as RecordingMode)}
                    >
                      <option value="continuous">Continuous</option>
                      <option value="motion">Motion only</option>
                      <option value="scheduled">Scheduled</option>
                      <option value="off">Off</option>
                    </Select>
                  )}
                </FormField>
                <FormField
                  label="Retention days"
                  id={`edit-camera-retention-${camera.id}`}
                  optional
                  hint="Blank uses the system default from Storage."
                >
                  {(field) => (
                    <Input
                      {...field}
                      type="number"
                      min={1}
                      max={3650}
                      value={retentionDays}
                      onChange={(event) => setRetentionDays(event.target.value)}
                      placeholder="Use system default"
                    />
                  )}
                </FormField>
              </div>
            </FormSection>

            <FormSection title="Motion detection">
              <CheckboxField
                label="Motion detection enabled"
                checked={motionEnabled}
                onChange={(event) => setMotionEnabled(event.target.checked)}
              />
              <div className="space-y-2">
                <Label htmlFor={`edit-camera-sensitivity-${camera.id}`}>
                  Motion sensitivity: {motionSensitivity}
                </Label>
                <input
                  id={`edit-camera-sensitivity-${camera.id}`}
                  type="range"
                  min={0}
                  max={100}
                  value={motionSensitivity}
                  onChange={(event) => setMotionSensitivity(Number(event.target.value))}
                  className="h-11 w-full accent-(--color-accent)"
                  disabled={!motionEnabled}
                />
              </div>
            </FormSection>

            <FormDisclosure
              title="Recording engine and ONVIF"
              description="Change these only from measured evidence about one camera."
              defaultOpen={engineTuned}
            >
              <FormField
                label="Recording quality profile"
                id={`edit-camera-profile-${camera.id}`}
                hint="Automatic is recommended and never enables re-encoding by itself. Normalised can repair malformed or H.265 recordings, but consumes substantial server CPU; enable it for one measured problem camera at a time."
              >
                {(field) => (
                  <Select
                    {...field}
                    value={recordingProfile}
                    onChange={(event) =>
                      setRecordingProfile(event.target.value as RecordingProfile)
                    }
                  >
                    <option value="auto">Automatic — stream copy, repair measured corruption</option>
                    <option value="passthrough">Passthrough — untouched stream copy</option>
                    <option value="resilient">
                      Resilient — discard corruption and repair timestamps
                    </option>
                    <option value="normalised">Normalised — re-encode locally to H.264</option>
                  </Select>
                )}
              </FormField>

              <FormField
                label="Recording source"
                id={`edit-camera-source-${camera.id}`}
                hint="Keep automatic unless measurements prove one local path is defective. Relay-only avoids opening an extra RTSP session; direct-only bypasses go2rtc."
              >
                {(field) => (
                  <Select
                    {...field}
                    value={recordingSourceMode}
                    onChange={(event) =>
                      setRecordingSourceMode(event.target.value as RecordingSourceMode)
                    }
                  >
                    <option value="auto">Automatic local failover</option>
                    <option value="relay">Shared local relay only</option>
                    <option value="direct">Direct camera stream only</option>
                  </Select>
                )}
              </FormField>

              <FormField
                label="Recording segment length"
                id={`edit-camera-segment-${camera.id}`}
                hint="Shorter segments limit loss from a broken timestamp cycle, but cameras with long keyframe intervals may need the 60-second default."
              >
                {(field) => (
                  <Select
                    {...field}
                    value={recordingSegmentSeconds}
                    onChange={(event) => setRecordingSegmentSeconds(event.target.value)}
                  >
                    <option value="">Inherit system default</option>
                    <option value="30">30 seconds</option>
                    <option value="60">60 seconds</option>
                    <option value="90">90 seconds</option>
                    <option value="120">120 seconds</option>
                  </Select>
                )}
              </FormField>

              <FormField
                label="ONVIF service address"
                id={`edit-camera-onvif-${camera.id}`}
                optional
                hint="Used for PTZ detection. It must point to the same host as the video stream."
              >
                {(field) => (
                  <Input
                    {...field}
                    value={onvifXaddr}
                    onChange={(event) => setOnvifXaddr(event.target.value)}
                    placeholder={`http://${camera.host}:2020/onvif/device_service`}
                    spellCheck={false}
                    autoComplete="off"
                  />
                )}
              </FormField>
            </FormDisclosure>

            {tapoSectionAvailable ? (
              <FormDisclosure
                title="Tapo LAN controls"
                description="Only applies to TP-Link/Tapo cameras. Everything here stays on your network."
                defaultOpen={vendorTuned}
              >
                <FormField
                  label="Enable Tapo LAN controls"
                  id={`edit-camera-tapo-controls-${camera.id}`}
                  hint={
                    <>
                      {camera.tapo_controls.reason}
                      {camera.tapo_controls_enabled === null
                        ? ' Set this explicitly only when the camera does not report its' +
                          ' manufacturer; Sentinel never guesses a brand from an address, because' +
                          ' several channels of one recorder share it.'
                        : ''}
                    </>
                  }
                >
                  {(field) => (
                    <Select
                      {...field}
                      value={tapoControls}
                      onChange={(event) =>
                        setTapoControls(event.target.value as TapoControlChoice)
                      }
                    >
                      <option value="auto">Automatic — decide from the camera&apos;s own details</option>
                      <option value="on">Always on — this is a TP-Link/Tapo camera</option>
                      <option value="off">Always off — this is not a Tapo camera</option>
                    </Select>
                  )}
                </FormField>

                <FormField
                  label="Resume Tapo patrol after PTZ"
                  id={`edit-camera-tapo-patrol-${camera.id}`}
                  hint="Tapo cameras stop their app patrol when moved through ONVIF. Sentinel restarts the selected direction after the PTZ button is released."
                >
                  {(field) => (
                    <Select
                      {...field}
                      value={tapoPatrolMode}
                      onChange={(event) =>
                        setTapoPatrolMode(event.target.value as TapoPatrolMode | '')
                      }
                    >
                      <option value="">Off</option>
                      <option value="horizontal">Horizontal patrol</option>
                      <option value="vertical">Vertical patrol</option>
                    </Select>
                  )}
                </FormField>

                <FormField
                  label="Tapo account password for LAN control"
                  id={`edit-camera-tapo-password-${camera.id}`}
                  hint="Enable Third-Party Compatibility in the Tapo app first. If the Camera Account password is rejected, enter the password used to sign in to the Tapo app. Sentinel uses the fixed vendor username admin against this camera's private address; your email is not needed and no TP-Link cloud endpoint is contacted."
                >
                  {(field) => (
                    <Input
                      {...field}
                      type="password"
                      value={tapoPassword}
                      onChange={(event) => setTapoPassword(event.target.value)}
                      placeholder={
                        camera.has_tapo_credentials
                          ? 'Leave blank to keep stored control password'
                          : 'Optional — required by some newer firmware'
                      }
                      autoComplete="new-password"
                      disabled={clearTapoPassword}
                    />
                  )}
                </FormField>

                {camera.has_tapo_credentials ? (
                  <CheckboxField
                    label="Remove the stored Tapo control password"
                    checked={clearTapoPassword}
                    onChange={(event) => setClearTapoPassword(event.target.checked)}
                  />
                ) : null}
              </FormDisclosure>
            ) : null}

            {errorText(update.error) ? (
              <Alert variant="danger">{errorText(update.error)}</Alert>
            ) : null}
          </DialogBody>

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={!name.trim() || !mainUrl.trim() || update.isPending}
              loading={update.isPending}
            >
              Save configuration
            </Button>
          </DialogFooter>
        </DialogForm>
      </DialogContent>
    </Dialog>
  )
}
