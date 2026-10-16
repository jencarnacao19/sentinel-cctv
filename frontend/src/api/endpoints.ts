import { api } from '@/api/client'
import type {
  ApiToken,
  ApiTokenScope,
  AuthStatus,
  Camera,
  CameraCreate,
  CameraIdentity,
  CameraRecordingResult,
  CameraTestResult,
  CameraUpdate,
  CreatedApiToken,
  HealthResponse,
  LoginBlock,
  LoginResponse,
  LoginSecuritySettings,
  PtzCapability,
  PtzPatrolResult,
  PtzPreset,
  PtzSetup,
  PtzVector,
  PrivacyReason,
  PrivacyStatus,
  RecorderStatus,
  RecordingSegment,
  SegmentList,
  StreamTestResult,
  SystemInfo,
  TapoAction,
  TapoBatteryClip,
  TapoBatteryStatus,
  TapoBatterySyncResult,
  TapoBatteryTestPayload,
  TapoCapabilities,
  TapoFeature,
  TapoFeaturePayload,
  TapoState,
  TimedRecordingOptions,
  TimedRecordingStatus,
  User,
} from '@/types/api'

export const authApi = {
  status: () => api.get<AuthStatus>('/auth/status'),
  createAdmin: (username: string, password: string) =>
    api.post<User>('/auth/setup', { username, password }),
  login: (username: string, password: string) =>
    api.post<LoginResponse>('/auth/login', { username, password }),
  logout: () => api.post<void>('/auth/logout'),
  me: () => api.get<User>('/auth/me'),
  changePassword: (currentPassword: string, newPassword: string) =>
    api.post<void>('/auth/change-password', {
      current_password: currentPassword,
      new_password: newPassword,
    }),
  listTokens: () => api.get<ApiToken[]>('/auth/tokens'),
  createToken: (name: string, scope: ApiTokenScope = 'read_only', expiresInDays?: number) =>
    api.post<CreatedApiToken>('/auth/tokens', {
      name,
      scope,
      ...(expiresInDays ? { expires_in_days: expiresInDays } : {}),
    }),
  revokeToken: (id: string) => api.delete<void>(`/auth/tokens/${id}`),
  loginSecurity: () => api.get<LoginSecuritySettings>('/auth/login-security'),
  updateLoginSecurity: (payload: {
    enabled: boolean
    failure_threshold: number
    block_duration_seconds: number | null
  }) => api.patch<LoginSecuritySettings>('/auth/login-security', payload),
  listLoginBlocks: () => api.get<LoginBlock[]>('/auth/login-blocks'),
  removeLoginBlock: (address: string) =>
    api.delete<void>(`/auth/login-blocks/${encodeURIComponent(address)}`),
  clearLoginBlocks: () => api.post<{ removed: number }>('/auth/login-blocks/clear', { confirm: true }),
}

export const systemApi = {
  info: () => api.get<SystemInfo>('/system'),
  health: () => api.get<HealthResponse>('/system/health'),
  privacy: () => api.get<PrivacyStatus>('/system/privacy'),
  setPrivacy: (enabled: boolean, reason?: PrivacyReason) =>
    api.put<PrivacyStatus>('/system/privacy', {
      enabled,
      reason: enabled ? reason : null,
    }),
}

