import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useRealtimeEvents } from '@/features/realtime/useRealtimeEvents'

/** Minimal WebSocket stand-in, same shape as client.test.ts's. */
class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  static readonly CONNECTING = 0
  static readonly OPEN = 1

  readyState = FakeWebSocket.CONNECTING
  closeCalled = false
  private listeners: Record<string, ((event: unknown) => void)[]> = {}

  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
  }

  addEventListener(type: string, handler: (event: unknown) => void): void {
    ;(this.listeners[type] ??= []).push(handler)
  }

  send(): void {
    /* not exercised by these tests */
  }

  close(): void {
    this.closeCalled = true
  }

  emit(type: string, event: unknown = {}): void {
    for (const handler of this.listeners[type] ?? []) handler(event)
  }

  open(): void {
    this.readyState = FakeWebSocket.OPEN
    this.emit('open', {})
  }

  message(data: unknown): void {
    this.emit('message', { data: JSON.stringify(data) })
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
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useRealtimeEvents', () => {
  it('starts connecting and opens once hello arrives', () => {
    const { result } = renderHook(() => useRealtimeEvents())
    expect(result.current.status).toBe('connecting')

    act(() => {
      last().open()
      last().message({ type: 'hello', resumed: false, replayed: 0 })
    })
    expect(result.current.status).toBe('open')
  })

  it('surfaces delivered events through both the callback and lastEvent', () => {
    const received: unknown[] = []
    const { result } = renderHook(() => useRealtimeEvents({ onEvent: (e) => received.push(e) }))

    act(() => {
      last().open()
      last().message({ type: 'hello', resumed: false, replayed: 0 })
      last().message({ type: 'camera.online', seq: 1, ts: 't', data: { camera_id: 'a' } })
    })

    expect(received).toHaveLength(1)
    expect(result.current.lastEvent?.type).toBe('camera.online')
  })

  it('calls onResyncRequired when the server cannot fill a resume gap', () => {
    let resynced = false
    renderHook(() => useRealtimeEvents({ onResyncRequired: () => (resynced = true) }))

    act(() => {
      last().open()
      last().message({ type: 'resync_required', seq: 99 })
    })
    expect(resynced).toBe(true)
  })

  it('never opens a socket when disabled', () => {
    const { result } = renderHook(() => useRealtimeEvents({ enabled: false }))
    expect(FakeWebSocket.instances).toHaveLength(0)
    expect(result.current.status).toBe('closed')
  })

  it('closes the socket on unmount', () => {
    const { unmount } = renderHook(() => useRealtimeEvents())
    const socket = last()
    unmount()
    expect(socket.closeCalled).toBe(true)
  })

  it('tears the connection down when enabled flips to false', () => {
    const { rerender } = renderHook(({ enabled }) => useRealtimeEvents({ enabled }), {
      initialProps: { enabled: true },
    })
    const socket = last()

    rerender({ enabled: false })
    expect(socket.closeCalled).toBe(true)
    expect(FakeWebSocket.instances).toHaveLength(1) // no new connection opened
  })
})
