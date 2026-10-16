import { BatteryCharging, CheckCircle2, Video } from 'lucide-react'
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
  DialogForm,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { FormField, FormSection } from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { useCreateCamera, useTestStream } from '@/features/cameras/useCameras'
import { useTestTapoBattery } from '@/features/cameras/useTapoBattery'
import { cn } from '@/lib/utils'
import type { CameraKind, StreamTestResult, TapoBatteryStatus } from '@/types/api'

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * The two kinds of camera Sentinel can add, as a real radio group.
 *
 * They were two ordinary buttons whose only difference was which one currently
 * looked filled -- a state a screen reader was never told about, and which a
 * keyboard user had to discover by tabbing to each in turn. A mutually
 * exclusive choice is a radio group, and saying so is what makes the selected
 * state announced and the arrow keys work.
 *
 * Each option states what it means for recording, because that is the
 * consequence and it cannot be undone by editing the camera afterwards.
 */
function KindOption({
  active,
  icon,
  title,
  detail,
  onSelect,
}: {
  active: boolean
  icon: React.ReactNode
  title: string
  detail: string
  onSelect: () => void
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      onClick={onSelect}
      className={cn(
        'flex min-h-11 flex-1 flex-col items-start gap-1 rounded-(--radius-card) border p-3',
        'text-left transition-colors duration-(--duration-fast)',
        'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-(--color-accent)',
        active
          ? 'border-(--color-accent) bg-(--color-accent-subtle)'
          : 'border-(--color-border) bg-(--color-surface-raised) hover:bg-(--color-surface-hover)',
      )}
    >
      <span className="flex items-center gap-2 text-sm font-semibold [&_svg]:size-4">
        {icon}
        {title}
      </span>
      <span className="text-xs leading-4 text-(--color-fg-muted)">{detail}</span>
    </button>
  )
}

function TestResult({ result }: { result: StreamTestResult }) {
  if (!result.ok) {
    return (
      <Alert variant="danger" title="Could not open the stream">
        {result.error}
      </Alert>
    )
  }
  return (
    <div className="space-y-3 rounded-(--radius-card) border border-(--color-success)/40 bg-(--color-success)/10 p-4">
      <p className="flex items-center gap-2 text-sm font-semibold text-(--color-success)">
        <CheckCircle2 className="size-4" aria-hidden="true" />
        Connection OK
      </p>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
        <dt className="text-(--color-fg-muted)">Codec</dt>
        <dd className="tabular font-medium uppercase">{result.codec ?? '—'}</dd>
        <dt className="text-(--color-fg-muted)">Resolution</dt>
        <dd className="tabular font-medium">{result.resolution ?? '—'}</dd>
        <dt className="text-(--color-fg-muted)">Frame rate</dt>
        <dd className="tabular font-medium">{result.fps ? `${result.fps} fps` : '—'}</dd>
        <dt className="text-(--color-fg-muted)">Audio</dt>
        <dd className="tabular font-medium uppercase">{result.audio_codec ?? 'none'}</dd>
        <dt className="text-(--color-fg-muted)">Bitrate</dt>
        <dd className="tabular font-medium">
          {result.bitrate_kbps ? `${(result.bitrate_kbps / 1000).toFixed(1)} Mbps` : '—'}
        </dd>
      </dl>
      {result.can_stream_copy ? (
        <Badge variant="success">Records without re-encoding</Badge>
      ) : (
        <Alert variant="warning" title="This codec cannot be copied directly">
          Recording would need to re-encode, which costs far more CPU. An H.264 or H.265
          stream is strongly preferred.
        </Alert>
      )}
    </div>
  )
}

function BatteryTestResult({ result }: { result: TapoBatteryStatus }) {
  return (
    <div className="space-y-3 rounded-(--radius-card) border border-(--color-success)/40 bg-(--color-success)/10 p-4">
      <p className="flex items-center gap-2 text-sm font-semibold text-(--color-success)">
        <CheckCircle2 className="size-4" aria-hidden="true" />
        Local connection OK
      </p>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
        <dt className="text-(--color-fg-muted)">Model</dt>
        <dd className="font-medium">{result.model ?? 'Tapo battery'}</dd>
        <dt className="text-(--color-fg-muted)">Firmware</dt>
        <dd className="font-medium">{result.firmware_version ?? 'Unknown'}</dd>
        <dt className="text-(--color-fg-muted)">Battery</dt>
        <dd className="font-medium tabular-nums">
          {result.battery_percent === null ? 'Unknown' : `${result.battery_percent}%`}
        </dd>
        {result.children && result.children.length > 0 ? (
          <>
            <dt className="text-(--color-fg-muted)">Hub children</dt>
            <dd className="font-medium">{result.children.length} camera(s) detected</dd>
          </>
        ) : null}
      </dl>
      <Badge variant="success">LAN provider confirmed</Badge>
    </div>
  )
}

