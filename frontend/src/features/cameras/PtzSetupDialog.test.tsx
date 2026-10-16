import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PtzSetupDialog } from '@/features/cameras/PtzSetupDialog'
import { renderWithProviders } from '@/test/utils'
import type { Camera, PtzSetup } from '@/types/api'

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}

const CAMERA: Camera = {
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
  username: 'camera-user',
  has_credentials: true,
  has_tapo_credentials: false,
  onvif_device_xaddr: 'http://192.168.50.50:2020/onvif/device_service',
  ptz_supported: true,
  ptz_zoom_supported: false,
  ptz_status_message: null,
  ptz_last_checked_at: '2026-08-26T12:00:00Z',
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
  last_seen_at: '2026-08-26T12:00:00Z',
  stream: {
    codec: 'h264',
    width: 2560,
    height: 1440,
    fps: 25,
    audio_codec: 'aac',
    bitrate_kbps: 4200,
  },
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-26T12:00:00Z',
}

const SETUP: PtzSetup = {
  presets: [
    { token: '1', name: 'Door' },
    { token: '2', name: 'Driveway' },
  ],
  preset_capacity: 8,
  preset_tour_capacity: 8,
  home_supported: false,
  patrol_backend: 'none',
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('PtzSetupDialog', () => {
  it('creates a camera-owned LAN patrol from selected ONVIF presets', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      if (url.endsWith('/ptz/setup')) return Promise.resolve(json(SETUP))
      if (url.endsWith('/ptz/patrol') && init?.method === 'PUT') {
        return Promise.resolve(json({ enabled: true, backend: 'onvif_lan' }))
      }
      throw new Error(`Unexpected request: ${init?.method} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderWithProviders(<PtzSetupDialog camera={CAMERA} open onOpenChange={vi.fn()} />)

    expect(await screen.findByText('ONVIF LAN')).toBeInTheDocument()
    await user.click(screen.getByLabelText('Include Door in patrol'))
    await user.click(screen.getByLabelText('Include Driveway in patrol'))
    await user.click(screen.getByRole('button', { name: 'Start local patrol' }))

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/v1/cameras/cam-1/ptz/patrol',
        expect.objectContaining({ method: 'PUT' }),
      ),
    )
    const patrolCall = fetchMock.mock.calls.find(
      ([input, init]) => {
        const url =
          typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
        return url.endsWith('/ptz/patrol') && init?.method === 'PUT'
      },
    )
    const body = patrolCall?.[1]?.body
    expect(JSON.parse(typeof body === 'string' ? body : '{}')).toMatchObject({
      preset_tokens: ['1', '2'],
      stay_seconds: 10,
    })
  })
})
