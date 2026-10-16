/** Shapes returned by the Sentinel API. Kept in step with the Pydantic models. */

export interface AuthStatus {
  initialised: boolean
}

export interface User {
  id: string
  username: string
  is_admin: boolean
  is_active: boolean
  last_login_at: string | null
  created_at: string
}

export interface LoginResponse {
  access_token: string
  token_type: string
  expires_at: string
}

export interface LoginSecuritySettings {
  enabled: boolean
  failure_threshold: number
  /** Null means the block has no automatic expiry. */
  block_duration_seconds: number | null
  public_hostnames: string[]
  trusted_proxy_cidrs: string[]
  proxy_ready: boolean
  blocked_count: number
}

export interface LoginBlock {
  ip_address: string
  public_hostname: string
  failed_attempts: number
  first_failed_at: string
  last_failed_at: string
  blocked_at: string
  blocked_until: string | null
}

export type ApiTokenScope = 'read_only' | 'camera_control'

export interface ApiToken {
  id: string
  name: string
  prefix: string
  scope: ApiTokenScope
  created_at: string
  last_used_at: string | null
  expires_at: string | null
  revoked_at: string | null
}

/** Only ever returned by the create call. */
export interface CreatedApiToken extends ApiToken {
  token: string
}

export type RecordingMode = 'continuous' | 'motion' | 'scheduled' | 'events' | 'off'
export type RecordingProfile = 'auto' | 'passthrough' | 'resilient' | 'normalised'
export type RecordingSourceMode = 'auto' | 'relay' | 'direct'
export type CameraStatus = 'online' | 'offline' | 'connecting' | 'error' | 'disabled'
export type CameraKind = 'rtsp' | 'tapo_battery'

/**
 * Why a camera does or does not get vendor-specific controls.
 *
 * `unknown` is the one that earns its own state: it means Sentinel could not
 * identify the camera, not that it identified it as something else, and it is
 * the only case where offering the manual opt-in is honest.
 */
export type TapoControlSource = 'manual' | 'kind' | 'metadata' | 'other_vendor' | 'unknown'

export interface TapoControlAvailability {
  supported: boolean
  source: TapoControlSource
  detected_vendor: string | null
  reason: string
  can_opt_in: boolean
}

/** What a recorder is actually doing. `failed` is a real, reportable state. */
export type RecordingRuntimeState = 'recording' | 'off' | 'failed' | 'paused' | 'unsupported'

export interface CameraRecordingResult {
  camera_id: string
  recording_enabled: boolean
  state: RecordingRuntimeState
  message: string
}

export interface CameraIdentity {
  camera_id: string
  manufacturer: string | null
  model: string | null
  firmware_version: string | null
  tapo_controls: TapoControlAvailability
  message: string
}
export type TapoPatrolMode = 'horizontal' | 'vertical'

export interface CameraStreamInfo {
  codec: string | null
  width: number | null
  height: number | null
  fps: number | null
  audio_codec: string | null
  bitrate_kbps: number | null
}

export interface Camera {
  id: string
  kind: CameraKind
  name: string
  manufacturer: string | null
  model: string | null
  nvr_group: string | null
  host: string
  main_stream_url: string | null
  sub_stream_url: string | null
  username: string | null
  /** The password itself is never returned by the API. */
  has_credentials: boolean
  /** Whether a separate encrypted Tapo control password is stored. */
  has_tapo_credentials: boolean
  tapo_child_id: string | null
  onvif_device_xaddr: string | null
  ptz_supported: boolean | null
  ptz_zoom_supported: boolean
  ptz_status_message: string | null
  ptz_last_checked_at: string | null
  onvif_patrol_enabled: boolean
  tapo_patrol_mode: TapoPatrolMode | null
  /** Whether Sentinel offers this camera's Tapo LAN controls, and why. */
  tapo_controls: TapoControlAvailability
  /** The administrator's explicit answer; null leaves it to detection. */
  tapo_controls_enabled: boolean | null
  /** What the recorder is doing, as opposed to what was configured. */
  recording_state: RecordingRuntimeState
  recording_enabled: boolean
  enabled: boolean
  recording_mode: RecordingMode
  recording_profile: RecordingProfile
  recording_source_mode: RecordingSourceMode
  recording_segment_seconds: number | null
  retention_days: number | null
  motion_enabled: boolean
  motion_sensitivity: number
  status: CameraStatus
  status_message: string | null
  last_seen_at: string | null
  stream: CameraStreamInfo
  created_at: string
  updated_at: string
}

