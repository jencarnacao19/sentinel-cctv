import { Clock3, Download, Film, ShieldCheck, Trash2 } from 'lucide-react'
import { useMemo } from 'react'

import { API_BASE, ApiError, NetworkError } from '@/api/client'
import { EmptyState } from '@/components/EmptyState'
import { PageHeader } from '@/components/layout/PageHeader'
import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { useCameras } from '@/features/cameras/useCameras'
import { isExportActive, type ExportJob } from '@/features/timeline/types'
import { useDeleteExport, useExports, useUpdateExport } from '@/features/timeline/useTimeline'
import { formatBytes } from '@/lib/utils'

function localTime(value: string): string {
  return new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' })
}

function durationLabel(job: ExportJob): string {
  const seconds = Math.round(
    (new Date(job.end_at).getTime() - new Date(job.start_at).getTime()) / 1000,
  )
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  return minutes > 0 ? `${minutes}m ${remainder}s` : `${remainder}s`
}

function errorText(error: unknown): string {
  if (error instanceof NetworkError) return 'Could not reach the Sentinel server.'
  if (error instanceof ApiError) return error.message
  return 'Something went wrong.'
}

export function ClipsPage() {
  const cameras = useCameras()
  const exports = useExports()
  const updateExport = useUpdateExport()
  const deleteExport = useDeleteExport()
  const cameraNames = useMemo(
    () => new Map((cameras.data ?? []).map((camera) => [camera.id, camera.name])),
    [cameras.data],
  )
  const jobs = exports.data?.exports ?? []

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <PageHeader
        title="Saved clips"
        description="Permanent MP4 clips. Automatic storage retention never removes these."
      />

      {exports.isError || cameras.isError ? (
        <Alert variant="danger" title="Could not load saved clips.">
          {errorText(exports.error ?? cameras.error)}
        </Alert>
      ) : null}
      {updateExport.isError ? (
        <Alert variant="danger" title="Could not change clip protection.">
          {errorText(updateExport.error)}
        </Alert>
      ) : null}
      {deleteExport.isError ? (
        <Alert variant="danger" title="Could not delete the clip.">
          {errorText(deleteExport.error)}
        </Alert>
      ) : null}

      {exports.isPending || cameras.isPending ? (
        <div className="space-y-2">
          <Skeleton className="h-20" />
          <Skeleton className="h-20" />
        </div>
      ) : jobs.length === 0 ? (
        <EmptyState
          icon={Film}
          title="No clips saved yet"
          description="Open Timeline, choose a moment and save the previous 30 seconds, 1 minute, 5 minutes or any custom range."
        />
      ) : (
        <ul className="space-y-2">
          {jobs.map((job) => (
            <li
              key={job.id}
              className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-(--color-border) bg-(--color-surface-raised) px-4 py-3"
            >
              <div className="min-w-0">
                <p className="truncate text-sm font-medium">
                  {cameraNames.get(job.camera_id) ?? 'Removed camera'} · {localTime(job.start_at)}
                </p>
                <p className="mt-0.5 text-xs text-(--color-fg-muted)">
                  {durationLabel(job)}
                  {job.size_bytes != null ? ` · ${formatBytes(job.size_bytes)}` : ''}
                  {isExportActive(job) ? ` · ${Math.round(job.progress * 100)}%` : ''}
                  {job.error ? ` · ${job.error}` : ''}
                </p>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={job.protected ? 'success' : 'warning'}>
                  {job.protected ? 'Saved permanently' : 'Temporary'}
                </Badge>
                <Badge variant={job.status === 'failed' ? 'danger' : 'neutral'}>{job.status}</Badge>
                {job.status === 'complete' ? (
                  <a
                    href={`${API_BASE}/exports/${job.id}/download`}
                    className="inline-flex min-h-11 items-center gap-1.5 rounded-lg border border-(--color-border) px-3 text-xs font-medium hover:bg-(--color-surface-hover)"
                    download
                  >
                    <Download className="size-4" aria-hidden />
                    Download
                  </a>
                ) : null}
                {job.status !== 'expired' && job.status !== 'cancelled' ? (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      updateExport.mutate({ id: job.id, protectedClip: !job.protected })
                    }
                    title={job.protected ? 'Allow deletion after 24 hours' : 'Keep permanently'}
                  >
                    {job.protected ? (
                      <ShieldCheck className="size-4" aria-hidden />
                    ) : (
                      <Clock3 className="size-4" aria-hidden />
                    )}
                    {job.protected ? 'Permanent' : 'Keep'}
                  </Button>
                ) : null}
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => deleteExport.mutate(job.id)}
                  aria-label={`Delete clip from ${localTime(job.start_at)}`}
                >
                  <Trash2 className="size-4" aria-hidden />
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
