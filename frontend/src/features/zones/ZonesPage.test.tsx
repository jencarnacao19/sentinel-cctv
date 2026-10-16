import { act, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ZonesPage } from '@/features/zones/ZonesPage'
import { renderWithProviders } from '@/test/utils'
import type { Camera } from '@/types/api'

/**
 * Full-mount tests for the zone editor page.
 *
 * `zoneMath.test.ts` already proves `sanitizeServerZones` itself tolerates
 * garbage input, and `ZoneCanvas.test.tsx` proves the drawing surface reacts
 * correctly to mouse and keyboard once it has a valid zone list. Neither
 * proves the rest of the page survives being handed a real, malformed
 * response from the server -- the hook that calls the sanitizer, the editor
 * that hydrates from its result, the list and canvas that render the
 * outcome. A camera whose stored zones were written by an older, buggier
 * version of this same feature (or edited by hand through the API) is the
 * realistic scenario this guards against: the operator must still be able to
 * open the editor and fix it, not be met with a blank page or a thrown
 * error. This file mounts `ZonesPage` itself, stubbing only `fetch`, so
 * nothing about the wiring between `useMotionZones`, `ZoneEditor`, `ZoneList`
 * and `ZoneCanvas` is mocked away.
 */

type Handler = (url: string, init?: RequestInit) => Response

/**
 * The request body as text.
 *
 * `BodyInit` also covers Blob, FormData and streams, and `String()`-ing one of
 * those yields "[object Object]" -- a JSON.parse failure several lines later
 * that says nothing about the cause. The page sends JSON strings; if that ever
 * stops being true, failing here names the reason.
 */
function bodyText(body: BodyInit | null | undefined): string {
  if (typeof body !== 'string') {
    throw new TypeError(`Expected a JSON string request body, got ${typeof body}`)
  }
  return body
}

function stubRoutes(routes: Record<string, Handler>) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const path = url.split('?')[0] ?? url
    const handler = routes[path]
    if (!handler) return Promise.reject(new Error(`Unexpected request to ${url}`))
    return Promise.resolve(handler(url, init))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

const CAMERA: Camera = {
  id: 'cam-1',
  kind: 'rtsp',
  name: 'Front door',
  manufacturer: null,
  model: null,
  nvr_group: null,
  host: '192.168.1.50',
  tapo_child_id: null,
  main_stream_url: 'rtsp://192.168.1.50:554/stream1',
  sub_stream_url: null,
  username: null,
  has_credentials: false,
  has_tapo_credentials: false,
  onvif_device_xaddr: null,
  ptz_supported: null,
  ptz_zoom_supported: false,
  ptz_status_message: null,
  ptz_last_checked_at: null,
  onvif_patrol_enabled: false,
  tapo_patrol_mode: null,
  tapo_controls: {
    supported: true,
    source: 'metadata',
    detected_vendor: 'TP-Link',
    reason: 'TP-Link identifies this as a TP-Link/Tapo camera.',
    can_opt_in: false,
  },
  tapo_controls_enabled: null,
  recording_state: 'recording',
  recording_enabled: true,
  enabled: true,
  recording_mode: 'continuous',
  recording_profile: 'auto',
  recording_source_mode: 'auto',
  recording_segment_seconds: null,
  retention_days: null,
  motion_enabled: true,
  motion_sensitivity: 0.5,
  status: 'online',
  status_message: null,
  last_seen_at: null,
  stream: {
    codec: 'h264',
    width: 1920,
    height: 1080,
    fps: 15,
    audio_codec: null,
    bitrate_kbps: null,
  },
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
}

/** Every route the page hits besides `motion-zones`, which each test sets up
 * itself. The preview is made to fail on purpose (a plausible real answer
 * from `GET /motion-preview` for an unreachable camera) so these tests stay
 * about zone data, not about mocking a JPEG past `usePreviewFrame`'s blob
 * handling. The tuning panel's two endpoints are background noise it polls
 * regardless of what is under test here. */
function backgroundRoutes(): Record<string, Handler> {
  return {
    '/api/v1/cameras': () => json([CAMERA]),
    '/api/v1/cameras/cam-1/motion-preview': () =>
      json({ detail: 'This camera has no stream URL.' }, 409),
    '/api/v1/events/status': () => json({ workers: [] }),
    '/api/v1/events': () => json({ events: [], total: 0, limit: 5, offset: 0 }),
  }
}

