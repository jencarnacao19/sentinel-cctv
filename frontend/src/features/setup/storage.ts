import { useQuery } from '@tanstack/react-query'

import { api } from '@/api/client'
import type { StoragePolicyPreview, StorageSettings, StorageUsage } from '@/features/storage/types'

export const RETENTION_CHOICES = ['7', '14', '30', '60', '90'] as const
export type RetentionChoice = (typeof RETENTION_CHOICES)[number]

export function useStorageOverview() {
  const usage = useQuery({
    queryKey: ['storage', 'usage'],
    queryFn: () => api.get<StorageUsage>('/storage'),
  })
  const settings = useQuery({
    queryKey: ['storage', 'settings'],
    queryFn: () => api.get<StorageSettings>('/storage/settings'),
  })
  return { usage, settings }
}

/**
 * Apply the chosen retention without the review the Storage page asks for.
 *
 * That review exists because a shorter retention deletes footage. During setup
 * there is none old enough to be affected, and the preview proves it; if it
 * says otherwise the change is refused here and left to the Storage page.
 */
export async function applyRetention(days: number, current: number | null): Promise<void> {
  if (days === current) return
  const preview = await api.post<StoragePolicyPreview>('/storage/settings/preview', {
    retention_days: days,
  })
  if (preview.expired_segment_count > 0) {
    throw new Error(
      `Keeping ${days} days would delete ${preview.expired_segment_count} existing recordings. ` +
        'Change it on the Storage page, where the deletion can be reviewed.',
    )
  }
  await api.patch<StorageSettings>('/storage/settings', {
    retention_days: days,
    preview_token: preview.preview_token,
    confirmation: preview.required_confirmation ?? '',
  })
}
