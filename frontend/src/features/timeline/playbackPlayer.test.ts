import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/features/timeline/api', () => ({
  playbackApi: { seek: vi.fn(), manifest: vi.fn() },
}))
vi.mock('@/features/live/mse', () => ({
  isMseSupported: vi.fn(() => true),
}))

import { isMseSupported } from '@/features/live/mse'
import { playbackApi } from '@/features/timeline/api'
import { PlaybackPlayer, type PlaybackState } from '@/features/timeline/playbackPlayer'
import type { ManifestItem, PlaybackManifest, PlaybackTarget } from '@/features/timeline/types'

const mockedSeek = vi.mocked(playbackApi.seek)
const mockedManifest = vi.mocked(playbackApi.manifest)
const mockedIsMseSupported = vi.mocked(isMseSupported)

/** Minimal SourceBuffer stand-in that resolves appendBuffer asynchronously,
 * the same way a real one does, and records what it was asked to do. */
class FakeSourceBuffer extends EventTarget {
  mode: 'segments' | 'sequence' = 'segments'
  updating = false
  appendWindowStart = 0
  appendWindowEnd = Number.POSITIVE_INFINITY
  buffered = {
    length: 0,
    start: () => 0,
    end: () => 0,
  }
  appended: { bytes: ArrayBuffer; windowStart: number; windowEnd: number }[] = []

  appendBuffer(bytes: ArrayBuffer): void {
    if (rejectAppend) throw new DOMException('detached buffer', 'InvalidStateError')
    this.appended.push({ bytes, windowStart: this.appendWindowStart, windowEnd: this.appendWindowEnd })
    this.updating = true
    queueMicrotask(() => {
      this.updating = false
      this.dispatchEvent(new Event('updateend'))
    })
  }

  remove(): void {
    /* not exercised here */
  }
}

/** Every SourceBuffer any FakeMediaSource has ever created, so a test can
 * inspect what the player actually appended without threading a reference
 * through the player's own (private) construction of its MediaSource. */
let createdBuffers: FakeSourceBuffer[] = []
let rejectAppend = false

class FakeMediaSource extends EventTarget {
  static isTypeSupported(): boolean {
    return true
  }

  readyState: 'closed' | 'open' | 'ended' = 'closed'
  sourceBuffers: FakeSourceBuffer[] = []

  addSourceBuffer(): FakeSourceBuffer {
    const buffer = new FakeSourceBuffer()
    this.sourceBuffers.push(buffer)
    createdBuffers.push(buffer)
    return buffer
  }

  removeSourceBuffer(): void {}
  endOfStream(): void {}
}

function fakeVideo(): HTMLVideoElement {
  const element = document.createElement('video')
  Object.defineProperty(element, 'play', { value: vi.fn(() => Promise.resolve()), writable: true })
  Object.defineProperty(element, 'load', { value: vi.fn(), writable: true })
  return element
}

function manifestItem(overrides: Partial<ManifestItem> = {}): ManifestItem {
  return {
    segment_id: 'seg-1',
    started_at: '2026-08-13T10:00:00Z',
    ended_at: '2026-08-13T10:01:00Z',
    offset_seconds: 5,
    duration_seconds: 55,
    url: '/api/v1/recordings/seg-1/download',
    ...overrides,
  }
}

function foundTarget(overrides: Partial<PlaybackTarget> = {}): PlaybackTarget {
  return {
    camera_id: 'cam-1',
    at: '2026-08-13T10:00:05Z',
    found: true,
    segment_id: 'seg-1',
    url: '/api/v1/recordings/seg-1/download',
    started_at: '2026-08-13T10:00:00Z',
    ended_at: '2026-08-13T10:01:00Z',
    offset_seconds: 5,
    next_segment_id: null,
    next_url: null,
    next_started_at: null,
    continuous: false,
    gap_seconds: null,
    ...overrides,
  }
}

function manifestWith(items: ManifestItem[], overrides: Partial<PlaybackManifest> = {}): PlaybackManifest {
  return {
    camera_id: 'cam-1',
    start: '2026-08-13T10:00:00Z',
    end: '2026-08-13T10:30:00Z',
    items,
    gaps: [],
    total_seconds: items.reduce((sum, item) => sum + item.duration_seconds, 0),
    truncated: false,
    ...overrides,
  }
}