beforeEach(() => {
  // Each test uses the same camera id; without this a save in one test (or a
  // leftover run order) would leak names/enabled-state into the next.
  localStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ZonesPage — malformed server data', () => {
  it('keeps the editor usable when some saved zones are malformed, and reports how many were dropped', async () => {
    stubRoutes({
      ...backgroundRoutes(),
      '/api/v1/cameras/cam-1/motion-zones': () =>
        json({
          camera_id: 'cam-1',
          zones: [
            [
              [0, 0],
              [1, 0],
              [1, 1],
            ], // valid triangle -- kept
            [
              [0, 0],
              [1, 0],
            ], // only two points -- not a polygon
            'not-a-polygon', // not an array at all
            null, // not an array at all
            [
              [0, 0],
              [2, 2], // out of the 0-1 range -- dropped, leaving only 2 usable points
              [1, 1],
            ],
          ],
        }),
    })

    renderWithProviders(<ZonesPage />)

    // The editor reached the drawing surface at all -- the headline claim of
    // this test -- rather than the page staying blank or unmounting after an
    // uncaught error.
    expect(await screen.findByRole('group', { name: /Front door/ })).toBeInTheDocument()

    // The one well-formed polygon survived and is usable, listed by name.
    expect(await screen.findByDisplayValue('Zone 1')).toBeInTheDocument()
    expect(screen.getByText('3 points')).toBeInTheDocument()
    expect(screen.getByText('Zones (1/32)')).toBeInTheDocument()

    // The four malformed entries are accounted for, not silently lost.
    expect(
      screen.getByText(/4 saved zones could not be read and were skipped/),
    ).toBeInTheDocument()
  })

  it('shows an empty, still-usable editor when the zones payload is not a list at all', async () => {
    stubRoutes({
      ...backgroundRoutes(),
      '/api/v1/cameras/cam-1/motion-zones': () =>
        json({ camera_id: 'cam-1', zones: 'this used to be JSON, now it is just a string' }),
    })

    renderWithProviders(<ZonesPage />)

    expect(await screen.findByRole('group', { name: /Front door/ })).toBeInTheDocument()
    expect(
      screen.getByText('No zones yet. Use "New zone" above to draw the first one.'),
    ).toBeInTheDocument()
    expect(screen.getByText('Zones (0/32)')).toBeInTheDocument()
    // A payload that was never a list of zones is zero zones, not a partial
    // loss -- the "N dropped" warning must stay silent, not claim a count it
    // cannot actually name.
    expect(screen.queryByText(/could not be read/)).not.toBeInTheDocument()
  })

  it('does not crash when a zone entry is a well-formed array of nonsense values', async () => {
    stubRoutes({
      ...backgroundRoutes(),
      '/api/v1/cameras/cam-1/motion-zones': () =>
        json({
          camera_id: 'cam-1',
          zones: [null, 1, 'x', { not: 'a polygon' }, [{ x: 1 }, [1, 2, 3]], [[0, 0], [1, 0], [1, 1]]],
        }),
    })

    renderWithProviders(<ZonesPage />)

    expect(await screen.findByRole('group', { name: /Front door/ })).toBeInTheDocument()
    expect(await screen.findByDisplayValue('Zone 1')).toBeInTheDocument()
    expect(screen.getByText('Zones (1/32)')).toBeInTheDocument()
  })
})

describe('ZonesPage — end-to-end save', () => {
  it('draws a zone with the keyboard alone through the real page and saves exactly its normalised points', async () => {
    const user = userEvent.setup()
    const fetchMock = stubRoutes({
      ...backgroundRoutes(),
      '/api/v1/cameras/cam-1/motion-zones': (_url, init) => {
        if (init?.method === 'PUT') {
          const body: { zones: unknown } = JSON.parse(bodyText(init.body))
          return json({ camera_id: 'cam-1', zones: body.zones })
        }
        return json({ camera_id: 'cam-1', zones: [] })
      },
    })

    renderWithProviders(<ZonesPage />)
    const canvas = await screen.findByRole('group', { name: /Front door/ })

    act(() => canvas.focus())
    await user.keyboard('{Enter}') // start a new zone
    await user.keyboard('{Enter}') // place point at the centre, (0.5, 0.5)
    await user.keyboard('{ArrowRight}{ArrowRight}{ArrowRight}')
    await user.keyboard('{Enter}') // (0.56, 0.5)
    await user.keyboard('{ArrowDown}{ArrowDown}')
    await user.keyboard('{Enter}') // (0.56, 0.54)
    await user.keyboard('f') // finish

    await user.click(screen.getByRole('button', { name: 'Save zones' }))

    expect(await screen.findByText('Zones saved.')).toBeInTheDocument()

    const putCall = fetchMock.mock.calls.find((call) => call[1]?.method === 'PUT')
    expect(putCall).toBeDefined()
    const body: unknown = JSON.parse(bodyText(putCall?.[1]?.body))
    expect(body).toEqual({
      zones: [
        [
          [0.5, 0.5],
          [0.56, 0.5],
          [0.56, 0.54],
        ],
      ],
    })
  })
})
