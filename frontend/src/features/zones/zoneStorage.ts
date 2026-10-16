/**
 * Per-device memory of zone names and enabled/disabled state.
 *
 * The server only ever stores bare polygons (see `types.ts`), so a name and
 * a "keep this zone but stop enforcing it" toggle have nowhere to live but
 * here. Written only at the moment of an explicit Save, in step with the
 * server write, so a browser refresh mid-edit loses exactly as much as a
 * refresh would lose server-side -- nothing surprising either way.
 */

import type { EditorZone, Point } from '@/features/zones/types'
import { MAX_POINTS, MIN_POINTS } from '@/features/zones/zoneMath'

const STORAGE_PREFIX = 'sentinel.motion-zones.'

function isPoint(value: unknown): value is Point {
  return (
    !!value &&
    typeof value === 'object' &&
    typeof (value as Point).x === 'number' &&
    typeof (value as Point).y === 'number' &&
    Number.isFinite((value as Point).x) &&
    Number.isFinite((value as Point).y)
  )
}

function sanitizeStoredZone(raw: unknown): EditorZone | null {
  if (!raw || typeof raw !== 'object') return null
  const { id, name, enabled, points } = raw as Record<string, unknown>
  if (typeof id !== 'string' || !id) return null
  if (typeof name !== 'string') return null
  if (typeof enabled !== 'boolean') return null
  if (!Array.isArray(points)) return null
  const cleanPoints = points.filter(isPoint)
  if (cleanPoints.length < MIN_POINTS || cleanPoints.length > MAX_POINTS) return null
  return { id, name, enabled, points: cleanPoints }
}

/** Returns `null` when this device has nothing remembered for this camera
 * (first visit, cleared storage, or private browsing) -- distinct from an
 * empty array, which would mean "remembered, and there are zero zones". */
export function loadStoredZones(cameraId: string): EditorZone[] | null {
  try {
    const raw = localStorage.getItem(STORAGE_PREFIX + cameraId)
    if (raw === null) return null
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return null
    const zones = parsed.map(sanitizeStoredZone).filter((zone): zone is EditorZone => zone !== null)
    return zones
  } catch {
    // Corrupted JSON, or storage disabled entirely: behave like a first
    // visit rather than crashing the editor over a cache the user cannot see.
    return null
  }
}

export function saveStoredZones(cameraId: string, zones: EditorZone[]): void {
  try {
    localStorage.setItem(STORAGE_PREFIX + cameraId, JSON.stringify(zones))
  } catch {
    // Private browsing or a full quota: the server copy still saved, which is
    // the part that actually controls detection. Only the local names and
    // disabled-zone shapes fail to stick around for next time.
  }
}
