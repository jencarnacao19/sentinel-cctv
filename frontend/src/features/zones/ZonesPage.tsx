/**
 * The motion zone editor: named detection zones and areas to ignore.
 *
 * Ties together a live preview (`usePreviewFrame`), the drawing surface
 * (`ZoneCanvas`), the accessible text list (`ZoneList`), undo/redo
 * (`useZoneHistory`) and the empirical tuning feedback (`TuningPanel`) into
 * one page, per camera.
 *
 * `ZoneEditor` is remounted (via `key={cameraId}`) every time the camera
 * selection changes, rather than reconciling its internal state in place.
 * That single choice is what keeps undo history, the in-progress zone and the
 * "unsaved changes" flag from one camera ever leaking into another -- a fresh
 * mount hydrates once from that camera's own saved zones and starts a clean
 * history, which is what a camera switch should mean anyway.
 */

import { Redo2, Save, Undo2, Video } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'

import { EmptyState } from '@/components/EmptyState'
import { PageHeader } from '@/components/layout/PageHeader'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Segmented } from '@/components/ui/segmented'
import { Select } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { useCameras } from '@/features/cameras/useCameras'
import { TuningPanel } from '@/features/zones/TuningPanel'
import { ZoneCanvas } from '@/features/zones/ZoneCanvas'
import { ZoneList } from '@/features/zones/ZoneList'
import type { EditorZone, Point, ZoneKind } from '@/features/zones/types'
import { useMotionZones, useSaveMotionZones } from '@/features/zones/useMotionZones'
import { usePreviewFrame } from '@/features/zones/usePreviewFrame'
import { useZoneHistory } from '@/features/zones/useZoneHistory'
import {
  MAX_POINTS,
  MAX_ZONES,
  MIN_POINTS,
  defaultZoneName,
  makeZoneId,
  sanitizeServerZones,
  zoneProblems,
  zonesSignature,
  zonesToPayload,
} from '@/features/zones/zoneMath'
import { errorText } from '@/lib/errors'

function sanitizeSavedZones(raw: unknown): EditorZone[] {
  return sanitizeServerZones(raw).zones
}

