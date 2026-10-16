import { beforeEach, describe, expect, it, vi } from 'vitest'

describe('viewer key handling', () => {
  beforeEach(() => {
    vi.resetModules()
    window.sessionStorage.clear()
    window.history.replaceState(null, '', '/')
  })

  it('moves a fragment key to session storage and scrubs the address', async () => {
    window.history.replaceState(null, '', '/#viewer_key=read-only-secret')
    const { getViewerKey } = await import('@/features/viewer/access')

    expect(getViewerKey()).toBe('read-only-secret')
    expect(window.location.hash).toBe('')
    expect(window.sessionStorage.getItem('sentinel.viewer-key')).toBe('read-only-secret')
  })

  it('uses the key for session creation but keeps it out of the WebSocket URL', async () => {
    window.sessionStorage.setItem('sentinel.viewer-key', 'embedded-secret')
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)
    const { createViewerSession, viewerApi, viewerWebsocketPath, viewerWebsocketProtocols } =
      await import('@/features/viewer/access')

    await createViewerSession()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/viewer/session',
      expect.objectContaining({
        method: 'POST',
        body: 'embedded-secret',
        headers: expect.objectContaining({ 'Content-Type': 'text/plain;charset=UTF-8' }),
      }),
    )
    expect(viewerWebsocketPath('camera-1', true)).toBe(
      '/api/v1/streams/camera-1/viewer-ws?sub=true',
    )
    const protocols = viewerWebsocketProtocols()
    expect(protocols).toHaveLength(1)
    expect(protocols?.[0]).toMatch(/^sentinel\.viewer\./)
    expect(protocols?.[0]).not.toContain('embedded-secret')

    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    await viewerApi.get('/viewer/cameras')
    expect(fetchMock).toHaveBeenLastCalledWith(
      '/api/v1/viewer/cameras',
      expect.objectContaining({
        method: 'GET',
        headers: expect.objectContaining({}),
      }),
    )
    const request = fetchMock.mock.calls.at(-1)?.[1] as RequestInit
    expect(new Headers(request.headers).get('X-Sentinel-Viewer-Key')).toBe('embedded-secret')
  })
})
