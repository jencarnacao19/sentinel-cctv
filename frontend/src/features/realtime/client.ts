/**
 * Client for `WS /api/v1/events/stream` -- the realtime push endpoint.
 *
 * A plain, framework-free class in the same shape as `features/live/mse.ts`:
 * state changes reported through a callback, so it can be driven by a React
 * hook (see `useRealtimeEvents.ts`) or exercised directly in a test with
 * neither React nor a real socket. See
 * `backend/app/integrations/CONTRACT.md` section 5 for the wire protocol
 * this implements.
 *
 * Auth rides on the session cookie exactly like every other request this
 * frontend makes (see `api/client.ts`) -- nothing to attach here, the
 * browser sends it on the WebSocket upgrade automatically.
 */

export type RealtimeStatus = 'connecting' | 'open' | 'reconnecting' | 'closed'

export interface RealtimeEvent {
  type: string
  seq: number
  ts: string
  data: Record<string, unknown>
}

export interface RealtimeClientEvents {
  onStatusChange?: (status: RealtimeStatus) => void
  /** Every event frame, live or replayed after a resume, in order. */
  onEvent?: (event: RealtimeEvent) => void
  /** The server could not fill the requested resume gap (or none was
   * requested and this is moot on a fresh connect) -- anything built from
   * cached events should be refetched over the REST API. */
  onResyncRequired?: () => void
}

const RECONNECT_BASE_MS = 1000
const RECONNECT_MAX_MS = 30_000

/** How often this client pings the server -- the other half of "heartbeat
 * both ways": the server already pings an idle connection on its own, this
 * is this side's equivalent so a client behind a NAT or proxy that only
 * tracks traffic in one direction still looks alive. */
const CLIENT_PING_INTERVAL_MS = 25_000

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

/**
 * Narrows an already-parsed frame to a real event.
 *
 * Takes `unknown` rather than `Record<string, unknown>`: a type predicate's
 * asserted type has to be assignable to its parameter's, and `RealtimeEvent`
 * has named fields rather than an index signature, so it is not assignable to
 * a bare record. Widening the input is the honest fix -- the function does its
 * own shape checking anyway.
 */
function isRealtimeEvent(message: unknown): message is RealtimeEvent {
  if (!isPlainObject(message)) return false
  return (
    typeof message.type === 'string' &&
    typeof message.seq === 'number' &&
    typeof message.ts === 'string' &&
    message.type !== 'hello' &&
    message.type !== 'heartbeat' &&
    message.type !== 'resync_required' &&
    message.type !== 'pong'
  )
}

/** Absolute ws(s):// URL for the event stream, resuming after `since` when given. */
export function eventStreamUrl(since?: number): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const query = since !== undefined ? `?since=${since}` : ''
  return `${protocol}//${window.location.host}/api/v1/events/stream${query}`
}

/**
 * Keeps one WebSocket to the event stream alive for as long as it is
 * running: connects, resumes after a drop, and never gives up trying --
 * a Sentinel restart or a flaky network must end in the connection coming
 * back on its own, the same expectation the backend's own supervised
 * processes are held to.
 */
export class RealtimeClient {
  private ws: WebSocket | null = null
  private stopped = true
  private reconnectAttempt = 0
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private pingTimer: ReturnType<typeof setInterval> | null = null
  private lastSeq: number | null = null
  // Starts as null, not 'connecting', so the very first transition is
  // reported like every other one -- a consumer that only listens to the
  // callback would otherwise never learn the client had started.
  private status: RealtimeStatus | null = null

  constructor(private readonly events: RealtimeClientEvents = {}) {}

  start(): void {
    if (!this.stopped) return
    this.stopped = false
    this.reconnectAttempt = 0
    this.connect()
  }

  stop(): void {
    this.stopped = true
    this.clearTimers()
    if (this.ws) {
      this.ws.onopen = null
      this.ws.onmessage = null
      this.ws.onerror = null
      this.ws.onclose = null
      if (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING) {
        this.ws.close()
      }
      this.ws = null
    }
    this.setStatus('closed')
  }

  /** Highest sequence number processed so far, or `null` before the first
   * event -- exposed mainly for tests; reconnecting already resumes from
   * this on its own. */
  get lastSequence(): number | null {
    return this.lastSeq
  }

  private connect(): void {
    this.setStatus(this.reconnectAttempt === 0 ? 'connecting' : 'reconnecting')

    let socket: WebSocket
    try {
      socket = new WebSocket(eventStreamUrl(this.lastSeq ?? undefined))
    } catch {
      this.scheduleReconnect()
      return
    }
    this.ws = socket

    socket.addEventListener('open', () => {
      this.startPinging()
    })

    socket.addEventListener('message', (event: MessageEvent<unknown>) => {
      if (typeof event.data === 'string') this.handleMessage(event.data)
    })

    socket.addEventListener('close', () => {
      this.stopPinging()
      this.ws = null
      if (this.stopped) return
      this.scheduleReconnect()
    })

    // 'close' always follows an 'error' for a WebSocket, so reconnection is
    // handled once, there -- this listener only exists so a rejected
    // connection does not surface as an uncaught browser error event.
    socket.addEventListener('error', () => undefined)
  }

  private handleMessage(raw: string): void {
    let parsed: unknown
    try {
      parsed = JSON.parse(raw)
    } catch {
      return
    }
    if (!isPlainObject(parsed)) return

    if (parsed.type === 'hello') {
      this.setStatus('open')
      return
    }
    if (parsed.type === 'resync_required') {
      if (typeof parsed.seq === 'number') this.lastSeq = parsed.seq
      this.events.onResyncRequired?.()
      return
    }
    if (parsed.type === 'heartbeat' || parsed.type === 'pong') {
      return
    }
    if (isRealtimeEvent(parsed)) {
      // The server never sends a duplicate to one connection, but dropping
      // one defensively here costs nothing and protects against a replaying
      // intermediary or a future server bug turning into a double-counted
      // motion event on screen.
      if (this.lastSeq !== null && parsed.seq <= this.lastSeq) return
      this.lastSeq = parsed.seq
      this.events.onEvent?.(parsed)
    }
  }

  private startPinging(): void {
    this.stopPinging()
    this.pingTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: 'ping' }))
      }
    }, CLIENT_PING_INTERVAL_MS)
  }

  private stopPinging(): void {
    if (this.pingTimer !== null) {
      clearInterval(this.pingTimer)
      this.pingTimer = null
    }
  }

  private scheduleReconnect(): void {
    if (this.stopped) return
    this.setStatus('reconnecting')
    const delay = Math.min(RECONNECT_BASE_MS * 2 ** this.reconnectAttempt, RECONNECT_MAX_MS)
    this.reconnectAttempt += 1
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      if (!this.stopped) this.connect()
    }, delay)
  }

  private clearTimers(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.stopPinging()
  }

  private setStatus(status: RealtimeStatus): void {
    if (this.status === status) return
    this.status = status
    this.events.onStatusChange?.(status)
  }
}
