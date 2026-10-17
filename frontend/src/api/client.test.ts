import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api, NetworkError } from '@/api/client'

function mockFetch(response: Response | Error) {
  // Typed with fetch's real signature so mock.calls keeps its argument types.
  const fn = vi.fn(
    (_input: RequestInfo | URL, _init?: RequestInit): Promise<Response> =>
      response instanceof Error ? Promise.reject(response) : Promise.resolve(response),
  )
  vi.stubGlobal('fetch', fn)
  return fn
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('api client', () => {
  it('returns the parsed body on success', async () => {
    mockFetch(jsonResponse({ initialised: true }))
    await expect(api.get('/auth/status')).resolves.toEqual({ initialised: true })
  })

  it('prefixes the versioned API base', async () => {
    const fetchMock = mockFetch(jsonResponse({}))
    await api.get('/system')
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/system')
  })

  it('sends JSON bodies with the right content type', async () => {
    const fetchMock = mockFetch(jsonResponse({}))
    await api.post('/auth/login', { username: 'a', password: 'b' })
    const init = fetchMock.mock.calls[0]?.[1]
    expect(init?.method).toBe('POST')
    expect(init?.body).toBe(JSON.stringify({ username: 'a', password: 'b' }))
    expect((init?.headers as Record<string, string>)['Content-Type']).toBe('application/json')
  })

  it('resolves 204 responses to undefined', async () => {
    mockFetch(new Response(null, { status: 204 }))
    await expect(api.delete('/auth/tokens/x')).resolves.toBeUndefined()
  })

  it('raises ApiError with the server detail', async () => {
    mockFetch(jsonResponse({ detail: 'Incorrect username or password.' }, 401))
    const error = await api.post('/auth/login', {}).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(401)
    expect((error as ApiError).isUnauthorized).toBe(true)
    expect((error as ApiError).message).toBe('Incorrect username or password.')
  })

  it('flattens FastAPI validation detail arrays', async () => {
    mockFetch(jsonResponse({ detail: [{ loc: ['body', 'password'], msg: 'too short' }] }, 422))
    const error = (await api.post('/auth/setup', {}).catch((e: unknown) => e)) as ApiError
    expect(error.message).toContain('too short')
  })

  it('hides internal server error bodies behind a generic message', async () => {
    mockFetch(jsonResponse({}, 500))
    const error = (await api.get('/system').catch((e: unknown) => e)) as ApiError
    expect(error.message).toBe('The Sentinel server reported an internal error.')
  })

  it('raises NetworkError when the server is unreachable', async () => {
    mockFetch(new TypeError('Failed to fetch'))
    await expect(api.get('/system')).rejects.toBeInstanceOf(NetworkError)
  })

  it('flags rate limiting distinctly', async () => {
    mockFetch(jsonResponse({ detail: 'Too many attempts.' }, 429))
    const error = (await api.post('/auth/login', {}).catch((e: unknown) => e)) as ApiError
    expect(error.isRateLimited).toBe(true)
  })
})

describe('answers that did not come from Sentinel', () => {
  const CLOUDFLARE_520 = new Response(
    '<!DOCTYPE html><html><head><title>cctv.example.net | 520: Web server is returning an ' +
      'unknown error</title></head><body><h1>Error 520</h1><h2>Web server is returning an ' +
      'unknown error</h2><p>The origin web server returned an invalid or incomplete response ' +
      'to Cloudflare.</p></body></html>',
    { status: 520, headers: { 'content-type': 'text/html; charset=UTF-8' } },
  )

  it('never surfaces a proxy error page as the message', async () => {
    mockFetch(CLOUDFLARE_520.clone())
    const error = (await api.get('/cameras').catch((e: unknown) => e)) as ApiError
    expect(error.message).not.toMatch(/cloudflare/i)
    expect(error.message).not.toMatch(/origin web server/i)
    expect(error.message).toMatch(/failed before it could answer/i)
  })

  it('does not keep the proxy body where a component could render it', async () => {
    mockFetch(CLOUDFLARE_520.clone())
    const error = (await api.get('/cameras').catch((e: unknown) => e)) as ApiError
    expect(error.detail).toBeNull()
    expect(error.fromProxy).toBe(true)
    expect(error.isCameraFault).toBe(false)
  })

  it.each([521, 522, 523, 524, 525, 526, 527, 530])(
    'treats status %i as a proxy failure',
    async (status) => {
      mockFetch(new Response('<html>gateway</html>', { status, headers: { 'content-type': 'text/html' } }))
      const error = (await api.get('/cameras').catch((e: unknown) => e)) as ApiError
      expect(error.fromProxy).toBe(true)
    },
  )

  it('treats an HTML 502 as a proxy failure', async () => {
    mockFetch(new Response('<html>bad gateway</html>', { status: 502, headers: { 'content-type': 'text/html' } }))
    const error = (await api.get('/cameras').catch((e: unknown) => e)) as ApiError
    expect(error.fromProxy).toBe(true)
  })

  it("keeps Sentinel's own 502 wording, because a camera really did refuse", async () => {
    // The distinction that matters: an origin 502 with a detail is a deliberate
    // Sentinel answer about a camera, not a proxy giving up.
    mockFetch(jsonResponse({ detail: 'The camera rejected the stored Tapo credentials.' }, 502))
    const error = (await api.get('/cameras/1/tapo/state').catch((e: unknown) => e)) as ApiError
    expect(error.message).toBe('The camera rejected the stored Tapo credentials.')
    expect(error.fromProxy).toBe(false)
    expect(error.isCameraFault).toBe(true)
  })

  it("keeps Sentinel's own 503 wording", async () => {
    mockFetch(jsonResponse({ detail: 'The camera did not answer its local control service.' }, 503))
    const error = (await api.get('/cameras/1/tapo/state').catch((e: unknown) => e)) as ApiError
    expect(error.message).toBe('The camera did not answer its local control service.')
    expect(error.isCameraFault).toBe(true)
  })

  it("treats Sentinel's 422 camera auth rejection as a camera fault with Sentinel's message", async () => {
    mockFetch(
      jsonResponse(
        {
          detail:
            'The camera rejected the username or password. Use the account created on the camera itself, not a cloud account.',
        },
        422,
      ),
    )
    const error = (await api.post('/cameras/1/test', {}).catch((e: unknown) => e)) as ApiError
    expect(error.message).toContain('rejected the username or password')
    expect(error.message).toContain('not a cloud account')
    expect(error.fromProxy).toBe(false)
    expect(error.isCameraFault).toBe(true)
  })

  it("treats Sentinel's 424 unreachable error as a camera fault with Sentinel's message", async () => {
    mockFetch(
      jsonResponse(
        { detail: 'The camera did not respond.' },
        424,
      ),
    )
    const error = (await api.post('/cameras/1/test', {}).catch((e: unknown) => e)) as ApiError
    expect(error.message).toBe('The camera did not respond.')
    expect(error.fromProxy).toBe(false)
    expect(error.isCameraFault).toBe(true)
  })

  it('normalizes any HTML error response to the friendly proxy message', async () => {
    mockFetch(
      new Response(
        '<!DOCTYPE html><html><body>Error from gateway</body></html>',
        { status: 502, headers: { 'content-type': 'text/html' } },
      ),
    )
    const error = (await api.get('/cameras').catch((e: unknown) => e)) as ApiError
    expect(error.fromProxy).toBe(true)
    expect(error.message).not.toContain('<')
    expect(error.message).toMatch(/remote connection to Sentinel failed/i)
  })
})

describe('messages an operator can act on', () => {
  it.each([
    [401, 'Your session has expired. Sign in again.'],
    [403, 'This action needs administrator access.'],
    [423, 'Cameras are paused by privacy mode.'],
  ])('replaces a bare %i with a next step', async (status, expected) => {
    mockFetch(new Response('', { status }))
    const error = (await api.get('/cameras').catch((e: unknown) => e)) as ApiError
    expect(error.message).toBe(expected)
  })

  it('flags privacy mode distinctly', async () => {
    mockFetch(jsonResponse({ detail: 'Cameras are paused for privacy: Guests are present.' }, 423))
    const error = (await api.get('/cameras').catch((e: unknown) => e)) as ApiError
    expect(error.isLocked).toBe(true)
    expect(error.message).toContain('Guests are present')
  })
})
