import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, Lock, LockOpen } from 'lucide-react'
import { useMemo, useState } from 'react'

import { api } from '@/api/client'
import { EmptyState } from '@/components/EmptyState'
import { PageHeader } from '@/components/layout/PageHeader'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Select } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { useCameras } from '@/features/cameras/useCameras'
import { EventCleanupPanel } from '@/features/events/EventCleanupPanel'
import type { EventList, EventSummary, MotionEvent } from '@/features/events/types'
import { cn, formatDuration } from '@/lib/utils'

const PAGE_SIZE = 24

/** Local time, because an operator thinks in wall-clock, not UTC. */
function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

function formatDay(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  })
}

function EventCard({
  event,
  cameraName,
  onToggleProtected,
  busy,
}: {
  event: MotionEvent
  cameraName: string
  onToggleProtected: (event: MotionEvent) => void
  busy: boolean
}) {
  return (
    <Card className="group overflow-hidden">
      <div className="relative aspect-video bg-(--color-bg-subtle)">
        {event.thumbnail_url ? (
          <img
            src={event.thumbnail_url}
            alt={`Motion on ${cameraName} at ${formatTime(event.started_at)}`}
            className="size-full object-cover"
            loading="lazy"
          />
        ) : (
          <div className="flex size-full items-center justify-center">
            <Activity className="size-5 text-(--color-fg-subtle)" aria-hidden="true" />
          </div>
        )}

        <Button
          variant="ghost"
          size="icon"
          onClick={() => onToggleProtected(event)}
          loading={busy}
          aria-label={event.protected ? 'Remove protection' : 'Protect this event'}
          title={
            event.protected
              ? 'Protected: never deleted automatically'
              : 'Protect from automatic deletion'
          }
          className={cn(
            'absolute top-2 right-2 size-8 bg-black/60 text-white hover:bg-black/80 hover:text-white',
            !event.protected && 'opacity-0 group-hover:opacity-100 focus:opacity-100',
          )}
        >
          {event.protected ? <Lock aria-hidden="true" /> : <LockOpen aria-hidden="true" />}
        </Button>
      </div>

      <CardContent className="p-3 pt-3">
        <p className="truncate text-sm font-medium">{cameraName}</p>
        <p className="tabular mt-0.5 text-sm text-(--color-fg-muted)">
          <time dateTime={event.started_at}>{formatTime(event.started_at)}</time>
          {' · '}
          {formatDuration(event.duration_seconds)}
        </p>
      </CardContent>
    </Card>
  )
}

