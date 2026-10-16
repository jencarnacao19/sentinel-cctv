export interface MotionEvent {
  id: string
  camera_id: string
  started_at: string
  ended_at: string | null
  duration_seconds: number | null
  peak_score: number
  protected: boolean
  has_snapshot: boolean
  thumbnail_url: string | null
  snapshot_url: string | null
}

export interface EventList {
  events: MotionEvent[]
  total: number
  limit: number
  offset: number
}

export interface EventCameraSummary {
  camera_id: string
  camera_name: string
  count: number
  earliest: string | null
  latest: string | null
}

export interface EventSummary {
  total: number
  protected: number
  with_snapshot: number
  earliest: string | null
  latest: string | null
  cameras: EventCameraSummary[]
}

export interface EventCleanupRequest {
  camera_id?: string
  start?: string
  end?: string
  min_duration_seconds?: number
  max_duration_seconds?: number
  include_protected: boolean
  confirmation?: string
}

export interface EventCleanupPreview {
  event_count: number
  protected_count: number
  evidence_files: number
  evidence_bytes: number
  active_events_excluded: number
  required_confirmation: string
}

export type EventCleanupPhase =
  | 'idle'
  | 'running'
  | 'cancelling'
  | 'cancelled'
  | 'completed'
  | 'failed'

export interface EventCleanupStatus {
  phase: EventCleanupPhase
  job_id: string | null
  started_at: string | null
  finished_at: string | null
  camera_id: string | null
  start: string | null
  end: string | null
  min_duration_seconds: number | null
  max_duration_seconds: number | null
  include_protected: boolean
  matched_events: number
  matched_evidence_files: number
  matched_evidence_bytes: number
  deleted_events: number
  deleted_evidence_files: number
  freed_bytes: number
  failed_evidence_files: number
  message: string | null
  recordings_preserved: boolean
  saved_exports_preserved: boolean
}
