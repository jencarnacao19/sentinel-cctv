import { Eye, MapPin, Play, RefreshCw, Save, ShieldCheck, Square, Trash2 } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'

import { camerasApi, queryKeys } from '@/api/endpoints'
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
import { PtzControls } from '@/features/live/PtzControls'
import type { Camera } from '@/types/api'

interface Props {
  camera: Camera
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function PtzSetupDialog({ camera, open, onOpenChange }: Props) {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [staySeconds, setStaySeconds] = useState(10)
  const [frameVersion, setFrameVersion] = useState(0)
  const [error, setError] = useState<string | null>(null)

  const setup = useQuery({
    queryKey: queryKeys.cameraPtzSetup(camera.id),
    queryFn: () => camerasApi.getPtzSetup(camera.id),
    enabled: open && camera.ptz_supported === true,
  })

  useEffect(() => {
    if (!open) {
      setError(null)
      setSelected([])
      setName('')
    }
  }, [open])

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.cameraPtzSetup(camera.id) }),
      queryClient.invalidateQueries({ queryKey: queryKeys.cameras }),
    ])
    setFrameVersion((value) => value + 1)
  }

  const createPreset = useMutation({
    mutationFn: () => camerasApi.createPtzPreset(camera.id, name),
    onSuccess: async () => {
      setName('')
      setError(null)
      await refresh()
    },
    onError: (reason) => setError(reason instanceof Error ? reason.message : 'Could not save preset.'),
  })
  const gotoPreset = useMutation({
    mutationFn: (token: string) => camerasApi.gotoPtzPreset(camera.id, token),
    onSuccess: () => {
      setError(null)
      window.setTimeout(() => setFrameVersion((value) => value + 1), 900)
    },
    onError: (reason) => setError(reason instanceof Error ? reason.message : 'Could not move camera.'),
  })
  const removePreset = useMutation({
    mutationFn: (token: string) => camerasApi.removePtzPreset(camera.id, token),
    onSuccess: async (_result, token) => {
      setSelected((tokens) => tokens.filter((item) => item !== token))
      setError(null)
      await refresh()
    },
    onError: (reason) => setError(reason instanceof Error ? reason.message : 'Could not remove preset.'),
  })
  const startPatrol = useMutation({
    mutationFn: () => camerasApi.startPtzPatrol(camera.id, selected, staySeconds),
    onSuccess: async () => {
      setError(null)
      await refresh()
    },
    onError: (reason) => setError(reason instanceof Error ? reason.message : 'Could not start patrol.'),
  })
  const stopPatrol = useMutation({
    mutationFn: () => camerasApi.stopPtzPatrol(camera.id),
    onSuccess: async () => {
      setError(null)
      await refresh()
    },
    onError: (reason) => setError(reason instanceof Error ? reason.message : 'Could not stop patrol.'),
  })

  const busy =
    createPreset.isPending ||
    gotoPreset.isPending ||
    removePreset.isPending ||
    startPatrol.isPending ||
    stopPatrol.isPending
  // The frame is refetched deliberately, after each move, rather than on every
  // render: `frameVersion` is the cache key, so nothing else invalidates it.
  const snapshot = useMemo(
    () => camerasApi.snapshotUrl(camera.id, { width: 800, cacheKey: frameVersion }),
    [camera.id, frameVersion],
  )

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Local PTZ setup · {camera.name}</DialogTitle>
          <DialogDescription>Save positions and run patrol inside your LAN.</DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-5">
          <Alert title="Local control remains authoritative">
            <span className="inline-flex items-center gap-1.5">
              <ShieldCheck className="size-4" aria-hidden="true" />
              These controls talk directly to the camera. Cloud availability is never checked and
              cannot block video, recording, PTZ or patrol.
            </span>
          </Alert>

          <div className="relative aspect-video overflow-hidden rounded-(--radius-tile) bg-black">
            <img src={snapshot} alt={`Current position of ${camera.name}`} className="size-full object-contain" />
            <div className="absolute right-2 bottom-2 flex items-center gap-2">
              <Button
                variant="ghost"
                size="icon"
                className="size-8 bg-black/60 text-white hover:bg-black/80 hover:text-white"
                onClick={() => setFrameVersion((value) => value + 1)}
                aria-label="Refresh camera frame"
              >
                <RefreshCw aria-hidden="true" />
              </Button>
              <PtzControls
                cameraId={camera.id}
                cameraName={camera.name}
                viewer={false}
                zoomSupported={camera.ptz_zoom_supported}
              />
            </div>
          </div>

          {setup.isPending ? <p className="text-sm text-(--color-fg-muted)">Reading ONVIF capabilities…</p> : null}
          {setup.isError ? (
            <Alert variant="danger" title="Could not read PTZ setup">
              {setup.error instanceof Error ? setup.error.message : 'The camera did not answer.'}
            </Alert>
          ) : null}
          {error ? <Alert variant="danger">{error}</Alert> : null}

          {setup.data ? (
            <>
              <section className="space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h3 className="font-medium">Saved positions</h3>
                    <p className="text-xs text-(--color-fg-muted)">
                      {setup.data.presets.length} of {setup.data.preset_capacity || 'unknown'} presets used
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Badge variant="success">ONVIF LAN</Badge>
                    {setup.data.home_supported ? <Badge variant="neutral">Native home</Badge> : null}
                  </div>
                </div>

                <div className="flex flex-col gap-2 sm:flex-row">
                  <div className="flex-1">
                    <Label className="sr-only" htmlFor={`preset-name-${camera.id}`}>Preset name</Label>
                    <Input
                      id={`preset-name-${camera.id}`}
                      value={name}
                      onChange={(event) => setName(event.target.value)}
                      placeholder="e.g. Front door or Home"
                      maxLength={64}
                    />
                  </div>
                  <Button
                    onClick={() => void createPreset.mutateAsync().catch(() => undefined)}
                    disabled={!name.trim() || busy}
                    loading={createPreset.isPending}
                  >
                    <Save aria-hidden="true" />
                    Save current position
                  </Button>
                </div>

                <div className="divide-y divide-(--color-border) rounded-lg border border-(--color-border)">
                  {setup.data.presets.length === 0 ? (
                    <p className="p-4 text-sm text-(--color-fg-muted)">No positions saved yet.</p>
                  ) : (
                    setup.data.presets.map((preset) => (
                      <div key={preset.token} className="flex items-center gap-3 p-3">
                        <input
                          type="checkbox"
                          className="size-5 shrink-0 rounded-(--radius-xs) accent-(--color-accent)"
                          aria-label={`Include ${preset.name} in patrol`}
                          checked={selected.includes(preset.token)}
                          onChange={(event) =>
                            setSelected((tokens) =>
                              event.target.checked
                                ? [...tokens, preset.token]
                                : tokens.filter((token) => token !== preset.token),
                            )
                          }
                        />
                        <MapPin className="size-4 text-(--color-fg-subtle)" aria-hidden="true" />
                        <span className="min-w-0 flex-1 truncate text-sm font-medium">{preset.name}</span>
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => void gotoPreset.mutateAsync(preset.token).catch(() => undefined)}
                          disabled={busy || setup.data.patrol_backend === 'onvif_lan'}
                        >
                          <Eye aria-hidden="true" />
                          Go to
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          aria-label={`Remove ${preset.name}`}
                          onClick={() => void removePreset.mutateAsync(preset.token).catch(() => undefined)}
                          disabled={busy || setup.data.patrol_backend === 'onvif_lan'}
                        >
                          <Trash2 aria-hidden="true" />
                        </Button>
                      </div>
                    ))
                  )}
                </div>
              </section>

              <section className="space-y-3 border-t border-(--color-border) pt-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h3 className="font-medium">Automatic patrol</h3>
                    <p className="text-xs text-(--color-fg-muted)">
                      {setup.data.preset_tour_capacity > 0
                        ? `Camera supports ${setup.data.preset_tour_capacity} native ONVIF tour${setup.data.preset_tour_capacity === 1 ? '' : 's'}.`
                        : 'This camera does not expose standard ONVIF tours.'}
                    </p>
                  </div>
                  <Badge variant={setup.data.patrol_backend === 'none' ? 'neutral' : 'success'}>
                    {setup.data.patrol_backend === 'onvif_lan'
                      ? 'ONVIF patrol active'
                      : setup.data.patrol_backend === 'tapo_lan'
                        ? 'Tapo LAN fallback'
                        : 'Patrol off'}
                  </Badge>
                </div>

                {setup.data.preset_tour_capacity > 0 ? (
                  <div className="flex flex-col gap-3 rounded-lg border border-(--color-border) p-3 sm:flex-row sm:items-end">
                    <div>
                      <Label htmlFor={`patrol-stay-${camera.id}`}>Seconds at each position</Label>
                      <Input
                        id={`patrol-stay-${camera.id}`}
                        type="number"
                        min={2}
                        max={300}
                        value={staySeconds}
                        onChange={(event) => setStaySeconds(Number(event.target.value))}
                        className="mt-1 w-36"
                      />
                    </div>
                    <p className="min-w-0 flex-1 text-xs text-(--color-fg-muted)">
                      Select two or more positions above. The tour is stored and executed by the camera itself.
                    </p>
                    {setup.data.patrol_backend === 'onvif_lan' ? (
                      <Button
                        variant="secondary"
                        onClick={() => void stopPatrol.mutateAsync().catch(() => undefined)}
                        loading={stopPatrol.isPending}
                      >
                        <Square aria-hidden="true" />
                        Stop patrol
                      </Button>
                    ) : (
                      <Button
                        onClick={() => void startPatrol.mutateAsync().catch(() => undefined)}
                        disabled={selected.length < 2 || busy}
                        loading={startPatrol.isPending}
                      >
                        <Play aria-hidden="true" />
                        Start local patrol
                      </Button>
                    )}
                  </div>
                ) : (
                  <Alert>
                    Manual ONVIF control and presets remain fully local. If configured, the existing Tapo cruise
                    adapter is also sent directly over the LAN and never waits for the cloud.
                  </Alert>
                )}
              </section>
            </>
          ) : null}
        </DialogBody>

        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>Done</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
