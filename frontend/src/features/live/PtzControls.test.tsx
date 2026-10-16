import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { vi } from 'vitest'

import { PtzControls } from '@/features/live/PtzControls'

describe('PtzControls', () => {
  it('starts immediately on press and stops on release', async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(new Response(null, { status: 204 })),
    )
    vi.stubGlobal('fetch', fetchMock)

    render(
      <PtzControls
        cameraId="camera-1"
        cameraName="Porch"
        viewer={false}
        zoomSupported={false}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Open PTZ controls for Porch' }))

    const right = screen.getByRole('button', { name: 'Pan right' })
    fireEvent.pointerDown(right, { pointerId: 1 })

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/cameras/camera-1/ptz/move')
    const body = fetchMock.mock.calls[0]?.[1]?.body
    expect(typeof body).toBe('string')
    if (typeof body !== 'string') throw new TypeError('Expected a JSON request body.')
    expect(JSON.parse(body)).toEqual({
      pan: 0.65,
      tilt: 0,
      zoom: 0,
    })

    fireEvent.pointerUp(right, { pointerId: 1 })
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    expect(fetchMock.mock.calls[1]?.[0]).toBe('/api/v1/cameras/camera-1/ptz/stop')
  })

  it('uses the isolated viewer operator endpoint in an embed', async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(new Response(null, { status: 204 })),
    )
    vi.stubGlobal('fetch', fetchMock)

    render(
      <PtzControls
        cameraId="camera/2"
        cameraName="Courtyard"
        viewer
        zoomSupported
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Open PTZ controls for Courtyard' }))
    const zoom = screen.getByRole('button', { name: 'Zoom in' })
    fireEvent.pointerDown(zoom, { pointerId: 2 })

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/viewer/cameras/camera%2F2/ptz/move')
    fireEvent.pointerUp(zoom, { pointerId: 2 })
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  })
})