/**
 * Add a camera by RTSP URL, or add a local Tapo battery camera.
 *
 * The password field is separate from the URL, but a URL pasted with credentials
 * still works -- the server extracts and encrypts them either way.
 *
 * The form is grouped rather than flat: what kind of camera this is, what to
 * call it, how to reach it, and how to authenticate to it. Twenty controls in
 * one undifferentiated column gave an operator adding their first camera no way
 * to tell the two required fields from the optional ones.
 */
export function AddCameraDialog({ open, onOpenChange }: Props) {
  const createCamera = useCreateCamera()
  const testStream = useTestStream()
  const testBattery = useTestTapoBattery()

  const [kind, setKind] = useState<CameraKind>('rtsp')
  const [name, setName] = useState('')
  const [mainUrl, setMainUrl] = useState('')
  const [subUrl, setSubUrl] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [batteryHost, setBatteryHost] = useState('')
  const [batteryTopology, setBatteryTopology] = useState<'standalone' | 'hub'>('standalone')
  const [childId, setChildId] = useState('')
  const [testedBatteryFingerprint, setTestedBatteryFingerprint] = useState<string | null>(null)

  const battery = kind === 'tapo_battery'
  const effectiveChildId = batteryTopology === 'hub' ? childId.trim() : ''
  const effectiveBatteryUsername = batteryTopology === 'hub' ? username.trim() : ''
  const batteryFingerprint = `${batteryTopology}\u0000${batteryHost.trim()}\u0000${effectiveChildId}\u0000${password}\u0000${effectiveBatteryUsername}`
  const canTest = battery
    ? batteryHost.trim().length > 0 && password.length > 0 && !testBattery.isPending
    : mainUrl.trim().length > 0 && !testStream.isPending
  const canSave =
    name.trim().length > 0 &&
    (battery
      ? batteryHost.trim().length > 0 &&
        password.length > 0 &&
        (batteryTopology === 'standalone' || effectiveChildId.length > 0) &&
        testedBatteryFingerprint === batteryFingerprint
      : mainUrl.trim().length > 0)

  function invalidateBatteryProof() {
    setTestedBatteryFingerprint(null)
    testBattery.reset()
  }

  function reset() {
    setKind('rtsp')
    setName('')
    setMainUrl('')
    setSubUrl('')
    setUsername('')
    setPassword('')
    setBatteryHost('')
    setBatteryTopology('standalone')
    setChildId('')
    setTestedBatteryFingerprint(null)
    testStream.reset()
    testBattery.reset()
    createCamera.reset()
  }

  function handleOpenChange(next: boolean) {
    if (!next) reset()
    onOpenChange(next)
  }

  function handleTest(): void {
    if (battery) {
      setTestedBatteryFingerprint(null)
      void testBattery
        .mutateAsync({
          host: batteryHost.trim(),
          password,
          child_id: effectiveChildId || null,
          hub: batteryTopology === 'hub',
          ...(effectiveBatteryUsername ? { username: effectiveBatteryUsername } : {}),
        })
        .then(() => setTestedBatteryFingerprint(batteryFingerprint))
        .catch(() => undefined)
    } else {
      void testStream
        .mutateAsync({ url: mainUrl.trim(), username, password })
        .catch(() => undefined)
    }
  }

  function handleSubmit(event: React.FormEvent): void {
    event.preventDefault()
    if (!canSave) return
    void (async () => {
      try {
        await createCamera.mutateAsync(
          battery
            ? {
                name: name.trim(),
                kind: 'tapo_battery',
                host: batteryHost.trim(),
                main_stream_url: null,
                tapo_password: password,
                tapo_child_id: effectiveChildId || null,
                recording_mode: 'events',
                motion_enabled: false,
                manufacturer: 'Tapo',
                model: 'TC82',
                ...(effectiveBatteryUsername ? { username: effectiveBatteryUsername } : {}),
              }
            : {
                name: name.trim(),
                kind: 'rtsp',
                main_stream_url: mainUrl.trim(),
                sub_stream_url: subUrl.trim() || null,
                username: username || null,
                password: password || null,
              },
        )
        handleOpenChange(false)
      } catch {
        /* surfaced through saveError */
      }
    })()
  }

  const saveError =
    createCamera.error instanceof NetworkError
      ? 'Could not reach the Sentinel server.'
      : createCamera.error instanceof ApiError
        ? createCamera.error.message
        : createCamera.error
          ? 'Could not save the camera.'
          : null

  const testError =
    (battery ? testBattery.error : testStream.error) instanceof NetworkError
      ? 'Could not reach the Sentinel server.'
      : (battery ? testBattery.error : testStream.error) instanceof ApiError
        ? ((battery ? testBattery.error : testStream.error) as ApiError).message
        : null

  const testing = testStream.isPending || testBattery.isPending

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Add camera</DialogTitle>
          <DialogDescription>
            {battery
              ? 'An event-based Tapo battery camera, added without pretending it has RTSP.'
              : 'Enter the camera’s RTSP address, then test the connection before saving.'}
          </DialogDescription>
        </DialogHeader>

        <DialogForm onSubmit={handleSubmit} noValidate>
          <DialogBody className="space-y-6">
            <fieldset>
              <legend className="mb-2 text-sm leading-5 font-semibold">Camera type</legend>
              <div role="radiogroup" aria-label="Camera type" className="flex gap-2">
                <KindOption
                  active={!battery}
                  onSelect={() => setKind('rtsp')}
                  icon={<Video aria-hidden="true" />}
                  title="RTSP / NVR"
                  detail="Continuous recording and motion detection."
                />
                <KindOption
                  active={battery}
                  onSelect={() => setKind('tapo_battery')}
                  icon={<BatteryCharging aria-hidden="true" />}
                  title="Tapo battery"
                  detail="Event clips and on-demand video only."
                />
              </div>
            </fieldset>

            <FormField label="Name" id="camera-name">
              {(field) => (
                <Input
                  {...field}
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="Front door"
                  autoFocus
                  required
                />
              )}
            </FormField>

            {battery ? (
              <FormSection
                title="Local connection"
                description="A private LAN address only. Sentinel never contacts the TP-Link cloud."
              >
                <FormField
                  label="Connection type"
                  id="camera-battery-topology"
                  hint="Choose the device whose local address you are entering."
                >
                  {(field) => (
                    <Select
                      {...field}
                      value={batteryTopology}
                      onChange={(event) => {
                        const next = event.target.value as 'standalone' | 'hub'
                        setBatteryTopology(next)
                        setChildId('')
                        if (next === 'standalone') setUsername('')
                        invalidateBatteryProof()
                      }}
                    >
                      <option value="standalone">Standalone TC82</option>
                      <option value="hub">H200 hub with paired camera</option>
                    </Select>
                  )}
                </FormField>

                <FormField
                  label="Camera or H200 local address"
                  id="camera-battery-host"
                  hint={
                    batteryTopology === 'hub'
                      ? "The H200 hub's local LAN address."
                      : "The camera's own local LAN address."
                  }
                >
                  {(field) => (
                    <Input
                      {...field}
                      value={batteryHost}
                      onChange={(event) => {
                        setBatteryHost(event.target.value)
                        invalidateBatteryProof()
                      }}
                      placeholder="192.168.1.60"
                      spellCheck={false}
                      autoComplete="off"
                      required
                    />
                  )}
                </FormField>

                {batteryTopology === 'hub' ? (
                  <FormField
                    label="Hub child ID"
                    id="camera-child-id"
                    optional
                    hint="Test the hub first to discover paired cameras, or enter the exact child ID."
                  >
                    {(field) => (
                      <Input
                        {...field}
                        value={childId}
                        onChange={(event) => {
                          setChildId(event.target.value)
                          invalidateBatteryProof()
                        }}
                        spellCheck={false}
                        autoComplete="off"
                      />
                    )}
                  </FormField>
                ) : null}

                {batteryTopology === 'hub' &&
                testBattery.data?.children &&
                testBattery.data.children.length > 0 ? (
                  <FormField
                    label="Discovered hub cameras"
                    id="camera-discovered-children"
                    hint="Selecting a camera fills its child ID automatically."
                  >
                    {(field) => (
                      <Select
                        {...field}
                        value={childId}
                        onChange={(event) => {
                          const selected = event.target.value
                          setChildId(selected)
                          invalidateBatteryProof()
                          const found = testBattery.data?.children?.find(
                            (c) => c.child_id === selected,
                          )
                          if (found && !name.trim() && found.name) {
                            setName(found.name)
                          }
                        }}
                      >
                        <option value="">Select a discovered camera…</option>
                        {testBattery.data?.children?.map((child) => (
                          <option key={child.child_id} value={child.child_id}>
                            {child.name
                              ? `${child.name} (${child.model ?? 'TC82'}) - ${child.child_id}`
                              : `${child.model ?? 'TC82'} - ${child.child_id}`}
                          </option>
                        ))}
                      </Select>
                    )}
                  </FormField>
                ) : null}

                {batteryTopology === 'hub' ? (
                  <FormField
                    label="Tapo username"
                    id="camera-battery-username"
                    optional
                    hint={
                      <>
                        Leave blank to use <code>admin</code>. This field is not used by a
                        standalone TC82 Direct Stream.
                      </>
                    }
                  >
                    {(field) => (
                      <Input
                        {...field}
                        value={username}
                        onChange={(event) => {
                          setUsername(event.target.value)
                          invalidateBatteryProof()
                        }}
                        placeholder="Optional hub username"
                        autoComplete="username"
                      />
                    )}
                  </FormField>
                ) : null}

                <FormField
                  label="Tapo account password"
                  id="camera-password"
                  hint={
                    <>
                      Enter the password used for the Tapo mobile app, not the Camera Account or
                      RTSP password. Sentinel encrypts it and sends it only to this private address.
                    </>
                  }
                >
                  {(field) => (
                    <Input
                      {...field}
                      type="password"
                      value={password}
                      onChange={(event) => {
                        setPassword(event.target.value)
                        invalidateBatteryProof()
                      }}
                      autoComplete="new-password"
                    />
                  )}
                </FormField>
              </FormSection>
            ) : (
              <>
                <FormSection
                  title="Streams"
                  description="Sentinel records the main stream and detects motion on the substream."
                >
                  <FormField
                    label="Main stream (recording)"
                    id="camera-main-url"
                    hint="The full RTSP address, including the path your camera uses."
                  >
                    {(field) => (
                      <Input
                        {...field}
                        value={mainUrl}
                        onChange={(event) => setMainUrl(event.target.value)}
                        placeholder="rtsp://192.168.1.50:554/stream1"
                        spellCheck={false}
                        autoComplete="off"
                        required
                      />
                    )}
                  </FormField>

                  <FormField
                    label="Substream"
                    id="camera-sub-url"
                    optional
                    hint="A low-resolution stream, used for motion detection and for the camera wall."
                  >
                    {(field) => (
                      <Input
                        {...field}
                        value={subUrl}
                        onChange={(event) => setSubUrl(event.target.value)}
                        placeholder="rtsp://192.168.1.50:554/stream2"
                        spellCheck={false}
                        autoComplete="off"
                      />
                    )}
                  </FormField>
                </FormSection>

                <FormSection
                  title="Camera sign-in"
                  description="The account you created on the camera itself, not a cloud account."
                >
                  <div className="grid gap-4 sm:grid-cols-2">
                    <FormField label="Username" id="camera-username">
                      {(field) => (
                        <Input
                          {...field}
                          value={username}
                          onChange={(event) => setUsername(event.target.value)}
                          autoComplete="off"
                        />
                      )}
                    </FormField>
                    <FormField label="Password" id="camera-password">
                      {(field) => (
                        <Input
                          {...field}
                          type="password"
                          value={password}
                          onChange={(event) => setPassword(event.target.value)}
                          autoComplete="new-password"
                        />
                      )}
                    </FormField>
                  </div>
                </FormSection>
              </>
            )}

            <p className="text-xs leading-5 text-(--color-fg-subtle)">
              Credentials are encrypted before storage and never shown again.
              {battery
                ? ' Direct Stream and clip traffic stays on the LAN, and no continuous recorder is started.'
                : ''}
            </p>

            {testError ? <Alert variant="danger">{testError}</Alert> : null}
            {testStream.data ? <TestResult result={testStream.data} /> : null}
            {testBattery.data && testedBatteryFingerprint === batteryFingerprint ? (
              <BatteryTestResult result={testBattery.data} />
            ) : null}
            {saveError ? <Alert variant="danger">{saveError}</Alert> : null}
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="secondary"
              onClick={handleTest}
              disabled={!canTest}
              loading={testing}
            >
              {testing ? 'Testing…' : 'Test connection'}
            </Button>
            <Button type="submit" disabled={!canSave} loading={createCamera.isPending}>
              Add camera
            </Button>
          </DialogFooter>
        </DialogForm>
      </DialogContent>
    </Dialog>
  )
}
