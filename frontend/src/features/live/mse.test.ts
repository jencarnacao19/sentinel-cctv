import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { isMseSupported, MsePlayer, type PlayerState, websocketUrl } from '@/features/live/mse'

/** Minimal WebSocket stand-in that records what the player sends. */
class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  static readonly CONNECTING = 0
  static readonly OPEN = 1

  readyState = FakeWebSocket.OPEN
  binaryType = ''
  sent: string[] = []
  closed = false
  onmessage: unknown = null
  onerror: unknown = null
  onclose: unknown = null

  private listeners: Record<string, ((event: unknown) => void)[]> = {}

  constructor(
    public url: string,
    public protocols?: string | string[],
  ) {
    FakeWebSocket.instances.push(this)
  }

  addEventListener(type: string, handler: (event: unknown) => void): void {
    ;(this.listeners[type] ??= []).push(handler)
  }

  send(data: string): void {
    this.sent.push(data)
  }

  close(): void {
    this.closed = true
  }

  emit(type: string, event: unknown): void {
    for (const handler of this.listeners[type] ?? []) handler(event)
  }
}

class FakeSourceBuffer extends EventTarget {
  updating = false
  mode: AppendMode = 'segments'
  readonly appended: ArrayBuffer[] = []
  readonly removed: [number, number][] = []
  readonly buffered = {
    length: 1,
    start: () => 0,
    end: () => 40,
  }

  appendBuffer(chunk: ArrayBuffer): void {
    this.appended.push(chunk)
    this.updating = true
  }

  remove(start: number, end: number): void {
    this.removed.push([start, end])
    this.updating = true
  }

  finishUpdate(): void {
    this.updating = false
    this.dispatchEvent(new Event('updateend'))
  }
}

class FakeMediaSource extends EventTarget {
  static instances: FakeMediaSource[] = []
  static isTypeSupported = () => true

  readyState: ReadyState = 'open'
  readonly buffer = new FakeSourceBuffer()

  constructor() {
    super()
    FakeMediaSource.instances.push(this)
  }

  addSourceBuffer(): SourceBuffer {
    return this.buffer as unknown as SourceBuffer
  }

  removeSourceBuffer(): void {}

  endOfStream(): void {
    this.readyState = 'closed'
  }
}

function fakeVideo(): HTMLVideoElement {
  const element = document.createElement('video')
  Object.defineProperty(element, 'play', { value: vi.fn(() => Promise.resolve()) })
  Object.defineProperty(element, 'load', { value: vi.fn() })
  return element
}

