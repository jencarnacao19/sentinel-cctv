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

/**
 * One ignore zone as the editor works with it.
 *
 * The API only ever stores bare polygons (see `MotionZonesUpdate` in
 * `app/motion/router.py`) -- no name, no enabled flag. `name` and `enabled`
 * are a local convenience layered on top and persisted in `localStorage`
 * (see `zoneStorage.ts`), not sent to the server. Only `points` round-trips.
 */
export interface EditorZone {
  id: string
  name: string
  enabled: boolean
  points: Point[]
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
