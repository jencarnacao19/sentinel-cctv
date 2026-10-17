/**
 * Local types for the zone editor.
 *
 * `frontend/src/types/api.ts` is owned by the orchestrator and does not yet
 * describe the motion-zones, motion-status or events shapes this feature
 * needs -- the same reason `EventsPage.tsx` keeps its own local `MotionEvent`
 * interface rather than importing one. These mirror the Pydantic models in
 * `backend/app/motion/router.py` for the fields this editor actually reads.
 */

/** Normalised 0-1 coordinate, resolution-independent by construction. */
export interface Point {
  x: number
  y: number
}

export type ZoneKind = 'detect' | 'ignore'

/**
 * One zone as the editor works with it, and as the server stores it.
 *
 * A detection zone is where motion counts once any exist on the camera, with
 * its own Home Assistant sensor and, optionally, its own sensitivity (`null`
 * follows the camera's). An ignore zone is cut out of the picture entirely.
 * `id` is the server's for a saved zone; a zone drawn in this session gets a
 * temporary one until the first save replaces it.
 */
export interface EditorZone {
  id: string
  /** False until the server has assigned this zone its permanent id. */
  saved: boolean
  name: string
  kind: ZoneKind
  enabled: boolean
  sensitivity: number | null
  points: Point[]
}

/** A zone as `GET`/`PUT /cameras/{id}/motion-zones` exchanges it. */
export interface WireZone {
  id?: string
  name: string
  kind: ZoneKind
  enabled: boolean
  sensitivity: number | null
  points: [number, number][]
}

/** One worker entry from `GET /events/status`. The endpoint returns more than
 * this; only the fields the tuning panel displays are listed here. */
export interface MotionWorkerStatus {
  camera_id: string
  running: boolean
  frames_seen: number
  last_frame_at: string | null
  reconnects: number
  using_substream: boolean
  in_event: boolean
  active_zones?: string[]
  last_error: string | null
}

export interface MotionWorkerStatusResponse {
  workers: MotionWorkerStatus[]
}

/** Subset of `EventResponse` (`app/motion/router.py`) used by the "recent
 * events" panel that makes tuning empirical instead of guesswork. */
export interface MotionEventSummary {
  id: string
  camera_id: string
  started_at: string
  ended_at: string | null
  duration_seconds: number | null
  peak_score: number
}

export interface MotionEventListResponse {
  events: MotionEventSummary[]
  total: number
  limit: number
  offset: number
}
