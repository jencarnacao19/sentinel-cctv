/**
 * Pure geometry and (de)serialisation helpers for the zone editor.
 *
 * Kept free of React and of the DOM so drawing-a-polygon and
 * malformed-input behaviour can be tested without mounting anything.
 */

import type { EditorZone, Point } from '@/features/zones/types'

/** Mirrors `MotionZonesUpdate` in `backend/app/motion/router.py`. Duplicated
 * here deliberately -- the two live in separate deployable artifacts, so the
 * frontend has to defend its own limits rather than trust the server always
 * agrees, especially when reading a value the server wrote in the past under
 * a different version of that validator. */
export const MAX_ZONES = 32
export const MIN_POINTS = 3
export const MAX_POINTS = 128

export function clamp01(value: number): number {
  if (Number.isNaN(value)) return 0
  return Math.min(1, Math.max(0, value))
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

/** A single `[x, y]` pair from the wire, or `null` if it is not one.
 * Out-of-range coordinates are rejected rather than clamped: clamping would
 * silently relocate a point the server considered invalid, which is worse
 * than just leaving it out. */
function sanitizeWirePoint(raw: unknown): Point | null {
  if (!Array.isArray(raw) || raw.length !== 2) return null
  const [x, y] = raw as [unknown, unknown]
  if (!isFiniteNumber(x) || !isFiniteNumber(y)) return null
  if (x < 0 || x > 1 || y < 0 || y > 1) return null
  return { x, y }
}

/** One polygon from the wire. Points that do not parse are dropped rather
 * than failing the whole polygon -- mirrors the tolerance in
 * `app/motion/service.py:parse_zones`, whose comment is the reason: a bad
 * value must not stop the operator from seeing everything that IS valid. */
function sanitizeWirePolygon(raw: unknown): Point[] | null {
  if (!Array.isArray(raw)) return null
  const points: Point[] = []
  for (const entry of raw) {
    const point = sanitizeWirePoint(entry)
    if (point) points.push(point)
    if (points.length > MAX_POINTS) break
  }
  if (points.length < MIN_POINTS || points.length > MAX_POINTS) return null
  return points
}

export interface SanitizedZones {
  zones: Point[][]
  /** Count of top-level entries that were not usable polygons, so the page
   * can say "N zones could not be read" instead of quietly showing fewer
   * zones than the operator remembers saving. */
  dropped: number
}

/** Turn whatever `GET /cameras/{id}/motion-zones` returned into zones this
 * editor can render, never throwing regardless of what is in `raw`. */
export function sanitizeServerZones(raw: unknown): SanitizedZones {
  if (!Array.isArray(raw)) return { zones: [], dropped: 0 }

  const zones: Point[][] = []
  let dropped = 0
  for (const entry of raw) {
    if (zones.length >= MAX_ZONES) {
      dropped += 1
      continue
    }
    const polygon = sanitizeWirePolygon(entry)
    if (polygon) zones.push(polygon)
    else dropped += 1
  }
  return { zones, dropped }
}

function roundCoord(value: number): number {
  return Math.round(clamp01(value) * 10_000) / 10_000
}

/** The wire format `PUT /cameras/{id}/motion-zones` expects: bare arrays of
 * `[x, y]` pairs, enabled zones only. A disabled zone is a real zone the
 * operator is keeping for later, not one that should still suppress motion --
 * so it is deliberately left out of what gets saved. */
export function zonesToPayload(zones: EditorZone[]): [number, number][][] {
  return zones
    .filter((zone) => zone.enabled)
    .map((zone) => zone.points.map((point): [number, number] => [roundCoord(point.x), roundCoord(point.y)]))
}

/** Identifies a polygon by its rounded points, for matching a locally-known
 * zone against what the server has without caring about point-object
 * identity. Order-sensitive on purpose: two polygons with the same points in
 * a different winding order are not "the same zone" as far as this editor's
 * merge logic needs to care, and treating them as different only ever causes
 * an extra harmless import, never a lost zone. */
export function polygonKey(points: Point[]): string {
  return points.map((point) => `${roundCoord(point.x)},${roundCoord(point.y)}`).join('|')
}

let localIdCounter = 0

/** A per-tab-session unique id for a locally-known zone. Not sent to the
 * server -- it exists only so React can key list items and so `localStorage`
 * entries survive a reorder. `crypto.randomUUID` is unavailable in some older
 * embedded browsers this runs on (tablets used to aim a camera are not always
 * new), hence the fallback. */
export function makeZoneId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  localIdCounter += 1
  return `zone-${Date.now()}-${localIdCounter}`
}

/**
 * Reconcile the zones remembered in this browser (with names and
 * enabled/disabled state) against what the server actually has right now.
 *
 * `stored === null` means this device has never edited this camera's zones
 * before (or its storage was cleared): every server zone becomes a freshly
 * named, enabled zone. Otherwise, any server zone that does not match an
 * *enabled* local zone is new -- either set through some future API-only
 * path, or the local copy was cleared independently -- and is imported
 * rather than silently dropped, because losing a zone that is actively
 * suppressing motion is worse than a spuriously named "Imported zone".
 */
export function mergeZones(stored: EditorZone[] | null, serverZones: Point[][]): EditorZone[] {
  if (stored === null) {
    return serverZones.map((points, index) => ({
      id: makeZoneId(),
      name: `Zone ${index + 1}`,
      enabled: true,
      points,
    }))
  }

  const knownKeys = new Set(stored.filter((zone) => zone.enabled).map((zone) => polygonKey(zone.points)))
  const imported = serverZones
    .filter((points) => !knownKeys.has(polygonKey(points)))
    .map((points, index) => ({
      id: makeZoneId(),
      name: `Imported zone ${index + 1}`,
      enabled: true,
      points,
    }))
  return [...stored, ...imported]
}

/** Ray-casting point-in-polygon test, used to pick which zone a click landed
 * inside when it did not land on a vertex handle. Works in any consistent
 * coordinate space (normalised or pixel) since it is scale-free. */
export function pointInPolygon(point: Point, polygon: Point[]): boolean {
  let inside = false
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const a = polygon[i]
    const b = polygon[j]
    const intersects =
      a.y > point.y !== b.y > point.y &&
      point.x < ((b.x - a.x) * (point.y - a.y)) / (b.y - a.y) + a.x
    if (intersects) inside = !inside
  }
  return inside
}

export function distance(a: Point, b: Point): number {
  return Math.hypot(a.x - b.x, a.y - b.y)
}

/** A stable, order-preserving signature of the editor's full state, used to
 * decide whether there are unsaved changes. Deliberately includes names and
 * the enabled flag, not just points: renaming a zone or turning it off is a
 * real edit even though a mouse never touched the canvas. */
export function zonesSignature(zones: EditorZone[]): string {
  return JSON.stringify(
    zones.map((zone) => [zone.id, zone.name, zone.enabled, zone.points.map((p) => [p.x, p.y])]),
  )
}
