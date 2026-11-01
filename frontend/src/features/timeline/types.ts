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

/** One recorded file's stretch of a playback playlist. */
export interface PlaybackSessionItem {
  started_at: string
  /** Where it starts on the player's own timeline, in seconds. */
  position: number
  duration: number
}

/** An HLS playlist of up to an hour of footage, from `POST .../playback/session`. */
export interface PlaybackSession {
  /** `null` when nothing was recorded in the hour after the instant. */
  url: string | null
  start_position: number
  items: PlaybackSessionItem[]
  /** Wall-clock end of this playlist: the next one opens from here. */
  end: string | null
  /** With no `url`: the next instant that has footage, if any. */
  next_at: string | null
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
