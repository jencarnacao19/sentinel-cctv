import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { ApiError, NetworkError } from '@/api/client'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { AuthLayout } from '@/features/auth/AuthLayout'
import { useLogin } from '@/features/auth/useAuth'

export function LoginPage() {
  const navigate = useNavigate()
  const login = useLogin()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [passwordVisible, setPasswordVisible] = useState(false)

  const errorMessage =
    login.error instanceof NetworkError
      ? 'Could not reach the Sentinel server. Check that it is running.'
      : login.error instanceof ApiError
        ? login.error.message
        : login.error
          ? 'Sign-in failed.'
          : null
  const canSubmit = username.trim().length > 0 && password.length > 0

  function handleSubmit(event: React.FormEvent): void {
    event.preventDefault()
    if (!canSubmit || login.isPending) return
    void (async () => {
      try {
        await login.mutateAsync({ username: username.trim(), password })
        await navigate('/admin', { replace: true })
      } catch {
        /* surfaced through login.error below */
      }
    })()
  }

  return (
    <AuthLayout
      eyebrow="Administrator access"
      title="Sign in"
      description="Use the administrator account created for this Sentinel server."
      footer="There is no cloud account or password recovery service. Access is managed on this server."
    >
      <form onSubmit={handleSubmit} className="space-y-5" noValidate>
        <div className="space-y-2">
          <Label htmlFor="username">Username</Label>
          <Input
            id="username"
            name="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            autoFocus
            required
            aria-invalid={Boolean(errorMessage) || undefined}
          />
        </div>

        <div className="space-y-2">
          <Label htmlFor="password">Password</Label>
          <div className="relative">
            <Input
              id="password"
              name="password"
              type={passwordVisible ? 'text' : 'password'}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="current-password"
              className="pr-16"
              required
              aria-invalid={Boolean(errorMessage) || undefined}
            />
            <button
              type="button"
              aria-label={passwordVisible ? 'Hide password' : 'Show password'}
              aria-pressed={passwordVisible}
              onClick={() => setPasswordVisible((visible) => !visible)}
              className="absolute inset-y-0 right-0 min-w-14 px-3 text-xs font-medium text-(--color-fg-muted) hover:text-(--color-fg) focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-(--color-accent)"
            >
              {passwordVisible ? 'Hide' : 'Show'}
            </button>
          </div>
        </div>

        {errorMessage ? <Alert variant="danger">{errorMessage}</Alert> : null}

        <Button
          type="submit"
          className="w-full"
          loading={login.isPending}
          disabled={!canSubmit}
        >
          Sign in
        </Button>
      </form>
    </AuthLayout>
  )
}
