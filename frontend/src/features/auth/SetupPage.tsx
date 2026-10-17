import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { ApiError, NetworkError } from '@/api/client'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { AuthLayout } from '@/features/auth/AuthLayout'
import { useCreateAdmin, useLogin } from '@/features/auth/useAuth'

const MIN_PASSWORD_LENGTH = 10

/**
 * First run, step one: create the administrator.
 *
 * Signing in straight afterwards hands over to the setup steps (storage,
 * cameras, family accounts, Home Assistant) in `SetupWizard`.
 */
export function SetupPage() {
  const navigate = useNavigate()
  const createAdmin = useCreateAdmin()
  const login = useLogin()

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')

  const passwordsMatch = password === confirmPassword
  const passwordLongEnough = password.length >= MIN_PASSWORD_LENGTH
  const canSubmit =
    username.trim().length >= 3 && passwordLongEnough && passwordsMatch && !createAdmin.isPending

  const error = createAdmin.error ?? login.error
  const errorMessage =
    error instanceof NetworkError
      ? 'Could not reach the Sentinel server.'
      : error instanceof ApiError
        ? error.message
        : error
          ? 'Could not create the administrator account.'
          : null

  function handleSubmit(event: React.FormEvent): void {
    event.preventDefault()
    if (!canSubmit) return
    void (async () => {
      try {
        await createAdmin.mutateAsync({ username: username.trim(), password })
        await login.mutateAsync({ username: username.trim(), password })
        await navigate('/setup', { replace: true })
      } catch {
        /* surfaced through errorMessage */
      }
    })()
  }

  return (
    <AuthLayout
      eyebrow="First-run setup"
      title="Welcome to Sentinel"
      description="Create the only administrator account for this server."
      footer="This account exists only on this machine. Store the password in your password manager; Sentinel cannot send a reset email."
    >
      <form onSubmit={handleSubmit} className="space-y-5" noValidate>
              <div className="space-y-2">
                <Label htmlFor="username">Username</Label>
                <Input
                  id="username"
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  autoComplete="username"
                  autoFocus
                  required
                  minLength={3}
                  pattern="[A-Za-z0-9._\-]+"
                />
                <p className="text-xs text-(--color-fg-subtle)">
                  Letters, numbers, dot, underscore or hyphen.
                </p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="password">Password</Label>
                <Input
                  id="password"
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  autoComplete="new-password"
                  required
                  aria-describedby="password-hint"
                />
                <p
                  id="password-hint"
                  className={
                    password && !passwordLongEnough
                      ? 'text-xs text-(--color-danger)'
                      : 'text-xs text-(--color-fg-subtle)'
                  }
                >
                  At least {MIN_PASSWORD_LENGTH} characters.
                </p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="confirm-password">Confirm password</Label>
                <Input
                  id="confirm-password"
                  type="password"
                  value={confirmPassword}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                  autoComplete="new-password"
                  required
                  aria-invalid={Boolean(confirmPassword) && !passwordsMatch}
                />
                {confirmPassword && !passwordsMatch ? (
                  <p className="text-xs text-(--color-danger)">Passwords do not match.</p>
                ) : null}
              </div>

        {errorMessage ? <Alert variant="danger">{errorMessage}</Alert> : null}

        <Button
          type="submit"
          className="w-full"
          loading={createAdmin.isPending || login.isPending}
          disabled={!canSubmit}
        >
          Create account
        </Button>
      </form>
    </AuthLayout>
  )
}
