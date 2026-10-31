import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Link2 } from 'lucide-react'
import { useState } from 'react'

import { haPanelApi, queryKeys } from '@/api/endpoints'
import { PageHeader } from '@/components/layout/PageHeader'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { HomeAssistantStep, Value } from '@/features/setup/HomeAssistantStep'
import { errorText } from '@/lib/errors'
import type { CreatedApiToken } from '@/types/api'

const MY_HOME_ASSISTANT = 'https://my.home-assistant.io/redirect'

/** Why a Sentinel address will not show in the Home Assistant panel everywhere. */
function addressWarning(address: string): string | null {
  let url: URL
  try {
    url = new URL(address)
  } catch {
    return 'Enter the whole address, starting with https://.'
  }
  if (url.protocol === 'http:') {
    return 'Home Assistant opened over https:// cannot show an http:// page. Use the https:// address you reach Sentinel on, if you have one.'
  }
  if (
    /^(10\.|127\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(url.hostname) ||
    url.hostname.endsWith('.local')
  ) {
    return 'This address only works on your home network.'
  }
  return null
}

function PanelCard() {
  const queryClient = useQueryClient()
  const status = useQuery({ queryKey: queryKeys.haPanel, queryFn: haPanelApi.status })
  const [address, setAddress] = useState(window.location.origin)
  const [origins, setOrigins] = useState<string | null>(null)
  const [key, setKey] = useState<string | null>(null)
  const [replacing, setReplacing] = useState(false)

  const saveOrigins = useMutation({
    mutationFn: (value: string) =>
      haPanelApi.setOrigins(
        value
          .split(/[\s,]+/)
          .map((origin) => origin.trim())
          .filter(Boolean),
      ),
    onSuccess: (next) => {
      queryClient.setQueryData(queryKeys.haPanel, next)
      setOrigins(null)
    },
  })
  const createLink = useMutation({
    mutationFn: haPanelApi.createLink,
    onSuccess: async ({ key: created }) => {
      setKey(created)
      setReplacing(false)
      await queryClient.invalidateQueries({ queryKey: queryKeys.haPanel })
    },
  })

  const base = address.trim().replace(/\/+$/, '')
  const link = key ? `${base}/embed#viewer_key=${key}` : null
  const warning = addressWarning(base)
  const originsText = origins ?? status.data?.origins.join('\n') ?? ''

  return (
    <Card>
      <CardHeader>
        <CardTitle>Show Sentinel in Home Assistant</CardTitle>
        <CardDescription>
          A link that opens the camera wall as a Home Assistant panel. It can watch, play back
          and save clips, never change settings.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6 text-sm leading-6">
        <section className="space-y-2">
          <Label htmlFor="ha-sentinel-address">1. The address Home Assistant opens</Label>
          <Input
            id="ha-sentinel-address"
            value={address}
            onChange={(event) => setAddress(event.target.value)}
            inputMode="url"
          />
          {warning ? <Alert variant="warning">{warning}</Alert> : null}
        </section>

        <section className="space-y-2">
          <Label htmlFor="ha-origins">2. Your Home Assistant addresses</Label>
          <p className="text-(--color-fg-muted)">
            One per line, as you open Home Assistant, for example{' '}
            <code>https://ha.example.net</code> and <code>http://192.168.1.10:8123</code>. Browsers
            show Sentinel only inside these.
          </p>
          <textarea
            id="ha-origins"
            className="min-h-20 w-full rounded-(--radius-control) border border-(--color-border) bg-(--color-surface) px-3 py-2 font-mono text-xs"
            value={originsText}
            onChange={(event) => setOrigins(event.target.value)}
          />
          <Button
            variant="secondary"
            loading={saveOrigins.isPending}
            disabled={origins === null}
            onClick={() => saveOrigins.mutate(originsText)}
          >
            Save addresses
          </Button>
          {errorText(saveOrigins.error, 'Could not save the addresses.') ? (
            <Alert variant="danger">
              {errorText(saveOrigins.error, 'Could not save the addresses.')}
            </Alert>
          ) : null}
        </section>

        <section className="space-y-2">
          <p className="font-medium">3. Create the link</p>
          {link ? (
            <>
              <p className="text-(--color-fg-muted)">
                Copy it now: Sentinel keeps only a fingerprint and cannot show it again.
              </p>
              <Value value={link} label="Copy the panel link" />
            </>
          ) : status.data?.link_exists && !replacing ? (
            <>
              <p className="text-(--color-fg-muted)">
                A link already exists. A new one stops the old one working, in every Home
                Assistant that uses it.
              </p>
              <Button variant="secondary" onClick={() => setReplacing(true)}>
                <Link2 aria-hidden="true" />
                Replace the link
              </Button>
            </>
          ) : (
            <Button
              variant={replacing ? 'danger' : 'primary'}
              loading={createLink.isPending}
              onClick={() => createLink.mutate()}
            >
              <Link2 aria-hidden="true" />
              {replacing ? 'Create a new link and stop the old one' : 'Create link'}
            </Button>
          )}
          {status.data?.env_key ? (
            <p className="text-xs text-(--color-fg-muted)">
              The key in this server&apos;s .env file keeps opening the panel as well.
            </p>
          ) : null}
          {errorText(createLink.error, 'Could not create the link.') ? (
            <Alert variant="danger">{errorText(createLink.error, 'Could not create the link.')}</Alert>
          ) : null}
        </section>

        <section className="space-y-2">
          <p className="font-medium">4. Add it in Home Assistant</p>
          <ol className="list-decimal space-y-1 pl-5 text-(--color-fg-muted)">
            <li>
              Open <em>Settings → Dashboards</em> and choose <em>Add dashboard</em>.
            </li>
            <li>
              Pick <em>Webpage</em> and paste the link.
            </li>
            <li>Give it a name and an icon, and keep it in the sidebar.</li>
            <li>Open it from the sidebar: the cameras start on their own.</li>
          </ol>
          <Button asChild variant="secondary">
            <a href={`${MY_HOME_ASSISTANT}/lovelace_dashboards/`} target="_blank" rel="noreferrer">
              <ExternalLink aria-hidden="true" />
              Open dashboards in Home Assistant
            </a>
          </Button>
          <p className="text-xs text-(--color-fg-muted)">
            Behind Cloudflare Access, leave <code>/embed</code>, <code>/assets/*</code>,{' '}
            <code>/api/v1/viewer/*</code> and <code>/api/v1/streams/*</code> outside the login:
            the link&apos;s key protects them.
          </p>
        </section>
      </CardContent>
    </Card>
  )
}

export function HomeAssistantPage() {
  const [token, setToken] = useState<CreatedApiToken | null>(null)
  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <PageHeader
        title="Home Assistant"
        description="The camera wall as a Home Assistant panel, and the optional integration."
      />
      <PanelCard />
      <Card>
        <CardHeader>
          <CardTitle>The integration (optional)</CardTitle>
          <CardDescription>
            Cameras, motion sensors and recording switches as Home Assistant entities, for
            automations. The panel above works without it.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <HomeAssistantStep token={token} onCreated={setToken} />
        </CardContent>
      </Card>
    </div>
  )
}
