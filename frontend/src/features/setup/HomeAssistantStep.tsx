import { useMutation } from '@tanstack/react-query'
import { Check, Copy, KeyRound } from 'lucide-react'
import { useState } from 'react'

import { ApiError, NetworkError } from '@/api/client'
import { authApi } from '@/api/endpoints'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import type { CreatedApiToken } from '@/types/api'

const REPOSITORY = 'https://github.com/jencarnacao19/sentinel-cctv'

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <Button
      size="sm"
      variant="secondary"
      aria-label={label}
      onClick={() => {
        void navigator.clipboard
          .writeText(value)
          .then(() => {
            setCopied(true)
            setTimeout(() => setCopied(false), 2000)
          })
          .catch(() => undefined)
      }}
    >
      {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
      {copied ? 'Copied' : 'Copy'}
    </Button>
  )
}

function Value({ value, label }: { value: string; label: string }) {
  return (
    <div className="mt-2 flex flex-wrap items-center gap-2">
      <code className="min-w-0 flex-1 truncate rounded-lg bg-(--color-bg-subtle) px-3 py-2 font-mono text-xs">
        {value}
      </code>
      <CopyButton value={value} label={label} />
    </div>
  )
}

/**
 * The three things Home Assistant needs, in the order it asks for them.
 *
 * The token is created here with Camera control, which the recording switch
 * needs, and shown once: Sentinel keeps only a hash of it. The wizard holds on
 * to it, so going back and returning to this step shows the same token rather
 * than offering to create another.
 */
export function HomeAssistantStep({
  token,
  onCreated,
}: {
  token: CreatedApiToken | null
  onCreated: (token: CreatedApiToken) => void
}) {
  const address = window.location.origin
  const createToken = useMutation({
    mutationFn: () => authApi.createToken('Home Assistant', 'camera_control'),
    onSuccess: onCreated,
  })
  const tokenError =
    createToken.error instanceof NetworkError
      ? 'Could not reach the Sentinel server.'
      : createToken.error instanceof ApiError
        ? createToken.error.message
        : createToken.error
          ? 'Could not create the token.'
          : null

  return (
    <ol className="space-y-5 text-sm leading-6">
      <li>
        <p className="font-medium">1. Install the integration</p>
        <p className="text-(--color-fg-muted)">
          In HACS, open the menu, choose <em>Custom repositories</em>, add this address with the
          type <em>Integration</em>, install <em>Sentinel CCTV</em> and restart Home Assistant.
        </p>
        <Value value={REPOSITORY} label="Copy the repository address" />
      </li>
      <li>
        <p className="font-medium">2. Add it</p>
        <p className="text-(--color-fg-muted)">
          Under Settings, Devices &amp; services, add <em>Sentinel CCTV</em>. Home Assistant may
          find this server on its own; if it asks for an address, use this one.
        </p>
        <Value value={address} label="Copy this server's address" />
      </li>
      <li>
        <p className="font-medium">3. Give it a token</p>
        {token ? (
          <>
            <p className="text-(--color-fg-muted)">
              Paste it when Home Assistant asks. It is shown only now: Sentinel keeps just a
              fingerprint of it.
            </p>
            <Value value={token.token} label="Copy the token" />
          </>
        ) : (
          <>
            <p className="text-(--color-fg-muted)">
              Home Assistant signs in with a token instead of your password. You can revoke it
              any time under Settings.
            </p>
            <Button
              className="mt-2"
              variant="secondary"
              onClick={() => createToken.mutate()}
              loading={createToken.isPending}
            >
              <KeyRound aria-hidden="true" />
              Create token
            </Button>
          </>
        )}
        {tokenError ? (
          <Alert variant="danger" className="mt-2">
            {tokenError}
          </Alert>
        ) : null}
      </li>
    </ol>
  )
}
