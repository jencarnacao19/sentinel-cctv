import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AutomationsPage } from '@/features/automations/AutomationsPage'
import { makeCamera } from '@/test/cameras'
import { renderWithProviders } from '@/test/utils'
import type { AutomationsState } from '@/types/api'

const TARGETS: NonNullable<AutomationsState['targets']> = {
  allowed: true,
  automations_file: true,
  notify: [{ id: 'mobile_app_phone', name: 'mobile app phone' }],
  lights: [{ id: 'light.porch', name: 'Porch' }],
  people: [],
  alarms: [{ id: 'alarm_control_panel.alarmo', name: 'Alarmo' }],
  reported_at: '2026-10-31T12:00:00Z',
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } })
}

function serve(state: AutomationsState) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = input instanceof Request ? input.url : input instanceof URL ? input.href : input
    if (url.endsWith('/automations') && init?.method === 'PUT') {
      const { recipes } = JSON.parse(init.body as string) as Pick<AutomationsState, 'recipes'>
      return Promise.resolve(json({ ...state, recipes }))
    }
    if (url.endsWith('/automations')) return Promise.resolve(json(state))
    if (url.includes('/motion-zones')) return Promise.resolve(json({ zones: [] }))
    return Promise.resolve(json([makeCamera({ id: 'gate', name: 'Gate' })]))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function sentRecipes(fetchMock: ReturnType<typeof serve>) {
  const call = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT')
  return (JSON.parse(call?.[1]?.body as string) as Pick<AutomationsState, 'recipes'>).recipes
}

afterEach(() => vi.unstubAllGlobals())

describe('AutomationsPage', () => {
  it('says where to allow it while Home Assistant has not', async () => {
    serve({ recipes: [], targets: { ...TARGETS, allowed: false }, status: {} })
    renderWithProviders(<AutomationsPage />)
    expect(await screen.findByText('Home Assistant has not allowed this yet')).toBeInTheDocument()
  })

  it('creates a motion alert for the chosen camera and phone', async () => {
    const fetchMock = serve({ recipes: [], targets: TARGETS, status: {} })
    renderWithProviders(<AutomationsPage />)

    await userEvent.type(await screen.findByLabelText('Name'), 'Gate at night')
    await userEvent.selectOptions(await screen.findByLabelText('Camera'), 'gate')
    await userEvent.selectOptions(screen.getByLabelText('When'), 'night')
    await userEvent.click(screen.getByRole('checkbox', { name: 'mobile app phone' }))
    await userEvent.click(screen.getByRole('button', { name: 'Create in Home Assistant' }))

    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(true))
    expect(sentRecipes(fetchMock)[0]).toMatchObject({
      kind: 'motion_alert',
      name: 'Gate at night',
      camera_id: 'gate',
      zone_id: null,
      when: 'night',
      notify: ['mobile_app_phone'],
      cooldown_minutes: 5,
    })
  })

  it('gives an automation edited in Home Assistant back to Sentinel', async () => {
    const recipe = {
      id: 'a1b2c3d4',
      kind: 'presence_recording' as const,
      name: 'Indoor',
      camera_id: null,
      zone_id: null,
      camera_ids: ['gate'],
      notify: [],
      lights: [],
      when: 'always' as const,
      alarm_entity: null,
      image: true,
      cooldown_minutes: 5,
      minutes: 5,
      trigger_alarm: false,
      storage_below_gb: 20,
      message: '',
      reclaim: 0,
    }
    const fetchMock = serve({
      recipes: [recipe],
      targets: TARGETS,
      status: { a1b2c3d4: { state: 'edited_in_ha', message: '' } },
    })
    renderWithProviders(<AutomationsPage />)

    await userEvent.click(await screen.findByRole('button', { name: 'Give back to Sentinel' }))
    await waitFor(() => expect(sentRecipes(fetchMock)[0].reclaim).toBe(1))
  })
})
