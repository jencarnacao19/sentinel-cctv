import { ApiError, NetworkError } from '@/api/client'

/**
 * What went wrong, and what the operator can do about it.
 *
 * These states were previously collapsed into one red box saying that something
 * failed. They are not the same thing and they do not have the same answer: a
 * camera that refused a password needs a new password, a camera that did not
 * answer needs to be power-checked, a rate limit needs nothing but a pause, and
 * a tunnel failure is not about the camera at all. Showing one wording for all
 * of them sent operators to reboot cameras that were working.
 *
 * The `subject` is what the failed request was about, so the same classifier can
 * say "the camera did not answer" on a camera action and "Sentinel did not
 * answer" on a page load.
 */
export type FailureTone = 'warning' | 'danger'

export interface FailureDescription {
  title: string
  /** One or two plain sentences: what happened, then what to do. */
  detail: string
  tone: FailureTone
  /** True when this is about the path to Sentinel rather than about a camera. */
  transport: boolean
}

export interface FailureOptions {
  /** Named in the wording, e.g. "Front door". Falls back to "the camera". */
  subject?: string
  /** What the reader was doing, e.g. "loading the camera list". */
  action?: string
}

export function describeFailure(
  error: unknown,
  { subject, action }: FailureOptions = {},
): FailureDescription | null {
  if (!error) return null
  const target = subject ?? 'the camera'
  const doing = action ? ` while ${action}` : ''

  if (error instanceof NetworkError) {
    return {
      title: 'Sentinel is not reachable',
      detail:
        `The browser could not reach the Sentinel server${doing}. Recording and motion detection ` +
        'continue on the server itself. Check the network, then reload.',
      tone: 'danger',
      transport: true,
    }
  }

  if (!(error instanceof ApiError)) {
    return {
      title: 'Something went wrong',
      detail: `The action did not complete${doing}. Try it again.`,
      tone: 'danger',
      transport: false,
    }
  }

  // A reverse proxy answered instead of Sentinel. Never show its own page: it
  // describes the proxy's opinion of the origin and reads as though the camera
  // product is broken.
  if (error.fromProxy) {
    return {
      title: 'The remote connection failed before Sentinel answered',
      detail:
        'This came from the tunnel in front of Sentinel, not from a camera, so nothing here ' +
        'says a camera is faulty. Try again, or reach Sentinel on the local network.',
      tone: 'warning',
      transport: true,
    }
  }

  if (error.isRateLimited) {
    return {
      title: 'Too many camera operations in a short time',
      detail:
        'Sentinel is deliberately limiting how often cameras are contacted, so a burst of ' +
        'requests cannot knock one offline. Wait a few seconds and try once more.',
      tone: 'warning',
      transport: false,
    }
  }

  if (error.isLocked) {
    return {
      title: 'Cameras are paused by privacy mode',
      detail:
        'Live view, snapshots and camera controls are unavailable until an administrator ' +
        'switches privacy mode off. Recordings already on disk stay available.',
      tone: 'warning',
      transport: false,
    }
  }

  if (error.isUnauthorized) {
    return {
      title: 'Your session has expired',
      detail: 'Sign in again to continue. Nothing was changed.',
      tone: 'warning',
      transport: false,
    }
  }

  if (error.isForbidden) {
    return {
      title: 'This action needs administrator access',
      detail: 'A viewer session can watch and export, but not change configuration.',
      tone: 'warning',
      transport: false,
    }
  }

  // 422: the camera answered and refused the credentials Sentinel holds.
  if (error.status === 422) {
    return {
      title: `${capitalise(target)} refused the stored credentials`,
      detail:
        error.message ||
        'The camera answered but rejected the password Sentinel has for it. Re-enter it in ' +
          'Configure; on a Tapo device, enable Third-Party Compatibility first.',
      tone: 'danger',
      transport: false,
    }
  }

  // 409: the camera is busy, or the firmware does not support the request.
  if (error.status === 409) {
    return {
      title: `${capitalise(target)} could not do that now`,
      detail:
        error.message ||
        'The camera already has a session open, or its firmware does not support this ' +
          'operation. Close any other live view and try again.',
      tone: 'warning',
      transport: false,
    }
  }

  // 424: Sentinel reached out and the camera never answered.
  if (error.status === 424) {
    return {
      title: `${capitalise(target)} did not answer`,
      detail:
        error.message ||
        'Sentinel reached the network but the camera did not reply. A battery camera is asleep ' +
          'most of the time, so this is normal for one; a mains camera should be checked for ' +
          'power and network.',
      tone: 'warning',
      transport: false,
    }
  }

  if (error.status >= 500) {
    return {
      title: 'The Sentinel server reported an error',
      detail: `${error.message} Recording continues; the diagnostics download has the details.`,
      tone: 'danger',
      transport: true,
    }
  }

  return {
    title: 'That action did not complete',
    detail: error.message,
    tone: 'danger',
    transport: false,
  }
}

function capitalise(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1)
}
