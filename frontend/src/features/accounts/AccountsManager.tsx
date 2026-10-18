import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Trash2, UserPlus } from 'lucide-react'
import { useState } from 'react'

import { authApi, queryKeys } from '@/api/endpoints'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { FormField } from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import type { User } from '@/types/api'
import { errorText } from '@/lib/errors'

const MIN_PASSWORD_LENGTH = 10

function passwordProblem(password: string, confirmation: string): string | null {
  if (password.length < MIN_PASSWORD_LENGTH) return `Use at least ${MIN_PASSWORD_LENGTH} characters.`
  if (password !== confirmation) return 'The two passwords are different.'
  return null
}

function lastSignIn(user: User): string {
  if (!user.last_login_at) return 'Never signed in'
  const when = new Date(user.last_login_at).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
  return `Last signed in ${when}`
}

/**
 * Viewer accounts: people who watch the cameras and recordings and change
 * nothing.
 *
 * Shown both as a setup step and on Settings, so a household is set up once
 * and adjusted later in the same place. Administrators are not listed here:
 * there is one, and it manages itself under *Password*.
 */
export function AccountsManager() {
  const queryClient = useQueryClient()
  const users = useQuery({ queryKey: queryKeys.users, queryFn: authApi.listUsers })
  const viewers = users.data?.filter((user) => !user.is_admin) ?? []

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const problem = password || confirmation ? passwordProblem(password, confirmation) : null
  const canAdd = username.trim().length >= 3 && password.length > 0 && !problem

  const create = useMutation({
    mutationFn: () => authApi.createViewerAccount(username.trim(), password),
    onSuccess: async () => {
      setUsername('')
      setPassword('')
      setConfirmation('')
      await queryClient.invalidateQueries({ queryKey: queryKeys.users })
    },
  })

  return (
    <div className="space-y-5">
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault()
          if (canAdd) create.mutate()
        }}
        noValidate
      >
        <FormField
          label="Name"
          id="viewer-account-name"
          hint="What they type to sign in. Letters, numbers, dot, underscore or hyphen."
        >
          {(field) => (
            <Input
              {...field}
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              placeholder="amy"
            />
          )}
        </FormField>
        <div className="grid gap-4 sm:grid-cols-2">
          <FormField label="Password" id="viewer-account-password" error={problem}>
            {(field) => (
              <Input
                {...field}
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="new-password"
              />
            )}
          </FormField>
          <FormField label="Password again" id="viewer-account-confirmation">
            {(field) => (
              <Input
                {...field}
                type="password"
                value={confirmation}
                onChange={(event) => setConfirmation(event.target.value)}
                autoComplete="new-password"
              />
            )}
          </FormField>
        </div>
        {errorText(create.error, 'Could not add the account.') ? (
          <Alert variant="danger">{errorText(create.error, 'Could not add the account.')}</Alert>
        ) : null}
        <Button type="submit" variant="secondary" disabled={!canAdd} loading={create.isPending}>
          <UserPlus aria-hidden="true" />
          Add account
        </Button>
      </form>

      {users.isPending ? (
        <Skeleton className="h-16" />
      ) : users.isError ? (
        <Alert variant="danger">{errorText(users.error, 'Could not load the accounts.')}</Alert>
      ) : viewers.length > 0 ? (
        <ul
          aria-label="Viewer accounts"
          className="divide-y divide-(--color-border) rounded-lg border border-(--color-border)"
        >
          {viewers.map((user) => (
            <AccountRow key={user.id} user={user} />
          ))}
        </ul>
      ) : (
        <p className="text-sm text-(--color-fg-muted)">No viewer accounts yet.</p>
      )}
    </div>
  )
}

function AccountRow({ user }: { user: User }) {
  const queryClient = useQueryClient()
  const [mode, setMode] = useState<'idle' | 'password' | 'remove'>('idle')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const problem = password || confirmation ? passwordProblem(password, confirmation) : null

  const setNewPassword = useMutation({
    mutationFn: () => authApi.setViewerPassword(user.id, password),
    onSuccess: () => {
      setPassword('')
      setConfirmation('')
      setMode('idle')
    },
  })
  const remove = useMutation({
    mutationFn: () => authApi.removeViewerAccount(user.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.users })
    },
  })
  const error =
    errorText(setNewPassword.error, 'Could not change the password.') ??
    errorText(remove.error, 'Could not remove the account.')

  return (
    <li className="space-y-3 px-4 py-3">
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">{user.username}</p>
          <p className="mt-0.5 text-xs text-(--color-fg-muted)">{lastSignIn(user)}</p>
        </div>
        {mode === 'idle' ? (
          <div className="flex flex-wrap gap-1">
            <Button variant="ghost" size="sm" onClick={() => setMode('password')}>
              <KeyRound aria-hidden="true" />
              Set password
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setMode('remove')}>
              <Trash2 aria-hidden="true" />
              Remove
            </Button>
          </div>
        ) : null}
      </div>

      {mode === 'password' ? (
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault()
            if (password && !problem) setNewPassword.mutate()
          }}
          noValidate
        >
          <div className="grid gap-3 sm:grid-cols-2">
            <FormField label={`New password for ${user.username}`} id={`password-${user.id}`} error={problem}>
              {(field) => (
                <Input
                  {...field}
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  autoComplete="new-password"
                />
              )}
            </FormField>
            <FormField label="New password again" id={`password-again-${user.id}`}>
              {(field) => (
                <Input
                  {...field}
                  type="password"
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                  autoComplete="new-password"
                />
              )}
            </FormField>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              type="submit"
              size="sm"
              disabled={!password || Boolean(problem)}
              loading={setNewPassword.isPending}
            >
              Save password
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setMode('idle')}>
              Cancel
            </Button>
          </div>
        </form>
      ) : null}

      {mode === 'remove' ? (
        <Alert variant="warning" title={`Remove ${user.username}?`}>
          <p>Every device signed in with this account stops showing the cameras at once.</p>
          <div className="mt-3 flex flex-wrap gap-2">
            <Button size="sm" variant="danger" onClick={() => remove.mutate()} loading={remove.isPending}>
              Remove {user.username}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setMode('idle')}>
              Keep
            </Button>
          </div>
        </Alert>
      ) : null}

      {error ? <Alert variant="danger">{error}</Alert> : null}
    </li>
  )
}
