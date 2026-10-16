import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Trash2 } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'

import { api } from '@/api/client'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { CheckboxField } from '@/components/ui/checkbox'
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
import type {
  EventCleanupPreview,
  EventCleanupRequest,
  EventCleanupStatus,
  EventSummary,
} from '@/features/events/types'
import { formatBytes } from '@/lib/utils'

const AGE_OPTIONS = [
  { value: 'all', label: 'All history' },
  { value: '1', label: 'Older than 24 hours' },
  { value: '7', label: 'Older than 7 days' },
  { value: '30', label: 'Older than 30 days' },
] as const

const DURATION_OPTIONS = [
  { value: 'all', label: 'Any duration' },
  { value: '1', label: 'At most 1 second' },
  { value: '3', label: 'At most 3 seconds' },
  { value: '10', label: 'At most 10 seconds' },
] as const

function buildRequest(
  cameraId: string,
  age: string,
  maxDuration: string,
  includeProtected: boolean,
): EventCleanupRequest {
  const request: EventCleanupRequest = { include_protected: includeProtected }
  if (cameraId !== 'all') request.camera_id = cameraId
  if (age !== 'all') {
    request.end = new Date(Date.now() - Number(age) * 24 * 60 * 60 * 1000).toISOString()
  }
  if (maxDuration !== 'all') request.max_duration_seconds = Number(maxDuration)
  return request
}

function describeSelection(request: EventCleanupRequest, summary: EventSummary): string {
  const camera = summary.cameras.find((entry) => entry.camera_id === request.camera_id)
  const target = camera?.camera_name ?? 'every camera'
  const age = request.end
    ? ` before ${new Date(request.end).toLocaleString()}`
    : ' across all history'
  const duration =
    request.max_duration_seconds != null
      ? `, lasting at most ${request.max_duration_seconds} seconds`
      : ''
  return `Events from ${target}${age}${duration}`
}

