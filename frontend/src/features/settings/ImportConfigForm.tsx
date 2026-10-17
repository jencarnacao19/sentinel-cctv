import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Upload } from 'lucide-react'
import { useId, useRef, useState } from 'react'

import { API_BASE, NetworkError } from '@/api/client'
import { queryKeys } from '@/api/endpoints'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { errorText, failure, type ImportSummary } from '@/features/settings/transfer'

/**
 * Add the cameras and settings from a file exported on another Sentinel.
 *
 * Used on Settings and as the first, optional setup step, so a new machine can
 * start from the old one's configuration instead of from nothing.
 */
export function ImportConfigForm({ onImported }: { onImported?: (summary: ImportSummary) => void }) {
  const queryClient = useQueryClient()
  const passphraseId = useId()
  const fileInput = useRef<HTMLInputElement>(null)
  const [passphrase, setPassphrase] = useState('')
  const [summary, setSummary] = useState<ImportSummary | null>(null)

  const importConfig = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData()
      form.append('file', file)
      if (passphrase) form.append('passphrase', passphrase)
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
    onSuccess: async (result) => {
      setSummary(result)
      setPassphrase('')
      onImported?.(result)
      // The file can add cameras and change the recording settings.
      await queryClient.invalidateQueries({ queryKey: queryKeys.cameras })
      await queryClient.invalidateQueries({ queryKey: ['storage'] })
    },
  })

  const error = errorText(importConfig.error, 'Could not import that file.')

  return (
    <div className="space-y-3">
      <div className="space-y-1">
        <Label htmlFor={passphraseId}>Passphrase (only if the file has passwords)</Label>
        <Input
          id={passphraseId}
          type="password"
          autoComplete="off"
          value={passphrase}
          onChange={(event) => setPassphrase(event.target.value)}
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
      {error ? <Alert variant="danger">{error}</Alert> : null}
      {summary ? <ImportResult summary={summary} /> : null}
    </div>
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