export function EventsPage() {
  const queryClient = useQueryClient()
  const cameras = useCameras()
  const [cameraId, setCameraId] = useState<string>('')
  const [protectedOnly, setProtectedOnly] = useState(false)
  const [timeRange, setTimeRange] = useState('7')
  const [minimumDuration, setMinimumDuration] = useState('all')
  const [offset, setOffset] = useState(0)

  const queryKey = [
    'events',
    'list',
    { cameraId, protectedOnly, timeRange, minimumDuration, offset },
  ] as const
  const events = useQuery({
    queryKey,
    queryFn: () => {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(offset),
      })
      if (cameraId) params.set('camera_id', cameraId)
      if (protectedOnly) params.set('protected', 'true')
      if (timeRange !== 'all') {
        params.set(
          'start',
          new Date(Date.now() - Number(timeRange) * 24 * 60 * 60 * 1000).toISOString(),
        )
      }
      if (minimumDuration !== 'all') {
        params.set('min_duration_seconds', minimumDuration)
      }
      return api.get<EventList>(`/events?${params.toString()}`)
    },
    refetchInterval: 30_000,
  })

  const summary = useQuery({
    queryKey: ['events', 'summary'],
    queryFn: () => api.get<EventSummary>('/events/summary'),
    refetchInterval: 30_000,
  })

  const toggleProtected = useMutation({
    mutationFn: (event: MotionEvent) =>
      api.patch<MotionEvent>(`/events/${event.id}`, { protected: !event.protected }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['events'] })
    },
  })

  const cameraNames = useMemo(
    () => new Map((cameras.data ?? []).map((camera) => [camera.id, camera.name])),
    [cameras.data],
  )

  // Grouped by day so a long list stays readable while scrolling back.
  const grouped = useMemo(() => {
    const groups = new Map<string, MotionEvent[]>()
    for (const event of events.data?.events ?? []) {
      const day = formatDay(event.started_at)
      const bucket = groups.get(day)
      if (bucket) bucket.push(event)
      else groups.set(day, [event])
    }
    return [...groups.entries()]
  }, [events.data])

  const total = events.data?.total ?? 0

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <PageHeader title="Events" description="Periods where movement was detected." />

      {summary.data ? (
        <dl className="grid overflow-hidden rounded-lg border border-(--color-border) sm:grid-cols-3">
          <div className="p-4">
            <dt className="text-xs text-(--color-fg-muted)">Event history</dt>
            <dd className="mt-1 text-xl font-semibold tabular-nums">
              {summary.data.total.toLocaleString()}
            </dd>
          </div>
          <div className="border-t border-(--color-border) p-4 sm:border-t-0 sm:border-l">
            <dt className="text-xs text-(--color-fg-muted)">With event image</dt>
            <dd className="mt-1 text-xl font-semibold tabular-nums">
              {summary.data.with_snapshot.toLocaleString()}
            </dd>
          </div>
          <div className="border-t border-(--color-border) p-4 sm:border-t-0 sm:border-l">
            <dt className="text-xs text-(--color-fg-muted)">Protected</dt>
            <dd className="mt-1 text-xl font-semibold tabular-nums">
              {summary.data.protected.toLocaleString()}
            </dd>
          </div>
        </dl>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <Select
          className="w-auto min-w-44"
          value={cameraId}
          onChange={(changed) => {
            setCameraId(changed.target.value)
            setOffset(0)
          }}
          aria-label="Filter by camera"
        >
          <option value="">All cameras</option>
          {(cameras.data ?? []).map((camera) => (
            <option key={camera.id} value={camera.id}>
              {camera.name}
            </option>
          ))}
        </Select>

        <Select
          className="w-auto min-w-36"
          value={timeRange}
          onChange={(changed) => {
            setTimeRange(changed.target.value)
            setOffset(0)
          }}
          aria-label="Filter by age"
        >
          <option value="1">Last 24 hours</option>
          <option value="7">Last 7 days</option>
          <option value="30">Last 30 days</option>
          <option value="all">All history</option>
        </Select>

        <Select
          className="w-auto min-w-40"
          value={minimumDuration}
          onChange={(changed) => {
            setMinimumDuration(changed.target.value)
            setOffset(0)
          }}
          aria-label="Filter by duration"
        >
          <option value="all">Any duration</option>
          <option value="1">At least 1 second</option>
          <option value="3">At least 3 seconds</option>
          <option value="10">At least 10 seconds</option>
        </Select>

        <Button
          variant={protectedOnly ? 'primary' : 'secondary'}
          size="sm"
          onClick={() => {
            setProtectedOnly((value) => !value)
            setOffset(0)
          }}
        >
          <Lock aria-hidden="true" />
          Protected only
        </Button>

        {total > 0 ? (
          <span className="tabular ml-auto text-sm text-(--color-fg-muted)">
            {total.toLocaleString()} events
          </span>
        ) : null}
      </div>

      {events.isError ? (
        <Alert variant="danger" title="Could not load events.">
          Detection continues on the server regardless of this page.
        </Alert>
      ) : null}

      {events.isPending ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 8 }, (_, index) => (
            <Skeleton key={index} className="aspect-4/3" />
          ))}
        </div>
      ) : grouped.length > 0 ? (
        <div className="space-y-8">
          {grouped.map(([day, dayEvents]) => (
            <section key={day} className="space-y-3">
              <h2 className="text-sm font-medium text-(--color-fg-muted)">{day}</h2>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                {dayEvents.map((event) => (
                  <EventCard
                    key={event.id}
                    event={event}
                    cameraName={cameraNames.get(event.camera_id) ?? 'Unknown camera'}
                    onToggleProtected={(target) => toggleProtected.mutate(target)}
                    busy={
                      toggleProtected.isPending && toggleProtected.variables?.id === event.id
                    }
                  />
                ))}
              </div>
            </section>
          ))}
        </div>
      ) : (
        <EmptyState
          icon={Activity}
          title={protectedOnly ? 'No protected events' : 'No motion detected yet'}
          description={
            protectedOnly
              ? 'Protect an event to keep it beyond the retention period.'
              : 'Events appear here as movement is detected on your cameras.'
          }
        />
      )}

      {total > PAGE_SIZE ? (
        <div className="flex items-center justify-between gap-4">
          <Button
            variant="secondary"
            size="sm"
            disabled={offset === 0}
            onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}
          >
            Newer
          </Button>
          <span className="tabular text-sm text-(--color-fg-muted)">
            {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
          </span>
          <Button
            variant="secondary"
            size="sm"
            disabled={offset + PAGE_SIZE >= total}
            onClick={() => setOffset((value) => value + PAGE_SIZE)}
          >
            Older
          </Button>
        </div>
      ) : null}

      {summary.data ? <EventCleanupPanel summary={summary.data} /> : null}
    </div>
  )
}
