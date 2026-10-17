import { BatteryCharging, Router, Video } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import type { Camera, CameraStatus } from '@/types/api'
import type { StatusTone } from '@/components/ui/status'

/**
 * What kind of thing a camera row is describing.
 *
 * Deliberately a monochrome symbol from the same family as the rest of the
 * interface, never a two-letter avatar. "CM", "MZ" and "ST" told an operator
 * nothing about whether a row was an RTSP camera, one channel of a shared NVR
 * or a battery device that only the dev build supports -- which is the only
 * distinction that changes what the row can do.
 *
 * The NVR case is decided by the camera's own `nvr_group`, assigned by the
 * server. It is never inferred from the address: on a typical NVR several
 * channels of one recorder share a single IP, so an address says nothing about
 * what a camera is.
 */
export interface CameraKindDescription {
  icon: LucideIcon
  label: string
}

export function describeKind(camera: Camera): CameraKindDescription {
  if (camera.kind === 'tapo_battery') {
    return { icon: BatteryCharging, label: 'Battery camera' }
  }
  if (camera.nvr_group) return { icon: Router, label: 'NVR channel' }
  return { icon: Video, label: 'RTSP camera' }
}

/**
 * Connection state in words an operator can act on.
 *
 * `error` was previously rendered as the bare word "error", which says that
 * something is wrong but not what -- and read identically whether the camera
 * had refused a password or simply been unplugged. The per-camera
 * `status_message` carries the specific reason and is shown underneath.
 */
export const STATUS_LABEL: Record<CameraStatus, string> = {
  online: 'Online',
  offline: 'Offline',
  connecting: 'Connecting',
  error: 'Not reachable',
  disabled: 'Disabled',
}

export const STATUS_TONE: Record<CameraStatus, StatusTone> = {
  online: 'ok',
  offline: 'idle',
  connecting: 'warning',
  error: 'danger',
  disabled: 'idle',
}