async function flush(): Promise<void> {
  for (let i = 0; i < 10; i += 1) {
    await Promise.resolve()
  }
}

beforeEach(() => {
  createdBuffers = []
  rejectAppend = false
  mockedIsMseSupported.mockReturnValue(true)
  vi.stubGlobal('MediaSource', FakeMediaSource)
  vi.stubGlobal('URL', {
    createObjectURL: (source: FakeMediaSource | Blob) => {
      if (source instanceof FakeMediaSource) {
        queueMicrotask(() => {
          source.readyState = 'open'
          source.dispatchEvent(new Event('sourceopen'))
        })
        return 'blob:media-source'
      }
      return 'blob:native-fallback'
    },
    revokeObjectURL: vi.fn(),
  })
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)),
      }),
    ),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('PlaybackPlayer.seek', () => {
  it('reports an error rather than a black frame when MSE is unsupported', async () => {
    mockedIsMseSupported.mockReturnValue(false)
    mockedSeek.mockResolvedValue(foundTarget())

    const states: PlaybackState[] = []
    const player = new PlaybackPlayer(fakeVideo(), 'cam-1', {
      onStateChange: (state) => states.push(state),
    })
    await player.seek(new Date('2026-08-13T10:00:05Z'))

    expect(states).toContain('error')
  })

  it('hops to the next available footage when the instant itself has none', async () => {
    mockedSeek
      .mockResolvedValueOnce(
        foundTarget({
          found: false,
          segment_id: null,
          started_at: null,
          ended_at: null,
          next_started_at: '2026-08-13T11:00:00Z',
          gap_seconds: 3600,
        }),
      )
      .mockResolvedValueOnce(foundTarget({ started_at: '2026-08-13T11:00:00Z' }))
    mockedManifest.mockResolvedValue(manifestWith([manifestItem()]))

    const player = new PlaybackPlayer(fakeVideo(), 'cam-1')
    await player.seek(new Date('2026-08-13T10:00:00Z'))
    await flush()

    expect(mockedSeek).toHaveBeenCalledTimes(2)
    expect(mockedSeek.mock.calls[1][1]).toEqual(new Date('2026-08-13T11:00:00Z'))
  })

  it('reports "ended" rather than hopping forever when there is truly nothing recorded', async () => {
    mockedSeek.mockResolvedValue(
      foundTarget({ found: false, segment_id: null, started_at: null, next_started_at: null }),
    )

    const states: PlaybackState[] = []
    const player = new PlaybackPlayer(fakeVideo(), 'cam-1', {
      onStateChange: (state) => states.push(state),
    })
    await player.seek(new Date('2026-08-13T10:00:00Z'))

    expect(states).toEqual(['loading', 'ended'])
  })

  it('opens a sequence-mode SourceBuffer and starts playback at the resolved offset', async () => {
    mockedSeek.mockResolvedValue(foundTarget({ offset_seconds: 12 }))
    mockedManifest.mockResolvedValue(manifestWith([manifestItem({ offset_seconds: 12, duration_seconds: 48 })]))

    const video = fakeVideo()
    const states: PlaybackState[] = []
    const player = new PlaybackPlayer(video, 'cam-1', {
      onStateChange: (state) => states.push(state),
    })
    await player.seek(new Date('2026-08-13T10:00:12Z'))
    await flush()

    expect(states).toContain('playing')
    expect(video.currentTime).toBe(12)
  })

  it('clips each append to the slice the manifest actually asked for', async () => {
    mockedSeek.mockResolvedValue(foundTarget())
    const item = manifestItem({ offset_seconds: 10, duration_seconds: 40 })
    mockedManifest
      .mockResolvedValueOnce(manifestWith([item]))
      // Nothing further once this is exhausted -- without this the fake
      // manifest endpoint would keep answering with the same item forever,
      // and the player's own lookahead would dutifully keep fetching it.
      .mockResolvedValue(manifestWith([]))

    const player = new PlaybackPlayer(fakeVideo(), 'cam-1')
    await player.seek(new Date('2026-08-13T10:00:10Z'))
    await flush()

    expect(createdBuffers).toHaveLength(1)
    expect(createdBuffers[0].mode).toBe('sequence')
    expect(createdBuffers[0].appended).toHaveLength(1)
    expect(createdBuffers[0].appended[0].windowStart).toBe(10)
    expect(createdBuffers[0].appended[0].windowEnd).toBe(50)
  })

  it('authenticates viewer segment downloads without relying on cookies', async () => {
    window.sessionStorage.setItem('sentinel.viewer-key', 'playback-viewer-secret')
    mockedSeek.mockResolvedValue(foundTarget())
    mockedManifest
      .mockResolvedValueOnce(manifestWith([manifestItem()]))
      .mockResolvedValue(manifestWith([]))

    const player = new PlaybackPlayer(fakeVideo(), 'cam-1', {}, true)
    await player.seek(new Date('2026-08-13T10:00:05Z'))
    await flush()

    const request = vi.mocked(fetch).mock.calls[0]?.[1]
    expect(new Headers(request?.headers).get('X-Sentinel-Viewer-Key')).toBe(
      'playback-viewer-secret',
    )
  })

  it('falls back to native MP4 playback when the browser detaches its SourceBuffer', async () => {
    rejectAppend = true
    mockedSeek.mockResolvedValue(foundTarget({ offset_seconds: 7 }))
    mockedManifest
      .mockResolvedValueOnce(manifestWith([manifestItem({ offset_seconds: 0 })]))
      .mockResolvedValue(manifestWith([]))

    const video = fakeVideo()
    const states: PlaybackState[] = []
    const player = new PlaybackPlayer(video, 'cam-1', {
      onStateChange: (state) => states.push(state),
    })
    await player.seek(new Date('2026-08-13T10:00:07Z'))
    await flush()
    video.dispatchEvent(new Event('loadedmetadata'))
    await flush()

    expect(video.src).toBe('blob:native-fallback')
    expect(video.currentTime).toBe(7)
    expect(states).toContain('playing')
    expect(states).not.toContain('error')
  })

  it('appends the segment after a gap right away -- a gap is a cut, not a stall', async () => {
    mockedSeek.mockResolvedValue(foundTarget())
    const before = manifestItem({
      segment_id: 'before',
      started_at: '2026-08-13T10:00:00Z',
      ended_at: '2026-08-13T10:01:00Z',
      offset_seconds: 5,
      duration_seconds: 55,
    })
    const afterGap = manifestItem({
      segment_id: 'after',
      started_at: '2026-08-13T11:00:00Z',
      ended_at: '2026-08-13T11:01:00Z',
      offset_seconds: 0,
      duration_seconds: 60,
    })
    mockedManifest
      .mockResolvedValueOnce(
        manifestWith([before, afterGap], {
          gaps: [{ start: '2026-08-13T10:01:00Z', end: '2026-08-13T11:00:00Z', duration_seconds: 3540 }],
        }),
      )
      .mockResolvedValue(manifestWith([]))

    const player = new PlaybackPlayer(fakeVideo(), 'cam-1')
    await player.seek(new Date('2026-08-13T10:00:05Z'))
    await flush()

    expect(createdBuffers).toHaveLength(1)
    expect(createdBuffers[0].appended.map((a) => a.windowEnd - a.windowStart)).toEqual([55, 60])
  })
})

describe('PlaybackPlayer.stop', () => {
  it('clears the video element and stops reacting to further events', async () => {
    mockedSeek.mockResolvedValue(foundTarget())
    mockedManifest.mockResolvedValue(manifestWith([manifestItem()]))

    const video = fakeVideo()
    const player = new PlaybackPlayer(video, 'cam-1')
    await player.seek(new Date('2026-08-13T10:00:00Z'))
    await flush()

    player.stop()

    expect(video.getAttribute('src')).toBeNull()
  })

  it('ignores a seek result that arrives after stop() was already called', async () => {
    let resolveSeek: (value: PlaybackTarget) => void = () => undefined
    mockedSeek.mockReturnValue(
      new Promise((resolve) => {
        resolveSeek = resolve
      }),
    )

    const states: PlaybackState[] = []
    const player = new PlaybackPlayer(fakeVideo(), 'cam-1', {
      onStateChange: (state) => states.push(state),
    })
    const seekPromise = player.seek(new Date('2026-08-13T10:00:00Z'))
    player.stop()
    resolveSeek(foundTarget())
    await seekPromise

    // "loading" is reported before stop() cuts in; nothing past that should be.
    expect(states).toEqual(['loading'])
  })
})
