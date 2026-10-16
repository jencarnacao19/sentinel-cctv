/** Shapes returned by the Camera Reliability Center. Kept in step with
 *  `backend/app/reliability/schemas.py`. */

export type CameraState =
  | 'healthy'
  | 'degraded'
  | 'offline'
  | 'recovering'
  | 'disabled'
  | 'privacy_paused'

export type RecordingProfile = 'auto' | 'passthrough' | 'resilient' | 'normalised'

export type SourceReason =
  | 'relay_preferred'
  | 'relay_requalifying'
  | 'relay_unavailable'
  | 'direct_qualified'
  | 'direct_only'
  | 'relay_only'
  | 'relay_pinned'
  | 'direct_pinned'
  | 'operator_reset'

/** Measured continuity, the thing a freshness check cannot see. */
export interface RecordingQuality {
  window_seconds: number
  /** `null` while there is not yet enough measured footage to judge. */
  coverage_ratio: number | null
  coverage_percent: number | null
  recorded_seconds: number
  segments: number
  damaged_segments: number
  short_segments: number
  gap_count: number
  longest_gap_seconds: number
  restarts_in_window: number
  corruption_per_minute: number
  corruption_events: number
  /** Classified category only — never a fragment of recorder output. */
  corruption_kind: string | null
  profile: RecordingProfile | null
  source_reason: SourceReason | null
  source_reason_label: string | null
  source_switches: number
}

export type IncidentReason =
  | 'recording_not_fresh'
  | 'recording_coverage_low'
  | 'recording_unstable'
  | 'stream_corruption'
  | 'recorder_restarting'
  | 'recorder_unavailable'
  | 'stream_source_failure'
  | 'motion_worker_unavailable'
  | 'relay_unavailable'
  | 'storage_unavailable'

export type StreamFault =
  | 'missing_stream_parameters'
  | 'corrupt_packets'
  | 'connection_failed'
  | 'timeout'
  | 'authentication_rejected'
  | 'unknown'

/** Which input the recorder reads from. Never a URL. */
export type RecorderSource = 'relay' | 'direct'

export type RecorderProcessState = 'stopped' | 'starting' | 'running' | 'restarting' | 'failed'

export interface CameraIncident {
  id: string
  camera_id: string
  reason: IncidentReason
  reason_label: string
  started_at: string
  resolved_at: string | null
  duration_seconds: number | null
  active: boolean
  detail: string | null
  fault: StreamFault | null
  fault_label: string | null
  source: RecorderSource | null
  restart_count: number
}

export interface RecorderDetail {
  present: boolean
  state: RecorderProcessState | null
  running: boolean
  started_at: string | null
  restart_count: number
  consecutive_failures: number
  source: RecorderSource | null
  fault: StreamFault | null
  fault_label: string | null
}

export interface CameraReliability {
  camera_id: string
  name: string
  state: CameraState
  detail: string
  issue: IncidentReason | null
  issue_label: string | null
  recording_expected: boolean
  /** `null` when recording is not expected, so the page can say so rather than
   *  implying a switched-off camera has stale footage. */
  recording_fresh: boolean | null
  last_segment_at: string | null
  segment_age_seconds: number | null
  freshness_limit_seconds: number
  last_healthy_at: string | null
  motion_expected: boolean
  motion_running: boolean
  recorder: RecorderDetail
  quality: RecordingQuality
  active_incident: CameraIncident | null
  recent_incidents: CameraIncident[]
}

export interface ReliabilityResponse {
  generated_at: string
  privacy_enabled: boolean
  privacy_message: string | null
  storage_available: boolean
  relay_expected: boolean
  relay_ready: boolean
  counts: Record<CameraState, number>
  cameras: CameraReliability[]
}

export interface IncidentList {
  incidents: CameraIncident[]
  total: number
  limit: number
  offset: number
}

export interface CameraRecoveryResult {
  camera_id: string
  requested_at: string
  accepted: boolean
  actions: string[]
  skipped: string[]
  state_before: CameraState
  message: string
}
