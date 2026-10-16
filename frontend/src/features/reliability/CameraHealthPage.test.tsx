import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { CameraHealthPage } from '@/features/reliability/CameraHealthPage'
import type {
  CameraIncident,
  CameraReliability,
  RecordingQuality,
  ReliabilityResponse,
} from '@/features/reliability/types'
import { renderWithProviders } from '@/test/utils'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function camera(overrides: Partial<CameraReliability> = {}): CameraReliability {
  return {
    camera_id: 'cam-courtyard',
    name: 'Courtyard',
    state: 'healthy',
    detail: 'Recording normally.',
    issue: null,
    issue_label: null,
    recording_expected: true,
    recording_fresh: true,
    last_segment_at: '2026-08-26T11:58:00Z',
    segment_age_seconds: 120,
    freshness_limit_seconds: 210,
    last_healthy_at: '2026-08-26T12:00:00Z',
    motion_expected: true,
    motion_running: true,
    recorder: {
      present: true,
      state: 'running',
      running: true,
      started_at: '2026-08-26T06:00:00Z',
      restart_count: 0,
      consecutive_failures: 0,
      source: 'relay',
      fault: null,
      fault_label: null,
    },
    quality: quality(),
    active_incident: null,
    recent_incidents: [],
    ...overrides,
  }
}

/** A camera recording continuously, which is what the panel calls healthy. */
function quality(overrides: Partial<RecordingQuality> = {}): RecordingQuality {
  return {
    window_seconds: 3600,
    coverage_ratio: 0.994,
    coverage_percent: 99.4,
    recorded_seconds: 3578,
    segments: 60,
    damaged_segments: 0,
    short_segments: 0,
    gap_count: 0,
    longest_gap_seconds: 0,
    restarts_in_window: 0,
    corruption_per_minute: 0,
    corruption_events: 0,
    corruption_kind: null,
    profile: 'passthrough',
    source_reason: 'relay_preferred',
    source_reason_label: 'Using the shared local relay (preferred).',
    source_switches: 0,
    ...overrides,
  }
}

function incident(overrides: Partial<CameraIncident> = {}): CameraIncident {
  return {
    id: 'incident-1',
    camera_id: 'cam-courtyard',
    reason: 'recording_not_fresh',
    reason_label: 'Recording not fresh',
    started_at: '2026-08-26T04:12:00Z',
    resolved_at: '2026-08-26T04:23:00Z',
    duration_seconds: 660,
    active: false,
    detail: 'The recorder is running but no segment has completed for 6 minutes.',
    fault: null,
    fault_label: null,
    source: 'relay',
    restart_count: 2,
    ...overrides,
  }
}

function payload(overrides: Partial<ReliabilityResponse> = {}): ReliabilityResponse {
  const cameras = overrides.cameras ?? [camera()]
  return {
    generated_at: '2026-08-26T12:00:00Z',
    privacy_enabled: false,
    privacy_message: null,
    storage_available: true,
    relay_expected: true,
    relay_ready: true,
    counts: {
      healthy: cameras.filter((item) => item.state === 'healthy').length,
      degraded: cameras.filter((item) => item.state === 'degraded').length,
      offline: cameras.filter((item) => item.state === 'offline').length,
      recovering: cameras.filter((item) => item.state === 'recovering').length,
      disabled: cameras.filter((item) => item.state === 'disabled').length,
      privacy_paused: cameras.filter((item) => item.state === 'privacy_paused').length,
    },
    ...overrides,
    cameras,
  }
}

interface StubOptions {
  reliability?: ReliabilityResponse
  incidents?: CameraIncident[]
  retry?: () => Response
}

