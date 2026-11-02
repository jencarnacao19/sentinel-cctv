import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/features/timeline/api', () => ({
  playbackApi: { session: vi.fn() },
}))
vi.mock('@/features/live/mse', () => ({
  isMseSupported: vi.fn(() => false),
}))
vi.mock('@/features/live/hls', () => ({
  isHlsSupported: vi.fn(() => true),
  prefersNativeHls: vi.fn(() => true),
}))

import { isHlsSupported } from '@/features/live/hls'
import { playbackApi } from '@/features/timeline/api'
import { PlaybackPlayer, wallClockAt, type PlaybackState } from '@/features/timeline/playbackPlayer'
import type { PlaybackSession } from '@/features/timeline/types'

const mockedSession = vi.mocked(playbackApi.session)

const START = '2026-11-01T10:00:00.000Z'

function session(overrides: Partial<PlaybackSession> = {}): PlaybackSession {
  return {
    url: '/api/v1/streams/cam/recordings/token/index.m3u8',
    start_position: 2.5,
    items: [
      { started_at: START, position: 0, duration: 60 },
      { started_at: '2026-11-01T10:05:00.000Z', position: 60, duration: 60 },
    ],
    end: '2026-11-01T10:06:00.000Z',
    next_at: null,
    ...overrides,
  }
}

const play = vi.fn(() => Promise.resolve())

function fakeVideo(): HTMLVideoElement {
  const element = document.createElement('video')
  Object.defineProperty(element, 'play', { value: play, writable: true })
  Object.defineProperty(element, 'load', { value: vi.fn(), writable: true })
  return element
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0))

describe('wallClockAt', () => {
  const items = session().items

  it('maps a playlist position to the wall clock of the file it falls in', () => {
    expect(wallClockAt(items, 30)?.toISOString()).toBe('2026-11-01T10:00:30.000Z')
    expect(wallClockAt(items, 61)?.toISOString()).toBe('2026-11-01T10:05:01.000Z')
  })

  it('jumps the gap between two files instead of counting through it', () => {
    expect(wallClockAt(items, 60)?.toISOString()).toBe('2026-11-01T10:05:00.000Z')
  })

  it('has nothing to say before the first item or without items', () => {
    expect(wallClockAt([], 3)).toBeNull()
    expect(wallClockAt([{ started_at: START, position: 5, duration: 1 }], 1)).toBeNull()
  })
})

describe('PlaybackPlayer', () => {
  let video: HTMLVideoElement
  let states: PlaybackState[]
  let player: PlaybackPlayer

  beforeEach(() => {
    mockedSession.mockReset()
    play.mockClear()
    video = fakeVideo()
    states = []
    player = new PlaybackPlayer(video, 'cam', { onStateChange: (state) => states.push(state) })
  })

  afterEach(() => player.stop())

  it('plays the playlist natively from the requested instant', async () => {
    mockedSession.mockResolvedValue(session())
    await player.seek(new Date(START))

    expect(mockedSession).toHaveBeenCalledWith('cam', new Date(START), false)
    expect(video.getAttribute('src')).toBe('/api/v1/streams/cam/recordings/token/index.m3u8')
    video.dispatchEvent(new Event('loadedmetadata'))
    await flush()
    expect(video.currentTime).toBe(2.5)
    expect(play).toHaveBeenCalled()
    expect(states).toContain('playing')
  })

  it('starts muted when the phone refuses to start it with sound', async () => {
    play
      .mockImplementationOnce(() => Promise.reject(new DOMException('no gesture', 'NotAllowedError')))
      .mockImplementationOnce(() => Promise.resolve())
    mockedSession.mockResolvedValue(session())
    video.muted = false
    await player.seek(new Date(START))
    await flush()
    expect(video.muted).toBe(true)
    expect(play).toHaveBeenCalledTimes(2)
    expect(states.at(-1)).toBe('playing')
  })

  it('says why the native player gave up', async () => {
    mockedSession.mockResolvedValue(session())
    await player.seek(new Date(START))
    Object.defineProperty(video, 'error', { value: { code: 4, message: 'unsupported' } })
    video.dispatchEvent(new Event('error'))
    expect(states.at(-1)).toBe('error')
  })

  it('skips an hour with no footage to the next recording', async () => {
    mockedSession
      .mockResolvedValueOnce(session({ url: null, items: [], next_at: '2026-11-01T14:00:00Z' }))
      .mockResolvedValueOnce(session())
    await player.seek(new Date(START))

    expect(mockedSession).toHaveBeenLastCalledWith('cam', new Date('2026-11-01T14:00:00Z'), false)
    expect(video.getAttribute('src')).toContain('index.m3u8')
  })

  it('says so when nothing was recorded after the instant', async () => {
    mockedSession.mockResolvedValue(session({ url: null, items: [], next_at: null }))
    await player.seek(new Date(START))
    expect(states.at(-1)).toBe('ended')
  })

  it('opens the next hour when this playlist ends', async () => {
    mockedSession.mockResolvedValue(session())
    await player.seek(new Date(START))
    video.dispatchEvent(new Event('ended'))
    await flush()
    expect(mockedSession).toHaveBeenLastCalledWith('cam', new Date('2026-11-01T10:06:00.000Z'), false)
  })

  it('reports the wall clock as the picture advances', async () => {
    const seen: Date[] = []
    player.stop()
    player = new PlaybackPlayer(video, 'cam', { onTimeUpdate: (at) => seen.push(at) })
    mockedSession.mockResolvedValue(session())
    await player.seek(new Date(START))
    video.currentTime = 70
    video.dispatchEvent(new Event('timeupdate'))
    expect(seen.at(-1)?.toISOString()).toBe('2026-11-01T10:05:10.000Z')
  })

  it('mutes fast playback and gives the listener their choice back', () => {
    video.muted = false
    player.setPlaybackRate(8)
    expect(video.playbackRate).toBe(8)
    expect(video.defaultPlaybackRate).toBe(8)
    expect(video.muted).toBe(true)
    player.setPlaybackRate(1)
    expect(video.muted).toBe(false)
  })

  it('refuses plainly when the browser can play neither HLS nor MSE', async () => {
    vi.mocked(isHlsSupported).mockReturnValueOnce(false)
    mockedSession.mockResolvedValue(session())
    await player.seek(new Date(START))
    expect(states.at(-1)).toBe('error')
  })

  it('reports an unreachable server', async () => {
    mockedSession.mockRejectedValue(new Error('offline'))
    await player.seek(new Date(START))
    expect(states.at(-1)).toBe('error')
  })
})
