import { useQuery } from '@tanstack/react-query'
import { Plus, Video } from 'lucide-react'
import { Link } from 'react-router-dom'

import { queryKeys, recordingsApi, systemApi } from '@/api/endpoints'
import { PageHeader } from '@/components/layout/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import {
  DiskPressureNotice,
  FailureNotice,
  RecorderStoppedNotice,
} from '@/components/system/StateNotice'
import { Button } from '@/components/ui/button'
import { SurfaceGroup } from '@/components/ui/form'
import { Skeleton } from '@/components/ui/skeleton'
import { Status, type StatusTone } from '@/components/ui/status'
import { STATUS_LABEL, STATUS_TONE } from '@/features/cameras/cameraKind'
import { useCameras } from '@/features/cameras/useCameras'
import { cn, formatBytes } from '@/lib/utils'

function StatusMetric({
  label,
  value,
  tone,
}: {
  label: string
  value: string
  tone?: 'success' | 'danger' | 'neutral'
}) {
  return (
    <div className="min-w-0 px-4 py-4">
      <p className="text-xs font-medium text-(--color-fg-subtle)">{label}</p>
      <p
        className={cn(
          'tabular mt-1.5 text-2xl leading-8 font-semibold tracking-[-0.02em]',
          tone === 'success'
            ? 'text-(--color-success)'
            : tone === 'danger'
              ? 'text-(--color-danger)'
              : 'text-(--color-fg)',
        )}
      >
        {value}
      </p>
    </div>
  )
}

const COMPONENT_TONE: Record<string, StatusTone> = {
  ok: 'ok',
  degraded: 'warning',
  failed: 'danger',
}

export function DashboardPage() {
  const health = useQuery({
    queryKey: queryKeys.systemHealth,
    queryFn: systemApi.health,
    refetchInterval: 15_000,
  })
  const cameras = useCameras()
  const recorder = useQuery({
    queryKey: queryKeys.recorderStatus,
    queryFn: recordingsApi.status,
    refetchInterval: 30_000,
  })

  const online = cameras.data?.filter((camera) => camera.status === 'online').length ?? 0
  const offline = (cameras.data?.length ?? 0) - online
  const usedPercent =
    recorder.data && recorder.data.total_bytes > 0
      ? ((recorder.data.total_bytes - recorder.data.free_bytes) / recorder.data.total_bytes) * 100
      : null

  return (
    <div className="mx-auto max-w-7xl space-y-5">
      <PageHeader
        title="Dashboard"
        description="Current state of this Sentinel server and its cameras. Refreshes every 15 seconds."
      />

      {health.isError ? (
        <FailureNotice error={health.error} action="reading server health" />
      ) : null}

      {/* Ordered by consequence: nothing being recorded, then storage that will
          stop it, then everything else. */}
      {recorder.data?.suspended_reason ? (
        <RecorderStoppedNotice reason={recorder.data.suspended_reason} />
      ) : usedPercent != null ? (
        <DiskPressureNotice
          usedPercent={usedPercent}
          freeLabel={formatBytes(recorder.data?.free_bytes)}
        />
      ) : null}

      <section
        aria-label="Server status"
        className="grid grid-cols-2 divide-x divide-y divide-(--color-border) overflow-hidden rounded-(--radius-card) border border-(--color-border) bg-(--color-surface) lg:grid-cols-4 lg:divide-y-0"
      >
        {health.isPending ? (
          Array.from({ length: 4 }, (_, index) => (
            <div key={index} className="p-4">
              <Skeleton className="h-12" />
            </div>
          ))
        ) : health.data ? (
          <>
            <StatusMetric
              label="Status"
              value={health.data.status === 'healthy' ? 'Healthy' : health.data.status}
              tone={
                health.data.status === 'healthy'
                  ? 'success'
                  : health.data.status === 'unhealthy'
                    ? 'danger'
                    : 'neutral'
              }
            />
            <StatusMetric
              label="Cameras online"
              value={cameras.isPending ? '—' : String(online)}
              tone={online > 0 ? 'success' : 'neutral'}
            />
            <StatusMetric
              label="Cameras offline"
              value={cameras.isPending ? '—' : String(offline)}
              tone={offline > 0 ? 'danger' : 'neutral'}
            />
            <StatusMetric
              label="Free space"
              value={formatBytes(recorder.data?.free_bytes)}
              tone={recorder.data?.storage_available === false ? 'danger' : 'neutral'}
            />
          </>
        ) : null}
      </section>

      {health.data ? (
        <section aria-label="Components" className="space-y-2">
          <h2 className="text-sm font-semibold">Recorder services</h2>
          <SurfaceGroup>
            {Object.entries(health.data.components).map(([name, component]) => (
              <div key={name} className="flex items-center justify-between gap-4 px-4 py-3">
                <div className="min-w-0">
                  <p className="text-sm font-medium capitalize">{name.replace(/_/g, ' ')}</p>
                  {component.detail ? (
                    <p className="mt-0.5 truncate text-xs text-(--color-fg-muted)">
                      {component.detail}
                    </p>
                  ) : null}
                </div>
                <Status tone={COMPONENT_TONE[component.state] ?? 'idle'}>{component.state}</Status>
              </div>
            ))}
          </SurfaceGroup>
        </section>
      ) : null}

      <section aria-label="Cameras" className="space-y-2">
        <div className="flex items-baseline justify-between gap-4">
          <h2 className="text-sm font-semibold">Camera inventory</h2>
          {cameras.data ? (
            <span className="text-xs text-(--color-fg-subtle)">
              {cameras.data.length} configured
            </span>
          ) : null}
        </div>
        {cameras.isPending ? (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {Array.from({ length: 3 }, (_, index) => (
              <Skeleton key={index} className="h-16 rounded-(--radius-card)" />
            ))}
          </div>
        ) : cameras.data && cameras.data.length > 0 ? (
          <ul
            className={cn(
              'grid overflow-hidden rounded-(--radius-card) border border-(--color-border)',
              'divide-y divide-(--color-border) bg-(--color-surface)',
              // Above one column the horizontal rules would cut across cells
              // that are not related, so the grid draws its own separators.
              'sm:grid-cols-2 sm:divide-y-0 lg:grid-cols-3',
            )}
          >
            {cameras.data.map((camera) => (
              <li
                key={camera.id}
                className="flex items-center justify-between gap-3 px-4 py-3 sm:border-r sm:border-b sm:border-(--color-border)"
              >
                <h3 className="min-w-0 truncate text-sm font-medium">{camera.name}</h3>
                <Status tone={STATUS_TONE[camera.status]} className="shrink-0">
                  {STATUS_LABEL[camera.status]}
                </Status>
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState
            icon={Video}
            title="You have not added any cameras yet"
            description="Nothing is being recorded until a camera is added."
            action={
              <Button asChild>
                <Link to="/admin/cameras">
                  <Plus aria-hidden="true" />
                  Add camera
                </Link>
              </Button>
            }
          />
        )}
      </section>
    </div>
  )
}