function ZoneEditor({ cameraId, cameraName }: { cameraId: string; cameraName: string }) {
  const zonesQuery = useMotionZones(cameraId)
  const preview = usePreviewFrame(cameraId)
  const saveZones = useSaveMotionZones(cameraId)

  const history = useZoneHistory<EditorZone[]>([])
  const { resetTo, commit, undo, redo, canUndo, canRedo, state: zones } = history

  const hydratedRef = useRef(false)
  const [savedSignature, setSavedSignature] = useState('')
  const [drawingZoneId, setDrawingZoneId] = useState<string | null>(null)
  const [activeZoneId, setActiveZoneId] = useState<string | null>(null)
  const [validationMessage, setValidationMessage] = useState<string | null>(null)
  const [saveMessage, setSaveMessage] = useState<string | null>(null)
  const [newZoneKind, setNewZoneKind] = useState<ZoneKind>('detect')

  // Hydrate exactly once, the moment this camera's zones first arrive. Later
  // refetches (a background poll, or the one this save triggers on its own
  // camera) must not reset the editor out from under an operator mid-edit.
  useEffect(() => {
    if (hydratedRef.current || !zonesQuery.data) return
    hydratedRef.current = true
    resetTo(zonesQuery.data.zones)
    setSavedSignature(zonesSignature(zonesQuery.data.zones))
  }, [zonesQuery.data, resetTo])

  const unsavedChanges = zonesSignature(zones) !== savedSignature
  const problems = zoneProblems(zones)
  const hasDetectionZones = zones.some((zone) => zone.kind === 'detect' && zone.enabled)

  function commitZones(updater: (current: EditorZone[]) => EditorZone[]): void {
    setSaveMessage(null)
    commit(updater)
  }

  function handleStartZone(): void {
    if (zones.length >= MAX_ZONES) return
    const id = makeZoneId()
    commitZones((current) => [
      ...current,
      {
        id,
        saved: false,
        name: defaultZoneName(newZoneKind, current),
        kind: newZoneKind,
        enabled: true,
        sensitivity: null,
        points: [],
      },
    ])
    setDrawingZoneId(id)
    setActiveZoneId(id)
    setValidationMessage(null)
  }

  function handleAddPoint(zoneId: string, point: Point): void {
    commitZones((current) =>
      current.map((zone) =>
        zone.id === zoneId && zone.points.length < MAX_POINTS
          ? { ...zone, points: [...zone.points, point] }
          : zone,
      ),
    )
  }

  function handleMovePoint(zoneId: string, pointIndex: number, point: Point): void {
    commitZones((current) =>
      current.map((zone) =>
        zone.id === zoneId
          ? { ...zone, points: zone.points.map((p, index) => (index === pointIndex ? point : p)) }
          : zone,
      ),
    )
  }

  function handleDeletePoint(zoneId: string, pointIndex: number): void {
    const zone = zones.find((entry) => entry.id === zoneId)
    if (!zone) return
    // A finished zone must keep at least three points to stay a valid
    // polygon; while it is still being drawn, points can be dropped freely
    // since it has not been offered to "Finish" yet.
    if (zone.id !== drawingZoneId && zone.points.length <= MIN_POINTS) {
      setValidationMessage(
        `"${zone.name}" needs at least ${MIN_POINTS} points to stay a valid zone. ` +
          'Delete the whole zone instead if you no longer need it.',
      )
      return
    }
    setValidationMessage(null)
    commitZones((current) =>
      current.map((entry) =>
        entry.id === zoneId
          ? { ...entry, points: entry.points.filter((_, index) => index !== pointIndex) }
          : entry,
      ),
    )
  }

  function handleFinishZone(zoneId: string): void {
    const zone = zones.find((entry) => entry.id === zoneId)
    if (!zone || zone.points.length < MIN_POINTS) return
    setDrawingZoneId(null)
    setValidationMessage(null)
  }

  function handleCancelZone(zoneId: string): void {
    // A zone that was never finished has nothing worth keeping.
    commitZones((current) => current.filter((entry) => entry.id !== zoneId))
    setDrawingZoneId(null)
    setActiveZoneId((current) => (current === zoneId ? null : current))
    setValidationMessage(null)
  }

  function handleDeleteZone(zoneId: string): void {
    commitZones((current) => current.filter((entry) => entry.id !== zoneId))
    setDrawingZoneId((current) => (current === zoneId ? null : current))
    setActiveZoneId((current) => (current === zoneId ? null : current))
    setValidationMessage(null)
  }

  function handleRenameZone(zoneId: string, name: string): void {
    commitZones((current) =>
      current.map((entry) => (entry.id === zoneId ? { ...entry, name } : entry)),
    )
  }

  function handleToggleZone(zoneId: string): void {
    commitZones((current) =>
      current.map((entry) => (entry.id === zoneId ? { ...entry, enabled: !entry.enabled } : entry)),
    )
  }

  function handleChangeKind(zoneId: string, kind: ZoneKind): void {
    commitZones((current) =>
      current.map((entry) =>
        entry.id === zoneId
          ? { ...entry, kind, sensitivity: kind === 'detect' ? entry.sensitivity : null }
          : entry,
      ),
    )
  }

  function handleChangeSensitivity(zoneId: string, sensitivity: number | null): void {
    commitZones((current) =>
      current.map((entry) => (entry.id === zoneId ? { ...entry, sensitivity } : entry)),
    )
  }

  function handleSave(): void {
    saveZones.mutate(zonesToPayload(zones), {
      onSuccess: (response) => {
        // The server assigns permanent ids to new zones; adopting its copy is
        // what keeps the next save from creating them a second time.
        const saved = sanitizeSavedZones(response.zones)
        resetTo(saved)
        setSavedSignature(zonesSignature(saved))
        setActiveZoneId(null)
        setSaveMessage('Zones saved.')
      },
    })
  }

  if (zonesQuery.isError) {
    return (
      <Alert variant="danger" title="Could not load this camera's saved zones.">
        <p>{errorText(zonesQuery.error)}</p>
        <p className="mt-1">
          Editing stays disabled until this loads, so a transient error here cannot save an
          incomplete copy over the zones already protecting this camera.
        </p>
        <Button
          variant="secondary"
          size="sm"
          className="mt-3"
          onClick={() => void zonesQuery.refetch()}
        >
          Retry
        </Button>
      </Alert>
    )
  }

  if (zonesQuery.isPending) {
    return <Skeleton className="h-96" />
  }

  const dropped = zonesQuery.data?.dropped ?? 0

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
      <div className="min-w-0 space-y-4">
        {dropped > 0 ? (
          <Alert variant="warning">
            {dropped} saved zone{dropped === 1 ? '' : 's'} could not be read and{' '}
            {dropped === 1 ? 'was' : 'were'} skipped rather than shown incorrectly. Saving from
            here will not restore {dropped === 1 ? 'it' : 'them'}.
          </Alert>
        ) : null}

        <ZoneCanvas
          cameraName={cameraName}
          previewUrl={preview.data ?? null}
          previewLoading={preview.isPending}
          previewError={preview.isError ? errorText(preview.error) : null}
          zones={zones}
          drawingZoneId={drawingZoneId}
          activeZoneId={activeZoneId}
          validationMessage={validationMessage}
          maxZonesReached={zones.length >= MAX_ZONES}
          newZoneLabel={newZoneKind === 'detect' ? 'New detection zone' : 'New ignore zone'}
          onSelectZone={setActiveZoneId}
          onStartZone={handleStartZone}
          onAddPoint={handleAddPoint}
          onMovePoint={handleMovePoint}
          onDeletePoint={handleDeletePoint}
          onFinishZone={handleFinishZone}
          onCancelZone={handleCancelZone}
          onDeleteZone={handleDeleteZone}
        />

        <div className="flex flex-wrap items-center gap-3">
          <span className="text-sm text-(--color-fg-muted)" aria-hidden="true">
            New zones
          </span>
          <Segmented<ZoneKind>
            ariaLabel="Kind of the next zone you draw"
            value={newZoneKind}
            onChange={setNewZoneKind}
            options={[
              { value: 'detect', label: 'Detect motion here', children: 'Detect motion here' },
              { value: 'ignore', label: 'Ignore this area', children: 'Ignore this area' },
            ]}
          />
        </div>

        <p className="text-sm text-(--color-fg-muted)">
          {hasDetectionZones
            ? 'This camera has detection zones, so only movement inside them counts. Each one is its own motion sensor in Home Assistant.'
            : 'With no detection zone, movement anywhere in the picture counts, except in ignored areas.'}
        </p>

        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" variant="secondary" size="sm" onClick={undo} disabled={!canUndo}>
            <Undo2 aria-hidden="true" />
            Undo
          </Button>
          <Button type="button" variant="secondary" size="sm" onClick={redo} disabled={!canRedo}>
            <Redo2 aria-hidden="true" />
            Redo
          </Button>
          <Button
            type="button"
            size="sm"
            className="ml-auto"
            onClick={handleSave}
            disabled={
              !unsavedChanges ||
              drawingZoneId !== null ||
              problems.length > 0 ||
              saveZones.isPending
            }
            loading={saveZones.isPending}
          >
            <Save aria-hidden="true" />
            Save zones
          </Button>
        </div>

        {problems.length > 0 ? (
          <Alert variant="warning">
            {problems.map((problem) => (
              <p key={problem}>{problem}</p>
            ))}
          </Alert>
        ) : null}
        {saveZones.isError ? <Alert variant="danger">{errorText(saveZones.error)}</Alert> : null}
        <p aria-live="polite" className="text-sm text-(--color-fg-muted)">
          {saveMessage ?? (unsavedChanges ? 'Unsaved changes.' : '')}
        </p>

        <section aria-label={`Zones for ${cameraName}, as a list`} className="space-y-2">
          <h2 className="text-sm font-medium text-(--color-fg-muted)">
            Zones ({zones.length}/{MAX_ZONES})
          </h2>
          <ZoneList
            zones={zones}
            activeZoneId={activeZoneId}
            drawingZoneId={drawingZoneId}
            onSelectZone={setActiveZoneId}
            onRenameZone={handleRenameZone}
            onToggleZone={handleToggleZone}
            onChangeKind={handleChangeKind}
            onChangeSensitivity={handleChangeSensitivity}
            onDeleteZone={handleDeleteZone}
          />
        </section>
      </div>

      <TuningPanel cameraId={cameraId} cameraName={cameraName} />
    </div>
  )
}

