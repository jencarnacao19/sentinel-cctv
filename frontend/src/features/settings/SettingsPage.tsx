import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Copy, Download, KeyRound, ShieldCheck, Trash2, Unlock, Upload } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { ApiError, API_BASE } from '@/api/client'
import { AccountsManager } from '@/features/accounts/AccountsManager'
import { SoundPolicyCard } from '@/features/appearance/SoundPolicyCard'
import { BuildChannelCard } from '@/features/settings/BuildChannelCard'
import { MoveMachineCard } from '@/features/settings/MoveMachineCard'
import { authApi, queryKeys } from '@/api/endpoints'
import { PageHeader } from '@/components/layout/PageHeader'
import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import type { ApiToken, ApiTokenScope, CreatedApiToken, LoginBlock } from '@/types/api'
import { errorText } from '@/lib/errors'

const tokenScopeLabels: Record<ApiTokenScope, string> = {
  read_only: 'Read only',
  camera_control: 'Camera control',
}

function formatDate(iso: string | null): string {
  if (!iso) return 'never'
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

/**
 * The plaintext token, shown once and never again.
 *
 * Sentinel stores only a hash, so this really is the single opportunity to copy
 * it — the panel says so rather than letting someone close it and find out.
 */
function NewTokenPanel({ token, onDismiss }: { token: CreatedApiToken; onDismiss: () => void }) {
  const [copied, setCopied] = useState(false)

  function copy(): void {
    void navigator.clipboard
      .writeText(token.token)
      .then(() => {
        setCopied(true)
        setTimeout(() => setCopied(false), 2000)
      })
      .catch(() => undefined)
  }

  return (
    <Alert variant="warning" title={`Token for “${token.name}” — copy it now`}>
      <p>This is the only time it will be shown. Sentinel stores only a hash of it.</p>
      <p className="mt-1">Permissions: {tokenScopeLabels[token.scope]}.</p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <code className="min-w-0 flex-1 truncate rounded-lg bg-(--color-bg-subtle) px-3 py-2 font-mono text-xs">
          {token.token}
        </code>
        <Button size="sm" variant="secondary" onClick={copy}>
          {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
          {copied ? 'Copied' : 'Copy'}
        </Button>
        <Button size="sm" variant="ghost" onClick={onDismiss}>
          Done
        </Button>
      </div>
    </Alert>
  )
}

function ViewerAccountsCard() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Viewer accounts</CardTitle>
        <CardDescription>
          For the people who watch: they sign in at this server&apos;s address to see the cameras,
          recordings and saved clips, and cannot change anything.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <AccountsManager />
      </CardContent>
    </Card>
  )
}

