import type { Camera, TapoControlAvailability } from '@/types/api'

/**
 * Camera fixtures modelled on a typical mixed installation.
 *
 * They live together because the interesting cases are the differences between
 * them: a Tapo camera, a Hikvision NVR channel that shares its subnet, a camera
 * nothing identifies, and a battery camera. A component that renders all four
 * correctly is one that has stopped guessing.
 */

const TAPO_DETECTED: TapoControlAvailability = {
  supported: true,
  source: 'metadata',
  detected_vendor: 'TP-Link',
  reason: 'TP-Link identifies this as a TP-Link/Tapo camera.',
  can_opt_in: false,
}

const NOT_TAPO: TapoControlAvailability = {
  supported: false,
  source: 'other_vendor',
  detected_vendor: 'Hikvision-compatible NVR',
  reason: 'The stream address uses the Hikvision-compatible NVR layout, so Tapo controls do not apply.',
  can_opt_in: false,
}

const UNKNOWN_VENDOR: TapoControlAvailability = {
  supported: false,
  source: 'unknown',
  detected_vendor: null,
  reason:
    "Sentinel could not identify this camera's manufacturer. Run Identify, or turn on Tapo LAN " +
    'controls in Configure if you know it is a TP-Link/Tapo camera.',
  can_opt_in: true,
}

const BATTERY_TAPO: TapoControlAvailability = {
  supported: true,
  source: 'kind',
  detected_vendor: 'Tapo',
  reason: 'This camera is registered as a Tapo battery device.',
  can_opt_in: false,
}

const BASE: Camera = {
  id: 'cam-1',
  kind: 'rtsp',
  name: 'Front door',
  manufacturer: 'TP-Link',
  model: 'Tapo C520WS',
  nvr_group: null,
  host: '192.168.50.50',
  tapo_child_id: null,
  main_stream_url: 'rtsp://192.168.50.50:554/stream1',
  sub_stream_url: null,
  username: 'admin',
  has_credentials: true,
  has_tapo_credentials: true,
  onvif_device_xaddr: null,
  ptz_supported: true,
  ptz_zoom_supported: false,
  ptz_status_message: null,
  ptz_last_checked_at: null,
  onvif_patrol_enabled: false,
  tapo_patrol_mode: null,
  tapo_controls: TAPO_DETECTED,
  tapo_controls_enabled: null,
  recording_state: 'recording',
  recording_enabled: true,
  enabled: true,
  recording_mode: 'continuous',
  recording_profile: 'auto',
  recording_source_mode: 'auto',
  recording_segment_seconds: null,
  retention_days: 30,
  motion_enabled: true,
  motion_sensitivity: 50,
  status: 'online',
  status_message: null,
  last_seen_at: null,
  stream: {
    codec: 'h264',
    width: 2560,
    height: 1440,
    fps: 25,
    audio_codec: 'aac',
    bitrate_kbps: 4200,
  },
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-27T00:00:00Z',
}

export function makeCamera(overrides: Partial<Camera> = {}): Camera {
  return { ...BASE, ...overrides }
}

/** A camera whose own metadata says TP-Link. */
export const tapoCamera = makeCamera()

/** One channel of an NVR. Its other channels share the same host. */
export const nvrCamera = makeCamera({
  id: 'cam-nvr',
  name: 'Courtyard',
  manufacturer: null,
  model: null,
  host: '192.168.50.179',
  main_stream_url: 'rtsp://192.168.50.179:554/Streaming/Channels/101',
  ptz_supported: false,
  has_tapo_credentials: false,
  tapo_controls: NOT_TAPO,
})

/** Reachable, recording, and Sentinel has no idea who made it. */
export const unknownCamera = makeCamera({
  id: 'cam-unknown',
  name: 'Storeroom',
  manufacturer: null,
  model: null,
  host: '192.168.50.118',
  main_stream_url: 'rtsp://192.168.50.118:554/live',
  ptz_supported: null,
  has_tapo_credentials: false,
  tapo_controls: UNKNOWN_VENDOR,
})

/** A TC82: no RTSP, no continuous recorder, event clips and Direct Stream. */
export const batteryCamera = makeCamera({
  id: 'cam-battery',
  kind: 'tapo_battery',
  name: 'Shed',
  manufacturer: 'Tapo',
  model: 'TC82',
  host: '192.168.50.220',
  main_stream_url: null,
  sub_stream_url: null,
  username: null,
  has_credentials: false,
  ptz_supported: null,
  recording_mode: 'events',
  recording_state: 'unsupported',
  motion_enabled: false,
  tapo_controls: BATTERY_TAPO,
  stream: {
    codec: null,
    width: null,
    height: null,
    fps: null,
    audio_codec: null,
    bitrate_kbps: null,
  },
})
