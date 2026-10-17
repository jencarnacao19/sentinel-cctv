import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AccountsManager } from '@/features/accounts/AccountsManager'
import { renderWithProviders } from '@/test/utils'
import type { User } from '@/types/api'

function json(body: unknown, status = 200): Response {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function account(username: string, isAdmin = false): User {
  return {
    id: username,
    username,
    is_admin: isAdmin,
    is_active: true,
    last_login_at: null,
    created_at: '2026-10-17T00:00:00Z',
  }
}

function serve(users: User[]) {
  const requests: { method: string; url: string; body: unknown }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
      const method = init?.method ?? 'GET'
      const body = typeof init?.body === 'string' ? (JSON.parse(init.body) as unknown) : null
      requests.push({ method, url, body })
      if (url.endsWith('/auth/users') && method === 'GET') return Promise.resolve(json(users))
      if (url.endsWith('/auth/users') && method === 'POST') {
        const created = account((body as { username: string }).username)
        users.push(created)
        return Promise.resolve(json(created, 201))
      }
      if (method === 'DELETE') {
        users.splice(
          users.findIndex((user) => url.endsWith(`/auth/users/${user.id}`)),
          1,
        )
        return Promise.resolve(new Response(null, { status: 204 }))
      }
      return Promise.resolve(new Response(null, { status: 204 }))
    }),
  )
  return requests
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('AccountsManager', () => {
  it('lists the viewer accounts and never the administrator', async () => {
    serve([account('operator', true), account('amy')])
    renderWithProviders(<AccountsManager />)

    const list = await screen.findByRole('list', { name: 'Viewer accounts' })
    expect(within(list).getByText('amy')).toBeInTheDocument()
    expect(screen.queryByText('operator')).not.toBeInTheDocument()
  })

  it('adds an account once the two passwords match and are long enough', async () => {
    const requests = serve([account('operator', true)])
    const user = userEvent.setup()
    renderWithProviders(<AccountsManager />)

    const add = screen.getByRole('button', { name: 'Add account' })
    await user.type(screen.getByLabelText('Name'), 'tom')
    await user.type(screen.getByLabelText('Password'), 'short')
    expect(add).toBeDisabled()
    await user.type(screen.getByLabelText('Password'), '-but-now-long')
    await user.type(screen.getByLabelText('Password again'), 'short-but-now-long')
    expect(add).toBeEnabled()
    await user.click(add)

    expect(await screen.findByText('tom')).toBeInTheDocument()
    expect(requests.find((request) => request.method === 'POST')?.body).toEqual({
      username: 'tom',
      password: 'short-but-now-long',
    })
  })

  it('asks before removing an account', async () => {
    const requests = serve([account('operator', true), account('amy')])
    const user = userEvent.setup()
    renderWithProviders(<AccountsManager />)

    await user.click(await screen.findByRole('button', { name: 'Remove' }))
    expect(requests.some((request) => request.method === 'DELETE')).toBe(false)
    await user.click(screen.getByRole('button', { name: 'Remove amy' }))

    await waitFor(() =>
      expect(requests.some((request) => request.method === 'DELETE')).toBe(true),
    )
    expect(await screen.findByText('No viewer accounts yet.')).toBeInTheDocument()
  })
})