export const camerasApi = {
  list: (enabledOnly = false) =>
    api.get<Camera[]>(`/cameras${enabledOnly ? '?enabled_only=true' : ''}`),
  get: (id: string) => api.get<Camera>(`/cameras/${id}`),
  create: (payload: CameraCreate) => api.post<Camera>('/cameras', payload),
  update: (id: string, payload: CameraUpdate) => api.patch<Camera>(`/cameras/${id}`, payload),
  remove: (id: string) => api.delete<void>(`/cameras/${id}`),
  test: (id: string) =>
    // Opening a stream can legitimately take a while; the default 20s ceiling
    // would abort a slow camera before FFprobe gives its own answer.
    api.post<CameraTestResult>(`/cameras/${id}/test`, undefined, { timeoutMs: 60_000 }),
  testStream: (url: string, username?: string, password?: string) =>
    api.post<StreamTestResult>(
      '/cameras/test-stream',
      { url, username: username || null, password: password || null },
      { timeoutMs: 60_000 },
    ),
  detectPtz: (id: string) =>
    api.post<PtzCapability>(`/cameras/${encodeURIComponent(id)}/ptz/detect`, undefined, {
      timeoutMs: 20_000,
    }),
  movePtz: (id: string, vector: PtzVector) =>
    api.post<void>(`/cameras/${encodeURIComponent(id)}/ptz/move`, vector, { timeoutMs: 4_000 }),
  stopPtz: (id: string) =>
    api.post<void>(`/cameras/${encodeURIComponent(id)}/ptz/stop`, undefined, { timeoutMs: 4_000 }),
  getPtzSetup: (id: string) =>
    api.get<PtzSetup>(`/cameras/${encodeURIComponent(id)}/ptz/setup`, { timeoutMs: 10_000 }),
  createPtzPreset: (id: string, name: string) =>
    api.post<PtzPreset>(`/cameras/${encodeURIComponent(id)}/ptz/presets`, { name }, { timeoutMs: 10_000 }),
  gotoPtzPreset: (id: string, token: string) =>
    api.post<void>(
      `/cameras/${encodeURIComponent(id)}/ptz/presets/${encodeURIComponent(token)}/goto`,
      undefined,
      { timeoutMs: 10_000 },
    ),
  removePtzPreset: (id: string, token: string) =>
    api.delete<void>(
      `/cameras/${encodeURIComponent(id)}/ptz/presets/${encodeURIComponent(token)}`,
      { timeoutMs: 10_000 },
    ),
  startPtzPatrol: (id: string, presetTokens: string[], staySeconds: number) =>
    api.put<PtzPatrolResult>(
      `/cameras/${encodeURIComponent(id)}/ptz/patrol`,
      { preset_tokens: presetTokens, stay_seconds: staySeconds, name: 'Sentinel local patrol' },
      { timeoutMs: 20_000 },
    ),
  stopPtzPatrol: (id: string) =>
    api.delete<PtzPatrolResult>(`/cameras/${encodeURIComponent(id)}/ptz/patrol`, {
      timeoutMs: 10_000,
    }),
  getTapoCapabilities: (id: string, refresh = false) =>
    api.get<TapoCapabilities>(
      `/cameras/${encodeURIComponent(id)}/tapo/capabilities${refresh ? '?refresh=true' : ''}`,
      { timeoutMs: 30_000 },
    ),
  getTapoState: (id: string, refresh = false) =>
    api.get<TapoState>(
      `/cameras/${encodeURIComponent(id)}/tapo/state${refresh ? '?refresh=true' : ''}`,
      { timeoutMs: 30_000 },
    ),
  setTapoFeature: (id: string, feature: TapoFeature, payload: TapoFeaturePayload) =>
    api.post<TapoAction>(
      `/cameras/${encodeURIComponent(id)}/tapo/${encodeURIComponent(feature)}`,
      payload,
      { timeoutMs: 30_000 },
    ),
  setTapoSiren: (id: string, active: boolean) =>
    api.post<TapoAction>(
      `/cameras/${encodeURIComponent(id)}/tapo/siren/${active ? 'start' : 'stop'}`,
      { confirm: true },
      { timeoutMs: 30_000 },
    ),
  rebootTapo: (id: string) =>
    api.post<TapoAction>(
      `/cameras/${encodeURIComponent(id)}/tapo/reboot`,
      { confirm: true },
      { timeoutMs: 30_000 },
    ),
  /**
   * Snapshot URL for an `<img>` tag.
   *
   * `cacheKey` busts the browser cache, and the *caller* owns it deliberately.
   * Reading `Date.now()` here instead meant a new URL on every React render, so
   * any unrelated state change refetched a JPEG from every camera on the page.
   * Passing a value the caller only changes when it wants a fresh frame makes
   * refreshing an action rather than a side effect.
   */
  snapshotUrl: (id: string, options: { width?: number; cacheKey?: string | number } = {}) => {
    const query = new URLSearchParams()
    if (options.width) query.set('width', String(options.width))
    if (options.cacheKey !== undefined) query.set('t', String(options.cacheKey))
    const suffix = query.toString()
    return `/api/v1/cameras/${encodeURIComponent(id)}/snapshot${suffix ? `?${suffix}` : ''}`
  },
  testTapoBattery: (payload: TapoBatteryTestPayload) =>
    api.post<TapoBatteryStatus>('/cameras/tapo-battery/test', payload, { timeoutMs: 30_000 }),
  getTapoBatteryStatus: (id: string) =>
    api.get<TapoBatteryStatus>(`/cameras/${encodeURIComponent(id)}/tapo-battery/status`, {
      timeoutMs: 30_000,
    }),
  listTapoBatteryClips: (id: string) =>
    api.get<TapoBatteryClip[]>(`/cameras/${encodeURIComponent(id)}/tapo-battery/clips`, {
      timeoutMs: 45_000,
    }),
  syncTapoBatteryClips: (id: string, hours = 24, limit = 20) =>
    api.post<TapoBatterySyncResult>(
      `/cameras/${encodeURIComponent(id)}/tapo-battery/sync`,
      { hours, limit },
      { timeoutMs: 240_000 },
    ),
  tapoBatteryLiveUrl: (id: string, quality: 'HD' | 'SD' = 'HD') =>
    `/api/v1/cameras/${encodeURIComponent(id)}/tapo-battery/live?quality=${quality}`,
  setRecording: (id: string, enabled: boolean, confirm = false) =>
    api.put<CameraRecordingResult>(
      `/cameras/${encodeURIComponent(id)}/recording`,
      { enabled, confirm },
      { timeoutMs: 30_000 },
    ),
  identify: (id: string) =>
    api.post<CameraIdentity>(`/cameras/${encodeURIComponent(id)}/identify`, undefined, {
      timeoutMs: 20_000,
    }),
  getTimedRecordingOptions: (id: string) =>
    api.get<TimedRecordingOptions>(
      `/cameras/${encodeURIComponent(id)}/tapo-battery/timed-recording/options`,
      { timeoutMs: 30_000 },
    ),
  setTimedRecordingMax: (maxMinutes: number) =>
    api.put<TimedRecordingOptions>('/cameras/tapo-battery/timed-recording/max-minutes', {
      max_minutes: maxMinutes,
    }),
  getTimedRecording: (id: string) =>
    api.get<TimedRecordingStatus | null>(
      `/cameras/${encodeURIComponent(id)}/tapo-battery/timed-recording`,
      // Polled while a recording runs, and answered from memory, so it gets a
      // short ceiling: a slow answer here means something is wrong, not busy.
      { timeoutMs: 10_000 },
    ),
  startTimedRecording: (id: string, minutes: number) =>
    api.post<TimedRecordingStatus>(
      `/cameras/${encodeURIComponent(id)}/tapo-battery/timed-recording`,
      { minutes, accept_battery_drain: true },
      { timeoutMs: 45_000 },
    ),
  stopTimedRecording: (id: string) =>
    api.delete<TimedRecordingStatus>(
      `/cameras/${encodeURIComponent(id)}/tapo-battery/timed-recording`,
      { timeoutMs: 60_000 },
    ),
}

