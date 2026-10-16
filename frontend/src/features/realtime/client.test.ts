import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { eventStreamUrl, RealtimeClient, type RealtimeStatus } from '@/features/realtime/client'

/** Minimal WebSocket stand-in, same shape as features/live/mse.test.ts's. */
class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3

  readyState = FakeWebSocket.CONNECTING
  sent: string[] = []
  closeCalled = false

  private listeners: Record<string, ((event: unknown) => void)[]> = {}

  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
  }

  addEventListener(type: string, handler: (event: unknown) => void): void {
    ;(this.listeners[type] ??= []).push(handler)
  }

  send(data: string): void {
    this.sent.push(data)
  }

  close(): void {
    this.closeCalled = true
    this.readyState = FakeWebSocket.CLOSED
  }

  emit(type: string, event: unknown = {}): void {
    for (const handler of this.listeners[type] ?? []) handler(event)
  }

  open(): void {
    this.readyState = FakeWebSocket.OPEN
    this.emit('open', {})
  }

  message(data: unknown): void {
    this.emit('message', { data: typeof data === 'string' ? data : JSON.stringify(data) })
  }

  serverClose(): void {
    this.readyState = FakeWebSocket.CLOSED
    this.emit('close', { code: 1000 })
  }
}

function last(): FakeWebSocket {
  const socket = FakeWebSocket.instances.at(-1)
  if (!socket) throw new Error('No FakeWebSocket was constructed')
  return socket
}

beforeEach(() => {
  FakeWebSocket.instances = []
  vi.stubGlobal('WebSocket', FakeWebSocket)
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('eventStreamUrl', () => {
  it('derives ws:// from an http page with no since', () => {
    expect(eventStreamUrl()).toBe(`ws://${window.location.host}/api/v1/events/stream`)
  })

  it('appends since when resuming', () => {
    expect(eventStreamUrl(42)).toBe(`ws://${window.location.host}/api/v1/events/stream?since=42`)
  })
})

describe('RealtimeClient', () => {
  it('connects to the event stream URL and reports connecting first', () => {
    const states: RealtimeStatus[] = []
    new RealtimeClient({ onStatusChange: (s) => states.push(s) }).start()

    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(last().url).toBe(`ws://${window.location.host}/api/v1/events/stream`)
    expect(states).toEqual(['connecting'])
  })

  it('only reports open once hello arrives, not on the raw socket open', () => {
    const states: RealtimeStatus[] = []
    new RealtimeClient({ onStatusChange: (s) => states.push(s) }).start()
    last().open()
    expect(states).toEqual(['connecting']) // not yet 'open'

    last().message({ type: 'hello', resumed: false, replayed: 0 })
    expect(states).toEqual(['connecting', 'open'])
  })

  it('delivers a well-formed event and tracks its sequence number', () => {
    const events: unknown[] = []
    const client = new RealtimeClient({ onEvent: (e) => events.push(e) })
    client.start()
    last().open()
    last().message({ type: 'hello', resumed: false, replayed: 0 })

    last().message({ type: 'motion.started', seq: 5, ts: '2026-01-01T00:00:00Z', data: { camera_id: 'a' } })

    expect(events).toEqual([
      { type: 'motion.started', seq: 5, ts: '2026-01-01T00:00:00Z', data: { camera_id: 'a' } },
    ])
    expect(client.lastSequence).toBe(5)
  })

  it('drops a duplicate or out-of-order event rather than double-delivering it', () => {
    const events: unknown[] = []
    const client = new RealtimeClient({ onEvent: (e) => events.push(e) })
    client.start()
    last().open()
    last().message({ type: 'hello', resumed: false, replayed: 0 })
    last().message({ type: 'motion.started', seq: 5, ts: 't', data: {} })
    last().message({ type: 'motion.started', seq: 5, ts: 't', data: {} })
    last().message({ type: 'motion.started', seq: 3, ts: 't', data: {} })

    expect(events).toHaveLength(1)
  })

  it('calls onResyncRequired and adopts its seq as the new resume point', () => {
    let resynced = false
    const client = new RealtimeClient({ onResyncRequired: () => (resynced = true) })
    client.start()
    last().open()
    last().message({ type: 'resync_required', seq: 340 })

    expect(resynced).toBe(true)
    expect(client.lastSequence).toBe(340)
  })

  it('ignores heartbeat and pong frames', () => {
    const events: unknown[] = []
    const client = new RealtimeClient({ onEvent: (e) => events.push(e) })
    client.start()
    last().open()
    last().message({ type: 'heartbeat', server_seq: 10, ts: 't' })
    last().message({ type: 'pong' })

    expect(events).toHaveLength(0)
    expect(client.lastSequence).toBeNull()
  })

  it('ignores an unparsable frame instead of throwing', () => {
    const client = new RealtimeClient()
    client.start()
    expect(() => last().message('not json at all')).not.toThrow()
  })

  it('sends a ping on its own interval while connected', () => {
    const client = new RealtimeClient()
    client.start()
    last().open()

    vi.advanceTimersByTime(25_000)
    expect(last().sent).toEqual([JSON.stringify({ type: 'ping' })])
  })

  it('reconnects after a drop, resuming from the last processed sequence', () => {
    const states: RealtimeStatus[] = []
    const client = new RealtimeClient({ onStatusChange: (s) => states.push(s) })
    client.start()
    last().open()
    last().message({ type: 'hello', resumed: false, replayed: 0 })
    last().message({ type: 'motion.started', seq: 7, ts: 't', data: {} })

    last().serverClose()
    expect(states.at(-1)).toBe('reconnecting')
    expect(FakeWebSocket.instances).toHaveLength(1) // not yet -- backoff pending

    vi.advanceTimersByTime(1000)
    expect(FakeWebSocket.instances).toHaveLength(2)
    expect(last().url).toBe(`ws://${window.location.host}/api/v1/events/stream?since=7`)
  })

  it('backs off exponentially across repeated drops', () => {
    const client = new RealtimeClient()
    client.start()
    last().serverClose() // attempt 0 -> next delay 1000ms
    vi.advanceTimersByTime(999)
    expect(FakeWebSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances).toHaveLength(2)

    last().serverClose() // attempt 1 -> next delay 2000ms
    vi.advanceTimersByTime(1999)
    expect(FakeWebSocket.instances).toHaveLength(2)
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances).toHaveLength(3)
  })

  it('stop() closes the socket, reports closed, and never reconnects', () => {
    const states: RealtimeStatus[] = []
    const client = new RealtimeClient({ onStatusChange: (s) => states.push(s) })
    client.start()
    last().open()

    client.stop()
    expect(last().closeCalled).toBe(true)
    expect(states.at(-1)).toBe('closed')

    // A close event arriving after stop() (the socket's own, caused by our
    // close() call) must not be treated as a drop needing a reconnect.
    last().serverClose()
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(states.at(-1)).toBe('closed')
  })

  it('stop() also cancels the ping timer', () => {
    const client = new RealtimeClient()
    client.start()
    last().open()
    client.stop()

    vi.advanceTimersByTime(60_000)
    expect(last().sent).toEqual([])
  })

  it('start() is idempotent while already running', () => {
    const client = new RealtimeClient()
    client.start()
    client.start()
    expect(FakeWebSocket.instances).toHaveLength(1)
  })
})