export function EventCleanupPanel({ summary }: { summary: EventSummary }) {
  const queryClient = useQueryClient()
  const [cameraId, setCameraId] = useState('all')
  const [age, setAge] = useState('7')
  const [maxDuration, setMaxDuration] = useState('all')
  const [includeProtected, setIncludeProtected] = useState(false)
  const [pendingRequest, setPendingRequest] = useState<EventCleanupRequest | null>(null)
  const [confirmation, setConfirmation] = useState('')
  const [confirmOpen, setConfirmOpen] = useState(false)
  const handledJob = useRef<string | null>(null)

  const cleanup = useQuery({
    queryKey: ['events', 'cleanup'],
    queryFn: () => api.get<EventCleanupStatus>('/events/cleanup'),
    refetchInterval: (query) => {
      const phase = query.state.data?.phase
      return phase === 'running' || phase === 'cancelling' ? 1_000 : false
    },
  })

  useEffect(() => {
    const state = cleanup.data
    if (!state?.job_id || handledJob.current === state.job_id) return
    if (state.phase !== 'completed' && state.phase !== 'cancelled' && state.phase !== 'failed') return
    handledJob.current = state.job_id
    void queryClient.invalidateQueries({ queryKey: ['events'] })
  }, [cleanup.data, queryClient])

  const preview = useMutation({
    mutationFn: (payload: EventCleanupRequest) =>
      api.post<EventCleanupPreview>('/events/cleanup/preview', payload),
    onSuccess: () => {
      setConfirmation('')
      setConfirmOpen(true)
    },
  })

  const start = useMutation({
    mutationFn: (payload: EventCleanupRequest) =>
      api.post<EventCleanupStatus>('/events/cleanup', payload),
    onSuccess: async (state) => {
      setConfirmOpen(false)
      setPendingRequest(null)
      queryClient.setQueryData(['events', 'cleanup'], state)
      await queryClient.invalidateQueries({ queryKey: ['events', 'cleanup'] })
    },
  })

  const cancel = useMutation({
    mutationFn: () => api.delete<EventCleanupStatus>('/events/cleanup'),
    onSuccess: (state) => queryClient.setQueryData(['events', 'cleanup'], state),
  })

  const running = cleanup.data?.phase === 'running' || cleanup.data?.phase === 'cancelling'
  const progress = useMemo(() => {
    const state = cleanup.data
    if (!state || state.matched_events <= 0) return 0
    return Math.min((state.deleted_events / state.matched_events) * 100, 100)
  }, [cleanup.data])

  function review(payload: EventCleanupRequest) {
    setPendingRequest(payload)
    preview.mutate(payload)
  }

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle>Manage event history</CardTitle>
        </CardHeader>
        <CardContent className="space-y-5">
          <p className="text-sm leading-5 text-(--color-fg-muted)">
            Remove detected-event metadata and its images by camera, age or duration. Video
            recordings and saved exports are never deleted here, and detection keeps running.
          </p>

          {running && cleanup.data ? (
            <div className="space-y-3 rounded-lg border border-(--color-border) bg-(--color-surface-subtle) p-4">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <div>
                  <p className="text-sm font-semibold">Event cleanup in progress</p>
                  <p className="mt-1 text-xs text-(--color-fg-muted)">{cleanup.data.message}</p>
                </div>
                <p className="text-sm font-medium tabular-nums">
                  {cleanup.data.deleted_events.toLocaleString()} /{' '}
                  {cleanup.data.matched_events.toLocaleString()}
                </p>
              </div>
              <div
                className="h-2 overflow-hidden rounded-full bg-(--color-bg-subtle)"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={cleanup.data.matched_events}
                aria-valuenow={cleanup.data.deleted_events}
              >
                <div
                  className="h-full rounded-full bg-(--color-accent) transition-[width]"
                  style={{ width: `${progress}%` }}
                />
              </div>
              <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-(--color-fg-muted)">
                <span>{formatBytes(cleanup.data.freed_bytes)} of event images removed</span>
                <Button
                  size="sm"
                  variant="secondary"
                  loading={cancel.isPending || cleanup.data.phase === 'cancelling'}
                  onClick={() => cancel.mutate()}
                >
                  Stop cleanup
                </Button>
              </div>
            </div>
          ) : null}

          {!running &&
          (cleanup.data?.phase === 'completed' || cleanup.data?.phase === 'cancelled') ? (
            <Alert variant={cleanup.data.failed_evidence_files > 0 ? 'warning' : 'success'}>
              Removed {cleanup.data.deleted_events.toLocaleString()} events and{' '}
              {formatBytes(cleanup.data.freed_bytes)} of images
              {cleanup.data.phase === 'cancelled' ? ' before stopping' : ''}. Recordings and
              exports were preserved.
            </Alert>
          ) : null}

          {!running && cleanup.data?.phase === 'failed' ? (
            <Alert variant="danger">{cleanup.data.message ?? 'The event cleanup failed.'}</Alert>
          ) : null}

          <div className="grid gap-4 md:grid-cols-3">
            <div className="space-y-2">
              <Label htmlFor="event-cleanup-camera">Camera</Label>
              <Select
                id="event-cleanup-camera"
                value={cameraId}
                onChange={(event) => setCameraId(event.target.value)}
                disabled={running}
              >
                <option value="all">All cameras — {summary.total.toLocaleString()} events</option>
                {summary.cameras.map((camera) => (
                  <option key={camera.camera_id} value={camera.camera_id}>
                    {camera.camera_name} — {camera.count.toLocaleString()}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="event-cleanup-age">Age</Label>
              <Select
                id="event-cleanup-age"
                value={age}
                onChange={(event) => setAge(event.target.value)}
                disabled={running}
              >
                {AGE_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="event-cleanup-duration">Duration</Label>
              <Select
                id="event-cleanup-duration"
                value={maxDuration}
                onChange={(event) => setMaxDuration(event.target.value)}
                disabled={running}
              >
                {DURATION_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </Select>
            </div>
          </div>

          <CheckboxField
            checked={includeProtected}
            onChange={(event) => setIncludeProtected(event.target.checked)}
            disabled={running}
            label="Include protected events"
            description="Only affects event metadata and event images; saved video exports remain untouched."
          />

          {preview.isError ? (
            <Alert variant="danger">Could not calculate this event selection.</Alert>
          ) : null}

          <div className="flex flex-wrap gap-2 border-t border-(--color-border) pt-4">
            <Button
              variant="secondary"
              disabled={running}
              loading={preview.isPending}
              onClick={() =>
                review(buildRequest(cameraId, age, maxDuration, includeProtected))
              }
            >
              Review selection
            </Button>
            <Button
              variant="danger"
              disabled={running}
              loading={preview.isPending}
              onClick={() => review({ include_protected: true })}
            >
              <Trash2 aria-hidden="true" />
              Erase all event history
            </Button>
          </div>
        </CardContent>
      </Card>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Confirm event cleanup</DialogTitle>
            <DialogDescription>
              This permanently removes the selected events and their images. It cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="space-y-5">
            {preview.data && pendingRequest ? (
              <>
                <div className="space-y-2 rounded-lg border border-(--color-danger)/35 bg-(--color-danger)/10 p-4">
                  <p className="text-sm font-semibold">
                    {describeSelection(pendingRequest, summary)}
                  </p>
                  <p className="text-2xl font-semibold tabular-nums">
                    {preview.data.event_count.toLocaleString()} events
                  </p>
                  <p className="text-sm text-(--color-fg-muted)">
                    {preview.data.evidence_files.toLocaleString()} event images ·{' '}
                    {formatBytes(preview.data.evidence_bytes)}
                    {preview.data.protected_count > 0
                      ? ` · ${preview.data.protected_count.toLocaleString()} protected`
                      : ''}
                  </p>
                </div>
                <Alert variant="info">
                  Video recordings, permanent exports and live detection remain active. Events
                  detected after this cleanup starts are preserved.
                </Alert>
                {preview.data.active_events_excluded > 0 ? (
                  <Alert variant="info">
                    {preview.data.active_events_excluded} event currently in progress will not be
                    removed.
                  </Alert>
                ) : null}
                {preview.data.event_count === 0 ? (
                  <Alert variant="info">No completed events match this selection.</Alert>
                ) : null}
                <div className="space-y-2">
                  <Label htmlFor="event-cleanup-confirmation">
                    Type <strong>{preview.data.required_confirmation}</strong>
                  </Label>
                  <Input
                    id="event-cleanup-confirmation"
                    autoComplete="off"
                    value={confirmation}
                    onChange={(event) => setConfirmation(event.target.value)}
                  />
                </div>
              </>
            ) : null}
            {start.isError ? (
              <Alert variant="danger">The event cleanup could not be started.</Alert>
            ) : null}
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              disabled={
                !preview.data ||
                preview.data.event_count === 0 ||
                !pendingRequest ||
                confirmation !== preview.data.required_confirmation
              }
              loading={start.isPending}
              onClick={() => {
                if (!pendingRequest) return
                start.mutate({ ...pendingRequest, confirmation })
              }}
            >
              Delete selected events
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
