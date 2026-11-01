import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AutoRecordingPanel } from '@/features/cameras/AutoRecordingPanel'
import { batteryCamera } from '@/test/cameras'
import { requestedUrl, sentJson } from '@/test/requests'
import { renderWithProviders } from '@/test/utils'
import type { TapoAutoRecording } from '@/types/api'

const RULES: TapoAutoRecording = {
  on_wake: true,
  on_wake_minutes: 2,
  on_motion: false,
  on_motion_minutes: 2,
  on_wake_supported: true,
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function route(rules: TapoAutoRecording = RULES) {
  vi.mocked(fetch).mockImplementation((input, init) => {
    const url = requestedUrl(input)
    if (url.includes('/options')) {
      return Promise.resolve(
        json({
          presets_minutes: [1, 2, 5, 10],
          max_minutes: 10,
          hard_max_minutes: 120,
          min_minutes: 1,
          battery_percent: null,
          warning: '',
        }),
      )
    }
    if (init?.method === 'PUT') {
      return Promise.resolve(json({ ...rules, ...(sentJson(init) as object) }))
    }
    return Promise.resolve(json(rules))
  })
}

function sentRules() {
  const call = vi.mocked(fetch).mock.calls.find(([, init]) => init?.method === 'PUT')
  return call ? sentJson(call[1]) : null
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn())
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('automatic recording', () => {
  it('shows the saved rules', async () => {
    route()
    renderWithProviders(<AutoRecordingPanel camera={batteryCamera} open />)
    expect(await screen.findByRole('switch', { name: 'When it wakes up by itself' })).toBeChecked()
    expect(screen.getByRole('switch', { name: 'When another camera sees motion' })).not.toBeChecked()
  })

  it('saves a switch at once, keeping the other rule', async () => {
    route()
    const user = userEvent.setup()
    renderWithProviders(<AutoRecordingPanel camera={batteryCamera} open />)
    await user.click(await screen.findByRole('switch', { name: 'When another camera sees motion' }))
    await waitFor(() =>
      expect(sentRules()).toEqual({
        on_wake: true,
        on_wake_minutes: 2,
        on_motion: true,
        on_motion_minutes: 2,
      }),
    )
    expect(requestedUrl(vi.mocked(fetch).mock.calls.at(-1)![0])).toContain(
      `/cameras/${batteryCamera.id}/tapo-battery/auto-recording`,
    )
  })

  it('saves a new duration from the allowed presets', async () => {
    route()
    const user = userEvent.setup()
    renderWithProviders(<AutoRecordingPanel camera={batteryCamera} open />)
    await screen.findByRole('switch', { name: 'When it wakes up by itself' })
    const [wakeMinutes] = await screen.findAllByRole('combobox', { name: 'Record for' })
    await waitFor(() => expect(wakeMinutes.querySelectorAll('option')).toHaveLength(4))
    await user.selectOptions(wakeMinutes, '1')
    await waitFor(() => expect(sentRules()).toMatchObject({ on_wake_minutes: 1 }))
  })

  it('cannot watch for wakes behind a hub', async () => {
    route({ ...RULES, on_wake: false, on_wake_supported: false })
    renderWithProviders(<AutoRecordingPanel camera={batteryCamera} open />)
    expect(await screen.findByRole('switch', { name: 'When it wakes up by itself' })).toBeDisabled()
    expect(screen.getByText(/hub is always awake/)).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'When another camera sees motion' })).toBeEnabled()
  })
})
