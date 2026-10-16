import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TapoLocalControlsDialog } from '@/features/cameras/TapoLocalControlsDialog'
import { renderWithProviders } from '@/test/utils'
import type { Camera, TapoState } from '@/types/api'

const CAMERA = {
  id: 'cam-1',
  kind: 'rtsp',
  name: 'Front door',
  manufacturer: 'TP-Link',
  model: 'Tapo C520WS',
  nvr_group: null,
  host: '192.168.50.50',
  tapo_child_id: null,
  main_stream_url: 'rtsp://192.168.50.50:554/stream1',
  sub_stream_url: null,
  username: 'admin',
  has_credentials: true,
  has_tapo_credentials: true,
  onvif_device_xaddr: null,
  ptz_supported: true,
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
  retention_days: 30,
  motion_enabled: true,
  motion_sensitivity: 50,
  status: 'online',
  status_message: null,
  last_seen_at: null,
  stream: {
    codec: 'h264',
    width: 2560,
    height: 1440,
    fps: 25,
    audio_codec: 'aac',
    bitrate_kbps: 4200,
  },
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-27T00:00:00Z',
} satisfies Camera

const STATE: TapoState = {
  led_enabled: true,
  privacy_mask_enabled: false,
  day_night_mode: 'auto',
  smart_track_enabled: true,
  floodlight_enabled: null,
  siren_active: false,
  cruise_enabled: true,
  preset_count: 3,
  sd_card: { present: true, status: 'normal', total_gb: 64, free_gb: 20.5 },
  detections: {
    person: { supported: true, enabled: true, sensitivity: 50, sensitivity_label: null },
  },
  read_at: '2026-08-27T12:00:00Z',
}

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}

function requestUrl(input: RequestInfo | URL): string {
  return typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
}

function requestJson(init: RequestInit | undefined): unknown {
  const body = init?.body
  expect(typeof body).toBe('string')
  return JSON.parse(typeof body === 'string' ? body : '{}') as unknown
}

afterEach(() => vi.unstubAllGlobals())

