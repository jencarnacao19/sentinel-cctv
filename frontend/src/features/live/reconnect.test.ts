import { describe, expect, it, vi } from 'vitest'

import {
  Backoff,
  RECONNECT_DELAYS_MS,
  RECONNECT_JITTER,
  type BackoffTimer,
  type TimerHandle,
} from '@/features/live/reconnect'

/**
 * The ladder is driven with an injected clock and an injected random source, so
 * every assertion is exact and nothing here waits on a real timer.
 */
function fakeTimer() {
  const queued = new Map<number, () => void>()
  let next = 1
  const timer: BackoffTimer = {
    set(callback, _delayMs) {
      const handle = next++
      queued.set(handle, callback)
      return handle as unknown as TimerHandle
    },
    clear(handle) {
      queued.delete(handle as unknown as number)
    },
  }
  return {
    timer,
    get pending() {
      return queued.size
    },
    fireAll() {
      const callbacks = [...queued.values()]
      queued.clear()
      for (const callback of callbacks) callback()
    },
  }
}

describe('Backoff', () => {
  it('walks the ladder and then stays at its last rung for ever', () => {
    const clock = fakeTimer()
    // random() === 0 means no jitter is subtracted, so the delay is the rung.
    const backoff = new Backoff({ timer: clock.timer, random: () => 0 })

    const delays: number[] = []
    for (let index = 0; index < RECONNECT_DELAYS_MS.length + 3; index += 1) {
      delays.push(backoff.schedule(() => undefined).delayMs)
    }

    expect(delays.slice(0, RECONNECT_DELAYS_MS.length)).toEqual([...RECONNECT_DELAYS_MS])
    const cap = RECONNECT_DELAYS_MS[RECONNECT_DELAYS_MS.length - 1]
    // A camera unplugged for a week must keep trying, but never faster.
    expect(delays.slice(RECONNECT_DELAYS_MS.length)).toEqual([cap, cap, cap])
  })

  it('only ever shortens a delay, so the rung stays a true upper bound', () => {
    const clock = fakeTimer()
    const backoff = new Backoff({ timer: clock.timer, random: () => 1 })
    const { delayMs } = backoff.schedule(() => undefined)

    const rung = RECONNECT_DELAYS_MS[0]
    expect(delayMs).toBeLessThanOrEqual(rung)
    expect(delayMs).toBeGreaterThanOrEqual(Math.round(rung * (1 - RECONNECT_JITTER)))
  })

  it('spreads a wall of cameras rather than letting them retry in lockstep', () => {
    const clock = fakeTimer()
    const values = [0, 0.2, 0.4, 0.6, 0.8]
    let index = 0
    const delays = values.map(() => {
      const backoff = new Backoff({ timer: clock.timer, random: () => values[index++] })
      return backoff.schedule(() => undefined).delayMs
    })
    expect(new Set(delays).size).toBeGreaterThan(1)
  })

  it('never leaves two retries outstanding at once', () => {
    const clock = fakeTimer()
    const backoff = new Backoff({ timer: clock.timer, random: () => 0 })

    backoff.schedule(() => undefined)
    backoff.schedule(() => undefined)
    backoff.schedule(() => undefined)

    // Scheduling again replaces the pending retry instead of stacking a second
    // connection attempt behind it.
    expect(clock.pending).toBe(1)
    expect(backoff.attempt).toBe(3)
  })

  it('cancel drops the pending retry but keeps the ladder position', () => {
    const clock = fakeTimer()
    const retry = vi.fn()
    const backoff = new Backoff({ timer: clock.timer, random: () => 0 })

    backoff.schedule(retry)
    backoff.schedule(retry)
    backoff.cancel()
    clock.fireAll()

    expect(retry).not.toHaveBeenCalled()
    expect(backoff.pending).toBe(false)
    // An operator retrying now should not also reset the next failure to one
    // second.
    expect(backoff.schedule(retry).delayMs).toBe(RECONNECT_DELAYS_MS[2])
  })

  it('reset returns a stream that worked to a fast first retry', () => {
    const clock = fakeTimer()
    const backoff = new Backoff({ timer: clock.timer, random: () => 0 })

    backoff.schedule(() => undefined)
    backoff.schedule(() => undefined)
    backoff.reset()

    expect(backoff.attempt).toBe(0)
    expect(backoff.pending).toBe(false)
    expect(backoff.schedule(() => undefined).delayMs).toBe(RECONNECT_DELAYS_MS[0])
  })

  it('runs the retry only when the timer fires', () => {
    const clock = fakeTimer()
    const retry = vi.fn()
    const backoff = new Backoff({ timer: clock.timer, random: () => 0 })

    backoff.schedule(retry)
    expect(retry).not.toHaveBeenCalled()

    clock.fireAll()
    expect(retry).toHaveBeenCalledTimes(1)
    // The handle is released once it has fired, so nothing thinks a retry is
    // still outstanding.
    expect(backoff.pending).toBe(false)
  })
})