function ApiTokensCard() {
  const queryClient = useQueryClient()
  const [name, setName] = useState('Home Assistant')
  const [scope, setScope] = useState<ApiTokenScope>('read_only')
  const [created, setCreated] = useState<CreatedApiToken | null>(null)

  const tokens = useQuery({ queryKey: queryKeys.tokens, queryFn: authApi.listTokens })

  const createToken = useMutation({
    mutationFn: ({ tokenName, tokenScope }: { tokenName: string; tokenScope: ApiTokenScope }) =>
      authApi.createToken(tokenName, tokenScope),
    onSuccess: async (token) => {
      setCreated(token)
      await queryClient.invalidateQueries({ queryKey: queryKeys.tokens })
    },
  })

  const revokeToken = useMutation({
    mutationFn: (id: string) => authApi.revokeToken(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.tokens })
    },
  })

  const active = (token: ApiToken) => !token.revoked_at

  return (
    <Card>
      <CardHeader>
        <CardTitle>API tokens</CardTitle>
        <CardDescription>
          Home Assistant authenticates with one of these. Create one token per client so it
          can be revoked on its own.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {created ? <NewTokenPanel token={created} onDismiss={() => setCreated(null)} /> : null}

        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (name.trim()) createToken.mutate({ tokenName: name.trim(), tokenScope: scope })
          }}
        >
          <div className="min-w-48 flex-1 space-y-2">
            <Label htmlFor="token-name">Name</Label>
            <Input
              id="token-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Home Assistant"
              required
            />
          </div>
          <div className="min-w-48 flex-1 space-y-2">
            <Label htmlFor="token-scope">Permissions</Label>
            <Select
              id="token-scope"
              aria-describedby="token-permissions-help"
              value={scope}
              onChange={(event) => setScope(event.target.value as ApiTokenScope)}
            >
              <option value="read_only">Read only</option>
              <option value="camera_control">Camera control</option>
            </Select>
          </div>
          <Button
            type="submit"
            className="w-full sm:w-auto"
            loading={createToken.isPending}
            disabled={!name.trim()}
          >
            <KeyRound aria-hidden="true" />
            Create token
          </Button>
        </form>

        <p id="token-permissions-help" className="text-xs text-(--color-fg-muted)">
          Read only allows live video, playback and status. Choose Camera control for Home
          Assistant recording switches, PTZ, privacy and local camera controls. Tokens cannot
          manage security, storage, backups or other tokens, or delete footage.
          Existing integration tokens retain Camera control.
        </p>

        {errorText(createToken.error, 'Could not create the token.') ? (
          <Alert variant="danger">
            {errorText(createToken.error, 'Could not create the token.')}
          </Alert>
        ) : null}

        {tokens.isPending ? (
          <Skeleton className="h-20" />
        ) : tokens.data && tokens.data.length > 0 ? (
          <ul className="divide-y divide-(--color-border) rounded-lg border border-(--color-border)">
            {tokens.data.map((token) => (
              <li key={token.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                <div className="min-w-0 flex-1">
                  <p className="flex items-center gap-2 truncate text-sm font-medium">
                    {token.name}
                    <Badge>{tokenScopeLabels[token.scope]}</Badge>
                    {active(token) ? null : <Badge variant="danger">revoked</Badge>}
                  </p>
                  <p className="tabular mt-0.5 truncate text-xs text-(--color-fg-muted)">
                    <span className="font-mono">{token.prefix}…</span> · created{' '}
                    {formatDate(token.created_at)} · last used {formatDate(token.last_used_at)}
                  </p>
                </div>
                {active(token) ? (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => revokeToken.mutate(token.id)}
                    loading={revokeToken.isPending && revokeToken.variables === token.id}
                  >
                    <Trash2 aria-hidden="true" />
                    Revoke
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-(--color-fg-muted)">No tokens yet.</p>
        )}
      </CardContent>
    </Card>
  )
}

function describeBlock(block: LoginBlock): string {
  if (!block.blocked_until) return 'Permanent'
  return `Until ${formatDate(block.blocked_until)}`
}

function LoginSecurityCard() {
  const queryClient = useQueryClient()
  const security = useQuery({
    queryKey: queryKeys.loginSecurity,
    queryFn: authApi.loginSecurity,
  })
  const blocks = useQuery({
    queryKey: queryKeys.loginBlocks,
    queryFn: authApi.listLoginBlocks,
  })
  const [enabled, setEnabled] = useState(false)
  const [threshold, setThreshold] = useState('1')
  const [duration, setDuration] = useState<string>('permanent')

  useEffect(() => {
    if (!security.data) return
    setEnabled(security.data.enabled)
    setThreshold(String(security.data.failure_threshold))
    setDuration(
      security.data.block_duration_seconds === null
        ? 'permanent'
        : String(security.data.block_duration_seconds),
    )
  }, [security.data])

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.loginSecurity }),
      queryClient.invalidateQueries({ queryKey: queryKeys.loginBlocks }),
    ])
  }

  const save = useMutation({
    mutationFn: () =>
      authApi.updateLoginSecurity({
        enabled,
        failure_threshold: Math.min(20, Math.max(1, Number(threshold) || 1)),
        block_duration_seconds: duration === 'permanent' ? null : Number(duration),
      }),
    onSuccess: refresh,
  })

  const unblock = useMutation({
    mutationFn: (address: string) => authApi.removeLoginBlock(address),
    onSuccess: refresh,
  })

  const clearAll = useMutation({
    mutationFn: authApi.clearLoginBlocks,
    onSuccess: refresh,
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Public login protection</CardTitle>
        <CardDescription>
          Block an internet address after failed administrator sign-ins. Camera viewing,
          recording and direct LAN login are not blocked.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        {security.isPending ? (
          <Skeleton className="h-44" />
        ) : security.data ? (
          <>
            {!security.data.proxy_ready ? (
              <Alert variant="warning" title="Trusted proxy setup required">
                Sentinel will not enable this protection until the public hostname and the
                Cloudflare tunnel network are configured on the server.
              </Alert>
            ) : null}

            <form
              className="space-y-4"
              onSubmit={(event) => {
                event.preventDefault()
                save.mutate()
              }}
            >
              <div className="flex items-center justify-between gap-4 rounded-lg border border-(--color-border) px-4 py-3">
                <div>
                  <Label htmlFor="login-ip-blocking">Block failed public sign-ins</Label>
                  <p className="mt-1 text-xs text-(--color-fg-muted)">
                    {enabled ? 'Protection is enabled.' : 'Protection is disabled.'}
                  </p>
                </div>
                <Switch
                  id="login-ip-blocking"
                  tone="accent"
                  checked={enabled}
                  onCheckedChange={setEnabled}
                  disabled={!security.data.proxy_ready || save.isPending}
                />
              </div>

              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="login-failure-threshold">Failed attempts before block</Label>
                  <Input
                    id="login-failure-threshold"
                    type="number"
                    min={1}
                    max={20}
                    value={threshold}
                    onChange={(event) => setThreshold(event.target.value)}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="login-block-duration">Block duration</Label>
                  <Select
                    id="login-block-duration"
                    value={duration}
                    onChange={(event) => setDuration(event.target.value)}
                  >
                    <option value="permanent">Permanent — manual unblock</option>
                    <option value="900">15 minutes</option>
                    <option value="3600">1 hour</option>
                    <option value="86400">24 hours</option>
                    <option value="604800">7 days</option>
                    <option value="2592000">30 days</option>
                  </Select>
                </div>
              </div>

              <div className="rounded-lg bg-(--color-bg-subtle) px-4 py-3 text-xs text-(--color-fg-muted)">
                <p>
                  Public host:{' '}
                  <span className="font-mono text-(--color-fg)">
                    {security.data.public_hostnames.join(', ') || 'not configured'}
                  </span>
                </p>
                <p className="mt-1">
                  Trusted tunnel network:{' '}
                  <span className="font-mono text-(--color-fg)">
                    {security.data.trusted_proxy_cidrs.join(', ') || 'not configured'}
                  </span>
                </p>
                <p className="mt-2">
                  Recovery: open Sentinel through its local IP, sign in, then remove the
                  blocked address below.
                </p>
              </div>

              {errorText(save.error, 'Could not save public login protection.') ? (
                <Alert variant="danger">
                  {errorText(save.error, 'Could not save public login protection.')}
                </Alert>
              ) : null}

              <Button
                type="submit"
                className="w-full sm:w-auto"
                loading={save.isPending}
                disabled={
                  !security.data.proxy_ready ||
                  !threshold ||
                  Number(threshold) < 1 ||
                  Number(threshold) > 20
                }
              >
                <ShieldCheck aria-hidden="true" />
                Save protection
              </Button>
            </form>

            <div className="border-t border-(--color-border) pt-5">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold">Blocked addresses</h3>
                  <p className="mt-1 text-xs text-(--color-fg-muted)">
                    {security.data.blocked_count} currently blocked.
                  </p>
                </div>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={!blocks.data?.length || clearAll.isPending}
                  loading={clearAll.isPending}
                  onClick={() => {
                    if (window.confirm('Remove every public login block and pending failure?')) {
                      clearAll.mutate()
                    }
                  }}
                >
                  Clear all
                </Button>
              </div>

              {blocks.isPending ? (
                <Skeleton className="mt-3 h-16" />
              ) : blocks.data && blocks.data.length > 0 ? (
                <ul className="mt-3 divide-y divide-(--color-border) rounded-lg border border-(--color-border)">
                  {blocks.data.map((block) => (
                    <li key={block.ip_address} className="flex flex-wrap items-center gap-3 px-4 py-3">
                      <div className="min-w-0 flex-1">
                        <p className="flex flex-wrap items-center gap-2 text-sm font-medium">
                          <span className="font-mono">{block.ip_address}</span>
                          <Badge variant="danger">{describeBlock(block)}</Badge>
                        </p>
                        <p className="mt-1 text-xs text-(--color-fg-muted)">
                          {block.failed_attempts} failed attempt
                          {block.failed_attempts === 1 ? '' : 's'} · blocked{' '}
                          {formatDate(block.blocked_at)} · {block.public_hostname}
                        </p>
                      </div>
                      <Button
                        size="sm"
                        variant="secondary"
                        loading={unblock.isPending && unblock.variables === block.ip_address}
                        onClick={() => unblock.mutate(block.ip_address)}
                      >
                        <Unlock aria-hidden="true" />
                        Unblock
                      </Button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-3 text-sm text-(--color-fg-muted)">No blocked addresses.</p>
              )}

              {errorText(unblock.error ?? clearAll.error, 'Could not update the block list.') ? (
                <Alert variant="danger" className="mt-3">
                  {errorText(unblock.error ?? clearAll.error, 'Could not update the block list.')}
                </Alert>
              ) : null}
            </div>
          </>
        ) : (
          <Alert variant="danger">
            {errorText(security.error, 'Could not load public login protection.')}
          </Alert>
        )}
      </CardContent>
    </Card>
  )
}

interface RestoreResult {
  restart_required: boolean
  message: string
}

function BackupCard() {
  const fileInput = useRef<HTMLInputElement>(null)
  const [restored, setRestored] = useState<RestoreResult | null>(null)

  const download = useMutation({
    mutationFn: async () => {
      // Fetched rather than linked: the endpoint is a POST and needs the session
      // cookie, and a blob download lets the browser name the file properly.
      const response = await fetch(`${API_BASE}/system/backup`, {
        method: 'POST',
        credentials: 'same-origin',
      })
      if (!response.ok) throw new ApiError(response.status, 'Could not create the backup.')
      const blob = await response.blob()

      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `sentinel-backup-${new Date().toISOString().slice(0, 10)}.zip`
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
    },
  })

  const restore = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData()
      form.append('file', file)
      const response = await fetch(`${API_BASE}/system/backup/restore`, {
        method: 'POST',
        credentials: 'same-origin',
        body: form,
      })
      const payload: unknown = await response.json().catch(() => null)
      if (!response.ok) {
        const detail =
          payload && typeof payload === 'object' && 'detail' in payload
            ? String(payload.detail)
            : 'Could not restore that backup.'
        throw new ApiError(response.status, detail)
      }
      return payload as RestoreResult
    },
    onSuccess: (result) => setRestored(result),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Backup</CardTitle>
        <CardDescription>
          Cameras, motion zones, retention, users and tokens. Recordings are not included.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <Alert variant="info" title="Camera passwords are not backed up">
          They are encrypted with a secret that stays on this machine, so a backup
          containing them would be either useless or unsafe. Each camera&apos;s password has
          to be entered again after restoring.
        </Alert>

        <div className="flex flex-wrap gap-2">
          <Button
            className="w-full sm:w-auto"
            onClick={() => download.mutate()}
            loading={download.isPending}
          >
            <Download aria-hidden="true" />
            Download backup
          </Button>
          <Button
            variant="secondary"
            className="w-full sm:w-auto"
            onClick={() => fileInput.current?.click()}
            loading={restore.isPending}
          >
            <Upload aria-hidden="true" />
            Restore from file
          </Button>
          <input
            ref={fileInput}
            type="file"
            accept=".zip,application/zip"
            className="sr-only"
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) restore.mutate(file)
              event.target.value = ''
            }}
          />
        </div>

        {errorText(download.error, 'Could not create the backup.') ? (
          <Alert variant="danger">
            {errorText(download.error, 'Could not create the backup.')}
          </Alert>
        ) : null}
        {errorText(restore.error, 'Could not restore that backup.') ? (
          <Alert variant="danger">
            {errorText(restore.error, 'Could not restore that backup.')}
          </Alert>
        ) : null}
        {restored ? (
          <Alert variant="warning" title="Restart required">
            {restored.message}
          </Alert>
        ) : null}
      </CardContent>
    </Card>
  )
}

