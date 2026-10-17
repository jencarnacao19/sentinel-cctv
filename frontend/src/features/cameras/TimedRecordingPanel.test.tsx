import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { TimedRecordingPanel } from '@/features/cameras/TimedRecordingPanel'
import { batteryCamera } from '@/test/cameras'
import { requestedUrl, sentJson } from '@/test/requests'
import { renderWithProviders } from '@/test/utils'
import type { TimedRecordingOptions, TimedRecordingStatus } from '@/types/api'

const OPTIONS: TimedRecordingOptions = {
  presets_minutes: [5, 10, 15, 30, 60],
  max_minutes: 60,
  hard_max_minutes: 120,
  min_minutes: 1,
  battery_percent: 62,
  warning:
    'Recording keeps this battery camera awake for the whole period, with its radio and encoder ' +
    'running continuously. That is far heavier than the event-only use it is designed for and can ' +
    'flatten the battery completely, after which the camera stops recording and stays offline ' +
    'until it is charged.',
}

const RUNNING: TimedRecordingStatus = {
  camera_id: batteryCamera.id,
  state: 'recording',
  requested_seconds: 1800,
  started_at: '2026-08-28T01:00:00Z',
  ends_at: '2026-08-28T01:30:00Z',
  seconds_remaining: 1234,
  seconds_recorded: 566,
  bytes_recorded: 5_242_880,
  connection: 'connected',
  reconnects: 0,
  battery_percent_at_start: 62,
  message: "Recording from the camera's Direct Stream.",
  stop_reason: null,
  segment_id: null,
  finished: false,
}

