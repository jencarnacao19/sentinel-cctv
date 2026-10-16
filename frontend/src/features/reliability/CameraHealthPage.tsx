import { useState } from 'react'

import { EmptyState } from '@/components/EmptyState'
import { PageHeader } from '@/components/layout/PageHeader'
import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import {
  useCameraReliability,
  useRecentIncidents,
  useRetryCameraServices,
} from '@/features/reliability/useReliability'
import type {
  CameraIncident,
  CameraReliability,
  CameraState,
  RecorderDetail,
  RecorderSource,
  RecordingProfile,
  RecordingQuality,
} from '@/features/reliability/types'
import { formatDuration } from '@/lib/utils'

const STATE_LABELS: Record<CameraState, string> = {
  healthy: 'Healthy',
  degraded: 'Degraded',
  offline: 'Offline',
  recovering: 'Recovering',
  disabled: 'Not recording',
  privacy_paused: 'Privacy paused',
}

const STATE_VARIANT: Record<CameraState, 'success' | 'warning' | 'danger' | 'accent' | 'neutral'> =
  {
    healthy: 'success',
    degraded: 'warning',
    offline: 'danger',
    recovering: 'accent',
    disabled: 'neutral',
    privacy_paused: 'neutral',
  }

const SOURCE_LABELS: Record<RecorderSource, string> = {
  relay: 'Local relay',
  direct: 'Direct from camera',
}

/** Fixed identifiers from the API, worded here rather than on the wire. */
const ACTION_LABELS: Record<string, string> = {
  relay_stream_refresh_requested: 'Requested a refresh of this camera’s relay stream',
  recorder_restarted: 'Restarted the recorder',
  motion_worker_restarted: 'Restarted motion detection',
}

const SKIPPED_LABELS: Record<string, string> = {
  relay_stream_unavailable: 'The local relay could not be reached',
  storage_unavailable: 'Recording storage is unavailable',
  recording_not_configured: 'Continuous recording is not selected',
  motion_not_configured: 'Motion detection is switched off',
  recorder_restart_failed: 'The recorder could not be restarted',
  motion_restart_failed: 'Motion detection could not be restarted',
}

const PROFILE_LABELS: Record<RecordingProfile, string> = {
  auto: 'Automatic',
  passthrough: 'Stream copy',
  resilient: 'Stream copy, corruption-tolerant',
  normalised: 'Re-encoded',
}

/**
 * How the continuity bar is coloured.
 *
 * The thresholds are the backend's, restated rather than invented: below 95%
 * the camera is degraded and below 75% it is offline, so the bar changes colour
 * at exactly the points the state does.
 */
function coverageTone(ratio: number | null): string {
  if (ratio === null) return 'bg-(--color-border-strong)'
  if (ratio < 0.75) return 'bg-(--color-danger)'
  if (ratio < 0.95) return 'bg-(--color-warning)'
  return 'bg-(--color-success)'
}

function secondsLabel(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '—'
  if (seconds < 90) return `${Math.round(seconds)}s`
  return `${Math.round(seconds / 60)} min`
}

/**
 * Measured recording quality for one camera.
 *
 * Every number here is something the backend counted. There is deliberately no
 * uptime percentage, no bitrate and no graph over time: the API cannot prove
 * any of them, and this page exists precisely because the previous one implied
 * a health it had not measured.
 */
function QualityPanel({ quality }: { quality: RecordingQuality }) {
  const measured = quality.coverage_ratio !== null && quality.segments > 0
  const windowLabel = `${Math.round(quality.window_seconds / 60)} min`

  return (
    <div className="mt-3 border-t border-(--color-border) pt-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <p className="text-[11px] font-medium text-(--color-fg-subtle)">
          Recorded continuity · last {windowLabel}
        </p>
        <p className="tabular text-sm font-semibold">
          {measured ? `${quality.coverage_percent?.toFixed(1)}%` : 'Not yet measurable'}
        </p>
      </div>

      <div
        className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-(--color-bg-subtle)"
        role="img"
        aria-label={
          measured
            ? `${quality.coverage_percent?.toFixed(0)} per cent of the last ${windowLabel} was recorded`
            : 'Not enough footage yet to measure continuity'
        }
      >
        <div
          className={`h-full rounded-full ${coverageTone(quality.coverage_ratio)}`}
          style={{ width: `${(quality.coverage_ratio ?? 0) * 100}%` }}
        />
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3 lg:grid-cols-5">
        <Fact
          label="Gaps"
          value={
            measured
              ? `${quality.gap_count} (longest ${secondsLabel(quality.longest_gap_seconds)})`
              : '—'
          }
          tone={quality.gap_count > 0 ? 'danger' : undefined}
        />
        <Fact
          label="Short segments"
          value={measured ? `${quality.short_segments} of ${quality.segments}` : '—'}
        />
        <Fact
          label="Damaged"
          value={measured ? String(quality.damaged_segments) : '—'}
          tone={quality.damaged_segments > 0 ? 'danger' : undefined}
        />
        <Fact
          label="Restarts"
          value={String(quality.restarts_in_window)}
          tone={quality.restarts_in_window >= 4 ? 'danger' : undefined}
        />
        <Fact
          label="Stream damage"
          value={
            quality.corruption_events === 0
              ? 'None counted'
              : `${quality.corruption_per_minute.toFixed(1)}/min`
          }
          tone={quality.corruption_per_minute >= 6 ? 'danger' : undefined}
        />
        <Fact
          label="Profile"
          value={quality.profile ? PROFILE_LABELS[quality.profile] : '—'}
        />
        <Fact label="Source switches" value={String(quality.source_switches)} />
      </dl>

      {quality.source_reason_label ? (
        <p className="mt-2.5 text-xs text-(--color-fg-muted)">{quality.source_reason_label}</p>
      ) : null}
    </div>
  )
}

