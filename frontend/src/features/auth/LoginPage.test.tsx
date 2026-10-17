import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { LoginPage } from '@/features/auth/LoginPage'
import { renderWithProviders } from '@/test/utils'

const navigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => navigate }
})

function stubFetch(status: number, body: unknown) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify(body), {
          status,
          headers: { 'content-type': 'application/json' },
        }),
      ),
    ),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
  navigate.mockReset()
})

describe('LoginPage', () => {
  it('renders the sign-in form', () => {
    renderWithProviders(<LoginPage />)
    expect(screen.getByLabelText('Username')).toBeInTheDocument()
    expect(screen.getByLabelText('Password')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('keeps the submit button disabled until both fields are filled', async () => {
    const user = userEvent.setup()
    renderWithProviders(<LoginPage />)
    const submit = screen.getByRole('button', { name: 'Sign in' })
    expect(submit).toBeDisabled()

    await user.type(screen.getByLabelText('Username'), 'operator')
    expect(submit).toBeDisabled()

    await user.type(screen.getByLabelText('Password'), 'secret-password')
    expect(submit).toBeEnabled()
  })

  it('shows and hides the password without changing it', async () => {
    const user = userEvent.setup()
    renderWithProviders(<LoginPage />)
    const password = screen.getByLabelText('Password')

    await user.type(password, 'secret-password')
    expect(password).toHaveAttribute('type', 'password')

    await user.click(screen.getByRole('button', { name: 'Show password' }))
    expect(password).toHaveAttribute('type', 'text')
    expect(password).toHaveValue('secret-password')

    await user.click(screen.getByRole('button', { name: 'Hide password' }))
    expect(password).toHaveAttribute('type', 'password')
  })

  it('navigates to the dashboard after a successful sign-in', async () => {
    stubFetch(200, {
      access_token: 'token',
      token_type: 'bearer',
      expires_at: '2026-08-15T00:00:00Z',
      is_admin: true,
    })
    const user = userEvent.setup()
    renderWithProviders(<LoginPage />)

    await user.type(screen.getByLabelText('Username'), 'operator')
    await user.type(screen.getByLabelText('Password'), 'secret-password')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/admin', { replace: true }))
  })

  it('sends a viewer account to the camera wall, not to administration', async () => {
    stubFetch(200, {
      access_token: 'token',
      token_type: 'bearer',
      expires_at: '2026-08-15T00:00:00Z',
      is_admin: false,
    })
    const user = userEvent.setup()
    renderWithProviders(<LoginPage />)

    await user.type(screen.getByLabelText('Username'), 'amy')
    await user.type(screen.getByLabelText('Password'), 'family-password-1')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/', { replace: true }))
  })

  it('shows the server message when credentials are wrong', async () => {
    stubFetch(401, { detail: 'Incorrect username or password.' })
    const user = userEvent.setup()
    renderWithProviders(<LoginPage />)

    await user.type(screen.getByLabelText('Username'), 'operator')
    await user.type(screen.getByLabelText('Password'), 'wrong')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Incorrect username or password.',
    )
    expect(navigate).not.toHaveBeenCalled()
  })

  it('explains an unreachable server rather than failing silently', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))))
    const user = userEvent.setup()
    renderWithProviders(<LoginPage />)

    await user.type(screen.getByLabelText('Username'), 'operator')
    await user.type(screen.getByLabelText('Password'), 'secret-password')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/could not reach/i)
  })
})
