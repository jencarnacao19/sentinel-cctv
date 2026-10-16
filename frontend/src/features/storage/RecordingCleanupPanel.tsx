import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Trash2 } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'

import { api } from '@/api/client'
import { queryKeys } from '@/api/endpoints'
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
  CleanupPreview,
  CleanupRequest,
  CleanupStatus,
  StorageUsage,
} from '@/features/storage/types'
import { formatBytes } from '@/lib/utils'

const AGE_OPTIONS = [
  { value: 'all', label: 'All history' },
  { value: '1', label: 'Older than 24 hours' },
  { value: '7', label: 'Older than 7 days' },
  { value: '30', label: 'Older than 30 days' },
  { value: '90', label: 'Older than 90 days' },
] as const

function requestFor(cameraId: string, age: string, includeProtected: boolean): CleanupRequest {
  const request: CleanupRequest = { include_protected: includeProtected }
  if (cameraId !== 'all') request.camera_id = cameraId
  if (age !== 'all') {
    const before = new Date(Date.now() - Number(age) * 24 * 60 * 60 * 1000)
    request.before = before.toISOString()
  }
  return request
}

function selectionDescription(request: CleanupRequest, usage: StorageUsage): string {
  const camera = usage.camera_usage.find((entry) => entry.camera_id === request.camera_id)
  const target = camera ? camera.camera_name : 'every camera'
  if (!request.before) return `All recording history from ${target}`
  return `Recording history from ${target} before ${new Date(request.before).toLocaleString()}`
}