function dateLabel(value: string | null): string {
  if (!value) return '—'
  return new Date(value).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

function ageLabel(seconds: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return '—'
  return `${formatDuration(seconds)} ago`
}

function recorderLabel(recorder: RecorderDetail): string {
  if (!recorder.present) return 'Not running'
  switch (recorder.state) {
    case 'running':
      return 'Running'
    case 'starting':
      return 'Starting'
    case 'restarting':
      return 'Reconnecting'
    case 'failed':
      return 'Failed'
    case 'stopped':
      return 'Stopped'
    default:
      return 'Unknown'
  }
}

function freshnessLabel(camera: CameraReliability): string {
  if (!camera.recording_expected) return 'Not expected'
  return camera.recording_fresh ? 'Fresh' : 'Not fresh'
}

/** One label/value pair. Reads as a list on a phone and a row on a desktop. */
function Fact({ label, value, tone }: { label: string; value: string; tone?: 'danger' }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] font-medium text-(--color-fg-subtle)">
        {label}
      </dt>
      <dd
        className={`tabular mt-1 truncate text-sm font-medium ${
          tone === 'danger' ? 'text-(--color-danger)' : ''
        }`}
        title={value}
      >
        {value}
      </dd>
    </div>
  )
}

function StateCount({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="min-w-0 px-4 py-4 sm:px-5">
      <p className="text-[11px] font-medium text-(--color-fg-subtle)">
        {label}
      </p>
      <p className={`tabular mt-2 text-2xl font-semibold tracking-tight ${tone ?? ''}`}>{value}</p>
    </div>
  )
}

function IncidentRow({ incident, showCamera }: { incident: CameraIncident; showCamera?: string }) {
  return (
    <li className="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-2.5">
      <Badge variant={incident.active ? 'danger' : 'neutral'}>
        {incident.active ? 'Active' : 'Recovered'}
      </Badge>
      {showCamera ? <span className="text-sm font-medium">{showCamera}</span> : null}
      <span className="text-sm">{incident.reason_label}</span>
      <span className="tabular text-xs text-(--color-fg-muted)">
        {dateLabel(incident.started_at)}
      </span>
      <span className="tabular text-xs text-(--color-fg-subtle)">
        {incident.active
          ? 'still open'
          : `lasted ${formatDuration(incident.duration_seconds ?? 0)}`}
      </span>
      {incident.fault_label ? (
        <span className="w-full text-xs text-(--color-fg-muted)">{incident.fault_label}</span>
      ) : null}
    </li>
  )
}

