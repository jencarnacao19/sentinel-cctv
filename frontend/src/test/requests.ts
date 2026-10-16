/**
 * Reading back what a component actually sent.
 *
 * `RequestInit['body']` is a `BodyInit`, which includes Blob, FormData and
 * streams -- so stringifying it directly is only correct because everything
 * Sentinel's API client sends is a JSON string. This narrows that assumption to
 * one place and fails loudly when it stops holding, instead of quietly
 * asserting against `[object Object]` in every test.
 */
export function sentJson(init?: RequestInit | null): unknown {
  const body = init?.body
  if (typeof body !== 'string') {
    throw new TypeError(`Expected a JSON request body, got ${typeof body}`)
  }
  return JSON.parse(body)
}

/** The URL a fetch mock was called with, whatever form it arrived in. */
export function requestedUrl(input: RequestInfo | URL): string {
  if (typeof input === 'string') return input
  if (input instanceof URL) return input.toString()
  return input.url
}