/** Deliberately small shape returned to a read-only live viewer. */
export interface ViewerCamera {
  id: string
  name: string
  kind: CameraKind
  status: CameraStatus
  has_substream: boolean
  ptz_supported: boolean
  ptz_zoom_supported: boolean
  stream: CameraStreamInfo
}

export interface CameraCreate {
  name: string
  kind?: CameraKind
  host?: string | null
  main_stream_url?: string | null
  sub_stream_url?: string | null
  username?: string | null
  password?: string | null
  tapo_password?: string | null
  tapo_child_id?: string | null
  onvif_device_xaddr?: string | null
  onvif_profile_token?: string | null
  manufacturer?: string | null
  model?: string | null
  nvr_group?: string | null
  enabled?: boolean
  recording_mode?: RecordingMode
  recording_profile?: RecordingProfile
  recording_source_mode?: RecordingSourceMode
  recording_segment_seconds?: number | null
  recording_enabled?: boolean
  retention_days?: number | null
  motion_enabled?: boolean
  motion_sensitivity?: number
  tapo_patrol_mode?: TapoPatrolMode | null
  tapo_controls_enabled?: boolean | null
}

export type CameraUpdate = Partial<CameraCreate>

export interface PtzCapability {
  supported: boolean
  zoom_supported: boolean
  preset_capacity: number
  preset_tour_capacity: number
  home_supported: boolean
  message: string
  checked_at: string
}

export interface PtzPreset {
  token: string
  name: string
}

export type PtzPatrolBackend = 'onvif_lan' | 'tapo_lan' | 'none'

export interface PtzSetup {
  presets: PtzPreset[]
  preset_capacity: number
  preset_tour_capacity: number
  home_supported: boolean
  patrol_backend: PtzPatrolBackend
}

export interface PtzPatrolResult {
  enabled: boolean
  backend: PtzPatrolBackend
}

export type TapoFeature =
  | 'led'
  | 'privacy_mask'
  | 'day_night'
  | 'smart_track'
  | 'floodlight'
  | 'motion'
  | 'person'
  | 'vehicle'
  | 'pet'
  | 'package'
  | 'line_crossing'
  | 'tamper'
  | 'baby_cry'
  | 'bark'

export interface TapoCapabilities {
  supported: string[]
  device: {
    model: string | null
    hardware_version: string | null
    firmware_version: string | null
  }
  /** True when part of the discovery did not answer. The rest is still real. */
  partial: boolean
  /** Vendor methods that did not answer, so a gap is visible as a gap. */
  unchecked: string[]
  checked_at: string | null
  excluded: string[]
}

export interface TapoDetectionState {
  supported: boolean
  enabled: boolean | null
  sensitivity: number | null
  sensitivity_label: string | null
}

export interface TapoState {
  led_enabled: boolean | null
  privacy_mask_enabled: boolean | null
  day_night_mode: string | null
  smart_track_enabled: boolean | null
  floodlight_enabled: boolean | null
  siren_active: boolean | null
  cruise_enabled: boolean | null
  preset_count: number | null
  sd_card: {
    present: boolean
    status: string
    total_gb: number | null
    free_gb: number | null
  } | null
  detections: Record<string, TapoDetectionState>
  read_at: string | null
}

export interface TapoFeaturePayload {
  enabled?: boolean
  sensitivity?: number
  sensitivity_label?: string
  mode?: 'day' | 'night' | 'auto'
}

export interface TapoAction {
  camera_id: string
  feature: string
  applied: boolean
  message: string
  state: TapoState | null
}

export interface PtzVector {
  pan: number
  tilt: number
  zoom: number
}

export interface StreamTestResult {
  ok: boolean
  error: string | null
  codec: string | null
  resolution: string | null
  width: number | null
  height: number | null
  fps: number | null
  audio_codec: string | null
  bitrate_kbps: number | null
  can_stream_copy: boolean
}

