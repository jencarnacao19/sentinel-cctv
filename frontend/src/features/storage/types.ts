export interface CameraStorageUsage {
  camera_id: string
  camera_name: string
  bytes: number
  segment_count: number
  protected_bytes: number
  oldest_recording: string | null
  newest_recording: string | null
  recording_rate_bytes_per_hour: number | null
}

export interface StorageUsage {
  path: string
  available: boolean
  reason: string | null
  total_bytes: number
  used_bytes: number
  free_bytes: number
  recordings_bytes: number
  snapshots_bytes: number
  database_bytes: number
  segment_count: number
  protected_count: number
  protected_bytes: number
  saved_clips_count: number
  saved_clips_bytes: number
  damaged_count: number
  oldest_recording: string | null
  newest_recording: string | null
  recording_rate_bytes_per_hour: number | null
  rate_window_hours: number | null
  estimated_retention_days: number | null
  estimated_days_remaining: number | null
  configured_retention_days: number | null
  camera_usage: CameraStorageUsage[]
  /** Recordings share a filesystem with Sentinel's data: normally the small system disk. */
  shares_system_disk: boolean
}

export interface StorageSettings {
  path: string | null
  retention_days: number | null
  max_bytes: number | null
  min_free_bytes: number | null
}

export interface StoragePolicyPreview {
  proposed_settings: StorageSettings
  preview_token: string
  expires_in_seconds: number
  required_confirmation: string | null
  segment_count: number
  estimated_bytes: number
  expired_segment_count: number
  expired_event_count: number
  protected_count: number
  protected_bytes: number
  active_or_recent_count: number
  free_bytes: number
  reserve_shortfall_bytes: number
  estimated_free_bytes_after: number
  remaining_shortfall_bytes: number
  recording_rate_bytes_per_hour: number | null
  rate_window_hours: number | null
  estimated_retention_days: number | null
  estimated_days_remaining: number | null
  oldest_affected_recording: string | null
  newest_affected_recording: string | null
  saved_clips_preserved: boolean
  warnings: string[]
}

export type CleanupPhase =
  | 'idle'
  | 'running'
  | 'cancelling'
  | 'cancelled'
  | 'completed'
  | 'failed'

export interface CleanupRequest {
  camera_id?: string
  before?: string
  include_protected: boolean
  confirmation?: string
}

export interface CleanupPreview {
  segment_count: number
  bytes: number
  protected_count: number
  protected_bytes: number
  required_confirmation: string
  saved_clips_preserved: boolean
}

export interface CleanupStatus {
  phase: CleanupPhase
  job_id: string | null
  started_at: string | null
  finished_at: string | null
  camera_id: string | null
  before: string | null
  include_protected: boolean
  matched_segments: number
  matched_bytes: number
  deleted_segments: number
  freed_bytes: number
  failed_segments: number
  message: string | null
  saved_clips_preserved: boolean
}

export interface ViewerSystemStatus {
  status: 'healthy' | 'degraded' | 'unhealthy'
  generated_at: string
  cameras_total: number
  cameras_online: number
  recorders_expected: number
  recorders_running: number
  motion_expected: number
  motion_running: number
  storage_available: boolean
  storage_total_bytes: number
  storage_used_bytes: number
  storage_free_bytes: number
  storage_used_percent: number
  recordings_bytes: number
  saved_clips_count: number
  saved_clips_bytes: number
  oldest_recording: string | null
  newest_recording: string | null
  recording_rate_bytes_per_hour: number | null
  configured_retention_days: number | null
  estimated_retention_days: number | null
  estimated_days_remaining: number | null
  privacy_enabled: boolean
  privacy_reason: import('@/types/api').PrivacyReason | null
  privacy_message: string | null
}
