import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SetupWizard } from '@/features/setup/SetupWizard'
import { makeCamera } from '@/test/cameras'
import { renderWithProviders } from '@/test/utils'

const navigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => navigate }
})

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

const USAGE = {
  path: '/recordings',
  available: true,
  reason: null,
  total_bytes: 2_000_000_000_000,
  used_bytes: 100_000_000_000,
  free_bytes: 1_900_000_000_000,
  shares_system_disk: false,
}

interface Server {
  cameras: unknown[]
  usage: typeof USAGE
  requests: { method: string; url: string; body: unknown }[]
}

function serve(server: Server) {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      const method = init?.method ?? 'GET'
      const body = typeof init?.body === 'string' ? (JSON.parse(init.body) as unknown) : null
      server.requests.push({ method, url, body })
      const path = url.replace(/^\/api\/v1/, '').split('?')[0]
      if (path === '/storage') return Promise.resolve(json(server.usage))
      if (path === '/storage/settings' && method === 'GET') {
        return Promise.resolve(
          json({ path: null, retention_days: 14, max_bytes: null, min_free_bytes: 5_368_709_120 }),
        )
      }
      if (path === '/storage/settings/preview') {
        return Promise.resolve(
          json({ preview_token: 'review', required_confirmation: null, expired_segment_count: 0 }),
        )
      }
      if (path === '/storage/settings' && method === 'PATCH') return Promise.resolve(json({}))
      if (path === '/cameras') return Promise.resolve(json(server.cameras))
      if (path === '/auth/users') return Promise.resolve(json([]))
      if (path === '/auth/tokens' && method === 'POST') {
        return Promise.resolve(
          json(
            {
              id: 'ha',
              name: 'Home Assistant',
              scope: 'camera_control',
              prefix: 'sctv_ha',
              token: 'sctv_home_assistant_token',
              created_at: '2026-10-17T00:00:00Z',
              last_used_at: null,
              expires_at: null,
              revoked_at: null,
            },
            201,
          ),
        )
      }
      if (path === '/system/setup/complete') return Promise.resolve(json({ setup_completed: true }))
      return Promise.resolve(json({ detail: `No route for ${method} ${path}` }, 404))
    }),
  )
}

function server(overrides: Partial<Server> = {}): Server {
  const state: Server = { cameras: [], usage: USAGE, requests: [], ...overrides }
  serve(state)
  return state
}

async function next(user: ReturnType<typeof userEvent.setup>, name = 'Continue') {
  await user.click(screen.getByRole('button', { name }))
}

beforeEach(() => {
  window.localStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  navigate.mockReset()
})

describe('SetupWizard', () => {
  it('walks a new installation to the camera wall', async () => {
    const state = server({ cameras: [makeCamera({ name: 'Front door', status: 'online' })] })
    const user = userEvent.setup()
    renderWithProviders(<SetupWizard />)

    expect(
      screen.getByRole('heading', { name: 'Coming from another Sentinel?' }),
    ).toBeInTheDocument()
    await next(user, 'Skip')

    expect(await screen.findByRole('heading', { name: 'Where recordings go' })).toBeInTheDocument()
    expect(await screen.findByText(/free of/)).toBeInTheDocument()
    await user.click(screen.getByRole('radio', { name: '30 days' }))
    await next(user)

    expect(await screen.findByRole('heading', { name: 'Add your cameras' })).toBeInTheDocument()
    expect(
      within(await screen.findByRole('list', { name: 'Cameras added' })).getByText('Front door'),
    ).toBeInTheDocument()
    await next(user)

    expect(screen.getByRole('heading', { name: 'Accounts for the family' })).toBeInTheDocument()
    await next(user)

    expect(screen.getByRole('heading', { name: 'Connect Home Assistant' })).toBeInTheDocument()
    await next(user, 'Skip')

    expect(screen.getByRole('heading', { name: 'Sentinel is ready' })).toBeInTheDocument()
    await next(user, 'Open the cameras')

    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/', { replace: true }))
    const calls = state.requests.map((request) => `${request.method} ${request.url}`)
    expect(calls).toContain('POST /api/v1/storage/settings/preview')
    expect(calls).toContain('POST /api/v1/system/setup/complete')
    const saved = state.requests.find(
      (request) => request.method === 'PATCH' && request.url.endsWith('/storage/settings'),
    )
    expect(saved?.body).toMatchObject({ retention_days: 30, preview_token: 'review' })
  })

  it('leaves the retention alone when it was not changed', async () => {
    const state = server()
    const user = userEvent.setup()
    renderWithProviders(<SetupWizard />)

    await next(user, 'Skip')
    await screen.findByText(/free of/)
    await next(user)
    await screen.findByRole('heading', { name: 'Add your cameras' })

    expect(state.requests.some((request) => request.method === 'PATCH')).toBe(false)
  })

  it('warns when recordings would fill the system disk', async () => {
    server({ usage: { ...USAGE, shares_system_disk: true } })
    const user = userEvent.setup()
    renderWithProviders(<SetupWizard />)

    await next(user, 'Skip')
    expect(await screen.findByText('Recordings are on the system disk')).toBeInTheDocument()
  })

  it('does not move past the cameras until there is one', async () => {
    window.localStorage.setItem('sentinel.setup-step', 'cameras')
    server()
    renderWithProviders(<SetupWizard />)

    expect(await screen.findByText(/No cameras yet/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled()
  })

  it('creates the Home Assistant token on request and shows it once', async () => {
    window.localStorage.setItem('sentinel.setup-step', 'home-assistant')
    const state = server()
    const user = userEvent.setup()
    renderWithProviders(<SetupWizard />)

    await user.click(screen.getByRole('button', { name: 'Create token' }))
    expect(await screen.findByText('sctv_home_assistant_token')).toBeInTheDocument()
    const created = state.requests.find(
      (request) => request.method === 'POST' && request.url.endsWith('/auth/tokens'),
    )
    expect(created?.body).toEqual({ name: 'Home Assistant', scope: 'camera_control' })
  })

  it('keeps the same token when the step is left and opened again', async () => {
    window.localStorage.setItem('sentinel.setup-step', 'home-assistant')
    const state = server()
    const user = userEvent.setup()
    renderWithProviders(<SetupWizard />)

    await user.click(screen.getByRole('button', { name: 'Create token' }))
    expect(await screen.findByText('sctv_home_assistant_token')).toBeInTheDocument()
    await next(user, 'Back')
    await next(user)

    expect(await screen.findByText('sctv_home_assistant_token')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create token' })).not.toBeInTheDocument()
    const creations = state.requests.filter(
      (request) => request.method === 'POST' && request.url.endsWith('/auth/tokens'),
    )
    expect(creations).toHaveLength(1)
  })
})