export interface CameraTestResult {
  main: StreamTestResult
  sub: StreamTestResult | null
}

export type SegmentStatus = 'recording' | 'complete' | 'damaged' | 'missing'
export type SegmentOrigin = 'continuous' | 'tapo_event' | 'tapo_manual'

export interface RecordingSegment {
  id: string
  camera_id: string
  origin: SegmentOrigin
  started_at: string
  ended_at: string | null
  duration_seconds: number | null
  size_bytes: number
  codec: string | null
  width: number | null
  height: number | null
  fps: number | null
  has_motion: boolean
  motion_seconds: number
  protected: boolean
  status: SegmentStatus
}

export interface TapoBatteryChildDevice {
  child_id: string
  name?: string | null
  model?: string | null
}

export interface TapoBatteryStatus {
  ok: boolean
  model: string | null
  hardware_version: string | null
  firmware_version: string | null
  battery_percent: number | null
  charging: boolean | null
  charging_source: string | null
  read_at: string
  transport: 'local_lan'
  continuous_recording: false
  children?: TapoBatteryChildDevice[]
}

export interface TapoBatteryTestPayload {
  host: string
  password: string
  username?: string | null
  child_id?: string | null
  hub?: boolean
}

export interface TapoBatteryClip {
  external_id: string
  started_at: string
  ended_at: string
  duration_seconds: number
  event_type: string
  imported: boolean
  segment_id: string | null
}

export interface TapoBatterySyncResult {
  discovered: number
  imported: number
  already_present: number
  failed: number
  segment_ids: string[]
}

export interface SegmentList {
  segments: RecordingSegment[]
  total: number
  limit: number
  offset: number
}

export interface RecorderStatus {
  storage_path: string | null
  storage_available: boolean
  /** Non-null when recording has been stopped to protect the system disk. */
  suspended_reason: string | null
  total_bytes: number
  free_bytes: number
  recorders: Record<string, unknown>[]
}

export interface SystemInfo {
  name: string
  version: string
  uptime_seconds: number
  setup_completed: boolean
  timezone: string
  python_version: string
  platform: string
  authenticated_as: { kind: string; name: string }
}

export type PrivacyReason =
  | 'privacy'
  | 'guests'
  | 'private_event'
  | 'maintenance'
  | 'temporary'

export interface PrivacyStatus {
  enabled: boolean
  reason: PrivacyReason | null
  reason_label: string | null
  message: string | null
  changed_at: string | null
  changed_by: string | null
  reasons: Record<PrivacyReason, string>
}

export type ComponentState = 'ok' | 'degraded' | 'failed' | 'skipped'

export interface HealthComponent {
  state: ComponentState
  detail?: string
  data?: Record<string, unknown>
}

export interface HealthResponse {
  status: 'healthy' | 'degraded' | 'unhealthy'
  database: boolean
  storage: boolean
  go2rtc: boolean
  recording: boolean
  components: Record<string, HealthComponent>
  version: string
  uptime_seconds: number
}

/** A bounded, explicitly requested recording from a Tapo battery camera. */
export type TimedRecordingState =
  | 'starting'
  | 'recording'
  | 'reconnecting'
  | 'stopping'
  | 'completed'
  | 'partial'
  | 'failed'

export type TimedRecordingStopReason =
  | 'duration_reached'
  | 'operator'
  | 'privacy'
  | 'shutdown'
  | 'camera_unreachable'
  | 'error'

export interface TimedRecordingOptions {
  presets_minutes: number[]
  max_minutes: number
  hard_max_minutes: number
  min_minutes: number
  battery_percent: number | null
  warning: string
}

export interface TimedRecordingStatus {
  camera_id: string
  state: TimedRecordingState
  requested_seconds: number
  started_at: string
  ends_at: string
  seconds_remaining: number
  seconds_recorded: number
  bytes_recorded: number
  connection: 'connecting' | 'connected' | 'reconnecting' | 'closed'
  reconnects: number
  battery_percent_at_start: number | null
  message: string
  stop_reason: TimedRecordingStopReason | null
  segment_id: string | null
  finished: boolean
}
