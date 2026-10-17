/**
 * Pure geometry and (de)serialisation helpers for the zone editor.
 *
 * Kept free of React and of the DOM so drawing-a-polygon and
 * malformed-input behaviour can be tested without mounting anything.
 */

import type { EditorZone, Point, WireZone, ZoneKind } from '@/features/zones/types'

/** Mirrors `MotionZonesUpdate` in `backend/app/motion/router.py`. Duplicated
 * here deliberately -- the two live in separate deployable artifacts, so the
 * frontend has to defend its own limits rather than trust the server always
 * agrees, especially when reading a value the server wrote in the past under
 * a different version of that validator. */
export const MAX_ZONES = 32
export const MAX_DETECT_ZONES = 16
export const MIN_POINTS = 3
export const MAX_POINTS = 128
export const MAX_NAME_LENGTH = 48

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
  zones: EditorZone[]
  /** Count of top-level entries that were not usable zones, so the page
   * can say "N zones could not be read" instead of quietly showing fewer
   * zones than the operator remembers saving. */
  dropped: number
}

function sanitizeWireZone(raw: unknown): EditorZone | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const { id, name, kind, enabled, sensitivity, points } = raw as Record<string, unknown>
  if (typeof id !== 'string' || !id) return null
  if (typeof name !== 'string' || !name.trim()) return null
  if (kind !== 'detect' && kind !== 'ignore') return null
  const polygon = sanitizeWirePolygon(points)
  if (!polygon) return null
  return {
    id,
    saved: true,
    name,
    kind,
    enabled: enabled !== false,
    sensitivity:
      kind === 'detect' && isFiniteNumber(sensitivity)
        ? Math.round(Math.min(100, Math.max(0, sensitivity)))
        : null,
    points: polygon,
  }
}

/** Turn whatever `GET /cameras/{id}/motion-zones` returned into zones this
 * editor can render, never throwing regardless of what is in `raw`. */
export function sanitizeServerZones(raw: unknown): SanitizedZones {
  if (!Array.isArray(raw)) return { zones: [], dropped: 0 }

  const zones: EditorZone[] = []
  let dropped = 0
  for (const entry of raw) {
    if (zones.length >= MAX_ZONES) {
      dropped += 1
      continue
    }
    const zone = sanitizeWireZone(entry)
    if (zone) zones.push(zone)
    else dropped += 1
  }
  return { zones, dropped }
}

function roundCoord(value: number): number {
  return Math.round(clamp01(value) * 10_000) / 10_000
}

/** The wire format `PUT /cameras/{id}/motion-zones` expects. Disabled zones
 * are sent too: the server keeps them, so a zone switched off for a while is
 * still there, with its name, when it is switched back on. A zone drawn in
 * this session is sent without an id so the server assigns its permanent one. */
export function zonesToPayload(zones: EditorZone[]): WireZone[] {
  return zones.map((zone) => ({
    ...(zone.saved ? { id: zone.id } : {}),
    name: zone.name.trim(),
    kind: zone.kind,
    enabled: zone.enabled,
    sensitivity: zone.kind === 'detect' ? zone.sensitivity : null,
    points: zone.points.map((point): [number, number] => [roundCoord(point.x), roundCoord(point.y)]),
  }))
}

/** Problems that would make the server refuse a save, worded for the operator. */
export function zoneProblems(zones: EditorZone[]): string[] {
  const problems: string[] = []
  const names = new Map<string, number>()
  for (const zone of zones) {
    const key = zone.name.trim().toLocaleLowerCase()
    if (!key) problems.push('Every zone needs a name.')
    names.set(key, (names.get(key) ?? 0) + 1)
  }
  for (const [name, count] of names) {
    if (name && count > 1) problems.push(`Two zones are called "${name}". Each name must be different.`)
  }
  if (zones.filter((zone) => zone.kind === 'detect').length > MAX_DETECT_ZONES) {
    problems.push(`At most ${MAX_DETECT_ZONES} detection zones per camera.`)
  }
  return [...new Set(problems)]
}

export function defaultZoneName(kind: ZoneKind, zones: EditorZone[]): string {
  const base = kind === 'detect' ? 'Zone' : 'Ignored area'
  const taken = new Set(zones.map((zone) => zone.name.trim().toLocaleLowerCase()))
  for (let index = 1; ; index += 1) {
    const candidate = `${base} ${index}`
    if (!taken.has(candidate.toLocaleLowerCase())) return candidate
  }
}

let localIdCounter = 0

/** A temporary id for a zone drawn in this session, used to key list items
 * until the first save, when the server assigns the permanent one.
 * `crypto.randomUUID` is unavailable in some older embedded browsers this
 * runs on (tablets used to aim a camera are not always new), hence the
 * fallback. */
export function makeZoneId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  localIdCounter += 1
  return `zone-${Date.now()}-${localIdCounter}`
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
    zones.map((zone) => [
      zone.id,
      zone.name,
      zone.kind,
      zone.enabled,
      zone.sensitivity,
      zone.points.map((p) => [p.x, p.y]),
    ]),
  )
}
