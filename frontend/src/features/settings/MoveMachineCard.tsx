/**
 * Moving this installation to another machine with one YAML file.
 *
 * Export writes every camera, its motion zones and the recording settings.
 * Camera passwords are optional: with a passphrase they travel encrypted,
 * without one they are left out and typed again after importing. Import adds
 * the cameras the file describes to this server and keeps the ones it has.
 */

import { useMutation } from '@tanstack/react-query'
import { Download } from 'lucide-react'
import { useState } from 'react'

import { API_BASE, NetworkError } from '@/api/client'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { CheckboxField } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ImportConfigForm } from '@/features/settings/ImportConfigForm'
import { errorText, failure } from '@/features/settings/transfer'

const MIN_PASSPHRASE_LENGTH = 8

export function MoveMachineCard() {
  const [includePasswords, setIncludePasswords] = useState(false)
  const [passphrase, setPassphrase] = useState('')
  const [confirmation, setConfirmation] = useState('')

  const passphraseProblem = !includePasswords
    ? null
    : passphrase.length < MIN_PASSPHRASE_LENGTH
      ? `Use at least ${MIN_PASSPHRASE_LENGTH} characters.`
      : passphrase !== confirmation
        ? 'The two passphrases are different.'
        : null

  const exportConfig = useMutation({
    mutationFn: async () => {
      let response: Response
      try {
        response = await fetch(`${API_BASE}/system/config/export`, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(
            includePasswords ? { include_passwords: true, passphrase } : { include_passwords: false },
          ),
        })
      } catch {
        throw new NetworkError()
      }
      if (!response.ok) throw await failure(response, 'Could not export the configuration.')
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `sentinel-config-${new Date().toISOString().slice(0, 10)}.yaml`
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
    },
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Move to another machine</CardTitle>
        <CardDescription>
          One YAML file with every camera, its motion zones and the recording settings. Import it on
          the new Sentinel. Recordings, users and API tokens stay here.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <section aria-labelledby="move-export-heading" className="space-y-3">
          <h3 id="move-export-heading" className="text-sm font-medium">
            Export
          </h3>
          <CheckboxField
            label="Include camera passwords"
            description="They are encrypted with a passphrase you choose, which the import asks for. Without them, you type each camera's password again after importing."
            checked={includePasswords}
            onChange={(event) => setIncludePasswords(event.target.checked)}
          />
          {includePasswords ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label htmlFor="move-passphrase">Passphrase</Label>
                <Input
                  id="move-passphrase"
                  type="password"
                  autoComplete="new-password"
                  value={passphrase}
                  onChange={(event) => setPassphrase(event.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="move-passphrase-confirm">Passphrase again</Label>
                <Input
                  id="move-passphrase-confirm"
                  type="password"
                  autoComplete="new-password"
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                />
              </div>
              {passphraseProblem ? (
                <p className="text-xs text-(--color-fg-muted) sm:col-span-2">{passphraseProblem}</p>
              ) : null}
            </div>
          ) : null}
          <Button
            className="w-full sm:w-auto"
            onClick={() => exportConfig.mutate()}
            loading={exportConfig.isPending}
            disabled={passphraseProblem !== null}
          >
            <Download aria-hidden="true" />
            Download configuration
          </Button>
          {errorText(exportConfig.error, 'Could not export the configuration.') ? (
            <Alert variant="danger">
              {errorText(exportConfig.error, 'Could not export the configuration.')}
            </Alert>
          ) : null}
        </section>

        <section aria-labelledby="move-import-heading" className="space-y-3">
          <h3 id="move-import-heading" className="text-sm font-medium">
            Import
          </h3>
          <ImportConfigForm />
        </section>
      </CardContent>
    </Card>
  )
}