export function RecordingCleanupPanel({ usage }: { usage: StorageUsage }) {
  const queryClient = useQueryClient()
  const [cameraId, setCameraId] = useState('all')
  const [age, setAge] = useState('30')
  const [includeProtected, setIncludeProtected] = useState(false)
  const [pendingRequest, setPendingRequest] = useState<CleanupRequest | null>(null)
  const [confirmation, setConfirmation] = useState('')
  const [confirmOpen, setConfirmOpen] = useState(false)
  const handledJob = useRef<string | null>(null)

  const cleanup = useQuery({
    queryKey: ['storage', 'cleanup'],
    queryFn: () => api.get<CleanupStatus>('/storage/cleanup'),
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
    void queryClient.invalidateQueries({ queryKey: ['storage', 'usage'] })
    void queryClient.invalidateQueries({ queryKey: queryKeys.recordings })
  }, [cleanup.data, queryClient])

  const preview = useMutation({
    mutationFn: (payload: CleanupRequest) =>
      api.post<CleanupPreview>('/storage/cleanup/preview', payload),
    onSuccess: () => {
      setConfirmation('')
      setConfirmOpen(true)
    },
  })

  const start = useMutation({
    mutationFn: (payload: CleanupRequest) =>
      api.post<CleanupStatus>('/storage/cleanup', payload),
    onSuccess: async (state) => {
      setConfirmOpen(false)
      setPendingRequest(null)
      queryClient.setQueryData(['storage', 'cleanup'], state)
      await queryClient.invalidateQueries({ queryKey: ['storage', 'cleanup'] })
    },
  })

  const cancel = useMutation({
    mutationFn: () => api.delete<CleanupStatus>('/storage/cleanup'),
    onSuccess: (state) => queryClient.setQueryData(['storage', 'cleanup'], state),
  })

  const running = cleanup.data?.phase === 'running' || cleanup.data?.phase === 'cancelling'
  const progress = useMemo(() => {
    const state = cleanup.data
    if (!state || state.matched_segments <= 0) return 0
    return Math.min((state.deleted_segments / state.matched_segments) * 100, 100)
  }, [cleanup.data])

  function review(payload: CleanupRequest) {
    setPendingRequest(payload)
    preview.mutate(payload)
  }

  const selectedDescription = pendingRequest
    ? selectionDescription(pendingRequest, usage)
    : 'Selected recording history'

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle>Manage recordings</CardTitle>
        </CardHeader>
        <CardContent className="space-y-5">
          <p className="text-sm leading-5 text-(--color-fg-muted)">
            Remove footage by camera and age, or clear the recording volume in one operation.
            Permanent saved clips are stored separately and are not deleted.
          </p>

          {running && cleanup.data ? (
            <div className="space-y-3 rounded-lg border border-(--color-border) bg-(--color-surface-subtle) p-4">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <div>
                  <p className="text-sm font-semibold">Cleanup in progress</p>
                  <p className="mt-1 text-xs text-(--color-fg-muted)">{cleanup.data.message}</p>
                </div>
                <p className="text-sm font-medium tabular-nums">
                  {cleanup.data.deleted_segments.toLocaleString()} /{' '}
                  {cleanup.data.matched_segments.toLocaleString()}
                </p>
              </div>
              <div
                className="h-2 overflow-hidden rounded-full bg-(--color-bg-subtle)"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={cleanup.data.matched_segments}
                aria-valuenow={cleanup.data.deleted_segments}
              >
                <div
                  className="h-full rounded-full bg-(--color-accent) transition-[width]"
                  style={{ width: `${progress}%` }}
                />
              </div>
              <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-(--color-fg-muted)">
                <span>{formatBytes(cleanup.data.freed_bytes)} freed</span>
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
            <Alert variant={cleanup.data.failed_segments > 0 ? 'warning' : 'success'}>
              Removed {cleanup.data.deleted_segments.toLocaleString()} segments and freed{' '}
              {formatBytes(cleanup.data.freed_bytes)}
              {cleanup.data.phase === 'cancelled' ? ' before the cleanup was stopped' : ''}. Saved
              clips were preserved.
            </Alert>
          ) : null}

          {!running && cleanup.data?.phase === 'failed' ? (
            <Alert variant="danger">{cleanup.data.message ?? 'The cleanup failed.'}</Alert>
          ) : null}

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="cleanup-camera">Camera</Label>
              <Select
                id="cleanup-camera"
                value={cameraId}
                onChange={(event) => setCameraId(event.target.value)}
                disabled={running}
              >
                <option value="all">All cameras</option>
                {usage.camera_usage
                  .filter((camera) => camera.segment_count > 0)
                  .map((camera) => (
                    <option key={camera.camera_id} value={camera.camera_id}>
                      {camera.camera_name} — {camera.segment_count.toLocaleString()} segments
                    </option>
                  ))}
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="cleanup-age">Age</Label>
              <Select
                id="cleanup-age"
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
          </div>

          <CheckboxField
            checked={includeProtected}
            onChange={(event) => setIncludeProtected(event.target.checked)}
            disabled={running}
            label="Include protected recording segments"
            description="This still leaves permanent saved clips and exports untouched."
          />

          {preview.isError ? (
            <Alert variant="danger">Could not calculate this recording selection.</Alert>
          ) : null}

          <div className="flex flex-wrap gap-2 border-t border-(--color-border) pt-4">
            <Button
              variant="secondary"
              disabled={running}
              loading={preview.isPending}
              onClick={() => review(requestFor(cameraId, age, includeProtected))}
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
              Erase all recording history
            </Button>
          </div>
        </CardContent>
      </Card>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Confirm recording cleanup</DialogTitle>
            <DialogDescription>
              This permanently removes the selected video files and cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="space-y-5">
            {preview.data ? (
              <>
                <div className="space-y-2 rounded-lg border border-(--color-danger)/35 bg-(--color-danger)/10 p-4">
                  <p className="text-sm font-semibold">{selectedDescription}</p>
                  <p className="text-2xl font-semibold tabular-nums">
                    {formatBytes(preview.data.bytes)}
                  </p>
                  <p className="text-sm text-(--color-fg-muted)">
                    {preview.data.segment_count.toLocaleString()} video segments
                    {preview.data.protected_count > 0
                      ? `, including ${preview.data.protected_count.toLocaleString()} protected`
                      : ''}
                  </p>
                </div>
                <Alert variant="info">
                  Permanent saved clips and exports remain available. For a full-history cleanup,
                  continuous recorders pause briefly and resume automatically.
                </Alert>
                {preview.data.segment_count === 0 ? (
                  <Alert variant="info">No recording segments match this selection.</Alert>
                ) : null}
                <div className="space-y-2">
                  <Label htmlFor="cleanup-confirmation">
                    Type <strong>{preview.data.required_confirmation}</strong>
                  </Label>
                  <Input
                    id="cleanup-confirmation"
                    autoComplete="off"
                    value={confirmation}
                    onChange={(event) => setConfirmation(event.target.value)}
                  />
                </div>
              </>
            ) : null}
            {start.isError ? (
              <Alert variant="danger">The recording cleanup could not be started.</Alert>
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
                preview.data.segment_count === 0 ||
                !pendingRequest ||
                confirmation !== preview.data.required_confirmation
              }
              loading={start.isPending}
              onClick={() => {
                if (!pendingRequest) return
                start.mutate({ ...pendingRequest, confirmation })
              }}
            >
              Delete selected footage
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