function CameraHealthRow({
  camera,
  privacyEnabled,
}: {
  camera: CameraReliability
  privacyEnabled: boolean
}) {
  const retry = useRetryCameraServices()
  const [confirmOpen, setConfirmOpen] = useState(false)
  const recorder = camera.recorder
  const notFresh = camera.recording_expected && camera.recording_fresh === false

  return (
    <li className="border-b border-(--color-border) bg-(--color-surface) px-4 py-4 sm:px-5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <h3 className="min-w-0 truncate text-sm font-semibold">{camera.name}</h3>
        <Badge variant={STATE_VARIANT[camera.state]}>{STATE_LABELS[camera.state]}</Badge>
        {camera.issue_label ? (
          <span className="text-xs text-(--color-fg-muted)">{camera.issue_label}</span>
        ) : null}
        <div className="ml-auto">
          <Button
            variant="secondary"
            size="sm"
            onClick={() => {
              retry.reset()
              setConfirmOpen(true)
            }}
            loading={retry.isPending}
            disabled={privacyEnabled || camera.state === 'disabled'}
            title={
              privacyEnabled
                ? 'Camera services cannot be restarted while privacy mode is active.'
                : 'Restarts this camera’s recorder and motion worker, and reloads its relay ' +
                  'stream. Recording for this camera is interrupted for a few seconds. The ' +
                  'camera itself is not rebooted and its settings are unchanged.'
            }
          >
            <span aria-hidden="true">↻</span>
            Retry camera services
          </Button>
        </div>
      </div>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Retry services for {camera.name}?</DialogTitle>
            <DialogDescription>
              This asks Sentinel to refresh this camera’s local relay definition and restart its
              configured recorder and motion worker.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="space-y-3 text-sm text-(--color-fg-muted)">
            <p>
              Recording for this camera is briefly interrupted while FFmpeg closes its current
              segment and reconnects. Other cameras are not restarted.
            </p>
            <p>
              The physical camera is not rebooted, no cloud service is contacted and Sentinel will
              only report recovery after a new segment is completed.
            </p>
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => {
                setConfirmOpen(false)
                void retry.mutateAsync(camera.camera_id).catch(() => undefined)
              }}
              disabled={privacyEnabled}
            >
              Retry services
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <p className="mt-2 text-sm text-(--color-fg-muted)">{camera.detail}</p>

      <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3 lg:grid-cols-5">
        <Fact
          label="Recording"
          value={freshnessLabel(camera)}
          tone={notFresh ? 'danger' : undefined}
        />
        <Fact label="Newest segment" value={dateLabel(camera.last_segment_at)} />
        <Fact label="Segment age" value={ageLabel(camera.segment_age_seconds)} />
        <Fact
          label="Recorder"
          value={
            recorder.present && recorder.source
              ? `${recorderLabel(recorder)} · ${SOURCE_LABELS[recorder.source]}`
              : recorderLabel(recorder)
          }
        />
        <Fact
          label="Restarts"
          value={
            recorder.consecutive_failures > 0
              ? `${recorder.restart_count} (${recorder.consecutive_failures} failing)`
              : String(recorder.restart_count)
          }
        />
        <Fact label="Last healthy" value={dateLabel(camera.last_healthy_at)} />
        <Fact
          label="Motion detection"
          value={
            !camera.motion_expected
              ? 'Switched off'
              : camera.motion_running
                ? 'Running'
                : 'Not running'
          }
        />
      </dl>

      <QualityPanel quality={camera.quality} />

      {recorder.fault_label ? (
        <p className="mt-3 border-l-2 border-(--color-border-strong) pl-3 text-xs text-(--color-fg-muted)">
          {recorder.fault_label}
        </p>
      ) : null}

      {retry.isSuccess && retry.data ? (
        <Alert className="mt-3" variant="info" title="Retry requested">
          <p>{retry.data.message}</p>
          {retry.data.actions.length > 0 ? (
            <ul className="mt-1.5 list-disc pl-4">
              {retry.data.actions.map((action) => (
                <li key={action}>{ACTION_LABELS[action] ?? action}</li>
              ))}
            </ul>
          ) : null}
          {retry.data.skipped.length > 0 ? (
            <p className="mt-1.5">
              Not attempted:{' '}
              {retry.data.skipped.map((item) => SKIPPED_LABELS[item] ?? item).join('; ')}.
            </p>
          ) : null}
        </Alert>
      ) : null}

      {retry.isError ? (
        <Alert className="mt-3" variant="danger" title="The retry was not accepted.">
          {retry.error instanceof Error
            ? retry.error.message
            : 'Check the system diagnostics page.'}
        </Alert>
      ) : null}

      {camera.recent_incidents.length > 0 ? (
        <details className="mt-3 border-t border-(--color-border) pt-2.5">
          <summary className="cursor-pointer text-xs font-medium text-(--color-fg-muted) hover:text-(--color-fg)">
            Recent incidents ({camera.recent_incidents.length})
          </summary>
          <ul className="mt-1 divide-y divide-(--color-border)">
            {camera.recent_incidents.map((incident) => (
              <IncidentRow key={incident.id} incident={incident} />
            ))}
          </ul>
        </details>
      ) : null}
    </li>
  )
}

