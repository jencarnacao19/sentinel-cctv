import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { GitBranch } from 'lucide-react'
import { useState } from 'react'

import { channelApi, queryKeys } from '@/api/endpoints'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { errorText } from '@/lib/errors'
import type { BuildChannel } from '@/types/api'

const NAMES: Record<BuildChannel, string> = { stable: 'stable', dev: 'dev' }

/** The helper looks every 30 seconds; two minutes of silence means it is not running. */
function helperRunning(seenAt: string | null, now = Date.now()): boolean {
  return seenAt !== null && now - Date.parse(seenAt) < 2 * 60_000
}

/**
 * Moves this server between the stable and the dev build.
 *
 * The switch restarts Sentinel on a build that may not be a release, so it asks
 * for the build's name typed in capitals, and the server refuses it otherwise.
 */
export function BuildChannelCard() {
  const queryClient = useQueryClient()
  const state = useQuery({
    queryKey: queryKeys.buildChannel,
    queryFn: channelApi.state,
    // While a switch is pending the server restarts under this page.
    refetchInterval: (query) => (query.state.data?.requested ? 5_000 : false),
  })
  const [open, setOpen] = useState(false)
  const [typed, setTyped] = useState('')

  const current = state.data?.current ?? 'stable'
  const target: BuildChannel = current === 'dev' ? 'stable' : 'dev'
  const word = target.toUpperCase()
  const ready = state.data ? helperRunning(state.data.helper_seen_at) : false

  const request = useMutation({
    mutationFn: () => channelApi.request(target, typed),
    onSuccess: (next) => {
      queryClient.setQueryData(queryKeys.buildChannel, next)
      setOpen(false)
      setTyped('')
    },
  })

  const last = state.data?.last_result
  return (
    <Card>
      <CardHeader>
        <CardTitle>Build</CardTitle>
        <CardDescription>
          This server runs the <strong>{NAMES[current]}</strong> build. The dev build gets new
          features first, before they are released, with the bugs that can come with them.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {state.data?.requested ? (
          <Alert variant="info">
            Switching to the {NAMES[state.data.requested]} build. Sentinel restarts within a
            minute; this page carries on by itself.
          </Alert>
        ) : last?.state === 'failed' ? (
          <Alert variant="danger" title="The last switch did not happen">
            {last.message}
          </Alert>
        ) : null}
        {state.data && !ready ? (
          <Alert variant="warning">
            The helper that applies a switch is not running on this server. Run the installer
            again to add it.
          </Alert>
        ) : null}
        <Button
          variant="secondary"
          disabled={!ready || Boolean(state.data?.requested)}
          onClick={() => setOpen(true)}
        >
          <GitBranch aria-hidden="true" />
          Switch to the {NAMES[target]} build
        </Button>
      </CardContent>

      <Dialog
        open={open}
        onOpenChange={(next) => {
          setOpen(next)
          if (!next) setTyped('')
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Switch to the {NAMES[target]} build?</DialogTitle>
            <DialogDescription>Sentinel restarts, and stops recording for a moment.</DialogDescription>
          </DialogHeader>
          <DialogBody className="space-y-3">
            <Alert variant="warning">
              {target === 'dev'
                ? 'The dev build is not a release: a new feature can break recording or playback. The database and compose.yml are backed up first, and the previous build comes back if the new one does not start.'
                : 'If the dev build changed how the database is stored, the stable build may need the backup made when you switched to dev.'}
            </Alert>
            <div className="space-y-2">
              <Label htmlFor="build-confirm">Type {word} to confirm</Label>
              <Input
                id="build-confirm"
                value={typed}
                onChange={(event) => setTyped(event.target.value)}
                autoComplete="off"
              />
            </div>
            {errorText(request.error, 'Could not ask for the switch.') ? (
              <Alert variant="danger">{errorText(request.error, 'Could not ask for the switch.')}</Alert>
            ) : null}
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Stay on {NAMES[current]}
            </Button>
            <Button
              variant="danger"
              disabled={typed !== word}
              loading={request.isPending}
              onClick={() => request.mutate()}
            >
              Switch to {NAMES[target]}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}
