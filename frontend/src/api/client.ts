/**
 * Typed fetch wrapper for the Sentinel API.
 *
 * Auth rides on the HttpOnly session cookie the login endpoint sets, so no token
 * is ever kept in localStorage where a script could read it. A 401 is surfaced
 * as a distinct error type, which is what the router listens for to send the
 * user back to the sign-in screen.
 */

export const API_BASE = '/api/v1'

export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown
  /** True when a proxy answered instead of Sentinel. Nothing about a camera. */
  readonly fromProxy: boolean

  constructor(status: number, message: string, detail?: unknown, fromProxy = false) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.fromProxy = fromProxy
  }

  get isUnauthorized(): boolean {
    return this.status === 401
  }

  get isForbidden(): boolean {
    return this.status === 403
  }

  get isNotFound(): boolean {
    return this.status === 404
  }

  get isRateLimited(): boolean {
    return this.status === 429
  }

  /** Cameras are paused by privacy mode. */
  get isLocked(): boolean {
    return this.status === 423
  }

  /** The camera answered and refused, or could not be reached at all. */
  get isCameraFault(): boolean {
    return (
      !this.fromProxy &&
      (this.status === 422 ||
        this.status === 424 ||
        this.status === 502 ||
        this.status === 503)
    )
  }
}

/** Raised when the backend cannot be reached at all (server down, LAN drop). */
export class NetworkError extends Error {
  constructor(message = 'Could not reach the Sentinel server.') {
    super(message)
    this.name = 'NetworkError'
  }
}

interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown
  /** Send an already-encoded body without JSON conversion. */
  rawBody?: BodyInit
  /** Abort the request after this many milliseconds. */
  timeoutMs?: number
}

const DEFAULT_TIMEOUT_MS = 20_000

/**
 * Statuses a reverse proxy invents when it cannot get an answer from Sentinel.
 *
 * 520-527 are Cloudflare's own range and never come from an origin. 502/504
 * can come from either, so they are only treated as proxy failures when the
 * body is not Sentinel's JSON -- an origin 502 with a `detail` is a real,
 * deliberate Sentinel answer (a camera rejecting a credential, for one) and
 * must keep its own wording.
 */
const PROXY_STATUSES = new Set([520, 521, 522, 523, 524, 525, 526, 527, 530])

/**
 * A response that did not come from Sentinel.
 *
 * The test is structural, not textual: Sentinel answers every error with JSON,
 * so an HTML body on an error is by definition something in between. Matching
 * on the wording of a proxy's error page instead would only work until the
 * proxy reworded it.
 */
export function isProxyFailure(status: number, contentType: string, payload: unknown): boolean {
  if (PROXY_STATUSES.has(status)) return true
  // Any HTML response or string containing HTML markup on an error is from a proxy or gateway
  if (
    contentType.includes('text/html') ||
    (typeof payload === 'string' && /<html|<!doctype|<body|<head/i.test(payload))
  ) {
    return true
  }
  if (status !== 502 && status !== 503 && status !== 504) return false
  const isSentinelJson =
    contentType.includes('application/json') &&
    Boolean(payload) &&
    typeof payload === 'object' &&
    'detail' in (payload as object)
  return !isSentinelJson
}

/**
 * What to show when the answer came from a proxy rather than from Sentinel.
 *
 * Never the proxy's own page. "The origin web server returned an invalid or
 * incomplete response to Cloudflare" tells an operator looking at a camera
 * nothing they can act on, and reads as though the camera product is broken.
 */
const PROXY_MESSAGE =
  'The remote connection to Sentinel failed before it could answer, so this is not something a ' +
  'camera reported. Try again, or reach Sentinel on the local network.'

function extractMessage(status: number, contentType: string, payload: unknown): string {
  if (isProxyFailure(status, contentType, payload)) return PROXY_MESSAGE
  if (
    typeof payload === 'string' &&
    (/<html|<!doctype|<body|<head/i.test(payload) || payload.includes('Cloudflare'))
  ) {
    return PROXY_MESSAGE
  }
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail: unknown = payload.detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) =>
          item && typeof item === 'object' && 'msg' in item
            ? String((item as { msg: unknown }).msg)
            : typeof item === 'string'
              ? item
              : null,
        )
        .filter((value): value is string => Boolean(value))
      if (messages.length > 0) return messages.join(' ')
    }
  }
  if (status === 401) return 'Your session has expired. Sign in again.'
  if (status === 403) return 'This action needs administrator access.'
  if (status === 423) return 'Cameras are paused by privacy mode.'
  if (status === 429) return 'Too many requests in a short time. Try again shortly.'
  if (status >= 500) return 'The Sentinel server reported an internal error.'
  return `Request failed with status ${status}.`
}

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, rawBody, timeoutMs = DEFAULT_TIMEOUT_MS, headers, signal, ...rest } = options

  if (body !== undefined && rawBody !== undefined) {
    throw new TypeError('Only one request body format may be supplied.')
  }

  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), timeoutMs)
  // Honour a caller-supplied signal as well as our own timeout.
  signal?.addEventListener('abort', () => controller.abort(), { once: true })

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...rest,
      signal: controller.signal,
      credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...headers,
      },
      ...(rawBody !== undefined
        ? { body: rawBody }
        : body !== undefined
          ? { body: JSON.stringify(body) }
          : {}),
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new NetworkError('The request to Sentinel timed out.')
    }
    throw new NetworkError()
  } finally {
    clearTimeout(timeout)
  }

  if (response.status === 204) return undefined as T

  const contentType = response.headers.get('content-type') ?? ''
  const payload: unknown = contentType.includes('application/json')
    ? await (response.json() as Promise<unknown>).catch(() => null)
    : await response.text().catch(() => null)

  if (!response.ok) {
    // The raw body is deliberately not carried on a proxy failure: it is a
    // third party's HTML error page, and anything that renders `detail` would
    // otherwise put it in front of the operator as though it were a camera
    // fault.
    const proxied = isProxyFailure(response.status, contentType, payload)
    throw new ApiError(
      response.status,
      extractMessage(response.status, contentType, payload),
      proxied ? null : payload,
      proxied,
    )
  }
  return payload as T
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) =>
    apiFetch<T>(path, { ...options, method: 'GET' }),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    apiFetch<T>(path, { ...options, method: 'POST', body }),
  put: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    apiFetch<T>(path, { ...options, method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    apiFetch<T>(path, { ...options, method: 'PATCH', body }),
  delete: <T>(path: string, options?: RequestOptions) =>
    apiFetch<T>(path, { ...options, method: 'DELETE' }),
}
