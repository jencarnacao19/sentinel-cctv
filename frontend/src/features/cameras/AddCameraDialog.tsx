import { CheckCircle2 } from 'lucide-react'
import { useState } from 'react'

import { ApiError, NetworkError } from '@/api/client'
import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogForm,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { FormField, FormSection } from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { useCreateCamera, useTestStream } from '@/features/cameras/useCameras'
import type { StreamTestResult } from '@/types/api'

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
}

function TestResult({ result }: { result: StreamTestResult }) {
  if (!result.ok) {
    return (
      <Alert variant="danger" title="Could not open the stream">
        {result.error}
      </Alert>
    )
  }
  return (
    <div className="space-y-3 rounded-(--radius-card) border border-(--color-success)/40 bg-(--color-success)/10 p-4">
      <p className="flex items-center gap-2 text-sm font-semibold text-(--color-success)">
        <CheckCircle2 className="size-4" aria-hidden="true" />
        Connection OK
      </p>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
        <dt className="text-(--color-fg-muted)">Codec</dt>
        <dd className="tabular font-medium uppercase">{result.codec ?? '—'}</dd>
        <dt className="text-(--color-fg-muted)">Resolution</dt>
        <dd className="tabular font-medium">{result.resolution ?? '—'}</dd>
        <dt className="text-(--color-fg-muted)">Frame rate</dt>
        <dd className="tabular font-medium">{result.fps ? `${result.fps} fps` : '—'}</dd>
        <dt className="text-(--color-fg-muted)">Audio</dt>
        <dd className="tabular font-medium uppercase">{result.audio_codec ?? 'none'}</dd>
        <dt className="text-(--color-fg-muted)">Bitrate</dt>
        <dd className="tabular font-medium">
          {result.bitrate_kbps ? `${(result.bitrate_kbps / 1000).toFixed(1)} Mbps` : '—'}
        </dd>
      </dl>
      {result.can_stream_copy ? (
        <Badge variant="success">Records without re-encoding</Badge>
      ) : (
        <Alert variant="warning" title="This codec cannot be copied directly">
          Recording would need to re-encode, which costs far more CPU. An H.264 or H.265
          stream is strongly preferred.
        </Alert>
      )}
    </div>
  )
}

/**
 * Add a camera by RTSP URL.
 *
 * The password field is separate from the URL, but a URL pasted with credentials
 * still works -- the server extracts and encrypts them either way.
 *
 * The form is grouped rather than flat: what to call the camera, how to reach
 * it, and how to authenticate to it. Twenty controls in one undifferentiated
 * column gave an operator adding their first camera no way to tell the two
 * required fields from the optional ones.
 */
export function AddCameraDialog({ open, onOpenChange }: Props) {
  const createCamera = useCreateCamera()
  const testStream = useTestStream()

  const [name, setName] = useState('')
  const [mainUrl, setMainUrl] = useState('')
  const [subUrl, setSubUrl] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')

  const canTest = mainUrl.trim().length > 0 && !testStream.isPending
  const canSave = name.trim().length > 0 && mainUrl.trim().length > 0

  function reset() {
    setName('')
    setMainUrl('')
    setSubUrl('')
    setUsername('')
    setPassword('')
    testStream.reset()
    createCamera.reset()
  }

  function handleOpenChange(next: boolean) {
    if (!next) reset()
    onOpenChange(next)
  }

  function handleTest(): void {
    void testStream.mutateAsync({ url: mainUrl.trim(), username, password }).catch(() => undefined)
  }

  function handleSubmit(event: React.FormEvent): void {
    event.preventDefault()
    if (!canSave) return
    void (async () => {
      try {
        await createCamera.mutateAsync({
          name: name.trim(),
          kind: 'rtsp',
          main_stream_url: mainUrl.trim(),
          sub_stream_url: subUrl.trim() || null,
          username: username || null,
          password: password || null,
        })
        handleOpenChange(false)
      } catch {
        /* surfaced through saveError */
      }
    })()
  }

  const saveError =
    createCamera.error instanceof NetworkError
      ? 'Could not reach the Sentinel server.'
      : createCamera.error instanceof ApiError
        ? createCamera.error.message
        : createCamera.error
          ? 'Could not save the camera.'
          : null

  const testError =
    testStream.error instanceof NetworkError
      ? 'Could not reach the Sentinel server.'
      : testStream.error instanceof ApiError
        ? testStream.error.message
        : null

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Add camera</DialogTitle>
          <DialogDescription>
            Enter the camera’s RTSP address, then test the connection before saving.
          </DialogDescription>
        </DialogHeader>

        <DialogForm onSubmit={handleSubmit} noValidate>
          <DialogBody className="space-y-6">
            <FormField label="Name" id="camera-name">
              {(field) => (
                <Input
                  {...field}
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="Front door"
                  autoFocus
                  required
                />
              )}
            </FormField>

            <FormSection
              title="Streams"
              description="Sentinel records the main stream and detects motion on the substream."
            >
              <FormField
                label="Main stream (recording)"
                id="camera-main-url"
                hint="The full RTSP address, including the path your camera uses."
              >
                {(field) => (
                  <Input
                    {...field}
                    value={mainUrl}
                    onChange={(event) => setMainUrl(event.target.value)}
                    placeholder="rtsp://192.168.50.50:554/stream1"
                    spellCheck={false}
                    autoComplete="off"
                    required
                  />
                )}
              </FormField>

              <FormField
                label="Substream"
                id="camera-sub-url"
                optional
                hint="A low-resolution stream, used for motion detection and for the camera wall."
              >
                {(field) => (
                  <Input
                    {...field}
                    value={subUrl}
                    onChange={(event) => setSubUrl(event.target.value)}
                    placeholder="rtsp://192.168.50.50:554/stream2"
                    spellCheck={false}
                    autoComplete="off"
                  />
                )}
              </FormField>
            </FormSection>

            <FormSection
              title="Camera sign-in"
              description="The account you created on the camera itself, not a cloud account."
            >
              <div className="grid gap-4 sm:grid-cols-2">
                <FormField label="Username" id="camera-username">
                  {(field) => (
                    <Input
                      {...field}
                      value={username}
                      onChange={(event) => setUsername(event.target.value)}
                      autoComplete="off"
                    />
                  )}
                </FormField>
                <FormField label="Password" id="camera-password">
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
              </div>
            </FormSection>

            <p className="text-xs leading-5 text-(--color-fg-subtle)">
              Credentials are encrypted before storage and never shown again.
            </p>

            {testError ? <Alert variant="danger">{testError}</Alert> : null}
            {testStream.data ? <TestResult result={testStream.data} /> : null}
            {saveError ? <Alert variant="danger">{saveError}</Alert> : null}
          </DialogBody>

          <DialogFooter>
            <Button
              type="button"
              variant="secondary"
              onClick={handleTest}
              disabled={!canTest}
              loading={testStream.isPending}
            >
              {testStream.isPending ? 'Testing…' : 'Test connection'}
            </Button>
            <Button type="submit" disabled={!canSave} loading={createCamera.isPending}>
              Add camera
            </Button>
          </DialogFooter>
        </DialogForm>
      </DialogContent>
    </Dialog>
  )
}
