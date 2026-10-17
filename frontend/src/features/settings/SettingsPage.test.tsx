import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SettingsPage } from '@/features/settings/SettingsPage'
import { renderWithProviders } from '@/test/utils'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

describe('integration token permissions', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('defaults to read only, sends the selected permission and lists existing scopes', async () => {
    const created: Array<{ name: string; scope: string }> = []
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      if (url.endsWith('/auth/tokens')) {
        if (init?.method === 'POST') {
          if (typeof init.body !== 'string') throw new Error('Expected a JSON token request')
          const body = JSON.parse(init.body) as { name: string; scope: string }
          created.push(body)
          return Promise.resolve(json({
            ...body, id: String(created.length), prefix: 'sctv_new', token: 'sctv_example',
            created_at: '2026-09-12T00:00:00Z', last_used_at: null, expires_at: null, revoked_at: null,
          }, 201))
        }
        return Promise.resolve(json([{
          id: 'existing', name: 'Existing HA', prefix: 'sctv_old', scope: 'camera_control',
          created_at: '2026-09-01T00:00:00Z', last_used_at: null, expires_at: null, revoked_at: null,
        }]))
      }
      if (url.endsWith('/auth/users')) return Promise.resolve(json([]))
      if (url.endsWith('/auth/login-blocks')) return Promise.resolve(json([]))
      if (url.endsWith('/auth/login-security')) return Promise.resolve(json({
        enabled: false, failure_threshold: 1, block_duration_seconds: null,
        public_hostnames: [], trusted_proxy_cidrs: [], proxy_ready: false, blocked_count: 0,
      }))
      return Promise.resolve(json({ sounds_enabled: false }))
    }))
    const user = userEvent.setup()
    renderWithProviders(<SettingsPage />)
    const existing = await screen.findByText('Existing HA')
    expect(within(existing.closest('li')!).getByText('Camera control')).toBeInTheDocument()
    expect(screen.getByLabelText('Permissions')).toHaveValue('read_only')
    await user.click(screen.getByRole('button', { name: 'Create token' }))
    await waitFor(() => expect(created[0]).toEqual({ name: 'Home Assistant', scope: 'read_only' }))
    await user.selectOptions(screen.getByLabelText('Permissions'), 'camera_control')
    await user.click(screen.getByRole('button', { name: 'Create token' }))
    await waitFor(() => expect(created[1]).toEqual({ name: 'Home Assistant', scope: 'camera_control' }))
  })
})

describe('public login protection settings', () => {
  afterEach(() => vi.restoreAllMocks())

  it('configures the threshold and unblocks an address', async () => {
    let removed = false
    const requests: Array<{ url: string; method: string; body: unknown }> = []
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
        const method = init?.method ?? 'GET'
        const body = typeof init?.body === 'string' ? JSON.parse(init.body) : null
        requests.push({ url, method, body })

        if (url.endsWith('/auth/tokens')) return json([])
        if (url.endsWith('/auth/users')) return json([])
        if (url.endsWith('/auth/login-security') && method === 'PATCH') {
          return json({
            ...body,
            public_hostnames: ['cctv.example.com'],
            trusted_proxy_cidrs: ['192.168.50.205/32'],
            proxy_ready: true,
            blocked_count: removed ? 0 : 1,
          })
        }
        if (url.endsWith('/auth/login-security')) {
          return json({
            enabled: true,
            failure_threshold: 1,
            block_duration_seconds: null,
            public_hostnames: ['cctv.example.com'],
            trusted_proxy_cidrs: ['192.168.50.205/32'],
            proxy_ready: true,
            blocked_count: removed ? 0 : 1,
          })
        }
        if (url.endsWith('/auth/login-blocks')) {
          return json(
            removed
              ? []
              : [
                  {
                    ip_address: '203.0.113.44',
                    public_hostname: 'cctv.example.com',
                    failed_attempts: 1,
                    first_failed_at: '2026-09-02T00:00:00Z',
                    last_failed_at: '2026-09-02T00:00:00Z',
                    blocked_at: '2026-09-02T00:00:00Z',
                    blocked_until: null,
                  },
                ],
          )
        }
        if (url.endsWith('/auth/login-blocks/203.0.113.44') && method === 'DELETE') {
          removed = true
          return new Response(null, { status: 204 })
        }
        throw new Error(`Unexpected request: ${method} ${url}`)
      }),
    )

    const user = userEvent.setup()
    renderWithProviders(<SettingsPage />)

    expect(await screen.findByText('203.0.113.44')).toBeInTheDocument()
    expect(screen.getByText('Permanent')).toBeInTheDocument()
    expect(screen.getByText('192.168.50.205/32')).toBeInTheDocument()

    const threshold = screen.getByLabelText('Failed attempts before block')
    await user.clear(threshold)
    await user.type(threshold, '3')
    await user.selectOptions(screen.getByLabelText('Block duration'), '86400')
    await user.click(screen.getByRole('button', { name: 'Save protection' }))

    await waitFor(() =>
      expect(
        requests.some(
          (request) =>
            request.method === 'PATCH' &&
            JSON.stringify(request.body) ===
              JSON.stringify({
                enabled: true,
                failure_threshold: 3,
                block_duration_seconds: 86400,
              }),
        ),
      ).toBe(true),
    )

    await user.click(screen.getByRole('button', { name: 'Unblock' }))
    await waitFor(() => expect(screen.queryByText('203.0.113.44')).not.toBeInTheDocument())
    expect(screen.getByText('No blocked addresses.')).toBeInTheDocument()
  })
})
