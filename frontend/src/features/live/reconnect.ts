/**
 * Bounded exponential backoff shared by the two live players.
 *
 * The MSE, WebRTC and HTTPS HLS players have to survive the same
 * things — go2rtc restarting, a camera dropping off the PoE switch, a phone
 * moving between cells — and both have to do it without hammering the relay. A
 * wall of cameras that all lost the same server would otherwise retry in
 * lockstep and arrive as one thundering herd the moment it came back: the
 * ladder bounds how fast any one of them can try, and the jitter spreads the
 * attempts across the window rather than stacking them on one instant.
 *
 * Everything that would otherwise make this untestable is injected — the random
 * source and the timer — so a test drives the whole ladder with a counter and a
 * queue and never waits on a real clock.
 */

/**
 * Delay before each successive retry, in milliseconds. The last rung is the
 * cap: attempt six and everything after it waits that long.
 *
 * It ends where `features/realtime/client.ts` ends for the same reason — half a
 * minute is long enough to stop pressing a server that is still coming back up,
 * and short enough that an operator watching a wall sees it recover on its own
 * rather than reaching for the reload button.
 */
export const RECONNECT_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 15_000, 30_000]

/**
 * Fraction of a rung the jitter may subtract.
 *
 * Deliberately one-sided. Shortening a delay but never lengthening it keeps the
 * ladder value a true upper bound, which is what lets a test (and the players'
 * own tests) advance a fake clock by exactly the rung and know the retry has
 * fired, without knowing what the random source returned.
 */
export const RECONNECT_JITTER = 0.25

export type TimerHandle = ReturnType<typeof setTimeout>

/** The clock, injected so a test never needs a real one. */
export interface BackoffTimer {
  set: (run: () => void, delayMs: number) => TimerHandle
  clear: (handle: TimerHandle) => void
}

export interface BackoffOptions {
  /** Replaces `RECONNECT_DELAYS_MS`. An empty ladder is ignored. */
  ladder?: readonly number[]
  /** Returns a number in [0, 1), like `Math.random`, which is the default. */
  random?: () => number
  timer?: BackoffTimer
}

export interface ScheduledRetry {
  /** 1 for the first retry since the last `reset()` — the number the operator
   *  is shown, so it counts from one rather than from zero. */
  attempt: number
  /** The jittered delay actually being waited, for assertions and diagnostics. */
  delayMs: number
}

const wallClockTimer: BackoffTimer = {
  // Resolved at call time rather than captured at module load, so a test that
  // installs fake timers after this module is imported still intercepts them.
  set: (run, delayMs) => setTimeout(run, delayMs),
  clear: (handle) => clearTimeout(handle),
}

/**
 * One pending retry and the ladder position that produced it.
 *
 * Pure apart from the injected timer: no knowledge of sockets, peers or React,
 * because both players and their very different failure modes need exactly the
 * same counting.
 */
export class Backoff {
  private attempts = 0
  private handle: TimerHandle | null = null
  private readonly ladder: readonly number[]
  private readonly random: () => number
  private readonly timer: BackoffTimer

  constructor(options: BackoffOptions = {}) {
    this.ladder = options.ladder?.length ? options.ladder : RECONNECT_DELAYS_MS
    this.random = options.random ?? Math.random
    this.timer = options.timer ?? wallClockTimer
  }

  /** Retries scheduled since the last `reset()`. */
  get attempt(): number {
    return this.attempts
  }

  /** True while a retry is waiting to fire. */
  get pending(): boolean {
    return this.handle !== null
  }

  /**
   * Waits out the next rung of the ladder, then runs `retry`.
   *
   * At most one retry is ever outstanding: scheduling again replaces the
   * pending one rather than stacking a second connection attempt behind it,
   * which is what keeps a flapping camera from ending up with several sockets
   * racing to open at once.
   */
  schedule(retry: () => void): ScheduledRetry {
    this.cancel()
    this.attempts += 1
    const rung = this.ladder[Math.min(this.attempts - 1, this.ladder.length - 1)]
    const delayMs = Math.round(rung * (1 - RECONNECT_JITTER * this.random()))
    this.handle = this.timer.set(() => {
      this.handle = null
      retry()
    }, delayMs)
    return { attempt: this.attempts, delayMs }
  }

  /**
   * Drops a pending retry, leaving the ladder position alone.
   *
   * Used where the wait is superseded but the history is not: an operator
   * asking to retry now should not also be told the next failure is back at one
   * second.
   */
  cancel(): void {
    if (this.handle === null) return
    this.timer.clear(this.handle)
    this.handle = null
  }

  /** Back to square one — no pending retry, next delay is the first rung. Called
   *  once a connection is actually delivering media, because a stream that
   *  worked has earned a fast first retry when it later drops. */
  reset(): void {
    this.cancel()
    this.attempts = 0
  }
}