export function CameraHealthPage() {
  const reliability = useCameraReliability()
  const incidents = useRecentIncidents()
  const data = reliability.data
  const counts = data?.counts
  const names = new Map((data?.cameras ?? []).map((camera) => [camera.camera_id, camera.name]))

  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <PageHeader
        title="Camera health"
        description="Per-camera recording state, taken from the recorder processes, the segment index and the local relay. No extra connection is opened to any camera, and it refreshes every 20 seconds."
      />

      {reliability.isError ? (
        <Alert variant="danger" title="Could not load camera health.">
          Retrying automatically. Recording continues on the server regardless of this page.
        </Alert>
      ) : null}

      {data?.privacy_enabled ? (
        <Alert variant="warning" title="All cameras are paused for privacy">
          <p>{data.privacy_message}</p>
          <p className="mt-1.5">
            Cameras are shown as paused, not offline. No incidents are opened or closed while
            privacy mode is active, and camera services cannot be restarted.
          </p>
        </Alert>
      ) : null}

      {data && !data.storage_available ? (
        <Alert variant="danger" title="Recording storage is unavailable">
          Recording stops rather than filling the system disk. It resumes automatically once the
          storage is back.
        </Alert>
      ) : null}

      {data && data.relay_expected && !data.relay_ready && !data.privacy_enabled ? (
        <Alert variant="warning" title="The local stream relay is not answering">
          Live view may be unavailable. Recorders can keep writing by connecting to a camera
          directly.
        </Alert>
      ) : null}

      <section
        aria-label="Camera state summary"
        aria-busy={reliability.isPending}
        className="grid grid-cols-2 divide-x divide-y divide-(--color-border) overflow-hidden rounded-(--radius-card) border border-(--color-border) bg-(--color-surface) shadow-(--shadow-card) lg:grid-cols-4 lg:divide-y-0"
      >
        {reliability.isPending || !counts ? (
          Array.from({ length: 4 }, (_, index) => (
            <div key={index} className="p-4 sm:p-5">
              <Skeleton className="h-12" />
            </div>
          ))
        ) : (
          <>
            <StateCount
              label="Healthy"
              value={counts.healthy}
              tone={counts.healthy > 0 ? 'text-(--color-success)' : undefined}
            />
            <StateCount
              label="Recovering"
              value={counts.recovering}
              tone={counts.recovering > 0 ? 'text-(--color-accent)' : undefined}
            />
            <StateCount
              label="Degraded"
              value={counts.degraded}
              tone={counts.degraded > 0 ? 'text-(--color-warning)' : undefined}
            />
            <StateCount
              label="Offline"
              value={counts.offline}
              tone={counts.offline > 0 ? 'text-(--color-danger)' : undefined}
            />
          </>
        )}
      </section>

      {counts && counts.disabled + counts.privacy_paused > 0 ? (
        <p className="text-xs text-(--color-fg-subtle)">
          {counts.disabled} not recording by configuration · {counts.privacy_paused} paused for
          privacy. Neither counts as a fault.
        </p>
      ) : null}

      <section aria-label="Cameras">
        <div className="mb-2 flex items-baseline justify-between gap-4">
          <h2 className="text-sm font-semibold">Per-camera state</h2>
          {data ? (
            <span className="text-[11px] font-medium text-(--color-fg-subtle)">
              Checked {dateLabel(data.generated_at)}
            </span>
          ) : null}
        </div>
        {reliability.isPending ? (
          <div className="space-y-3">
            {Array.from({ length: 3 }, (_, index) => (
              <Skeleton key={index} className="h-32" />
            ))}
          </div>
        ) : data && data.cameras.length > 0 ? (
          <ul className="overflow-hidden rounded-(--radius-card) border border-(--color-border) shadow-(--shadow-card) divide-y divide-(--color-border)">
            {data.cameras.map((camera) => (
              <CameraHealthRow
                key={camera.camera_id}
                camera={camera}
                privacyEnabled={data.privacy_enabled}
              />
            ))}
          </ul>
        ) : (
          <EmptyState
            title="No cameras are configured"
            description="Camera health appears here once a camera has been added."
          />
        )}
      </section>

      <section aria-label="Incident history">
        <div className="mb-2 flex items-baseline justify-between gap-4">
          <h2 className="text-sm font-semibold">Recent incidents</h2>
          {incidents.data ? (
            <span className="text-[11px] font-medium text-(--color-fg-subtle)">
              {incidents.data.total} recorded
            </span>
          ) : null}
        </div>
        {incidents.isPending ? (
          <Skeleton className="h-20" />
        ) : incidents.isError ? (
          <Alert variant="danger" title="Could not load incident history.">
            Retrying automatically. Current camera state is still shown above.
          </Alert>
        ) : incidents.data && incidents.data.incidents.length > 0 ? (
          <div className="overflow-x-auto border-y border-(--color-border)">
            <ul className="min-w-[36rem] divide-y divide-(--color-border) px-1">
              {incidents.data.incidents.map((incident) => (
                <IncidentRow
                  key={incident.id}
                  incident={incident}
                  showCamera={names.get(incident.camera_id) ?? undefined}
                />
              ))}
            </ul>
          </div>
        ) : (
          <p className="border-y border-(--color-border) py-4 text-sm text-(--color-fg-muted)">
            No camera incidents have been recorded.
          </p>
        )}
      </section>

      <p className="flex items-center gap-2 text-xs text-(--color-fg-subtle)">
        <span aria-hidden="true">◷</span>
        An incident opens only after a fault has lasted about ninety seconds and closes only after
        three minutes of sustained recovery, so a single reconnect is not recorded as an outage.
      </p>
    </div>
  )
}
