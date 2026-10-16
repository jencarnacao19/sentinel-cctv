import { useEffect, useState } from 'react'

import { ApiError, NetworkError } from '@/api/client'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { CheckboxField } from '@/components/ui/checkbox'
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
import { Select } from '@/components/ui/select'
import { useUpdateCamera } from '@/features/cameras/useCameras'
import { useTestTapoBattery } from '@/features/cameras/useTapoBattery'
import type { Camera, CameraUpdate } from '@/types/api'

interface Props {
  camera: Camera
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * The editable settings a battery camera actually has.
 *
 * A TC82/H200 has no RTSP URL, ONVIF account, continuous-recorder profile or
 * motion worker. Reusing the RTSP form made its required Main stream field
 * impossible to satisfy and invited settings the backend correctly rejects.
 *
 * The connection proof is the rule this dialog exists to enforce: changing the
 * host, the child ID or the password invalidates it, and nothing is saved until
 * that exact combination has answered on the LAN. A typo would otherwise leave
 * an apparently configured camera that only fails later, during live view.
 */
export function TapoBatteryConfigureDialog({ camera, open, onOpenChange }: Props) {
  const update = useUpdateCamera()
  const testConnection = useTestTapoBattery()
  const resetUpdate = update.reset
  const resetTestConnection = testConnection.reset
  const [name, setName] = useState(camera.name)
  const [host, setHost] = useState(camera.host)
  const storedTopology = camera.tapo_child_id ? 'hub' : 'standalone'
  const [topology, setTopology] = useState<'standalone' | 'hub'>(storedTopology)
  const storedUsername = camera.username && camera.username !== 'admin' ? camera.username : ''
  const [username, setUsername] = useState(storedUsername)
  const [childId, setChildId] = useState(camera.tapo_child_id ?? '')
  const [password, setPassword] = useState('')
  const [clearPassword, setClearPassword] = useState(false)
  const [enabled, setEnabled] = useState(camera.enabled)
  const [testedFingerprint, setTestedFingerprint] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setName(camera.name)
    setHost(camera.host)
    setTopology(camera.tapo_child_id ? 'hub' : 'standalone')
    setUsername(camera.username && camera.username !== 'admin' ? camera.username : '')
    setChildId(camera.tapo_child_id ?? '')
    setPassword('')
    setClearPassword(false)
    setEnabled(camera.enabled)
    setTestedFingerprint(null)
    resetUpdate()
    resetTestConnection()
  }, [camera, open, resetTestConnection, resetUpdate])

  const effectiveChildId = topology === 'hub' ? childId.trim() : ''
  const effectiveUsername = topology === 'hub' ? username.trim() : ''
  const fingerprint = `${topology}\u0000${host.trim()}\u0000${effectiveChildId}\u0000${password}\u0000${effectiveUsername}`
  const connectionChanged =
    topology !== storedTopology ||
    host.trim() !== camera.host ||
    (effectiveChildId || null) !== camera.tapo_child_id ||
    effectiveUsername !== storedUsername ||
    password.length > 0
  const connectionProven = !connectionChanged || testedFingerprint === fingerprint

  function invalidateConnectionProof() {
    setTestedFingerprint(null)
    testConnection.reset()
  }

  function testNewConnection() {
    if (!host.trim() || !password || clearPassword) return
    setTestedFingerprint(null)
    void testConnection
      .mutateAsync({
        host: host.trim(),
        password,
        child_id: effectiveChildId || null,
        ...(effectiveUsername ? { username: effectiveUsername } : {}),
      })
      .then(() => setTestedFingerprint(fingerprint))
      .catch(() => undefined)
  }

  async function save(event: React.FormEvent) {
    event.preventDefault()
    const payload: CameraUpdate = {
      name: name.trim(),
      host: host.trim(),
      tapo_child_id: effectiveChildId || null,
      enabled: clearPassword ? false : enabled,
      username: effectiveUsername || 'admin',
    }
    if (clearPassword) payload.tapo_password = ''
    else if (password) payload.tapo_password = password

    await update.mutateAsync({ id: camera.id, payload })
    onOpenChange(false)
  }

  const failure = errorText(update.error)
  const testFailure = errorText(testConnection.error)
  const missingCredential = enabled && !camera.has_tapo_credentials && !password
  const children = testConnection.data?.children ?? []

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Configure {camera.name}</DialogTitle>
          <DialogDescription>
            Local H200/TC82 connection and encrypted Tapo credential. This camera stays
            event-based, and records continuously only for an explicitly requested timed session.
          </DialogDescription>
        </DialogHeader>

        <DialogForm onSubmit={(event) => void save(event).catch(() => undefined)}>
          <DialogBody className="space-y-6">
            <FormSection>
              <FormField label="Name" id={`battery-name-${camera.id}`}>
                {(field) => (
                  <Input
                    {...field}
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                    required
                  />
                )}
              </FormField>
              <CheckboxField
                label="Camera enabled"
                checked={enabled}
                onChange={(event) => {
                  const next = event.target.checked
                  setEnabled(next)
                  if (next) setClearPassword(false)
                }}
              />
            </FormSection>

            <FormSection
              title="Local connection"
              description="A private LAN address only. Sentinel does not use a TP-Link cloud endpoint."
            >
              <FormField
                label="Connection type"
                id={`battery-topology-${camera.id}`}
                hint="Choose the device whose local address is entered below."
              >
                {(field) => (
                  <Select
                    {...field}
                    value={topology}
                    onChange={(event) => {
                      const next = event.target.value as 'standalone' | 'hub'
                      setTopology(next)
                      setChildId('')
                      setUsername('')
                      invalidateConnectionProof()
                    }}
                  >
                    <option value="standalone">Standalone TC82</option>
                    <option value="hub">H200 hub with paired camera</option>
                  </Select>
                )}
              </FormField>

              <FormField
                label="Camera or H200 local address"
                id={`battery-host-${camera.id}`}
              >
                {(field) => (
                  <Input
                    {...field}
                    value={host}
                    onChange={(event) => {
                      setHost(event.target.value)
                      invalidateConnectionProof()
                    }}
                    placeholder="192.168.50.60"
                    spellCheck={false}
                    autoComplete="off"
                    required
                  />
                )}
              </FormField>

              {topology === 'hub' ? (
                <FormField
                  label="H200 child ID"
                  id={`battery-child-${camera.id}`}
                  optional
                  hint="Test the hub to discover paired cameras, or enter the exact child ID."
                >
                  {(field) => (
                    <Input
                      {...field}
                      value={childId}
                      onChange={(event) => {
                        setChildId(event.target.value)
                        invalidateConnectionProof()
                      }}
                      autoComplete="off"
                    />
                  )}
                </FormField>
              ) : null}

              {topology === 'hub' && children.length > 0 ? (
                <FormField
                  label="Discovered hub cameras"
                  id={`battery-discovered-child-${camera.id}`}
                  hint="Selecting a camera fills its child ID automatically."
                >
                  {(field) => (
                    <Select
                      {...field}
                      value={childId}
                      onChange={(event) => {
                        setChildId(event.target.value)
                        invalidateConnectionProof()
                      }}
                    >
                      <option value="">Select a discovered camera…</option>
                      {children.map((child) => (
                        <option key={child.child_id} value={child.child_id}>
                          {child.name
                            ? `${child.name} (${child.model ?? 'TC82'}) - ${child.child_id}`
                            : `${child.model ?? 'TC82'} - ${child.child_id}`}
                        </option>
                      ))}
                    </Select>
                  )}
                </FormField>
              ) : null}

              {topology === 'hub' ? (
                <FormField
                  label="H200 username"
                  id={`battery-username-${camera.id}`}
                  optional
                  hint={
                    <>
                      Leave blank to use <code>admin</code>. Some H200 firmware requires the Tapo
                      account email for its local handshake. A standalone TC82 never uses this
                      field.
                    </>
                  }
                >
                  {(field) => (
                    <Input
                      {...field}
                      value={username}
                      onChange={(event) => {
                        setUsername(event.target.value)
                        invalidateConnectionProof()
                      }}
                      placeholder="Optional H200 username"
                      autoComplete="username"
                      disabled={clearPassword}
                    />
                  )}
                </FormField>
              ) : null}

              <FormField
                label="New Tapo account password"
                id={`battery-password-${camera.id}`}
                hint={
                  <>
                    Enter the password used for the Tapo mobile app, not the Camera Account or RTSP
                    password. Sentinel encrypts it and sends it only to this private address.
                  </>
                }
              >
                {(field) => (
                  <Input
                    {...field}
                    type="password"
                    value={password}
                    onChange={(event) => {
                      setPassword(event.target.value)
                      invalidateConnectionProof()
                    }}
                    placeholder={
                      camera.has_tapo_credentials ? 'Leave blank to keep the stored password' : ''
                    }
                    autoComplete="new-password"
                    disabled={clearPassword}
                  />
                )}
              </FormField>
            </FormSection>

            {testConnection.data && testedFingerprint === fingerprint ? (
              <Alert variant="success" title="Local connection confirmed">
                {testConnection.data.model ?? 'Tapo battery camera'} answered on the LAN
                {testConnection.data.battery_percent === null
                  ? ''
                  : ` with ${testConnection.data.battery_percent}% battery`}
                {children.length > 0
                  ? ` (${children.length} hub camera(s) detected).`
                  : '.'}
              </Alert>
            ) : null}

            {connectionChanged && !connectionProven ? (
              <Alert variant="warning" title="Test the changed connection before saving">
                Re-enter the Tapo app password and confirm that this address and child ID reach the
                intended camera.
              </Alert>
            ) : null}

            {camera.has_tapo_credentials ? (
              <FormSection grouped>
                <CheckboxField
                  label="Remove the stored Tapo password"
                  description="Disable the camera first. Removing its only credential saves both changes together; status, clips, live view and timed recording stay unavailable until a new password is stored and the camera is enabled again."
                  checked={clearPassword}
                  disabled={enabled}
                  onChange={(event) => setClearPassword(event.target.checked)}
                />
                {clearPassword ? (
                  <Alert variant="warning" title="The battery camera will remain disabled">
                    Saving removes the encrypted password and explicitly disables this camera.
                  </Alert>
                ) : null}
              </FormSection>
            ) : (
              <Alert variant="warning" title="No Tapo password is stored">
                Store a password before enabling status, clips, live view or timed recording.
              </Alert>
            )}

            {missingCredential ? (
              <Alert variant="warning" title="A password is required while the camera is enabled">
                Enter the local Tapo password, or disable the camera until one can be stored.
              </Alert>
            ) : null}

            {failure ? <Alert variant="danger">{failure}</Alert> : null}
            {testFailure ? <Alert variant="danger">{testFailure}</Alert> : null}
          </DialogBody>

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="secondary"
              onClick={testNewConnection}
              disabled={!host.trim() || !password || clearPassword || testConnection.isPending}
              loading={testConnection.isPending}
            >
              Test connection
            </Button>
            <Button
              type="submit"
              disabled={
                !name.trim() ||
                !host.trim() ||
                missingCredential ||
                !connectionProven ||
                update.isPending
              }
              loading={update.isPending}
            >
              Save battery camera
            </Button>
          </DialogFooter>
        </DialogForm>
      </DialogContent>
    </Dialog>
  )
}

function errorText(error: unknown): string | null {
  if (error instanceof NetworkError) return 'Could not reach the Sentinel server.'
  if (error instanceof ApiError) return error.message
  return error ? 'The battery-camera settings could not be saved.' : null
}
