import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { EditCameraDialog } from '@/features/cameras/EditCameraDialog'
import { renderWithProviders } from '@/test/utils'
import type { Camera } from '@/types/api'

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
  host: '192.168.1.50',
  tapo_child_id: null,
  main_stream_url: 'rtsp://192.168.1.50:554/stream1',
  sub_stream_url: null,
  username: 'camera-user',
  has_credentials: true,
  has_tapo_credentials: false,
  onvif_device_xaddr: 'http://192.168.1.50:2020/onvif/device_service',
  ptz_supported: true,
  ptz_zoom_supported: false,
  ptz_status_message: null,
  ptz_last_checked_at: '2026-08-25T12:00:00Z',
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
  last_seen_at: '2026-08-25T12:00:00Z',
  stream: {
    codec: 'h264',
    width: 2560,
    height: 1440,
    fps: 25,
    audio_codec: 'aac',
    bitrate_kbps: 4200,
  },
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-25T12:00:00Z',
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('EditCameraDialog', () => {
  it('saves the selected Tapo patrol direction as part of the camera configuration', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      expect(url).toBe('/api/v1/cameras/cam-1')
      expect(init?.method).toBe('PATCH')
      return Promise.resolve(json(CAMERA))
    })
    vi.stubGlobal('fetch', fetchMock)
    const onOpenChange = vi.fn()
    const user = userEvent.setup()

    renderWithProviders(
      <EditCameraDialog camera={CAMERA} open onOpenChange={onOpenChange} />,
    )

    await user.selectOptions(
      screen.getByLabelText('Resume Tapo patrol after PTZ'),
      'horizontal',
    )
    await user.selectOptions(screen.getByLabelText('Recording quality profile'), 'normalised')
    await user.selectOptions(screen.getByLabelText('Recording source'), 'relay')
    await user.selectOptions(screen.getByLabelText('Recording segment length'), '30')
    await user.type(
      screen.getByLabelText('Tapo account password for LAN control'),
      'tapo-app-secret',
    )
    await user.click(screen.getByRole('button', { name: 'Save configuration' }))

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
    expect(fetchMock).toHaveBeenCalledTimes(1)

    const body = fetchMock.mock.calls[0]?.[1]?.body
    expect(JSON.parse(typeof body === 'string' ? body : '{}')).toMatchObject({
      tapo_patrol_mode: 'horizontal',
      tapo_password: 'tapo-app-secret',
      recording_profile: 'normalised',
      recording_source_mode: 'relay',
      recording_segment_seconds: 30,
    })
  })
})