export function ZonesPage() {
  const cameras = useCameras()
  const [cameraId, setCameraId] = useState('')
  const rtspCameras = useMemo(
    () => (cameras.data ?? []).filter((camera) => camera.kind === 'rtsp'),
    [cameras.data],
  )

  // Land on the first camera automatically -- an editor with nothing selected
  // has nothing to show, and an operator arriving here almost always means
  // "the camera I'm standing in front of", which is usually the only one.
  useEffect(() => {
    if (!cameraId && rtspCameras.length > 0) {
      setCameraId(rtspCameras[0].id)
    }
  }, [rtspCameras, cameraId])

  const camera = rtspCameras.find((entry) => entry.id === cameraId) ?? null

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <PageHeader
        title="Motion zones"
        description="Draw named zones where motion counts, each with its own sensitivity and Home Assistant sensor, and areas to ignore such as a road or a tree."
      />

      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="zone-camera-select" className="sr-only">
          Camera
        </label>
        <Select
          id="zone-camera-select"
          className="w-auto min-w-44"
          value={cameraId}
          onChange={(event) => setCameraId(event.target.value)}
        >
          {rtspCameras.map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.name}
            </option>
          ))}
        </Select>
      </div>

      {cameras.isError ? (
        <Alert variant="danger" title="Could not load cameras.">
          {errorText(cameras.error)}
        </Alert>
      ) : null}

      {cameras.isPending ? (
        <Skeleton className="h-96" />
      ) : cameras.data && cameras.data.length === 0 ? (
        <EmptyState
          icon={Video}
          title="No cameras yet"
          description="Add a camera first, then come back here to draw its motion zones."
        />
      ) : cameraId ? (
        <ZoneEditor key={cameraId} cameraId={cameraId} cameraName={camera?.name ?? 'this camera'} />
      ) : null}
    </div>
  )
}