beforeEach(() => {
  FakeWebSocket.instances = []
  FakeMediaSource.instances = []
  vi.stubGlobal('WebSocket', FakeWebSocket)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('websocketUrl', () => {
  it('derives ws:// from an http page', () => {
    expect(websocketUrl('/api/v1/streams/a/ws')).toBe(
      `ws://${window.location.host}/api/v1/streams/a/ws`,
    )
  })
})

describe('MsePlayer', () => {
  it('reports an error instead of showing a black player when MSE is missing', () => {
    vi.stubGlobal('MediaSource', undefined)
    const states: PlayerState[] = []
    const messages: (string | undefined)[] = []

    new MsePlayer(fakeVideo(), 'ws://x/ws', {
      onStateChange: (state, detail) => {
        states.push(state)
        messages.push(detail)
      },
    }).start()

    expect(isMseSupported()).toBe(false)
    expect(states).toEqual(['error'])
    expect(messages[0]).toMatch(/cannot play/i)
  })

  it('offers only the codecs the browser actually supports', () => {
    vi.stubGlobal('MediaSource', {
      isTypeSupported: (type: string) => type.includes('avc1.640029'),
    })

    new MsePlayer(fakeVideo(), 'ws://x/ws').start()
    const socket = FakeWebSocket.instances[0]
    socket.emit('open', {})

    expect(JSON.parse(socket.sent[0])).toEqual({ type: 'mse', value: 'avc1.640029' })
  })

  it('passes viewer authentication as a WebSocket subprotocol', () => {
    vi.stubGlobal('MediaSource', { isTypeSupported: () => true })

    new MsePlayer(fakeVideo(), 'ws://x/viewer-ws', {}, ['sentinel.viewer.encoded']).start()

    expect(FakeWebSocket.instances[0].protocols).toEqual(['sentinel.viewer.encoded'])
    expect(FakeWebSocket.instances[0].url).not.toContain('encoded')
  })

  it('errors out when the browser supports none of the offered codecs', () => {
    vi.stubGlobal('MediaSource', { isTypeSupported: () => false })
    const states: PlayerState[] = []

    new MsePlayer(fakeVideo(), 'ws://x/ws', {
      onStateChange: (state) => states.push(state),
    }).start()

    expect(states).toEqual(['error'])
    expect(FakeWebSocket.instances).toHaveLength(0)
  })

  it('surfaces a server-reported stream error', () => {
    vi.stubGlobal('MediaSource', { isTypeSupported: () => true })
    let detail: string | undefined

    new MsePlayer(fakeVideo(), 'ws://x/ws', {
      onStateChange: (_state, message) => {
        detail = message ?? detail
      },
    }).start()

    FakeWebSocket.instances[0].emit('message', {
      data: JSON.stringify({ type: 'error', value: 'stream not found' }),
    })
    expect(detail).toBe('stream not found')
  })

  it('explains an unauthorised close rather than showing a bare failure', () => {
    vi.stubGlobal('MediaSource', { isTypeSupported: () => true })
    let detail: string | undefined

    new MsePlayer(fakeVideo(), 'ws://x/ws', {
      onStateChange: (_state, message) => {
        detail = message ?? detail
      },
    }).start()

    FakeWebSocket.instances[0].emit('close', { code: 1008 })
    expect(detail).toMatch(/not authorised/i)
  })

  it('ignores malformed control frames', () => {
    vi.stubGlobal('MediaSource', { isTypeSupported: () => true })
    const states: PlayerState[] = []

    new MsePlayer(fakeVideo(), 'ws://x/ws', {
      onStateChange: (state) => states.push(state),
    }).start()

    expect(() =>
      FakeWebSocket.instances[0].emit('message', { data: 'not json at all' }),
    ).not.toThrow()
    expect(states).toEqual(['connecting'])
  })

  it('closes the socket and clears the element on stop', () => {
    vi.stubGlobal('MediaSource', { isTypeSupported: () => true })
    const video = fakeVideo()
    const player = new MsePlayer(video, 'ws://x/ws')
    player.start()

    player.stop()

    expect(FakeWebSocket.instances[0].closed).toBe(true)
    expect(video.getAttribute('src')).toBeNull()
  })

  it('does not report a close that stop() caused', () => {
    vi.stubGlobal('MediaSource', { isTypeSupported: () => true })
    const states: PlayerState[] = []
    const player = new MsePlayer(fakeVideo(), 'ws://x/ws', {
      onStateChange: (state) => states.push(state),
    })
    player.start()
    player.stop()

    FakeWebSocket.instances[0].emit('close', { code: 1000 })
    expect(states).toEqual(['connecting'])
  })

  it('trims an old live buffer after append completion', () => {
    vi.stubGlobal('MediaSource', FakeMediaSource)
    vi.stubGlobal('URL', {
      createObjectURL: vi.fn(() => 'blob:sentinel-live'),
      revokeObjectURL: vi.fn(),
    })
    const player = new MsePlayer(fakeVideo(), 'ws://x/ws')
    player.start()

    const socket = FakeWebSocket.instances[0]
    socket.emit('message', {
      data: JSON.stringify({ type: 'mse', value: 'video/mp4; codecs="avc1.640029"' }),
    })
    const mediaSource = FakeMediaSource.instances[0]
    mediaSource.dispatchEvent(new Event('sourceopen'))
    socket.emit('message', { data: new ArrayBuffer(16) })

    expect(mediaSource.buffer.appended).toHaveLength(1)
    expect(mediaSource.buffer.removed).toHaveLength(0)

    mediaSource.buffer.finishUpdate()

    expect(mediaSource.buffer.removed).toEqual([[0, 28]])
    player.stop()
  })
})
