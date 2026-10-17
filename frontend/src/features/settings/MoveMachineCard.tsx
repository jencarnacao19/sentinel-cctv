/**
 * Moving this installation to another machine with one YAML file.
 *
 * Export writes every camera, its motion zones and the recording settings.
 * Camera passwords are optional: with a passphrase they travel encrypted,
 * without one they are left out and typed again after importing. Import adds
 * the cameras the file describes to this server and keeps the ones it has.
 */

import { useMutation } from '@tanstack/react-query'
import { Download, Upload } from 'lucide-react'
import { useRef, useState } from 'react'

import { ApiError, API_BASE, NetworkError } from '@/api/client'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { CheckboxField } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

const MIN_PASSPHRASE_LENGTH = 8

export interface ImportSummary {
  cameras_added: string[]
  cameras_skipped: { name: string; reason: string }[]
  settings_applied: string[]
  settings_skipped: { name: string; reason: string }[]
  warnings: string[]
  passwords_restored: boolean
}

function errorText(error: unknown, fallback: string): string | null {
  if (!error) return null
  if (error instanceof NetworkError) return 'Could not reach the Sentinel server.'
  if (error instanceof ApiError) return error.message
  return fallback
}

async function failure(response: Response, fallback: string): Promise<ApiError> {
  const payload: unknown = await response.json().catch(() => null)
  const detail =
    payload && typeof payload === 'object' && 'detail' in payload ? String(payload.detail) : fallback
  return new ApiError(response.status, detail)
}

export function MoveMachineCard() {
  const fileInput = useRef<HTMLInputElement>(null)
  const [includePasswords, setIncludePasswords] = useState(false)
  const [passphrase, setPassphrase] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [importPassphrase, setImportPassphrase] = useState('')
  const [summary, setSummary] = useState<ImportSummary | null>(null)

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

  const importConfig = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData()
      form.append('file', file)
      if (importPassphrase) form.append('passphrase', importPassphrase)
      let response: Response
      try {
        response = await fetch(`${API_BASE}/system/config/import`, {
          method: 'POST',
          credentials: 'same-origin',
          body: form,
        })
      } catch {
        throw new NetworkError()
      }
      if (!response.ok) throw await failure(response, 'Could not import that file.')
      return (await response.json()) as ImportSummary
    },
    onSuccess: (result) => {
      setSummary(result)
      setImportPassphrase('')
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
          <div className="space-y-1">
            <Label htmlFor="move-import-passphrase">Passphrase (only if the file has passwords)</Label>
            <Input
              id="move-import-passphrase"
              type="password"
              autoComplete="off"
              value={importPassphrase}
              onChange={(event) => setImportPassphrase(event.target.value)}
            />
          </div>
          <Button
            variant="secondary"
            className="w-full sm:w-auto"
            onClick={() => fileInput.current?.click()}
            loading={importConfig.isPending}
          >
            <Upload aria-hidden="true" />
            Import from file
          </Button>
          <input
            ref={fileInput}
            type="file"
            accept=".yaml,.yml,application/yaml,text/yaml"
            className="sr-only"
            aria-label="Configuration file to import"
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) importConfig.mutate(file)
              event.target.value = ''
            }}
          />
          {errorText(importConfig.error, 'Could not import that file.') ? (
            <Alert variant="danger">
              {errorText(importConfig.error, 'Could not import that file.')}
            </Alert>
          ) : null}
          {summary ? <ImportResult summary={summary} /> : null}
        </section>
      </CardContent>
    </Card>
  )
}

function ImportResult({ summary }: { summary: ImportSummary }) {
  const added = summary.cameras_added.length
  return (
    <Alert
      variant={summary.cameras_skipped.length || summary.settings_skipped.length ? 'warning' : 'success'}
      title={`${added} camera${added === 1 ? '' : 's'} added`}
    >
      <ul className="mt-1 list-disc space-y-1 pl-5">
        {summary.cameras_added.length ? <li>Added: {summary.cameras_added.join(', ')}.</li> : null}
        {summary.cameras_skipped.map((entry) => (
          <li key={`camera-${entry.name}`}>
            Not added, {entry.name}: {entry.reason}
          </li>
        ))}
        {summary.settings_skipped.map((entry) => (
          <li key={`setting-${entry.name}`}>
            Setting {entry.name} not applied: {entry.reason}
          </li>
        ))}
        {summary.warnings.map((warning) => (
          <li key={warning}>{warning}</li>
        ))}
        {added > 0 && !summary.passwords_restored ? (
          <li>The file had no passwords: enter each new camera&apos;s password on the Cameras page.</li>
        ) : null}
      </ul>
    </Alert>
  )
}
