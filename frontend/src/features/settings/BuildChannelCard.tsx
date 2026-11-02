import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { GitBranch, RefreshCw } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

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

/** A finished update or switch is worth showing for a while, not for ever. */
function recent(finishedAt: string | undefined, now = Date.now()): boolean {
  return finishedAt !== undefined && now - Date.parse(finishedAt) < 10 * 60_000
}

/**
 * Updates this server, or moves it between the stable and the dev build.
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

  // Once a switch or update this page watched has finished, the server runs a
  // new build: load its interface too, which also shows the helper's result.
  const wasPending = useRef(false)
  useEffect(() => {
    if (!state.data) return
    if (wasPending.current && !state.data.requested && state.data.last_result?.state === 'done') {
      window.location.reload()
    }
    wasPending.current = Boolean(state.data.requested)
  }, [state.data])

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

  const update = useMutation({
    mutationFn: channelApi.update,
    onSuccess: (next) => queryClient.setQueryData(queryKeys.buildChannel, next),
  })

  const requested = state.data?.requested
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
        {requested === current ? (
          <Alert variant="info">
            Updating the {NAMES[current]} build: downloading takes a minute or two, then
            Sentinel restarts if there was a newer one. This page reloads by itself when it is
            done.
          </Alert>
        ) : requested ? (
          <Alert variant="info">
            Switching to the {NAMES[requested]} build: downloading takes a minute or two, then
            Sentinel restarts. This page reloads by itself when it is done.
          </Alert>
        ) : last?.state === 'failed' ? (
          <Alert variant="danger" title="The last update or switch did not happen">
            {last.message}
          </Alert>
        ) : last?.state === 'done' && recent(last.finished_at) ? (
          <Alert variant="success">{last.message}</Alert>
        ) : null}
        {errorText(update.error, 'Could not ask for the update.') ? (
          <Alert variant="danger">{errorText(update.error, 'Could not ask for the update.')}</Alert>
        ) : null}
        {state.data && !ready ? (
          <Alert variant="warning">
            The helper that applies an update or switch is not running on this server. Run the
            installer again to add it.
          </Alert>
        ) : null}
        <div className="flex flex-wrap gap-2">
          <Button
            disabled={!ready || Boolean(requested)}
            loading={update.isPending}
            onClick={() => update.mutate()}
          >
            <RefreshCw aria-hidden="true" />
            Update
          </Button>
          <Button
            variant="secondary"
            disabled={!ready || Boolean(requested)}
            onClick={() => setOpen(true)}
          >
            <GitBranch aria-hidden="true" />
            Switch to the {NAMES[target]} build
          </Button>
        </div>
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
