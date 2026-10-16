import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, CalendarDays, HardDrive, ShieldCheck, TriangleAlert } from 'lucide-react'
import { useEffect, useState } from 'react'

import { api } from '@/api/client'
import { PageHeader } from '@/components/layout/PageHeader'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter,
  DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { RecordingCleanupPanel } from '@/features/storage/RecordingCleanupPanel'
import type { StoragePolicyPreview, StorageSettings, StorageUsage } from '@/features/storage/types'
import { formatBytes } from '@/lib/utils'

const GIBIBYTE = 1024 ** 3

const RETENTION_PRESETS = [7, 14, 30, 90]

function daysLabel(days: number | null): string {
  if (days == null || !Number.isFinite(days)) return 'Collecting data'
  if (days <= 0) return '0 hours'
  if (days < 1) return `${Math.max(Math.round(days * 24), 1)} hours`
  return `${days < 10 ? days.toFixed(1) : Math.round(days)} days`
}

function dateLabel(value: string | null): string {
  if (!value) return '—'
  return new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

/**
 * Proportional bar of what the disk holds.
 *
 * "Other" is deliberately shown: on a shared volume, most of the used space may
 * have nothing to do with Sentinel, and hiding that makes the numbers look wrong.
 */
function UsageBar({ usage }: { usage: StorageUsage }) {
  if (usage.total_bytes <= 0) return null

  const share = (bytes: number) => (bytes / usage.total_bytes) * 100
  const other = Math.max(
    0,
    usage.used_bytes -
      usage.recordings_bytes -
      usage.saved_clips_bytes -
      usage.snapshots_bytes -
      usage.database_bytes,
  )

  const parts = [
    { label: 'Recordings', bytes: usage.recordings_bytes, className: 'bg-(--color-accent)' },
    { label: 'Saved clips', bytes: usage.saved_clips_bytes, className: 'bg-(--color-success)' },
    { label: 'Snapshots', bytes: usage.snapshots_bytes, className: 'bg-(--color-motion)' },
    { label: 'Database', bytes: usage.database_bytes, className: 'bg-(--color-recording)' },
    { label: 'Other', bytes: other, className: 'bg-(--color-border-strong)' },
  ]

  return (
    <div className="space-y-3">
      <div
        className="flex h-3 w-full overflow-hidden rounded-full bg-(--color-bg-subtle)"
        role="img"
        aria-label={`${formatBytes(usage.used_bytes)} of ${formatBytes(usage.total_bytes)} used`}
      >
        {parts.map((part) =>
          part.bytes > 0 ? (
            <div
              key={part.label}
              className={part.className}
              style={{ width: `${share(part.bytes)}%` }}
            />
          ) : null,
        )}
      </div>
      <dl className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-sm sm:grid-cols-3 lg:grid-cols-5">
        {parts.map((part) => (
          <div key={part.label} className="flex items-center gap-2">
            <span className={`size-2 shrink-0 rounded-full ${part.className}`} aria-hidden="true" />
            <dt className="text-(--color-fg-muted)">{part.label}</dt>
            <dd className="tabular ml-auto font-medium">{formatBytes(part.bytes)}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

export function StoragePage() {
  const queryClient = useQueryClient()

  const usage = useQuery({
    queryKey: ['storage', 'usage'],
    queryFn: () => api.get<StorageUsage>('/storage'),
    refetchInterval: 60_000,
  })
  const settings = useQuery({
    queryKey: ['storage', 'settings'],
    queryFn: () => api.get<StorageSettings>('/storage/settings'),
  })

  const [retentionDays, setRetentionDays] = useState('')
  const [minFreeGb, setMinFreeGb] = useState('')
  const [review, setReview] = useState<StoragePolicyPreview | null>(null)
  const [confirmation, setConfirmation] = useState('')

  useEffect(() => {
    if (!settings.data) return
    setRetentionDays(settings.data.retention_days?.toString() ?? '')
    setMinFreeGb(
      settings.data.min_free_bytes == null ? '' : (settings.data.min_free_bytes / GIBIBYTE).toString(),
    )
  }, [settings.data])

  const save = useMutation({
    mutationFn: (payload: Partial<StorageSettings> & { preview_token: string; confirmation: string }) =>
      api.patch<StorageSettings>('/storage/settings', payload),
    onSuccess: async () => {
      setReview(null)
      await queryClient.invalidateQueries({ queryKey: ['storage'] })
    },
  })

  const preview = useMutation({
    mutationFn: (payload: Partial<StorageSettings>) =>
      api.post<StoragePolicyPreview>('/storage/settings/preview', payload),
    onSuccess: (result) => {
      save.reset()
      setConfirmation('')
      setReview(result)
    },
  })
  const reserve = settings.data?.min_free_bytes ?? 0
  const recommendedGiB = usage.data
    ? Math.min(20, Math.floor(usage.data.total_bytes / GIBIBYTE / 10))
    : 0
  const invalidPolicy = (retentionDays !== '' && (
    !Number.isInteger(Number(retentionDays)) || Number(retentionDays) < 1 || Number(retentionDays) > 3650
  )) || (minFreeGb !== '' && (!Number.isFinite(Number(minFreeGb)) || Number(minFreeGb) < 0))

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <PageHeader title="Storage" description="Where recordings are kept, and for how long." />

      {usage.data && !usage.data.available ? (
        <Alert variant="danger" title="Storage unavailable">
          {usage.data.reason}
        </Alert>
      ) : null}
      {usage.data?.available && usage.data.free_bytes < reserve ? (
        <Alert variant="warning" title="Recording reserve reached">
          Free space is {formatBytes(reserve - usage.data.free_bytes)} below the configured reserve.
          Automatic retention can remove the oldest eligible footage. Review the impact before
          increasing the reserve or shortening retention.
        </Alert>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Disk usage</CardTitle>
        </CardHeader>
        <CardContent className="space-y-5">
          {usage.isPending ? (
            <Skeleton className="h-24" />
          ) : usage.data ? (
            <>
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <span className="tabular text-2xl font-semibold">
                  {formatBytes(usage.data.free_bytes)}
                </span>
                <span className="text-sm text-(--color-fg-muted)">
                  free of {formatBytes(usage.data.total_bytes)}
                </span>
                <span className="ml-auto rounded-full bg-(--color-bg-subtle) px-2.5 py-1 text-xs font-semibold tabular-nums">
                  {usage.data.total_bytes > 0
                    ? `${Math.round((usage.data.used_bytes / usage.data.total_bytes) * 100)}% used`
                    : 'Unknown'}
                </span>
              </div>
              <UsageBar usage={usage.data} />
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                <div className="rounded-lg border border-(--color-border) bg-(--color-surface-subtle) p-3">
                  <p className="flex items-center gap-1.5 text-[11px] font-medium text-(--color-fg-subtle)">
                    <CalendarDays className="size-3.5" aria-hidden /> Estimated history
                  </p>
                  <p className="mt-1.5 text-lg font-semibold tabular-nums">
                    {daysLabel(usage.data.estimated_retention_days)}
                  </p>
                  <p className="mt-0.5 text-xs text-(--color-fg-subtle)">
                    {daysLabel(usage.data.estimated_days_remaining)} until reserve
                  </p>
                  <p className="mt-1 text-xs text-(--color-fg-subtle)">
                    Capacity at recent rate, excluding protected footage; retention and camera overrides may shorten it.
                  </p>
                </div>
                <div className="rounded-lg border border-(--color-border) bg-(--color-surface-subtle) p-3">
                  <p className="flex items-center gap-1.5 text-[11px] font-medium text-(--color-fg-subtle)">
                    <Activity className="size-3.5" aria-hidden /> Recording rate
                  </p>
                  <p className="mt-1.5 text-lg font-semibold tabular-nums">
                    {usage.data.recording_rate_bytes_per_hour == null
                      ? 'Collecting'
                      : `${formatBytes(usage.data.recording_rate_bytes_per_hour)}/h`}
                  </p>
                  <p className="mt-0.5 text-xs text-(--color-fg-subtle)">
                    {usage.data.recording_rate_bytes_per_hour == null
                      ? 'Needs recent footage'
                      : `${formatBytes(usage.data.recording_rate_bytes_per_hour * 24)}/day`}
                  </p>
                </div>
                <div className="rounded-lg border border-(--color-border) bg-(--color-surface-subtle) p-3">
                  <p className="flex items-center gap-1.5 text-[11px] font-medium text-(--color-fg-subtle)">
                    <ShieldCheck className="size-3.5" aria-hidden /> Saved clips
                  </p>
                  <p className="mt-1.5 text-lg font-semibold tabular-nums">
                    {formatBytes(usage.data.saved_clips_bytes)}
                  </p>
                  <p className="mt-0.5 text-xs text-(--color-fg-subtle)">
                    {usage.data.saved_clips_count.toLocaleString()} permanent exports
                  </p>
                </div>
                <div className="rounded-lg border border-(--color-border) bg-(--color-surface-subtle) p-3">
                  <p className="flex items-center gap-1.5 text-[11px] font-medium text-(--color-fg-subtle)">
                    <TriangleAlert className="size-3.5" aria-hidden /> Damaged
                  </p>
                  <p className="mt-1.5 text-lg font-semibold tabular-nums">
                    {usage.data.damaged_count.toLocaleString()}
                  </p>
                  <p className="mt-0.5 text-xs text-(--color-fg-subtle)">
                    Known gaps shown on the timeline
                  </p>
                </div>
              </div>
              <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
                <div className="flex justify-between gap-4">
                  <dt className="text-(--color-fg-muted)">Path</dt>
                  <dd className="truncate font-mono text-xs">{usage.data.path}</dd>
                </div>
                <div className="flex justify-between gap-4">
                  <dt className="text-(--color-fg-muted)">Segments</dt>
                  <dd className="tabular font-medium">
                    {usage.data.segment_count.toLocaleString()}
                    {usage.data.protected_count > 0
                      ? ` (${usage.data.protected_count} protected)`
                      : ''}
                  </dd>
                </div>
                <div className="flex justify-between gap-4">
                  <dt className="text-(--color-fg-muted)">Oldest footage</dt>
                  <dd className="text-right text-xs font-medium">
                    {dateLabel(usage.data.oldest_recording)}
                  </dd>
                </div>
                <div className="flex justify-between gap-4">
                  <dt className="text-(--color-fg-muted)">Newest footage</dt>
                  <dd className="text-right text-xs font-medium">
                    {dateLabel(usage.data.newest_recording)}
                  </dd>
                </div>
              </dl>
            </>
          ) : null}
        </CardContent>
      </Card>

      {usage.data && usage.data.camera_usage.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>Usage by camera</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {usage.data.camera_usage.map((camera) => {
              const share =
                usage.data.recordings_bytes > 0
                  ? (camera.bytes / usage.data.recordings_bytes) * 100
                  : 0
              return (
                <div
                  key={camera.camera_id}
                  className="rounded-lg border border-(--color-border) bg-(--color-surface-subtle) p-3"
                >
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-semibold">{camera.camera_name}</p>
                      <p className="mt-0.5 text-xs text-(--color-fg-subtle)">
                        {camera.segment_count.toLocaleString()} segments · oldest{' '}
                        {dateLabel(camera.oldest_recording)}
                      </p>
                    </div>
                    <div className="text-right">
                      <p className="text-sm font-semibold tabular-nums">{formatBytes(camera.bytes)}</p>
                      <p className="text-xs text-(--color-fg-subtle) tabular-nums">
                        {camera.recording_rate_bytes_per_hour == null
                          ? 'No recent rate'
                          : `${formatBytes(camera.recording_rate_bytes_per_hour)}/h`}
                      </p>
                    </div>
                  </div>
                  <div className="mt-2.5 h-1.5 overflow-hidden rounded-full bg-(--color-bg-subtle)">
                    <div
                      className="h-full rounded-full bg-(--color-accent)"
                      style={{ width: `${Math.min(share, 100)}%` }}
                    />
                  </div>
                  <div className="mt-1.5 flex justify-between gap-2 text-[11px] text-(--color-fg-subtle)">
                    <span>{share.toFixed(1)}% of recordings</span>
                    {camera.protected_bytes > 0 ? (
                      <span>{formatBytes(camera.protected_bytes)} protected</span>
                    ) : null}
                  </div>
                </div>
              )
            })}
          </CardContent>
        </Card>
      ) : null}

      {usage.data ? <RecordingCleanupPanel usage={usage.data} /> : null}

      <Card>
        <CardHeader>
          <CardTitle>Retention</CardTitle>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="space-y-2">
            <Label htmlFor="retention-days">Keep recordings for</Label>
            <div className="flex flex-wrap gap-2">
              {RETENTION_PRESETS.map((days) => (
                <Button
                  key={days}
                  type="button"
                  size="sm"
                  variant={retentionDays === String(days) ? 'primary' : 'secondary'}
                  onClick={() => setRetentionDays(String(days))}
                  disabled={preview.isPending || save.isPending}
                >
                  {days} days
                </Button>
              ))}
              <Input
                id="retention-days"
                type="number"
                min={1}
                max={3650}
                className="w-28"
                value={retentionDays}
                onChange={(event) => setRetentionDays(event.target.value)}
                aria-label="Custom retention in days"
                disabled={preview.isPending || save.isPending}
              />
            </div>
          </div>

          <div className="space-y-2">
            <Label htmlFor="min-free">Always keep free (GiB)</Label>
            <Input
              id="min-free"
              type="number"
              min={0}
              step="any"
              className="w-32"
              value={minFreeGb}
              onChange={(event) => setMinFreeGb(event.target.value)}
              disabled={preview.isPending || save.isPending}
            />
            {recommendedGiB > 0 && reserve < recommendedGiB * GIBIBYTE ? (
              <div className="space-y-2">
                <Button variant="secondary" size="sm" disabled={preview.isPending || save.isPending}
                  onClick={() => setMinFreeGb(String(recommendedGiB))}>
                  Review {recommendedGiB} GiB reserve
                </Button>
                <p className="text-xs text-(--color-fg-subtle)">
                  Suggested headroom for this volume. Selecting it only changes the draft; review and confirm to save.
                </p>
              </div>
            ) : null}
            <p className="text-xs text-(--color-fg-subtle)">
              Oldest unprotected recordings are removed first. Protected recordings are never
              deleted automatically. Saving a tighter policy authorizes the next automatic sweeps;
              removal can begin shortly after saving.
            </p>
          </div>

          {preview.isError ? (
            <Alert variant="danger">{preview.error.message}</Alert>
          ) : null}

          <div className="flex flex-wrap gap-2">
            <Button
              onClick={() => {
                preview.mutate({
                  retention_days: retentionDays ? Number(retentionDays) : null,
                  min_free_bytes: minFreeGb ? Math.round(Number(minFreeGb) * GIBIBYTE) : null,
                })
              }}
              loading={preview.isPending}
              disabled={!settings.data || !usage.data?.available || invalidPolicy || save.isPending}
            >
              Preview changes
            </Button>
          </div>

          {save.isSuccess ? (
            <Alert variant="success">Retention settings saved. Automatic sweeps now use this policy.</Alert>
          ) : null}
        </CardContent>
      </Card>

      <Dialog open={review !== null} onOpenChange={(open) => { if (!open && !save.isPending) setReview(null) }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Review retention policy</DialogTitle>
            <DialogDescription>
              Saving authorizes automatic deletion under this policy. This preview does not delete footage.
            </DialogDescription>
          </DialogHeader>
          {review ? <>
            <DialogBody className="space-y-4">
              <p className="text-sm">
                Proposed: {review.proposed_settings.retention_days ?? 'unlimited'} days;
                reserve {((review.proposed_settings.min_free_bytes ?? 0) / GIBIBYTE).toLocaleString()} GiB.
              </p>
              <dl className="space-y-2 text-sm">
                <div><dt>Free now (measured)</dt><dd>{formatBytes(review.free_bytes)}</dd></div>
                <div><dt>Eligible recordings</dt><dd>{review.segment_count.toLocaleString()} segments · {formatBytes(review.estimated_bytes)} estimated</dd></div>
                <div><dt>Affected footage</dt><dd>{dateLabel(review.oldest_affected_recording)} — {dateLabel(review.newest_affected_recording)}</dd></div>
                <div><dt>Expired motion events</dt><dd>{review.expired_event_count.toLocaleString()}</dd></div>
                <div><dt>Preserved</dt><dd>{review.protected_count} protected segments · {review.active_or_recent_count} active/recent segments · all saved exports</dd></div>
                <div><dt>Estimated free after eligible removal</dt><dd>{formatBytes(review.estimated_free_bytes_after)}</dd></div>
                <div><dt>Recording capacity at recent rate</dt><dd>{daysLabel(review.estimated_retention_days)}</dd></div>
              </dl>
              {review.warnings.map((warning) => <p key={warning} className="text-xs text-(--color-fg-muted)">{warning}</p>)}
              {review.remaining_shortfall_bytes > 0 ? <Alert variant="warning">
                Still short by {formatBytes(review.remaining_shortfall_bytes)} after all eligible footage is removed.
              </Alert> : null}
              {review.required_confirmation ? <div className="space-y-2">
                <Label htmlFor="retention-confirmation">Type {review.required_confirmation} to authorize this policy</Label>
                <Input id="retention-confirmation" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" />
              </div> : null}
              {save.isError ? <Alert variant="danger">{save.error.message} Close this review and preview again.</Alert> : null}
            </DialogBody>
            <DialogFooter>
              <Button variant="secondary" disabled={save.isPending} onClick={() => setReview(null)}>Cancel</Button>
              <Button loading={save.isPending}
                disabled={Boolean(review.required_confirmation && confirmation !== review.required_confirmation)}
                onClick={() => save.mutate({ ...review.proposed_settings, preview_token: review.preview_token, confirmation })}>
                Confirm and save policy
              </Button>
            </DialogFooter>
          </> : null}
        </DialogContent>
      </Dialog>

      <p className="flex items-center gap-2 text-xs text-(--color-fg-subtle)">
        <HardDrive className="size-3.5" aria-hidden="true" />
        Recording stops automatically if this storage becomes unavailable, so the system disk
        cannot fill up.
      </p>
    </div>
  )
}