function json(body: unknown, status = 200) {
  return new Response(body === null ? 'null' : JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

/** Route by URL so each test only says what it cares about. */
function route(handlers: {
  options?: TimedRecordingOptions
  session?: TimedRecordingStatus | null
  start?: Response
  stop?: Response
}) {
  vi.mocked(fetch).mockImplementation((input, init) => {
    const url = requestedUrl(input)
    if (url.includes('/options')) return Promise.resolve(json(handlers.options ?? OPTIONS))
    if (init?.method === 'POST') return Promise.resolve(handlers.start ?? json(RUNNING, 202))
    if (init?.method === 'DELETE') {
      return Promise.resolve(handlers.stop ?? json({ ...RUNNING, finished: true }))
    }
    return Promise.resolve(json(handlers.session ?? null))
  })
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn())
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('choosing a duration', () => {
  it('offers the five presets', async () => {
    route({})
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    for (const preset of [5, 10, 15, 30, 60]) {
      expect(await screen.findByRole('radio', { name: `${preset} min` })).toBeInTheDocument()
    }
  })

  it('shows the current charge before anything is confirmed', async () => {
    route({})
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    expect(await screen.findByText('Battery 62%')).toBeInTheDocument()
  })

  it('says the charge is unknown rather than inventing one', async () => {
    route({ options: { ...OPTIONS, battery_percent: null } })
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    expect(await screen.findByText('Battery unknown')).toBeInTheDocument()
  })

  it('accepts a custom duration inside the configured ceiling', async () => {
    route({})
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    const input = await screen.findByLabelText(/custom duration/i)
    await userEvent.type(input, '42')
    await userEvent.click(screen.getByRole('button', { name: /start timed recording/i }))
    expect(await screen.findByText(/42 minutes/)).toBeInTheDocument()
  })

  it('refuses a custom duration above the ceiling, in the interface', async () => {
    route({})
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    const input = await screen.findByLabelText(/custom duration/i)
    await userEvent.type(input, '90')
    expect(await screen.findByText(/between 1 and 60/i)).toBeInTheDocument()
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByRole('button', { name: /start timed recording/i })).toBeDisabled()
  })

  it('cannot start until a duration is chosen', async () => {
    route({})
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    await screen.findByRole('radio', { name: '5 min' })
    expect(screen.getByRole('button', { name: /start timed recording/i })).toBeDisabled()
  })
})

describe('the battery warning', () => {
  it('states the cost and the chosen duration before starting', async () => {
    route({})
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    await userEvent.click(await screen.findByRole('radio', { name: '30 min' }))
    await userEvent.click(screen.getByRole('button', { name: /start timed recording/i }))

    expect(await screen.findByText(/may flatten its battery/i)).toBeInTheDocument()
    expect(screen.getByText(/flatten the battery completely/i)).toBeInTheDocument()
    expect(screen.getByText('30 minutes')).toBeInTheDocument()
    expect(screen.getByText('62%')).toBeInTheDocument()
    expect(fetch).not.toHaveBeenCalledWith(
      expect.stringContaining('timed-recording'),
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('only starts once the cost is explicitly accepted', async () => {
    route({})
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    await userEvent.click(await screen.findByRole('radio', { name: '10 min' }))
    await userEvent.click(screen.getByRole('button', { name: /start timed recording/i }))
    await userEvent.click(
      await screen.findByRole('button', { name: /accept the battery cost and record/i }),
    )
    await waitFor(() => {
      const posted = vi
        .mocked(fetch)
        .mock.calls.find(([, init]) => init?.method === 'POST')
      expect(posted).toBeDefined()
      expect(sentJson(posted?.[1])).toEqual({ minutes: 10, accept_battery_drain: true })
    })
  })

  it('can be cancelled at the confirmation', async () => {
    route({})
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    await userEvent.click(await screen.findByRole('radio', { name: '5 min' }))
    await userEvent.click(screen.getByRole('button', { name: /start timed recording/i }))
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(await screen.findByRole('radio', { name: '5 min' })).toBeInTheDocument()
    expect(
      vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === 'POST'),
    ).toBe(false)
  })
})

describe('while it runs', () => {
  it('shows the countdown, the bytes and the connection state', async () => {
    route({ session: RUNNING })
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    expect(await screen.findByText('Timed recording in progress')).toBeInTheDocument()
    expect(screen.getByText('20:34')).toBeInTheDocument()
    expect(screen.getByText('5.0 MiB')).toBeInTheDocument()
    expect(screen.getByText('Connected')).toBeInTheDocument()
    expect(screen.getByText('30 min')).toBeInTheDocument()
  })

  it('reports a reconnect honestly instead of hiding it', async () => {
    route({
      session: {
        ...RUNNING,
        state: 'reconnecting',
        connection: 'reconnecting',
        reconnects: 2,
        message: 'The camera closed the stream; reopening it on the local network.',
      },
    })
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    expect(await screen.findByText('Reconnecting')).toBeInTheDocument()
    expect(screen.getByText('2')).toBeInTheDocument()
    expect(screen.getByText(/reopening it on the local network/i)).toBeInTheDocument()
  })

  it('offers a stop button that saves what was captured', async () => {
    route({ session: RUNNING })
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    const stop = await screen.findByRole('button', { name: /stop and save/i })
    await userEvent.click(stop)
    await waitFor(() =>
      expect(
        vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === 'DELETE'),
      ).toBe(true),
    )
  })

  it('hides the duration picker so a second session cannot be started', async () => {
    route({ session: RUNNING })
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    await screen.findByText('Timed recording in progress')
    expect(screen.queryByRole('radio', { name: '5 min' })).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /start timed recording/i }),
    ).not.toBeInTheDocument()
  })
})

describe('outcomes', () => {
  it('reports a partial recording as partial and says it was kept', async () => {
    route({
      session: {
        ...RUNNING,
        state: 'partial',
        finished: true,
        stop_reason: 'camera_unreachable',
        seconds_remaining: 0,
        segment_id: 'segment-1',
        message:
          'Camera powered off before the requested duration. 4.2 minutes of the requested 30 ' +
          'were saved and added to the timeline.',
      },
    })
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    expect(await screen.findByText(/ended early — the footage was kept/i)).toBeInTheDocument()
    expect(screen.getByText(/camera powered off before the requested duration/i)).toBeInTheDocument()
    expect(screen.getByText(/playable in sentinel and home assistant/i)).toBeInTheDocument()
  })

  it('never claims the full duration when it was not captured', async () => {
    route({
      session: {
        ...RUNNING,
        state: 'partial',
        finished: true,
        stop_reason: 'operator',
        segment_id: 'segment-1',
        message: 'Stopped by an administrator. 2.0 minutes of the requested 30 were saved.',
      },
    })
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    expect(await screen.findByText(/2.0 minutes of the requested 30/i)).toBeInTheDocument()
    expect(screen.queryByText(/recorded the requested 30 minutes/i)).not.toBeInTheDocument()
  })

  it('reports a session that captured nothing as a failure', async () => {
    route({
      session: {
        ...RUNNING,
        state: 'failed',
        finished: true,
        stop_reason: 'error',
        bytes_recorded: 0,
        segment_id: null,
        message: 'No playable video arrived from the camera.',
      },
    })
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    expect(await screen.findByText('No video was captured')).toBeInTheDocument()
    expect(screen.queryByText(/playable in sentinel/i)).not.toBeInTheDocument()
  })

  it('reports a completed session plainly', async () => {
    route({
      session: {
        ...RUNNING,
        state: 'completed',
        finished: true,
        stop_reason: 'duration_reached',
        segment_id: 'segment-1',
        message: 'Recorded the requested 30 minutes.',
      },
    })
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    expect(await screen.findByText('Timed recording finished')).toBeInTheDocument()
  })
})