describe('TapoLocalControlsDialog', () => {
  it('shows only confirmed LAN controls and applies a change through the local API', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input)
      if (url.endsWith('/tapo/capabilities')) {
        return Promise.resolve(
          response({
            supported: ['device_info', 'led', 'privacy_mask', 'day_night', 'smart_track', 'person'],
            device: { model: 'C520WS', hardware_version: '1.0', firmware_version: '2.1' },
            checked_at: '2026-08-27T12:00:00Z',
            excluded: ['firmware_upgrade', 'firmware_check', 'sd_card_format', 'factory_reset'],
          }),
        )
      }
      if (url.endsWith('/tapo/state')) return Promise.resolve(response(STATE))
      if (url.endsWith('/tapo/led') && init?.method === 'POST') {
        return Promise.resolve(
          response({
            camera_id: CAMERA.id,
            feature: 'led',
            applied: true,
            message: 'The camera accepted the change.',
            state: { ...STATE, led_enabled: false },
          }),
        )
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderWithProviders(
      <TapoLocalControlsDialog camera={CAMERA} open onOpenChange={vi.fn()} />,
    )

    const led = await screen.findByRole('switch', { name: 'Status LED' })
    expect(led).toBeChecked()
    expect(screen.getByText('LAN only')).toBeInTheDocument()
    expect(screen.queryByText(/firmware update/i)).toBeInTheDocument()

    await user.click(led)
    await waitFor(() => expect(led).not.toBeChecked())
    const write = fetchMock.mock.calls.find(
      ([input]) => typeof input === 'string' && input.endsWith('/tapo/led'),
    )
    const body = write?.[1]?.body
    expect(typeof body).toBe('string')
    expect(JSON.parse(typeof body === 'string' ? body : '{}')).toEqual({ enabled: false })
  })

  it('requires a second confirmation for the siren and camera reboot', async () => {
    const onOpenChange = vi.fn()
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input)
      if (url.endsWith('/tapo/capabilities')) {
        return Promise.resolve(
          response({
            supported: ['device_info', 'alarm', 'reboot', 'cruise', 'presets'],
            device: { model: 'C520WS', hardware_version: '1.0', firmware_version: '2.1' },
            checked_at: '2026-08-27T12:00:00Z',
            excluded: ['firmware_upgrade', 'firmware_check', 'sd_card_format', 'factory_reset'],
          }),
        )
      }
      if (url.endsWith('/tapo/state')) return Promise.resolve(response(STATE))
      if (url.endsWith('/tapo/siren/start') && init?.method === 'POST') {
        return Promise.resolve(
          response({
            camera_id: CAMERA.id,
            feature: 'siren',
            applied: true,
            message: 'The siren was started.',
            state: { ...STATE, siren_active: true },
          }),
        )
      }
      if (url.endsWith('/tapo/reboot') && init?.method === 'POST') {
        return Promise.resolve(
          response({
            camera_id: CAMERA.id,
            feature: 'reboot',
            applied: true,
            message: 'The camera was asked to restart.',
            state: null,
          }),
        )
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderWithProviders(
      <TapoLocalControlsDialog camera={CAMERA} open onOpenChange={onOpenChange} />,
    )

    await user.click(await screen.findByRole('button', { name: 'Sound siren' }))
    expect(fetchMock.mock.calls.some(([input]) => requestUrl(input).endsWith('/siren/start'))).toBe(
      false,
    )
    await user.click(screen.getByRole('button', { name: 'Confirm action' }))
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([input]) => requestUrl(input).endsWith('/tapo/siren/start')),
      ).toBe(true),
    )
    const sirenCall = fetchMock.mock.calls.find(([input]) =>
      requestUrl(input).endsWith('/tapo/siren/start'),
    )
    expect(requestJson(sirenCall?.[1])).toEqual({ confirm: true })

    await user.click(screen.getByRole('button', { name: 'Reboot camera' }))
    await user.click(screen.getByRole('button', { name: 'Confirm action' }))
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
    const rebootCall = fetchMock.mock.calls.find(([input]) =>
      requestUrl(input).endsWith('/tapo/reboot'),
    )
    expect(requestJson(rebootCall?.[1])).toEqual({ confirm: true })
  })

  it('stores a separate Tapo account password and retries LAN discovery', async () => {
    const camera = { ...CAMERA, has_tapo_credentials: false }
    let saved = false
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input)
      if (url.endsWith(`/cameras/${CAMERA.id}`) && init?.method === 'PATCH') {
        saved = true
        return Promise.resolve(response({ ...camera, has_tapo_credentials: true }))
      }
      if (url.includes('/tapo/capabilities')) {
        if (!saved) {
          return Promise.resolve(
            new Response(JSON.stringify({ detail: 'The camera rejected the stored credentials.' }), {
              status: 502,
              headers: { 'content-type': 'application/json' },
            }),
          )
        }
        return Promise.resolve(
          response({
            supported: ['device_info', 'led'],
            device: { model: 'C520WS', hardware_version: '1.0', firmware_version: '2.1' },
            checked_at: '2026-08-27T12:00:00Z',
            excluded: ['firmware_upgrade', 'firmware_check', 'sd_card_format', 'factory_reset'],
          }),
        )
      }
      if (url.endsWith('/tapo/state')) return Promise.resolve(response(STATE))
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderWithProviders(
      <TapoLocalControlsDialog camera={camera} open onOpenChange={vi.fn()} />,
    )

    // A rejected credential is named as such, with the fix next to it, rather
    // than as one generic "unavailable" for every possible failure.
    expect(await screen.findByText('Authentication rejected')).toBeInTheDocument()
    expect(screen.getByText(/enter the tapo account password below/i)).toBeInTheDocument()
    await user.type(screen.getByLabelText('Tapo account password'), 'tapo-app-password')
    await user.click(screen.getByRole('button', { name: 'Save & discover' }))

    expect(await screen.findByText('LAN only')).toBeInTheDocument()
    const updateCall = fetchMock.mock.calls.find(
      ([input, init]) =>
        requestUrl(input).endsWith(`/cameras/${CAMERA.id}`) && init?.method === 'PATCH',
    )
    expect(requestJson(updateCall?.[1])).toEqual({
      tapo_password: 'tapo-app-password',
    })
  })
})

