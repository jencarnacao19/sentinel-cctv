import { HardDrive } from 'lucide-react'

import { Alert } from '@/components/ui/alert'
import { Segmented } from '@/components/ui/segmented'
import { Skeleton } from '@/components/ui/skeleton'
import {
  RETENTION_CHOICES,
  type RetentionChoice,
  useStorageOverview,
} from '@/features/setup/storage'
import { formatBytes } from '@/lib/utils'

const PROXMOX_GUIDE = 'https://github.com/jencarnacao19/sentinel-cctv/blob/main/docs/proxmox.md'

export function StorageStep({
  retention,
  onRetentionChange,
}: {
  retention: RetentionChoice
  onRetentionChange: (value: RetentionChoice) => void
}) {
  const { usage } = useStorageOverview()

  return (
    <div className="space-y-6">
      {usage.isPending ? (
        <Skeleton className="h-20" />
      ) : !usage.data ? (
        <Alert variant="danger">Could not read the recordings storage.</Alert>
      ) : !usage.data.available ? (
        <Alert variant="danger" title="Sentinel cannot record yet">
          {usage.data.reason} Check the disk, then reload this page.
        </Alert>
      ) : (
        <div className="space-y-3">
          <div className="flex items-start gap-3 rounded-lg border border-(--color-border) p-4">
            <HardDrive className="mt-0.5 size-5 shrink-0 text-(--color-fg-muted)" aria-hidden="true" />
            <div className="min-w-0">
              <p className="text-sm font-medium">
                {formatBytes(usage.data.free_bytes)} free of {formatBytes(usage.data.total_bytes)}
              </p>
              <p className="mt-0.5 truncate font-mono text-xs text-(--color-fg-muted)">
                {usage.data.path}
              </p>
            </div>
          </div>
          {usage.data.shares_system_disk ? (
            <Alert variant="warning" title="Recordings are on the system disk">
              That is fine for a first test, but continuous recording fills it within days and the
              server stops when it is full. Give Sentinel its own disk for recordings: the Proxmox
              installer asks which disk to use, and the{' '}
              <a href={PROXMOX_GUIDE} target="_blank" rel="noopener noreferrer" className="underline">
                Proxmox guide
              </a>{' '}
              shows how to add one to this container.
            </Alert>
          ) : null}
        </div>
      )}

      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">Keep recordings for</legend>
        <Segmented
          ariaLabel="Keep recordings for"
          value={retention}
          onChange={onRetentionChange}
          options={RETENTION_CHOICES.map((days) => ({
            value: days,
            label: `${days} days`,
            children: `${days} days`,
          }))}
        />
        <p className="text-xs leading-5 text-(--color-fg-subtle)">
          If the disk fills up first, Sentinel deletes the oldest footage early. Saved clips are
          never deleted. You can change this later under Storage.
        </p>
      </fieldset>
    </div>
  )
}