describe('failures', () => {
  it('shows an actionable message when the camera rejects the credentials', async () => {
    route({
      start: json({ detail: 'The Tapo battery camera or local hub rejected the credentials.' }, 502),
    })
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    await userEvent.click(await screen.findByRole('radio', { name: '5 min' }))
    await userEvent.click(screen.getByRole('button', { name: /start timed recording/i }))
    await userEvent.click(
      await screen.findByRole('button', { name: /accept the battery cost and record/i }),
    )
    expect(await screen.findByText(/rejected the credentials/i)).toBeInTheDocument()
  })

  it('never shows a proxy error page', async () => {
    route({
      start: new Response(
        '<html><body>Error 520: The origin web server returned an invalid or incomplete ' +
          'response to Cloudflare.</body></html>',
        { status: 520, headers: { 'content-type': 'text/html' } },
      ),
    })
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    await userEvent.click(await screen.findByRole('radio', { name: '5 min' }))
    await userEvent.click(screen.getByRole('button', { name: /start timed recording/i }))
    await userEvent.click(
      await screen.findByRole('button', { name: /accept the battery cost and record/i }),
    )
    const alert = await screen.findByText(/failed before it could answer/i)
    expect(alert).toBeInTheDocument()
    expect(screen.queryByText(/cloudflare/i)).not.toBeInTheDocument()
  })

  it('reports a camera already busy with a live view', async () => {
    route({
      start: json(
        { detail: 'This camera already has a live view or recording open.' },
        409,
      ),
    })
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    await userEvent.click(await screen.findByRole('radio', { name: '5 min' }))
    await userEvent.click(screen.getByRole('button', { name: /start timed recording/i }))
    await userEvent.click(
      await screen.findByRole('button', { name: /accept the battery cost and record/i }),
    )
    expect(await screen.findByText(/already has a live view or recording open/i)).toBeInTheDocument()
  })
})

describe('a camera that will not answer', () => {
  it('offers no duration group at all, and says why', async () => {
    vi.mocked(fetch).mockImplementation((input) => {
      const url = requestedUrl(input)
      if (url.includes('/options')) {
        return Promise.resolve(
          json({ detail: 'The Tapo battery camera or local hub did not respond.' }, 503),
        )
      }
      return Promise.resolve(json(null))
    })
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)

    expect(await screen.findByText(/could not reach this camera/i)).toBeInTheDocument()
    // The regression this prevents: an empty "Duration" fieldset with no
    // presets, above a Start button that could never do anything.
    expect(screen.queryByText('Duration')).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /start timed recording/i }),
    ).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
  })

  it('still refuses to guess the battery level', async () => {
    vi.mocked(fetch).mockImplementation((input) => {
      const url = requestedUrl(input)
      if (url.includes('/options')) return Promise.resolve(json({ detail: 'nope' }, 503))
      return Promise.resolve(json(null))
    })
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open />)
    expect(await screen.findByText('Battery unknown')).toBeInTheDocument()
  })
})

describe('the closed panel', () => {
  it('contacts nothing until it is opened', () => {
    route({})
    renderWithProviders(<TimedRecordingPanel camera={batteryCamera} open={false} />)
    expect(fetch).not.toHaveBeenCalled()
  })
})