describe('one failing capability must not blank the panel', () => {
  function capabilities(overrides: Record<string, unknown> = {}) {
    return {
      supported: ['device_info', 'led', 'privacy_mask'],
      device: { model: 'C520WS', hardware_version: '1.0', firmware_version: '2.1' },
      checked_at: '2026-08-27T12:00:00Z',
      excluded: ['firmware_upgrade', 'firmware_check', 'sd_card_format', 'factory_reset'],
      partial: false,
      unchecked: [],
      ...overrides,
    }
  }

  it('keeps the confirmed controls usable and says the list may be short', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = requestUrl(input)
        if (url.includes('/tapo/capabilities')) {
          return Promise.resolve(
            response(capabilities({ partial: true, unchecked: ['getPresetConfig'] })),
          )
        }
        if (url.endsWith('/tapo/state')) return Promise.resolve(response(STATE))
        return Promise.reject(new Error(`Unexpected request: ${url}`))
      }),
    )

    renderWithProviders(<TapoLocalControlsDialog camera={CAMERA} open onOpenChange={vi.fn()} />)

    expect(await screen.findByRole('switch', { name: 'Status LED' })).toBeInTheDocument()
    expect(screen.getByText('Some checks did not answer')).toBeInTheDocument()
    expect(screen.getByText(/1 other check did not answer/i)).toBeInTheDocument()
  })

  it('leaves no loading skeleton behind after a failure', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({ detail: 'The camera did not answer its local control service.' }),
            { status: 503, headers: { 'content-type': 'application/json' } },
          ),
        ),
      ),
    )

    const { container } = renderWithProviders(
      <TapoLocalControlsDialog camera={CAMERA} open onOpenChange={vi.fn()} />,
    )

    expect(await screen.findByText('Camera did not answer')).toBeInTheDocument()
    // The regression: the state query stayed disabled, react-query reported it
    // as pending for ever, and two skeletons sat above the error indefinitely.
    await waitFor(() => expect(container.querySelectorAll('.skeleton')).toHaveLength(0))
    expect(screen.getByRole('button', { name: /refresh from camera/i })).toBeEnabled()
  })

  it('says plainly when a camera answered but offers nothing', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = requestUrl(input)
        if (url.includes('/tapo/capabilities')) {
          return Promise.resolve(response(capabilities({ supported: [] })))
        }
        if (url.endsWith('/tapo/state')) return Promise.resolve(response(STATE))
        return Promise.reject(new Error(`Unexpected request: ${url}`))
      }),
    )

    renderWithProviders(<TapoLocalControlsDialog camera={CAMERA} open onOpenChange={vi.fn()} />)
    expect(await screen.findByText('This camera confirmed no controls')).toBeInTheDocument()
    expect(screen.getByText(/recording, live\s+view and onvif are unaffected/i)).toBeInTheDocument()
  })

  it('never renders a proxy error page as a camera fault', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(
            '<html><body>Error 520: The origin web server returned an invalid or incomplete ' +
              'response to Cloudflare.</body></html>',
            { status: 520, headers: { 'content-type': 'text/html' } },
          ),
        ),
      ),
    )

    renderWithProviders(<TapoLocalControlsDialog camera={CAMERA} open onOpenChange={vi.fn()} />)
    expect(await screen.findByText('Remote connection interrupted')).toBeInTheDocument()
    expect(screen.getByText(/not known how far the request got/i)).toBeInTheDocument()
    expect(screen.queryByText(/cloudflare/i)).not.toBeInTheDocument()
  })

  it('reports a timeout as a camera timeout, not an internal error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({ detail: 'The camera did not answer its local control service.' }),
            { status: 503, headers: { 'content-type': 'application/json' } },
          ),
        ),
      ),
    )
    renderWithProviders(<TapoLocalControlsDialog camera={CAMERA} open onOpenChange={vi.fn()} />)
    expect(await screen.findByText('Camera did not answer')).toBeInTheDocument()
    expect(
      screen.getByText(/its local control service did not reply/i),
    ).toBeInTheDocument()
  })

  it('reports unsupported firmware as a conflict rather than a failure', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({ detail: 'This camera does not support that feature.' }),
            { status: 409, headers: { 'content-type': 'application/json' } },
          ),
        ),
      ),
    )
    renderWithProviders(<TapoLocalControlsDialog camera={CAMERA} open onOpenChange={vi.fn()} />)
    expect(await screen.findByText('Not supported')).toBeInTheDocument()
  })
})
