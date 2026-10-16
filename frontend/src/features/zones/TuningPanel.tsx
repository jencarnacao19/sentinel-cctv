/**
 * Live feedback for tuning zones empirically instead of guessing.
 *
 * What this panel would ideally show is this frame's motion score and which
 * zone, if any, just suppressed a contour. Neither exists on the wire: the
 * score lives and dies inside `MotionDetector.process` in
 * `app/motion/detector.py`, and nothing records which mask pixel rejected
 * which contour. Rather than fabricate a number, this shows the closest
 * honest signal the API actually exposes -- `GET /events/status` (is the
 * worker running, is a motion window open right now, how long ago was its
 * last frame) plus this camera's most recent motion events and their peak
 * score. That is enough to tell "this zone change stopped the false
 * triggers" empirically, without inventing telemetry the backend does not
 * compute. See PROJECT_STATUS.md for why: phase 9's push events do not exist
 * yet either, so this is polled, same as everywhere else in the app.
 */

import { Activity, RadioTower } from 'lucide-react'

import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { useMotionWorkerStatus, useRecentMotionEvents } from '@/features/zones/useMotionZones'
import { formatDuration } from '@/lib/utils'

function timeAgo(iso: string | null): string {
  if (!iso) return 'never'
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  if (seconds < 1) return 'just now'
  return `${formatDuration(seconds)} ago`
}

export function TuningPanel({ cameraId, cameraName }: { cameraId: string; cameraName: string }) {
  const workerStatus = useMotionWorkerStatus()
  const recentEvents = useRecentMotionEvents(cameraId, 5)
  const worker = workerStatus.data?.workers.find((entry) => entry.camera_id === cameraId)

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <RadioTower className="size-4" aria-hidden="true" />
          Live status
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {workerStatus.isPending ? (
          <Skeleton className="h-20" />
        ) : worker ? (
          <>
            <dl className="grid grid-cols-2 gap-3 text-sm">
              <div>
                <dt className="text-xs text-(--color-fg-muted)">Worker</dt>
                <dd>
                  <Badge variant={worker.running ? 'success' : 'danger'}>
                    {worker.running ? 'running' : 'stopped'}
                  </Badge>
                </dd>
              </div>
              <div>
                <dt className="text-xs text-(--color-fg-muted)">Motion right now</dt>
                <dd>
                  <Badge variant={worker.in_event ? 'motion' : 'neutral'}>
                    {worker.in_event ? 'active' : 'quiet'}
                  </Badge>
                </dd>
              </div>
              <div>
                <dt className="text-xs text-(--color-fg-muted)">Last analysed frame</dt>
                <dd className="tabular">{timeAgo(worker.last_frame_at)}</dd>
              </div>
              <div>
                <dt className="text-xs text-(--color-fg-muted)">Reconnects</dt>
                <dd className="tabular">{worker.reconnects}</dd>
              </div>
            </dl>
            {worker.last_error ? <Alert variant="warning">{worker.last_error}</Alert> : null}
          </>
        ) : (
          <p className="text-sm text-(--color-fg-muted)">
            No motion worker is running for {cameraName}. Turn on motion detection for this
            camera to see live status here while tuning zones.
          </p>
        )}

        <div>
          <h3 className="flex items-center gap-1.5 text-sm font-medium">
            <Activity className="size-3.5" aria-hidden="true" />
            Recent motion
          </h3>
          {recentEvents.isPending ? (
            <Skeleton className="mt-2 h-16" />
          ) : recentEvents.data && recentEvents.data.events.length > 0 ? (
            <ul className="mt-2 space-y-1.5 text-sm">
              {recentEvents.data.events.map((event) => (
                <li
                  key={event.id}
                  className="tabular flex items-center justify-between gap-2 text-(--color-fg-muted)"
                >
                  <span>{new Date(event.started_at).toLocaleTimeString()}</span>
                  <span>{formatDuration(event.duration_seconds)}</span>
                  <span>peak {(event.peak_score * 100).toFixed(1)}%</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-2 text-sm text-(--color-fg-muted)">
              No motion recorded yet against the current zones.
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