export interface SegmentQuery {
  camera_id?: string
  start?: string
  end?: string
  protected?: boolean
  has_motion?: boolean
  limit?: number
  offset?: number
}

export const recordingsApi = {
  list: (query: SegmentQuery = {}) => {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null) params.set(key, String(value))
    }
    const suffix = params.toString()
    return api.get<SegmentList>(`/recordings${suffix ? `?${suffix}` : ''}`)
  },
  get: (id: string) => api.get<RecordingSegment>(`/recordings/${id}`),
  setProtected: (id: string, isProtected: boolean) =>
    api.patch<RecordingSegment>(`/recordings/${id}`, { protected: isProtected }),
  status: () => api.get<RecorderStatus>('/recordings/status'),
  downloadUrl: (id: string) => `/api/v1/recordings/${id}/download`,
}

/** Query keys, centralised so invalidation cannot drift from the fetchers. */
export const queryKeys = {
  authStatus: ['auth', 'status'] as const,
  me: ['auth', 'me'] as const,
  tokens: ['auth', 'tokens'] as const,
  loginSecurity: ['auth', 'login-security'] as const,
  loginBlocks: ['auth', 'login-blocks'] as const,
  systemInfo: ['system', 'info'] as const,
  systemHealth: ['system', 'health'] as const,
  privacy: ['system', 'privacy'] as const,
  // Lists and per-camera resources deliberately have different prefixes.
  // Invalidating the camera list must not re-run an active Tapo capability,
  // battery-status or timed-recording query: those can wake a battery camera
  // or consume one of the vendor pool's scarce workers.
  cameras: ['cameras', 'list'] as const,
  camera: (id: string) => ['cameras', 'detail', id] as const,
  cameraPtzSetup: (id: string) => ['cameras', 'detail', id, 'ptz-setup'] as const,
  cameraTapoCapabilities: (id: string) =>
    ['cameras', 'detail', id, 'tapo-capabilities'] as const,
  cameraTapoState: (id: string) => ['cameras', 'detail', id, 'tapo-state'] as const,
  cameraTapoBatteryStatus: (id: string) =>
    ['cameras', 'detail', id, 'tapo-battery-status'] as const,
  cameraTimedRecording: (id: string) =>
    ['cameras', 'detail', id, 'timed-recording'] as const,
  cameraTimedRecordingOptions: (id: string) =>
    ['cameras', 'detail', id, 'timed-recording', 'options'] as const,
  recordings: ['recordings'] as const,
  recorderStatus: ['recordings', 'status'] as const,
}
