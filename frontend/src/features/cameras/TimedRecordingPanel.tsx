import { BatteryWarning, CircleStop, Timer } from 'lucide-react'
import { useEffect, useState } from 'react'

import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  useStartTimedRecording,
  useStopTimedRecording,
  useTimedRecording,
  useTimedRecordingOptions,
} from '@/features/cameras/useTimedRecording'
import { errorText } from '@/lib/errors'
import { cn } from '@/lib/utils'
import type { Camera, TimedRecordingState, TimedRecordingStatus } from '@/types/api'

const FAILED = 'The timed recording request did not complete.'

interface Props {
  camera: Camera
  open: boolean
  batteryPercent?: number | null
}

/**
 * Record a battery camera on purpose, for a fixed length of time.
 *
 * The confirmation step is the point of this panel, not friction around it. A
 * TC82 exists on a battery because it sleeps; holding it awake for an hour is a
 * real cost, sometimes the whole remaining charge, and the administrator has to
 * see the current percentage and accept that before anything opens.
 *
 * While it runs, the panel reports what is actually happening -- time left,
 * bytes captured, whether the camera is currently connected -- rather than a
 * spinner. When it ends early, it says so and says how much was kept.
 */
export function TimedRecordingPanel({ camera, open, batteryPercent }: Props) {
  const options = useTimedRecordingOptions(camera.id, open)
  const session = useTimedRecording(camera.id, open)
  const start = useStartTimedRecording(camera.id)
  const stop = useStopTimedRecording(camera.id)

  const [minutes, setMinutes] = useState<number | null>(null)
  const [custom, setCustom] = useState('')
  const [confirming, setConfirming] = useState(false)

  useEffect(() => {
    if (!open) {
      setConfirming(false)
      setCustom('')
      start.reset()
      stop.reset()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const active = session.data && !session.data.finished ? session.data : null
  const finished = session.data?.finished ? session.data : null
  const maxMinutes = options.data?.max_minutes ?? 60
  const presets = options.data?.presets_minutes ?? []
  const chosen = minutes ?? (custom ? Number(custom) : null)
  const customValue = Number(custom)
  const customInvalid =
    custom !== '' &&
    (!Number.isInteger(customValue) || customValue < 1 || customValue > maxMinutes)
  const canStart = Boolean(chosen) && !customInvalid && !start.isPending && !active

  // Deliberately not including `options.error`: that failure has its own branch
  // below, and titling it "could not start" would report a failure to do
  // something nobody asked for yet.
  const failure = errorText(start.error, FAILED) ?? errorText(stop.error, FAILED) ?? errorText(session.error, FAILED)
  const displayedBattery = batteryPercent ?? options.data?.battery_percent ?? null

  if (active) {
    return <RunningSession status={active} onStop={() => void stop.mutateAsync().catch(() => undefined)} stopping={stop.isPending} error={errorText(stop.error, FAILED) ?? errorText(session.error, FAILED)} />
  }

  return (
    <section className="space-y-3 rounded-lg border border-(--color-border) p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold">Timed recording</h3>
          <p className="mt-1 text-xs text-(--color-fg-muted)">
            One bounded recording, saved to Sentinel's timeline like any other footage.
          </p>
        </div>
        {displayedBattery != null ? (
          <Badge variant={displayedBattery <= 25 ? 'warning' : 'neutral'}>
            Battery {displayedBattery}%
          </Badge>
        ) : (
          <Badge variant="neutral">Battery unknown</Badge>
        )}
      </div>

      {finished ? <Outcome status={finished} /> : null}
      {failure ? (
        <Alert variant="danger" title="Timed recording could not start">
          {failure}
        </Alert>
      ) : null}

      {confirming ? (
        <div className="space-y-3">
          <Alert variant="warning" title="This keeps the camera awake and may flatten its battery">
            <div className="space-y-2">
              <p>{options.data?.warning}</p>
              <p>
                Requested duration: <strong>{chosen} minutes</strong>. Battery now:{' '}
                <strong>
                  {displayedBattery != null
                    ? `${displayedBattery}%`
                    : 'unknown'}
                </strong>
                . If the battery runs out the camera switches off and Sentinel keeps whatever was
                recorded up to that point.
              </p>
            </div>
          </Alert>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="danger"
              size="sm"
              loading={start.isPending}
              onClick={() => {
                setConfirming(false)
                if (chosen) void start.mutateAsync(chosen).catch(() => undefined)
              }}
            >
              <BatteryWarning aria-hidden="true" />
              Accept the battery cost and record
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : options.isPending ? (
        <p className="text-xs text-(--color-fg-muted)">Reading the camera's battery level…</p>
      ) : options.isError ? (
        // The durations come from the same request as the battery level, so
        // there is nothing honest to offer until the camera answers. Showing an
        // empty "Duration" group with no options was the worse alternative.
        <div className="space-y-2">
          <p className="text-xs text-(--color-fg-muted)">
            Sentinel could not reach this camera, so it cannot show the battery level or start a
            recording. Recording would keep it awake, and that is not something to begin without
            knowing the charge.
          </p>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => void options.refetch()}
            loading={options.isFetching}
          >
            Try again
          </Button>
        </div>
      ) : (
        <div className="space-y-3">
          <fieldset className="space-y-2">
            <legend className="text-xs font-medium text-(--color-fg-muted)">Duration</legend>
            <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Recording duration">
              {presets.map((preset) => (
                <button
                  key={preset}
                  type="button"
                  role="radio"
                  aria-checked={minutes === preset}
                  onClick={() => {
                    setMinutes(preset)
                    setCustom('')
                  }}
                  className={cn(
                    'min-h-11 rounded-md border px-3 text-sm font-medium transition-colors sm:min-h-9',
                    minutes === preset
                      ? 'border-(--color-accent) bg-(--color-accent-subtle) text-(--color-fg)'
                      : 'border-(--color-border) bg-(--color-surface-raised) text-(--color-fg-muted) hover:bg-(--color-bg-subtle)',
                  )}
                >
                  {preset} min
                </button>
              ))}
            </div>
          </fieldset>

          <div className="space-y-1">
            <Label htmlFor={`timed-custom-${camera.id}`}>
              Or a custom duration (1–{maxMinutes} minutes)
            </Label>
            <Input
              id={`timed-custom-${camera.id}`}
              type="number"
              inputMode="numeric"
              min={1}
              max={maxMinutes}
              value={custom}
              aria-invalid={customInvalid}
              aria-describedby={customInvalid ? `timed-custom-error-${camera.id}` : undefined}
              onChange={(event) => {
                setCustom(event.target.value)
                setMinutes(null)
              }}
              placeholder={`Up to ${maxMinutes}`}
            />
            {customInvalid ? (
              <p id={`timed-custom-error-${camera.id}`} className="text-xs text-(--color-danger)">
                Enter a whole number of minutes between 1 and {maxMinutes}.
              </p>
            ) : null}
          </div>

          <Button size="sm" disabled={!canStart} onClick={() => setConfirming(true)}>
            <Timer aria-hidden="true" />
            Start timed recording
          </Button>
        </div>
      )}
    </section>
  )
}

function RunningSession({
  status,
  onStop,
  stopping,
  error,
}: {
  status: TimedRecordingStatus
  onStop: () => void
  stopping: boolean
  error: string | null
}) {
  return (
    <section className="space-y-3 rounded-lg border border-(--color-recording)/40 bg-(--color-recording)/5 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <span
            className={cn(
              'size-2 rounded-full bg-(--color-recording)',
              status.connection === 'connected' ? 'pulse-recording' : 'opacity-50',
            )}
            aria-hidden="true"
          />
          Timed recording in progress
        </h3>
        <Badge variant={CONNECTION_VARIANT[status.connection]}>
          {CONNECTION_LABEL[status.connection]}
        </Badge>
      </div>

      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Metric label="Time left" value={formatDuration(status.seconds_remaining)} live />
        <Metric label="Recorded" value={formatBytes(status.bytes_recorded)} />
        <Metric label="Requested" value={`${Math.round(status.requested_seconds / 60)} min`} />
        <Metric
          label="Reconnects"
          value={String(status.reconnects)}
          hint={status.reconnects > 0 ? 'The camera closed the stream and it was reopened.' : null}
        />
      </dl>

      <p className="text-xs text-(--color-fg-muted)">{status.message}</p>
      {error ? (
        <p role="alert" className="text-xs text-(--color-danger)">
          {error}
        </p>
      ) : null}

      <Button variant="danger" size="sm" onClick={onStop} loading={stopping}>
        <CircleStop aria-hidden="true" />
        Stop and save
      </Button>
    </section>
  )
}

function Outcome({ status }: { status: TimedRecordingStatus }) {
  const variant = status.state === 'completed' ? 'info' : status.state === 'failed' ? 'danger' : 'warning'
  return (
    <Alert variant={variant} title={OUTCOME_TITLE[status.state]}>
      <div className="space-y-1">
        <p>{status.message}</p>
        {status.segment_id ? (
          <p className="text-(--color-fg-subtle)">
            Saved to the timeline as a manual Tapo battery recording, playable in Sentinel and
            Home Assistant.
          </p>
        ) : null}
      </div>
    </Alert>
  )
}

function Metric({
  label,
  value,
  hint,
  live = false,
}: {
  label: string
  value: string
  hint?: string | null
  live?: boolean
}) {
  return (
    <div>
      <dt className="text-[11px] text-(--color-fg-subtle)">{label}</dt>
      <dd
        className="tabular text-sm font-medium"
        // Only the countdown is announced; four polite regions updating every
        // second would make a screen reader unusable.
        aria-live={live ? 'polite' : undefined}
      >
        {value}
      </dd>
      {hint ? <p className="text-[11px] text-(--color-fg-subtle)">{hint}</p> : null}
    </div>
  )
}

const OUTCOME_TITLE: Record<TimedRecordingState, string> = {
  completed: 'Timed recording finished',
  partial: 'Recording ended early — the footage was kept',
  failed: 'No video was captured',
  starting: 'Starting',
  recording: 'Recording',
  reconnecting: 'Reconnecting',
  stopping: 'Stopping',
}

const CONNECTION_LABEL: Record<TimedRecordingStatus['connection'], string> = {
  connecting: 'Opening',
  connected: 'Connected',
  reconnecting: 'Reconnecting',
  closed: 'Closed',
}

const CONNECTION_VARIANT: Record<
  TimedRecordingStatus['connection'],
  'success' | 'warning' | 'neutral'
> = {
  connecting: 'neutral',
  connected: 'success',
  reconnecting: 'warning',
  closed: 'neutral',
}

function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const rest = total % 60
  const pad = (value: number) => String(value).padStart(2, '0')
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(rest)}` : `${minutes}:${pad(rest)}`
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KiB', 'MiB', 'GiB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`
}
