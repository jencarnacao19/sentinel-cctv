import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { StoragePage } from '@/features/storage/StoragePage'
import { renderWithProviders } from '@/test/utils'

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('StoragePage', () => {
  it('shows measured storage and requires preview and confirmation before saving a tighter policy', async () => {
    const user = userEvent.setup()
    const writes: Record<string, unknown>[] = []
    const previews: Record<string, unknown>[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
        if (url.endsWith('/storage/settings/preview')) {
          const proposal = JSON.parse(typeof init?.body === 'string' ? init.body : '{}') as Record<string, unknown>
          previews.push(proposal)
          return Promise.resolve(json({
            proposed_settings: { path: '/recordings', max_bytes: null, ...proposal },
            preview_token: 'review-1', required_confirmation: 'APPLY RETENTION POLICY',
            expires_in_seconds: 300, segment_count: 2, estimated_bytes: 6_000_000_000,
            expired_segment_count: 0, expired_event_count: 0, protected_count: 2,
            protected_bytes: 120_000_000, active_or_recent_count: 8, free_bytes: 4_000_000_000,
            reserve_shortfall_bytes: 17_474_836_480, estimated_free_bytes_after: 10_000_000_000,
            remaining_shortfall_bytes: 11_474_836_480, recording_rate_bytes_per_hour: 4_000_000_000,
            rate_window_hours: 6, estimated_retention_days: 3.6, estimated_days_remaining: 0,
            oldest_affected_recording: '2026-08-23T12:00:00Z', newest_affected_recording: '2026-08-23T12:02:00Z',
            saved_clips_preserved: true, warnings: ['Estimate from indexed file sizes.'],
          }))
        }
        if (url.endsWith('/storage/settings')) {
          if (init?.method === 'PATCH') {
            const body = JSON.parse(typeof init.body === 'string' ? init.body : '{}') as Record<string, unknown>
            writes.push(body)
            if (writes.length === 1) return Promise.resolve(new Response(
              JSON.stringify({ detail: 'Review expired; preview again.' }),
              { status: 409, headers: { 'content-type': 'application/json' } },
            ))
            return Promise.resolve(json(body))
          }
          return Promise.resolve(
            json({
              path: '/recordings',
              retention_days: 14,
              max_bytes: null,
              min_free_bytes: 10_000_000_000,
            }),
          )
        }
        if (url.endsWith('/storage/cleanup')) {
          return Promise.resolve(
            json({
              phase: 'idle',
              job_id: null,
              started_at: null,
              finished_at: null,
              camera_id: null,
              before: null,
              include_protected: false,
              matched_segments: 0,
              matched_bytes: 0,
              deleted_segments: 0,
              freed_bytes: 0,
              failed_segments: 0,
              message: null,
              saved_clips_preserved: true,
            }),
          )
        }
        if (url.endsWith('/storage')) {
          return Promise.resolve(
            json({
              path: '/recordings',
              available: true,
              reason: null,
              total_bytes: 400_000_000_000,
              used_bytes: 100_000_000_000,
              free_bytes: 300_000_000_000,
              recordings_bytes: 80_000_000_000,
              snapshots_bytes: 1_000_000_000,
              database_bytes: 500_000_000,
              segment_count: 1234,
              protected_count: 2,
              protected_bytes: 120_000_000,
              saved_clips_count: 3,
              saved_clips_bytes: 2_000_000_000,
              damaged_count: 4,
              oldest_recording: '2026-08-23T12:00:00Z',
              newest_recording: '2026-08-24T12:00:00Z',
              recording_rate_bytes_per_hour: 4_000_000_000,
              rate_window_hours: 6,
              estimated_retention_days: 3.8,
              estimated_days_remaining: 2.9,
              configured_retention_days: 14,
              camera_usage: [
                {
                  camera_id: 'front-door',
                  camera_name: 'Front door',
                  bytes: 55_000_000_000,
                  segment_count: 850,
                  protected_bytes: 120_000_000,
                  oldest_recording: '2026-08-23T12:00:00Z',
                  newest_recording: '2026-08-24T12:00:00Z',
                  recording_rate_bytes_per_hour: 2_500_000_000,
                },
              ],
            }),
          )
        }
        throw new Error(`Unexpected request: ${url}`)
      }),
    )

    renderWithProviders(<StoragePage />)

    expect(await screen.findByText('300.0 GB')).toBeInTheDocument()
    expect(screen.getByText('25% used')).toBeInTheDocument()
    expect(screen.getByText('3.8 days')).toBeInTheDocument()
    expect(screen.getByText('4.0 GB/h')).toBeInTheDocument()
    expect(screen.getByText('3 permanent exports')).toBeInTheDocument()
    expect(screen.getByText('Front door')).toBeInTheDocument()
    expect(screen.getByText('68.8% of recordings')).toBeInTheDocument()
    expect(screen.getByText('/recordings')).toBeInTheDocument()
    expect(screen.getByText('Manage recordings')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /erase all recording history/i })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Preview changes' }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(previews[0].min_free_bytes).toBe(10_000_000_000) // No silent GB/GiB rounding change.
    expect(writes).toEqual([])
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(writes).toEqual([])

    await user.click(screen.getByRole('button', { name: 'Review 20 GiB reserve' }))
    expect(writes).toEqual([])
    await user.click(screen.getByRole('button', { name: 'Preview changes' }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(previews[1].min_free_bytes).toBe(20 * 1024 ** 3)
    expect(screen.getByText(/2 protected segments · 8 active\/recent segments · all saved exports/)).toBeInTheDocument()
    const confirm = screen.getByRole('button', { name: 'Confirm and save policy' })
    expect(confirm).toBeDisabled()
    await user.type(screen.getByLabelText(/Type APPLY RETENTION POLICY/), 'APPLY RETENTION POLICY')
    await user.click(confirm)
    await waitFor(() => expect(writes).toHaveLength(1))
    expect(writes[0]).toMatchObject({
      min_free_bytes: 20 * 1024 ** 3, preview_token: 'review-1', confirmation: 'APPLY RETENTION POLICY',
    })
    expect(await screen.findByText(/Review expired; preview again/)).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.click(screen.getByRole('button', { name: 'Preview changes' }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(writes).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Confirm and save policy' })).toBeDisabled()
    await user.type(screen.getByLabelText(/Type APPLY RETENTION POLICY/), 'APPLY RETENTION POLICY')
    await user.click(screen.getByRole('button', { name: 'Confirm and save policy' }))
    expect(await screen.findByText(/Retention settings saved/)).toBeInTheDocument()
    expect(writes).toHaveLength(2)
  })
})