function stubApi(options: StubOptions = {}): ReturnType<typeof vi.fn> {
  const incidents = options.incidents ?? []
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url =
      typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
    if (url.includes('/reliability/cameras') && url.includes('/retry')) {
      return Promise.resolve(options.retry ? options.retry() : json({}))
    }
    if (url.includes('/reliability/cameras')) {
      return Promise.resolve(json(options.reliability ?? payload()))
    }
    if (url.includes('/reliability/incidents')) {
      return Promise.resolve(
        json({ incidents, total: incidents.length, limit: 20, offset: 0 }),
      )
    }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

/** Scoped to the camera list: the summary strip uses the same state words. */
function cameraList() {
  return within(screen.getByLabelText('Cameras'))
}

describe('CameraHealthPage', () => {
  it('marks the summary busy until the first response arrives', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise<Response>(() => {})),
    )
    renderWithProviders(<CameraHealthPage />)

    expect(screen.getByRole('heading', { name: 'Camera health' })).toBeInTheDocument()
    expect(screen.getByLabelText('Camera state summary')).toHaveAttribute('aria-busy', 'true')
  })

  it('says plainly when no camera is configured', async () => {
    stubApi({ reliability: payload({ cameras: [] }) })
    renderWithProviders(<CameraHealthPage />)

    expect(await screen.findByText('No cameras are configured')).toBeInTheDocument()
    expect(screen.getByText('No camera incidents have been recorded.')).toBeInTheDocument()
  })

  it('reports a healthy camera with only facts the backend proved', async () => {
    stubApi()
    renderWithProviders(<CameraHealthPage />)

    expect(await screen.findByText('Recording normally.')).toBeInTheDocument()
    const list = cameraList()
    expect(list.getByText('Courtyard')).toBeInTheDocument()
    expect(list.getByText('Healthy')).toBeInTheDocument()
    expect(list.getByText('Fresh')).toBeInTheDocument()
    expect(list.getByText('Running · Local relay')).toBeInTheDocument()
    // Nothing the backend cannot prove. Coverage IS a measured percentage, so
    // it is shown; uptime, bitrate and any quality score are not measured
    // anywhere and must never appear.
    expect(screen.queryByText(/uptime/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/bitrate/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/quality score/i)).not.toBeInTheDocument()
    expect(list.getByText('99.4%')).toBeInTheDocument()
  })

  it('shows the explanation for a camera deliberately pinned to the local relay', async () => {
    stubApi({
      reliability: payload({
        cameras: [
          camera({
            quality: quality({
              source_reason: 'relay_pinned',
              source_reason_label: 'Pinned to the shared local relay by camera configuration.',
            }),
          }),
        ],
      }),
    })
    renderWithProviders(<CameraHealthPage />)

    expect(
      await screen.findByText('Pinned to the shared local relay by camera configuration.'),
    ).toBeInTheDocument()
  })

  it('explains a degraded camera without hiding that recording continues', async () => {
    stubApi({
      reliability: payload({
        cameras: [
          camera({
            state: 'degraded',
            issue: 'motion_worker_unavailable',
            issue_label: 'Motion detection stopped',
            detail: 'Recording is fresh, but motion detection is not running for this camera.',
            motion_running: false,
          }),
        ],
      }),
    })
    renderWithProviders(<CameraHealthPage />)

    expect(
      await screen.findByText(
        'Recording is fresh, but motion detection is not running for this camera.',
      ),
    ).toBeInTheDocument()
    const list = cameraList()
    expect(list.getByText('Degraded')).toBeInTheDocument()
    expect(list.getByText('Motion detection stopped')).toBeInTheDocument()
    expect(list.getByText('Not running')).toBeInTheDocument()
    expect(list.getByText('Fresh')).toBeInTheDocument()
  })

  it('shows an offline camera, its classified fault and the open incident', async () => {
    const open = incident({
      id: 'incident-open',
      active: true,
      resolved_at: null,
      duration_seconds: null,
      reason: 'stream_source_failure',
      reason_label: 'Camera stream failing',
      fault: 'missing_stream_parameters',
      fault_label:
        'The stream did not repeat its H.264 setup data, so the recorder could not determine the picture size.',
      source: 'direct',
    })
    stubApi({
      reliability: payload({
        cameras: [
          camera({
            state: 'offline',
            issue: 'stream_source_failure',
            issue_label: 'Camera stream failing',
            detail: 'The recorder has failed 4 times in a row and is still retrying.',
            recording_fresh: false,
            recorder: {
              present: true,
              state: 'restarting',
              running: false,
              started_at: '2026-08-26T11:00:00Z',
              restart_count: 12,
              consecutive_failures: 4,
              source: 'direct',
              fault: 'missing_stream_parameters',
              fault_label:
                'The stream did not repeat its H.264 setup data, so the recorder could not determine the picture size.',
            },
            active_incident: open,
            recent_incidents: [open],
          }),
        ],
      }),
      incidents: [open],
    })
    renderWithProviders(<CameraHealthPage />)

    expect(
      await screen.findByText('The recorder has failed 4 times in a row and is still retrying.'),
    ).toBeInTheDocument()
    const list = cameraList()
    expect(list.getByText('Offline')).toBeInTheDocument()
    expect(list.getByText('Not fresh')).toBeInTheDocument()
    expect(list.getByText('Reconnecting · Direct from camera')).toBeInTheDocument()
    expect(list.getByText('12 (4 failing)')).toBeInTheDocument()
    expect(screen.getAllByText(/did not repeat its H.264 setup data/).length).toBeGreaterThan(0)
    expect(screen.getAllByText('Active').length).toBeGreaterThan(0)
    expect(screen.getAllByText('still open').length).toBeGreaterThan(0)
  })

  it('separates a recovering camera from a recovered incident', async () => {
    const recovered = incident()
    stubApi({
      reliability: payload({
        cameras: [
          camera({
            state: 'recovering',
            detail: 'Recording is fresh again. Waiting for the recovery to hold.',
            recent_incidents: [recovered],
          }),
        ],
      }),
      incidents: [recovered],
    })
    renderWithProviders(<CameraHealthPage />)

    expect(
      await screen.findByText('Recording is fresh again. Waiting for the recovery to hold.'),
    ).toBeInTheDocument()
    expect(cameraList().getByText('Recovering')).toBeInTheDocument()
    expect(screen.getByText('Recent incidents (1)')).toBeInTheDocument()
    expect(screen.getAllByText('Recovered').length).toBeGreaterThan(0)
    expect(screen.getAllByText('lasted 11m 00s').length).toBeGreaterThan(0)
  })

  it('presents privacy mode as a deliberate pause and blocks the retry control', async () => {
    stubApi({
      reliability: payload({
        privacy_enabled: true,
        privacy_message: 'Cameras are paused for privacy: Guests are present.',
        cameras: [
          camera({
            state: 'privacy_paused',
            detail: 'Paused by privacy mode. Recording and live view are stopped on purpose.',
            recording_expected: false,
            recording_fresh: null,
          }),
        ],
      }),
    })
    renderWithProviders(<CameraHealthPage />)

    expect(await screen.findByText('All cameras are paused for privacy')).toBeInTheDocument()
    const list = cameraList()
    expect(list.getByText('Privacy paused')).toBeInTheDocument()
    expect(list.getByText('Not expected')).toBeInTheDocument()
    expect(list.queryByText('Offline')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Retry camera services/ })).toBeDisabled()
  })

  it('reports what a retry did without claiming the video came back', async () => {
    stubApi({
      retry: () =>
        json({
          camera_id: 'cam-courtyard',
          requested_at: '2026-08-26T12:01:00Z',
          accepted: true,
          actions: ['relay_stream_refresh_requested', 'recorder_restarted'],
          skipped: ['motion_not_configured'],
          state_before: 'offline',
          message:
            'Camera services were asked to restart. Recording is not confirmed until a new segment completes, which normally takes a minute or two.',
        }),
    })
    renderWithProviders(<CameraHealthPage />)

    const button = await screen.findByRole('button', { name: /Retry camera services/ })
    await userEvent.click(button)
    expect(screen.getByRole('heading', { name: 'Retry services for Courtyard?' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Retry services' }))

    expect(await screen.findByText('Retry requested')).toBeInTheDocument()
    expect(
      screen.getByText('Requested a refresh of this camera’s relay stream'),
    ).toBeInTheDocument()
    expect(screen.getByText(/Recording is not confirmed/)).toBeInTheDocument()
    expect(screen.getByText('Restarted the recorder')).toBeInTheDocument()
    expect(screen.getByText(/Motion detection is switched off/)).toBeInTheDocument()
  })

  it('surfaces a refused retry instead of pretending it worked', async () => {
    stubApi({
      retry: () =>
        json({ detail: 'This camera was retried moments ago. Give it time to reconnect.' }, 429),
    })
    renderWithProviders(<CameraHealthPage />)

    const button = await screen.findByRole('button', { name: /Retry camera services/ })
    await userEvent.click(button)
    await userEvent.click(screen.getByRole('button', { name: 'Retry services' }))

    await waitFor(() => {
      expect(screen.getByText('The retry was not accepted.')).toBeInTheDocument()
    })
    expect(screen.getByText(/retried moments ago/)).toBeInTheDocument()
  })

  it('keeps the wide incident history scrolling inside its own container', async () => {
    const recovered = incident()
    stubApi({ incidents: [recovered] })
    const { container } = renderWithProviders(<CameraHealthPage />)

    await screen.findByText('Recording normally.')
    const history = container.querySelector('[aria-label="Incident history"] .overflow-x-auto')
    expect(history).not.toBeNull()
    // The list is wider than a phone, so the page body must never scroll with it.
    expect(history?.firstElementChild?.className).toContain('min-w-[36rem]')
  })

  it('reports poor continuity instead of implying a health it never measured', async () => {
    // A camera that looks fine and is not: fresh segments, every recorder
    // running, and half the hour simply not recorded.
    stubApi({
      reliability: payload({
        cameras: [
          camera({
            state: 'offline',
            issue: 'recording_coverage_low',
            issue_label: 'Recording is not continuous',
            detail: 'Only 50% of the last hour was recorded, in 154 gaps (longest 211s).',
            recording_fresh: true,
            quality: quality({
              coverage_ratio: 0.505,
              coverage_percent: 50.5,
              recorded_seconds: 1818,
              segments: 237,
              damaged_segments: 31,
              short_segments: 92,
              gap_count: 154,
              longest_gap_seconds: 211,
              restarts_in_window: 12,
              source_reason: 'relay_unavailable',
              source_reason_label:
                'The shared local relay failed repeatedly, so this camera is recorded over a direct connection.',
              source_switches: 3,
              profile: 'resilient',
            }),
          }),
        ],
      }),
    })
    renderWithProviders(<CameraHealthPage />)

    await screen.findByText(/Only 50% of the last hour was recorded/)
    const list = cameraList()
    expect(list.getByText('Offline')).toBeInTheDocument()
    expect(list.getByText('50.5%')).toBeInTheDocument()
    expect(list.getByText('154 (longest 4 min)')).toBeInTheDocument()
    expect(list.getByText('92 of 237')).toBeInTheDocument()
    // Recording is fresh and the camera is still not healthy: that is the whole
    // point of measuring continuity rather than recency.
    expect(list.getByText('Fresh')).toBeInTheDocument()
    expect(list.getByText(/relay failed repeatedly/)).toBeInTheDocument()
  })

  it('says a new camera is not yet measurable rather than guessing', async () => {
    stubApi({
      reliability: payload({
        cameras: [
          camera({
            quality: quality({ coverage_ratio: null, coverage_percent: null, segments: 0 }),
          }),
        ],
      }),
    })
    renderWithProviders(<CameraHealthPage />)

    await screen.findByText('Recording normally.')
    expect(cameraList().getByText('Not yet measurable')).toBeInTheDocument()
  })

  it('counts stream damage without ever showing recorder output', async () => {
    stubApi({
      reliability: payload({
        cameras: [
          camera({
            state: 'degraded',
            issue: 'stream_corruption',
            issue_label: 'Camera stream is corrupt',
            detail: 'The camera is delivering damaged media at about 160 events a minute.',
            quality: quality({
              corruption_per_minute: 160.4,
              corruption_events: 38_478,
              corruption_kind: 'empty_rtp_packet',
            }),
          }),
        ],
      }),
    })
    renderWithProviders(<CameraHealthPage />)

    await screen.findByText(/delivering damaged media/)
    expect(cameraList().getByText('160.4/min')).toBeInTheDocument()
    // A count, never the thousands of identical FFmpeg lines behind it.
    expect(screen.queryByText(/Empty H.264 RTP packet/)).not.toBeInTheDocument()
  })
})
