import { Check, Loader2, Radar, RefreshCw, Wifi } from 'lucide-react'
import { useMemo, useState } from 'react'

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
import { FormField, FormSection } from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'
import { useCreateCamera } from '@/features/cameras/useCameras'
import { cn } from '@/lib/utils'
import {
  type DiscoveredDevice,
  type MediaProfile,
  useDiscoveryScan,
  useOnvifProfiles,
} from '@/features/cameras/discovery'

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
}

function errorText(error: unknown): string | null {
  if (error instanceof NetworkError) return 'Could not reach the Sentinel server.'
  if (error instanceof ApiError) return error.message
  if (error) return 'The camera could not be discovered.'
  return null
}

function pixels(profile: MediaProfile): number {
  return (profile.width ?? 0) * (profile.height ?? 0)
}

function profileLabel(profile: MediaProfile): string {
  const resolution =
    profile.width && profile.height ? `${profile.width}×${profile.height}` : 'unknown resolution'
  return [profile.name, resolution, profile.encoding?.toUpperCase(), profile.fps && `${profile.fps} fps`]
    .filter(Boolean)
    .join(' · ')
}

export function DiscoverCameraDialog({ open, onOpenChange }: Props) {
  const scan = useDiscoveryScan()
  const resolveProfiles = useOnvifProfiles()
  const createCamera = useCreateCamera()

  const [device, setDevice] = useState<DiscoveredDevice | null>(null)
  const [xaddr, setXaddr] = useState('')
  const [name, setName] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [mainToken, setMainToken] = useState('')
  const [subToken, setSubToken] = useState('')

  const profiles = resolveProfiles.data?.profiles ?? []
  const main = profiles.find((profile) => profile.token === mainToken)
  const sub = profiles.find((profile) => profile.token === subToken)
  const devices = scan.data?.devices ?? []

  const interfaces = useMemo(
    () => scan.data?.interfaces_probed.filter((value) => !value.startsWith('172.')) ?? [],
    [scan.data],
  )

  function reset() {
    scan.reset()
    resolveProfiles.reset()
    createCamera.reset()
    setDevice(null)
    setXaddr('')
    setName('')
    setUsername('')
    setPassword('')
    setMainToken('')
    setSubToken('')
  }

  function handleOpenChange(next: boolean) {
    if (!next) reset()
    onOpenChange(next)
  }

  function chooseDevice(next: DiscoveredDevice) {
    setDevice(next)
    setXaddr(next.xaddrs[0] ?? '')
    setName(next.name || next.model || next.manufacturer || 'Discovered camera')
    setMainToken('')
    setSubToken('')
    resolveProfiles.reset()
  }

  async function findStreams() {
    const result = await resolveProfiles.mutateAsync({
      xaddr: xaddr.trim(),
      username,
      password,
    })
    const ordered = [...result.profiles].sort((a, b) => pixels(b) - pixels(a))
    setMainToken(ordered[0]?.token ?? '')
    setSubToken(ordered.length > 1 ? ordered[ordered.length - 1].token : '')
  }

  async function saveCamera() {
    if (!main) return
    await createCamera.mutateAsync({
      name: name.trim(),
      main_stream_url: main.stream_uri,
      sub_stream_url: sub?.stream_uri ?? null,
      username: username || null,
      password: password || null,
      onvif_device_xaddr: xaddr.trim(),
      onvif_profile_token: main.token,
      manufacturer: device?.manufacturer ?? null,
      model: device?.model ?? null,
      recording_mode: 'continuous',
      motion_enabled: true,
      enabled: true,
    })
    handleOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>Discover cameras</DialogTitle>
          <DialogDescription>
            Scan the local network with ONVIF, choose the streams and save credentials encrypted.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-5">
          <section className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <Button
                type="button"
                onClick={() => void scan.mutateAsync().catch(() => undefined)}
                disabled={scan.isPending}
              >
                {scan.isPending ? (
                  <Loader2 className="size-4 animate-spin" aria-hidden />
                ) : (
                  <Radar className="size-4" aria-hidden />
                )}
                {scan.data ? 'Scan again' : 'Scan local network'}
              </Button>
              {interfaces.length > 0 ? (
                <span className="text-xs text-(--color-fg-muted)">
                  Interfaces: {interfaces.join(', ')}
                </span>
              ) : null}
            </div>

            {errorText(scan.error) ? <Alert variant="danger">{errorText(scan.error)}</Alert> : null}
            {scan.data && devices.length === 0 ? (
              <Alert variant="warning" title="No ONVIF cameras answered.">
                Confirm ONVIF is enabled in each camera, that Sentinel and the cameras are on the
                same LAN/VLAN, or enter an ONVIF service address below.
              </Alert>
            ) : null}

            {devices.length > 0 ? (
              <div className="grid gap-2 sm:grid-cols-2">
                {devices.map((entry) => {
                  const selected = device?.endpoint === entry.endpoint
                  return (
                    <button
                      key={entry.uuid ?? entry.endpoint}
                      type="button"
                      onClick={() => chooseDevice(entry)}
                      aria-pressed={selected}
                      className={cn(
                        'flex min-h-20 items-start gap-3 rounded-(--radius-card) border p-3 text-left',
                        'transition-colors duration-(--duration-fast)',
                        selected
                          ? 'border-(--color-accent) bg-(--color-accent-subtle)'
                          : 'border-(--color-border) bg-(--color-surface-raised) hover:bg-(--color-surface-hover)',
                      )}
                    >
                      <Wifi className="mt-0.5 size-5 shrink-0 text-(--color-accent)" aria-hidden />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-semibold">
                          {entry.name || entry.model || 'ONVIF camera'}
                        </span>
                        <span className="mt-1 block truncate text-xs text-(--color-fg-muted)">
                          {[entry.manufacturer, entry.model].filter(Boolean).join(' · ') ||
                            entry.xaddrs[0]}
                        </span>
                      </span>
                      {selected ? <Check className="size-4 text-(--color-success)" aria-hidden /> : null}
                    </button>
                  )
                })}
              </div>
            ) : null}
          </section>

          <FormSection
            grouped
            title="Camera sign-in"
            description="ONVIF needs the account created on the camera itself. Sentinel encrypts it before storing it."
          >
            <FormField
              label="ONVIF service address"
              id="discovery-xaddr"
              hint="Filled in when you pick a discovered camera above. Enter it by hand for a camera that did not answer the scan."
            >
              {(field) => (
                <Input
                  {...field}
                  value={xaddr}
                  onChange={(event) => setXaddr(event.target.value)}
                  placeholder="http://192.168.1.50:2020/onvif/device_service"
                  spellCheck={false}
                  autoComplete="off"
                />
              )}
            </FormField>
            <div className="grid gap-4 sm:grid-cols-2">
              <FormField label="Camera username" id="discovery-username">
                {(field) => (
                  <Input
                    {...field}
                    value={username}
                    onChange={(event) => setUsername(event.target.value)}
                    autoComplete="off"
                  />
                )}
              </FormField>
              <FormField label="Camera password" id="discovery-password">
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
            <Button
              type="button"
              variant="secondary"
              disabled={!xaddr.trim() || resolveProfiles.isPending}
              onClick={() => void findStreams().catch(() => undefined)}
            >
              {resolveProfiles.isPending ? (
                <Loader2 className="size-4 animate-spin" aria-hidden />
              ) : (
                <RefreshCw className="size-4" aria-hidden />
              )}
              Find camera streams
            </Button>
            {errorText(resolveProfiles.error) ? (
              <Alert variant="danger">{errorText(resolveProfiles.error)}</Alert>
            ) : null}
          </FormSection>

          {profiles.length > 0 ? (
            <section className="space-y-4 rounded-(--radius-card) border border-(--color-success)/40 bg-(--color-success)/5 p-4">
              <div className="flex items-center gap-2">
                <Check className="size-4 text-(--color-success)" aria-hidden />
                <h3 className="text-sm font-semibold">{profiles.length} stream profiles found</h3>
                <Badge variant="success">ONVIF</Badge>
              </div>
              <FormField label="Camera name" id="discovery-name">
                {(field) => (
                  <Input
                    {...field}
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                  />
                )}
              </FormField>
              <div className="grid gap-4 sm:grid-cols-2">
                <FormField label="Main stream" id="discovery-main">
                  {(field) => (
                    <Select
                      {...field}
                      value={mainToken}
                      onChange={(event) => setMainToken(event.target.value)}
                    >
                      {profiles.map((profile) => (
                        <option key={profile.token} value={profile.token}>
                          {profileLabel(profile)}
                        </option>
                      ))}
                    </Select>
                  )}
                </FormField>
                <FormField label="Low-resolution substream" id="discovery-sub" optional>
                  {(field) => (
                    <Select
                      {...field}
                      value={subToken}
                      onChange={(event) => setSubToken(event.target.value)}
                    >
                      <option value="">None</option>
                      {profiles.map((profile) => (
                        <option key={profile.token} value={profile.token}>
                          {profileLabel(profile)}
                        </option>
                      ))}
                    </Select>
                  )}
                </FormField>
              </div>
              <p className="text-xs text-(--color-fg-muted)">
                The highest-resolution profile is selected for recording and the smallest for
                motion detection. You can change either choice.
              </p>
              {errorText(createCamera.error) ? (
                <Alert variant="danger">{errorText(createCamera.error)}</Alert>
              ) : null}
            </section>
          ) : null}
        </DialogBody>

        <DialogFooter>
          <Button type="button" variant="ghost" onClick={() => handleOpenChange(false)}>
            Cancel
          </Button>
          <Button
            type="button"
            disabled={!main || !name.trim() || createCamera.isPending}
            loading={createCamera.isPending}
            onClick={() => void saveCamera().catch(() => undefined)}
          >
            Save discovered camera
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
