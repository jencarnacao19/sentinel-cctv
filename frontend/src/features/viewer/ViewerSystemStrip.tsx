import { Skeleton } from '@/components/ui/skeleton'
import { useViewerSystemStatus } from '@/features/viewer/useViewerSystemStatus'
import { formatBytes } from '@/lib/utils'

function decimal(value: number | null, suffix = ''): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${value < 10 ? value.toFixed(1) : Math.round(value)}${suffix}`
}

function localDate(value: string | null): string {
  if (!value) return 'No footage yet'
  return new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

export function ViewerSystemStrip({ compact = false }: { compact?: boolean }) {
  const query = useViewerSystemStatus()

  if (query.isPending) return <Skeleton className={compact ? 'h-20' : 'h-28'} />
  if (!query.data) return null

  const status = query.data
  const healthy = status.status === 'healthy' && status.storage_available
  const recordersHealthy =
    status.privacy_enabled ||
    (status.recorders_expected > 0 && status.recorders_running === status.recorders_expected)

  return (
    <details className="group overflow-hidden rounded-(--radius-card) border border-(--color-border) bg-(--color-surface-raised)">
      <summary className="grid cursor-pointer list-none grid-cols-2 divide-x divide-y divide-(--color-border) marker:hidden sm:grid-cols-4 sm:divide-y-0">
        <div className="min-w-0 p-3.5 sm:p-4">
          <span className="flex items-center gap-2 text-[11px] font-medium text-(--color-fg-subtle)">
            <span
              className={`size-1.5 ${healthy ? 'bg-(--color-success)' : 'bg-(--color-danger)'}`}
              aria-hidden
            />
            System
          </span>
          <span className="mt-1.5 block truncate text-sm font-semibold capitalize">
            {status.status}
          </span>
        </div>

        <div className="min-w-0 p-3.5 sm:p-4">
          <span className="flex items-center gap-2 text-[11px] font-medium text-(--color-fg-subtle)">
            <span className="size-1.5 bg-(--color-accent)" aria-hidden />
            Cameras
          </span>
          <span className="mt-1.5 block truncate text-sm font-semibold tabular-nums">
            {status.privacy_enabled
              ? 'Privacy pause'
              : `${status.cameras_online}/${status.cameras_total} online`}
          </span>
        </div>

        <div className="min-w-0 p-3.5 sm:p-4">
          <span
            className="flex items-center gap-2 text-[11px] font-medium text-(--color-fg-subtle)"
          >
            <span
              className={`size-1.5 ${
                recordersHealthy ? 'bg-(--color-recording)' : 'bg-(--color-warning)'
              }`}
              aria-hidden
            />
            Recording
          </span>
          <span className="mt-1.5 block truncate text-sm font-semibold tabular-nums">
            {status.privacy_enabled
              ? 'Paused safely'
              : `${status.recorders_running}/${status.recorders_expected} active`}
          </span>
        </div>

        <div className="min-w-0 p-3.5 sm:p-4">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11px] font-medium text-(--color-fg-subtle)">
              Disk
            </span>
            <span className="text-xs font-semibold tabular-nums">
              {Math.round(status.storage_used_percent)}%
            </span>
          </div>
          <div className="mt-2 h-1.5 overflow-hidden bg-(--color-bg-subtle)">
            <div
              className={`h-full transition-[width] ${
                status.storage_used_percent >= 90
                  ? 'bg-(--color-danger)'
                  : status.storage_used_percent >= 75
                    ? 'bg-(--color-warning)'
                    : 'bg-(--color-accent)'
              }`}
              style={{ width: `${Math.min(status.storage_used_percent, 100)}%` }}
            />
          </div>
          <span className="mt-1 block truncate text-[11px] text-(--color-fg-subtle)">
            {formatBytes(status.storage_free_bytes)} free
          </span>
        </div>
      </summary>

      <div className="grid gap-3 border-t border-(--color-border) bg-(--color-surface-subtle) p-3 text-xs sm:grid-cols-2 lg:grid-cols-4 sm:p-4">
        <div>
          <p className="text-(--color-fg-subtle)">Estimated history</p>
          <p className="mt-0.5 font-semibold tabular-nums">
            {decimal(status.estimated_retention_days, ' days')}
          </p>
          <p className="mt-0.5 text-(--color-fg-subtle)">
            Policy limit: {status.configured_retention_days ?? 'unlimited'} days
          </p>
        </div>
        <div>
          <p className="text-(--color-fg-subtle)">Current recording rate</p>
          <p className="mt-0.5 font-semibold tabular-nums">
            {status.recording_rate_bytes_per_hour == null
              ? 'Collecting data'
              : `${formatBytes(status.recording_rate_bytes_per_hour)}/hour`}
          </p>
          <p className="mt-0.5 text-(--color-fg-subtle)">
            {decimal(status.estimated_days_remaining, ' days')} until the reserve
          </p>
        </div>
        <div>
          <p className="text-(--color-fg-subtle)">Available footage</p>
          <p className="mt-0.5 font-semibold">Since {localDate(status.oldest_recording)}</p>
          <p className="mt-0.5 text-(--color-fg-subtle)">
            {formatBytes(status.recordings_bytes)} indexed
          </p>
        </div>
        <div>
          <p className="text-(--color-fg-subtle)">Protected footage</p>
          <p className="mt-0.5 font-semibold tabular-nums">{formatBytes(status.saved_clips_bytes)}</p>
          <p className="mt-0.5 text-(--color-fg-subtle)">
            {status.saved_clips_count} clip{status.saved_clips_count === 1 ? '' : 's'} · never auto-deleted
          </p>
        </div>
      </div>
    </details>
  )
}
