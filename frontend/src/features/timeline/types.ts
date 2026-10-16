/**
 * Shapes returned by the timeline, playback and export endpoints. Kept in
 * step with the Pydantic response models in
 * `backend/app/recording/timeline_router.py`.
 */

export type GapReason = 'not_recording' | 'storage_unavailable' | 'damaged'

export interface TimeSpan {
  start: string
  end: string
  duration_seconds: number
}

export interface Gap {
  start: string
  end: string
  duration_seconds: number
  reason: GapReason
}

export interface MotionBand {
  start: string
  end: string
  peak_score: number
  event_count: number
}

export interface Timeline {
  camera_id: string
  start: string
  end: string
  coverage: TimeSpan[]
  gaps: Gap[]
  motion: MotionBand[]
  recorded_seconds: number
  motion_seconds: number
  damaged_seconds: number
  gap_seconds: number
  segment_count: number
  motion_event_count: number
  bucket_seconds: number
  truncated: boolean
}

export interface ManifestItem {
  segment_id: string
  started_at: string
  ended_at: string
  /** Non-zero only for the first item in a manifest. */
  offset_seconds: number
  duration_seconds: number
  url: string
}

export interface PlaybackManifest {
  camera_id: string
  start: string
  end: string
  items: ManifestItem[]
  gaps: TimeSpan[]
  total_seconds: number
  truncated: boolean
}

export interface PlaybackTarget {
  camera_id: string
  at: string
  found: boolean
  segment_id: string | null
  url: string | null
  started_at: string | null
  ended_at: string | null
  offset_seconds: number
  next_segment_id: string | null
  next_url: string | null
  next_started_at: string | null
  continuous: boolean
  gap_seconds: number | null
}

export type ExportStatus = 'queued' | 'running' | 'complete' | 'failed' | 'expired' | 'cancelled'

export interface ExportJob {
  id: string
  camera_id: string
  start_at: string
  end_at: string
  status: ExportStatus
  progress: number
  size_bytes: number | null
  error: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  expires_at: string | null
  /** Saved clips never expire automatically. */
  protected: boolean
  download_url: string | null
  note: string
}

export interface ExportList {
  exports: ExportJob[]
  total: number
  limit: number
  offset: number
}

export interface ExportCreate {
  camera_id: string
  start_at: string
  end_at: string
  protected?: boolean
}

/** Whether an export job can still be waited on. */
export function isExportActive(job: ExportJob): boolean {
  return job.status === 'queued' || job.status === 'running'
}