function PasswordCard() {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [done, setDone] = useState(false)

  const change = useMutation({
    mutationFn: () => authApi.changePassword(current, next),
    onSuccess: () => {
      setCurrent('')
      setNext('')
      setDone(true)
    },
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Password</CardTitle>
      </CardHeader>
      <CardContent>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            setDone(false)
            change.mutate()
          }}
        >
          <div className="space-y-2">
            <Label htmlFor="current-password">Current password</Label>
            <Input
              id="current-password"
              type="password"
              value={current}
              onChange={(event) => setCurrent(event.target.value)}
              autoComplete="current-password"
              required
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="new-password">New password</Label>
            <Input
              id="new-password"
              type="password"
              value={next}
              onChange={(event) => setNext(event.target.value)}
              autoComplete="new-password"
              minLength={10}
              required
            />
            <p className="text-xs text-(--color-fg-subtle)">At least 10 characters.</p>
          </div>

          {errorText(change.error, 'Could not change the password.') ? (
            <Alert variant="danger">
              {errorText(change.error, 'Could not change the password.')}
            </Alert>
          ) : null}
          {done ? <Alert variant="info">Password changed.</Alert> : null}

          <Button
            type="submit"
            className="w-full sm:w-auto"
            loading={change.isPending}
            disabled={!current || next.length < 10}
          >
            Change password
          </Button>
        </form>
      </CardContent>
    </Card>
  )
}

function DiagnosticsCard() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Diagnostics</CardTitle>
        <CardDescription>
          A snapshot of this install for troubleshooting. Credentials are removed before
          it is written, so it is safe to attach to an issue.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Button asChild variant="secondary" className="w-full sm:w-auto">
          <a href={`${API_BASE}/system/diagnostics/download`} download>
            <Download aria-hidden="true" />
            Download diagnostics
          </a>
        </Button>
      </CardContent>
    </Card>
  )
}

export function SettingsPage() {
  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <PageHeader
        title="Settings"
        description="Access, backup and troubleshooting for this server."
      />

      <ViewerAccountsCard />
      <ApiTokensCard />
      <SoundPolicyCard />
      <LoginSecurityCard />
      <BackupCard />
      <MoveMachineCard />
      <PasswordCard />
      <BuildChannelCard />
      <DiagnosticsCard />
    </div>
  )
}
