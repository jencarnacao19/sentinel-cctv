import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { EventCleanupPanel } from '@/features/events/EventCleanupPanel'
import type { EventCleanupStatus, EventSummary } from '@/features/events/types'
import { renderWithProviders } from '@/test/utils'

const summary: EventSummary = {
  total: 38_000,
  protected: 12,
  with_snapshot: 5_400,
  earliest: '2026-08-23T00:00:00Z',
  latest: '2026-09-01T20:00:00Z',
  cameras: [
    {
      camera_id: 'front',
      camera_name: 'Back door',
      count: 11_000,
      earliest: '2026-08-23T00:00:00Z',
      latest: '2026-09-01T20:00:00Z',
    },
  ],
}

const idle: EventCleanupStatus = {
  phase: 'idle',
  job_id: null,
  started_at: null,
  finished_at: null,
  camera_id: null,
  start: null,
  end: null,
  min_duration_seconds: null,
  max_duration_seconds: null,
  include_protected: false,
  matched_events: 0,
  matched_evidence_files: 0,
  matched_evidence_bytes: 0,
  deleted_events: 0,
  deleted_evidence_files: 0,
  freed_bytes: 0,
  failed_evidence_files: 0,
  message: null,
  recordings_preserved: true,
  saved_exports_preserved: true,
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('EventCleanupPanel', () => {
  it('previews and explicitly confirms erasing all event metadata', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      await Promise.resolve()
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      if (url.endsWith('/events/cleanup') && (!init?.method || init.method === 'GET')) {
        return json(idle)
      }
      if (url.endsWith('/events/cleanup/preview')) {
        return json({
          event_count: 38_000,
          protected_count: 12,
          evidence_files: 10_800,
          evidence_bytes: 2_700_000_000,
          active_events_excluded: 1,
          required_confirmation: 'DELETE ALL EVENTS',
        })
      }
      if (url.endsWith('/events/cleanup') && init?.method === 'POST') {
        return json({
          ...idle,
          phase: 'running',
          job_id: 'event-cleanup-1',
          matched_events: 38_000,
          matched_evidence_files: 10_800,
          matched_evidence_bytes: 2_700_000_000,
        })
      }
      throw new Error(`Unexpected request to ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    renderWithProviders(<EventCleanupPanel summary={summary} />)
    await user.click(screen.getByRole('button', { name: /erase all event history/i }))

    expect((await screen.findAllByText(/38.000 events/)).length).toBeGreaterThan(1)
    expect(screen.getByText(/Video recordings, permanent exports/)).toBeInTheDocument()
    const confirm = screen.getByLabelText(/Type DELETE ALL EVENTS/i)
    await user.type(confirm, 'DELETE ALL EVENTS')
    await user.click(screen.getByRole('button', { name: /delete selected events/i }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/events/cleanup'),
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({
            include_protected: true,
            confirmation: 'DELETE ALL EVENTS',
          }),
        }),
      )
    })
  })
})
