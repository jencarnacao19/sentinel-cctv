import { useQueryClient } from '@tanstack/react-query'
import { LogIn } from 'lucide-react'
import { useId, useState } from 'react'

import { ApiError, NetworkError } from '@/api/client'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useLogin } from '@/features/auth/useAuth'

/**
 * Signing in to watch, where the camera wall would be.
 *
 * For a family viewer account, or the administrator. Home Assistant and
 * Cloudflare Access visitors never see it: they are authorised before the
 * page asks for anything.
 */
export function ViewerSignIn() {
  const queryClient = useQueryClient()
  const login = useLogin()
  const usernameId = useId()
  const passwordId = useId()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')

  const errorMessage =
    login.error instanceof NetworkError
      ? 'Could not reach the Sentinel server.'
      : login.error instanceof ApiError
        ? login.error.message
        : login.error
          ? 'Sign-in failed.'
          : null
  const canSubmit = username.trim().length > 0 && password.length > 0 && !login.isPending

  function handleSubmit(event: React.FormEvent): void {
    event.preventDefault()
    if (!canSubmit) return
    void (async () => {
      try {
        await login.mutateAsync({ username: username.trim(), password })
        setPassword('')
        await queryClient.invalidateQueries({ queryKey: ['viewer'] })
      } catch {
        /* surfaced through errorMessage */
      }
    })()
  }

  return (
    <section
      aria-labelledby="viewer-sign-in-title"
      className="mx-auto w-full max-w-sm rounded-(--radius-card) border border-(--color-border) bg-(--color-surface) p-5 sm:p-6"
    >
      <h2 id="viewer-sign-in-title" className="text-lg font-semibold">
        Sign in to watch
      </h2>
      <p className="mt-1 text-sm leading-6 text-(--color-fg-muted)">
        Use the account the administrator made for you.
      </p>
      <form onSubmit={handleSubmit} className="mt-5 space-y-4" noValidate>
        <div className="space-y-2">
          <Label htmlFor={usernameId}>Username</Label>
          <Input
            id={usernameId}
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor={passwordId}>Password</Label>
          <Input
            id={passwordId}
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
          />
        </div>
        {errorMessage ? <Alert variant="danger">{errorMessage}</Alert> : null}
        <Button type="submit" className="w-full" disabled={!canSubmit} loading={login.isPending}>
          <LogIn aria-hidden="true" />
          Sign in
        </Button>
      </form>
    </section>
  )
}
