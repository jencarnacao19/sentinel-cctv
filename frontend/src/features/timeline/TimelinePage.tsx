/**
 * The playback screen: a scrubbable timeline over a video element.
 *
 * The page owns the three pieces of state the parts share -- the visible
 * window, the playhead, and the export selection -- because `TimelineBar` and
 * `PlaybackPlayer` each need to react to changes the other makes. Dragging on
 * the bar moves the playhead; the player advancing moves it back. Keeping that
 * in one place is what stops the two fighting over it.
 *
 * `PlaybackPlayer` is deliberately not React state. It owns a MediaSource, a
 * SourceBuffer and an in-flight fetch queue, none of which survive being
 * recreated on a render, so it lives in a ref for the lifetime of the video
 * element and is told about changes rather than rebuilt from them.
 */

import {
  Camera as CameraIcon,
  CheckCircle2,
  Clock3,
  Download,
  FastForward,
  FolderLock,
  Loader2,
  Maximize,
  Minimize,
  Pause,
  Play,
  Rewind,
  Scissors,
  ShieldCheck,
  SkipBack,
  SkipForward,
  Trash2,
  Video,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { ApiError, NetworkError } from '@/api/client'
import { EmptyState } from '@/components/EmptyState'
import { PageHeader } from '@/components/layout/PageHeader'
import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { useCameras } from '@/features/cameras/useCameras'
import { exportsApi } from '@/features/timeline/api'
import { TimelineBar } from '@/features/timeline/TimelineBar'
import { TimelineNavigator } from '@/features/timeline/TimelineNavigator'
import { PlaybackSpeedMenu } from '@/features/timeline/PlaybackSpeedMenu'
import { useFullscreen } from '@/features/timeline/useFullscreen'
import { clampPlaybackRate, type PlaybackSpeed } from '@/features/timeline/playbackBuffer'
import { PlaybackPlayer, type PlaybackState } from '@/features/timeline/playbackPlayer'
import { recentreWindow, type Window as TimeWindow } from '@/features/timeline/timelineMath'
import { isExportActive, type ExportJob } from '@/features/timeline/types'
import {
  useCreateExport,
  useDeleteExport,
  useExports,
  useTimeline,
  useUpdateExport,
} from '@/features/timeline/useTimeline'
import { ViewerNav } from '@/features/viewer/ViewerNav'
import { ViewerSignIn } from '@/features/viewer/ViewerSignIn'
import { useViewerCameras } from '@/features/viewer/useViewerCameras'
import { downloadViewerExport } from '@/features/viewer/access'
import { ViewerSystemStrip } from '@/features/viewer/ViewerSystemStrip'
import { cn, formatBytes } from '@/lib/utils'

/** The window the page opens on: the last six hours, which is short enough
 *  that individual events are still distinguishable at a glance. */
const INITIAL_WINDOW_SECONDS = 6 * 3600

function errorText(error: unknown): string {
  if (error instanceof NetworkError) return 'Could not reach the Sentinel server.'
  if (error instanceof ApiError) return error.message
  return 'Something went wrong.'
}

function initialWindow(): TimeWindow {
  const end = new Date()
  return { start: new Date(end.getTime() - INITIAL_WINDOW_SECONDS * 1000), end }
}

function localTime(value: Date): string {
  return value.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' })
}

function durationLabel(seconds: number): string {
  const total = Math.round(seconds)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const rest = total % 60
  if (hours > 0) return `${hours}h ${minutes}m`
  if (minutes > 0) return `${minutes}m ${rest}s`
  return `${rest}s`
}

function CameraPicker({
  cameras,
  cameraId,
  onChange,
}: {
  cameras: { id: string; name: string; status: string }[]
  cameraId: string
  onChange: (id: string) => void
}) {
  return (
    <div
      role="radiogroup"
      aria-label="Playback camera"
      className="flex gap-2 overflow-x-auto rounded-(--radius-card) border border-(--color-border) bg-(--color-surface-raised) p-2"
    >
      {cameras.map((camera) => {
        const active = camera.id === cameraId
        return (
          <button
            key={camera.id}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => onChange(camera.id)}
            className={cn(
              'flex min-h-11 shrink-0 items-center gap-2 rounded-lg border px-3 text-left transition-colors',
              active
                ? 'border-(--color-accent)/50 bg-(--color-accent)/10 text-(--color-fg)'
                : 'border-transparent text-(--color-fg-muted) hover:bg-(--color-surface-hover)',
            )}
          >
            <CameraIcon className="size-4" aria-hidden />
            <span className="max-w-40 truncate text-sm font-medium">{camera.name}</span>
            <span
              className={cn(
                'size-2 rounded-full',
                camera.status === 'online' ? 'bg-(--color-success)' : 'bg-(--color-danger)',
              )}
              aria-label={camera.status}
            />
          </button>
        )
      })}
    </div>
  )
}

const EXPORT_STATUS_VARIANT: Record<string, 'neutral' | 'success' | 'warning' | 'danger'> = {
  queued: 'neutral',
  running: 'warning',
  complete: 'success',
  failed: 'danger',
  expired: 'neutral',
  cancelled: 'neutral',
}

function ExportRow({
  job,
  onDelete,
  onToggleProtection,
  viewer = false,
}: {
  job: ExportJob
  onDelete: (id: string) => void
  onToggleProtection: (id: string, protectedClip: boolean) => void
  viewer?: boolean
}) {
  const [downloadError, setDownloadError] = useState<string | null>(null)
  const started = new Date(job.start_at)
  const ended = new Date(job.end_at)
  const seconds = (ended.getTime() - started.getTime()) / 1000

  return (
    <li className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-(--color-border) bg-(--color-surface-raised) px-4 py-3">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium">
          {localTime(started)} &middot; {durationLabel(seconds)}
        </p>
        <p className="mt-0.5 text-xs text-(--color-fg-muted)">
          {job.status === 'failed' && job.error
            ? job.error
            : job.size_bytes != null
              ? formatBytes(job.size_bytes)
              : isExportActive(job)
                ? `${Math.round(job.progress * 100)}%`
                : null}
        </p>
      </div>

      <div className="flex items-center gap-2">
        <Badge variant={job.protected ? 'success' : 'warning'}>
          {job.protected ? 'Saved' : 'Temporary'}
        </Badge>
        <Badge variant={EXPORT_STATUS_VARIANT[job.status] ?? 'neutral'}>{job.status}</Badge>
        {job.status === 'complete' && !viewer ? (
          <a
            href={exportsApi.downloadUrl(job.id, viewer)}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-(--color-border) px-3 text-xs font-medium hover:bg-(--color-surface-hover)"
          >
            <Download className="size-3.5" aria-hidden />
            Download
          </a>
        ) : null}
        {job.status === 'complete' && viewer ? (
          <Button
            variant="secondary"
            size="sm"
            onClick={() => {
              setDownloadError(null)
              void downloadViewerExport(job.id).catch(() => {
                setDownloadError('Could not download this clip.')
              })
            }}
          >
            <Download className="size-3.5" aria-hidden />
            Download
          </Button>
        ) : null}
        {!viewer && job.status !== 'expired' && job.status !== 'cancelled' ? (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onToggleProtection(job.id, !job.protected)}
            aria-label={job.protected ? 'Allow automatic deletion' : 'Keep permanently'}
            title={job.protected ? 'Allow automatic deletion' : 'Keep permanently'}
          >
            {job.protected ? (
              <ShieldCheck className="size-4" aria-hidden />
            ) : (
              <Clock3 className="size-4" aria-hidden />
            )}
          </Button>
        ) : null}
        {!viewer ? (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onDelete(job.id)}
            aria-label={`Delete export from ${localTime(started)}`}
          >
            <Trash2 className="size-4" aria-hidden />
          </Button>
        ) : null}
      </div>
      {downloadError ? <p className="w-full text-xs text-(--color-danger)">{downloadError}</p> : null}
    </li>
  )
}

function CameraTimeline({
  cameraId,
  cameraName,
  viewer = false,
}: {
  cameraId: string
  cameraName: string
  viewer?: boolean
}) {
  const [range, setRange] = useState<TimeWindow>(initialWindow)
  const [playheadAt, setPlayheadAt] = useState<Date | null>(null)
  const [selection, setSelection] = useState<TimeWindow | null>(null)
  const [playbackState, setPlaybackState] = useState<PlaybackState | null>(null)
  const [playbackDetail, setPlaybackDetail] = useState<string | null>(null)
  const [playbackRate, setPlaybackRate] = useState<PlaybackSpeed>(1)
  const stageRef = useRef<HTMLDivElement>(null)
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const playerRef = useRef<PlaybackPlayer | null>(null)
  const fullscreen = useFullscreen({ containerRef: stageRef, videoRef })
  /** Set while the player itself is reporting progress, so its own
   *  `onTimeUpdate` does not read as a user seek and restart playback. */
  const followingPlayer = useRef(false)

  const timeline = useTimeline(cameraId, range.start, range.end, viewer)
  const exports = useExports(cameraId, viewer)
  const createExport = useCreateExport(viewer)
  const deleteExport = useDeleteExport()
  const updateExport = useUpdateExport()

  // One player per camera, for the life of the video element. Recreating it
  // on a re-render would tear down the MediaSource mid-playback.
  useEffect(() => {
    const video = videoRef.current
    if (!video) return

    const player = new PlaybackPlayer(
      video,
      cameraId,
      {
        onStateChange: (state, detail) => {
          setPlaybackState(state)
          setPlaybackDetail(detail ?? null)
        },
        onTimeUpdate: (at) => {
          followingPlayer.current = true
          setPlayheadAt(at)
          followingPlayer.current = false
        },
      },
      viewer,
    )
    playerRef.current = player

    return () => {
      player.stop()
      playerRef.current = null
    }
  }, [cameraId, viewer])

  const handleSeek = useCallback((at: Date) => {
    setPlayheadAt(at)
    void playerRef.current?.seek(at)
  }, [])

  const seekRelative = useCallback(
    (seconds: number) => {
      const target = new Date((playheadAt ?? range.end).getTime() + seconds * 1000)
      if (target < range.start || target > range.end) {
        setRange((current) => recentreWindow(current, target))
      }
      handleSeek(target)
    },
    [handleSeek, playheadAt, range.end, range.start],
  )

  const jumpToEvent = useCallback(
    (direction: -1 | 1) => {
      const bands = timeline.data?.motion ?? []
      if (bands.length === 0) return
      const from = playheadAt ?? range.start
      const candidates = bands
        .map((band) => new Date(band.start))
        .filter((at) => (direction === 1 ? at > from : at < from))
      if (candidates.length === 0) return
      const target =
        direction === 1
          ? candidates.reduce((a, b) => (a < b ? a : b))
          : candidates.reduce((a, b) => (a > b ? a : b))
      // Keep the destination on screen; jumping the playhead outside the
      // visible window would leave the user looking at nothing.
      setRange((current) => recentreWindow(current, target))
      handleSeek(target)
    },
    [handleSeek, playheadAt, range.start, timeline.data],
  )

  const canExport = selection !== null
  const selectionSeconds = selection
    ? (selection.end.getTime() - selection.start.getTime()) / 1000
    : 0

  const handleExport = useCallback(() => {
    if (!selection) return
    createExport.mutate({
      camera_id: cameraId,
      start_at: selection.start.toISOString(),
      end_at: selection.end.toISOString(),
      ...(viewer ? {} : { protected: true }),
    })
  }, [cameraId, createExport, selection, viewer])

  const selectPrevious = useCallback(
    (seconds: number) => {
      const end = playheadAt ?? new Date()
      setSelection({ start: new Date(end.getTime() - seconds * 1000), end })
    },
    [playheadAt],
  )

  const isPlaying = playbackState === 'playing'
  const togglePlayback = useCallback(() => {
    const video = videoRef.current
    if (!video) return
    if (video.paused) void video.play()
    else video.pause()
  }, [])

  /**
   * Apply a chosen speed.
   *
   * The DOM write is deliberately outside the state updater: React 18's
   * StrictMode double-invokes updaters, and the previous cycler mutated
   * `videoRef.current.playbackRate` inside one, which could leave the element
   * a step ahead of the rendered label. The player owns the element's rate now
   * and re-applies it after every seek and fallback, so this only has to tell
   * it once.
   */
  const changePlaybackRate = useCallback((speed: number) => {
    const safe = clampPlaybackRate(speed)
    setPlaybackRate(safe)
    playerRef.current?.setPlaybackRate(safe)
  }, [])

  const jobs = useMemo(() => exports.data?.exports ?? [], [exports.data])

  return (
    <div className="space-y-4 sm:space-y-5">
      <TimelineNavigator
        range={range}
        playheadAt={playheadAt}
        timeline={timeline.data}
        loading={timeline.isFetching}
        onRangeChange={setRange}
        onSeek={handleSeek}
      />

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_21rem]">
        <section
          className={cn(
            'overflow-hidden border border-(--color-border) bg-black shadow-(--shadow-card)',
            fullscreen.mode === 'theatre'
              ? // The in-document fallback: a fixed layer over the viewport,
                // with the safe-area insets an iPhone needs in landscape. The
                // element is only restyled, never re-parented, so the player
                // keeps its <video>, its MediaSource and its in-flight fetches.
                'fixed inset-0 z-50 flex flex-col justify-center rounded-none ' +
                  'pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] ' +
                  'pl-[env(safe-area-inset-left)] pr-[env(safe-area-inset-right)]'
              : 'rounded-(--radius-card)',
          )}
        >
          <div ref={stageRef} className="relative">
            <video
              ref={videoRef}
              className={cn(
                'w-full bg-black object-contain',
                fullscreen.mode === 'theatre' ? 'max-h-[80dvh]' : 'aspect-video',
              )}
              playsInline
              controls={false}
              onLoadedMetadata={(event) => {
                // The player also re-applies this after every seek; the element
                // resets to its default on each load, and a fallback load never
                // reaches the player's own handler.
                event.currentTarget.defaultPlaybackRate = playbackRate
                event.currentTarget.playbackRate = playbackRate
              }}
              onPlay={() => setPlaybackState('playing')}
              onPause={() => setPlaybackState('paused')}
            />
            <div className="pointer-events-none absolute inset-x-0 top-0 flex items-start justify-between gap-3 bg-linear-to-b from-black/75 to-transparent p-3 text-white sm:p-4">
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold sm:text-base">{cameraName}</p>
                <p className="mt-0.5 text-xs text-white/70">
                  {playheadAt ? localTime(playheadAt) : 'Choose a moment below to begin playback'}
                </p>
              </div>
              <Badge
                variant={
                  playbackState === 'playing'
                    ? 'success'
                    : playbackState === 'error'
                      ? 'danger'
                      : 'neutral'
                }
              >
                {playbackState ?? 'ready'}
              </Badge>
            </div>
            {playheadAt === null ? (
              <button
                type="button"
                className="absolute inset-0 grid place-items-center text-white"
                onClick={() => handleSeek(new Date(range.end.getTime() - 5_000))}
              >
                <span className="grid size-16 place-items-center rounded-full border border-white/20 bg-black/70">
                  <Play className="ml-1 size-7" fill="currentColor" aria-hidden />
                </span>
                <span className="sr-only">Play the latest available footage</span>
              </button>
            ) : null}
          </div>

          {fullscreen.denied && fullscreen.mode === 'theatre' ? (
            <p className="material-media border-t border-(--color-media-border) px-3 py-1.5 text-xs text-(--color-media-fg-subtle)">
              This page cannot use the browser&rsquo;s own fullscreen here, so it is filling the
              window instead. Press Escape to leave.
            </p>
          ) : null}
          {/*
            Two rows on a phone, one on a desktop.

            Every control here used to sit in a single wrapping flex row, so on
            a 390px screen "Previous motion" and "Next motion" wrapped onto
            their own line and pushed the speed and fullscreen buttons off the
            bottom of a Home Assistant WebView. Transport now has its own
            centred row -- where a thumb reaches it -- and everything secondary
            sits on a second row that is allowed to be dense.
          */}
          <div className="material-media flex flex-col gap-1.5 border-t border-(--color-media-border) p-2 text-(--color-media-fg) sm:flex-row sm:items-center sm:gap-1.5 sm:px-4 sm:py-3">
            <div className="flex items-center justify-center gap-1 sm:justify-start">
              <Button
                variant="ghost"
                size="icon"
                className="size-11 text-(--color-media-fg) hover:bg-white/10 sm:size-10"
                onClick={() => jumpToEvent(-1)}
                disabled={(timeline.data?.motion.length ?? 0) === 0}
                aria-label="Jump to the previous motion event"
                title="Previous motion"
              >
                <SkipBack aria-hidden />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className="size-11 text-(--color-media-fg) hover:bg-white/10 sm:size-10"
                onClick={() => seekRelative(-10)}
                disabled={playheadAt === null}
                aria-label="Go back 10 seconds"
              >
                <Rewind aria-hidden />
              </Button>
              <Button
                variant="secondary"
                size="icon"
                className="size-12 rounded-full sm:size-11"
                onClick={togglePlayback}
                disabled={playheadAt === null}
                aria-label={isPlaying ? 'Pause' : 'Play'}
              >
                {isPlaying ? <Pause aria-hidden /> : <Play className="ml-0.5" aria-hidden />}
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className="size-11 text-(--color-media-fg) hover:bg-white/10 sm:size-10"
                onClick={() => seekRelative(10)}
                disabled={playheadAt === null}
                aria-label="Go forward 10 seconds"
              >
                <FastForward aria-hidden />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className="size-11 text-(--color-media-fg) hover:bg-white/10 sm:size-10"
                onClick={() => jumpToEvent(1)}
                disabled={(timeline.data?.motion.length ?? 0) === 0}
                aria-label="Jump to the next motion event"
                title="Next motion"
              >
                <SkipForward aria-hidden />
              </Button>
            </div>
            <div className="flex items-center justify-center gap-1.5 sm:ml-auto">
              <PlaybackSpeedMenu
                value={playbackRate}
                onChange={changePlaybackRate}
                disabled={playheadAt === null}
              />
              <Button
                variant="ghost"
                size="icon"
                className="size-11 text-(--color-media-fg) hover:bg-white/10 sm:size-10"
                onClick={fullscreen.toggle}
                aria-label={fullscreen.isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}
                aria-pressed={fullscreen.isFullscreen}
              >
                {fullscreen.isFullscreen ? (
                  <Minimize aria-hidden />
                ) : (
                  <Maximize aria-hidden />
                )}
              </Button>
            </div>
          </div>
        </section>

        <aside className="rounded-(--radius-card) border border-(--color-border) bg-(--color-surface-raised) p-4 shadow-(--shadow-card)">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h2 className="flex items-center gap-2 text-sm font-semibold">
                <Scissors className="size-4 text-(--color-accent)" aria-hidden /> Save a clip
              </h2>
              <p className="mt-1 text-xs text-(--color-fg-subtle)">
                Permanent footage, excluded from automatic deletion.
              </p>
            </div>
            {selection ? (
              <Button
                variant="ghost"
                size="icon"
                className="size-8"
                onClick={() => setSelection(null)}
                aria-label="Clear clip selection"
              >
                <X aria-hidden />
              </Button>
            ) : null}
          </div>

          <div className="mt-4 grid grid-cols-2 gap-2">
            {[
              { label: 'Last 15s', seconds: 15 },
              { label: 'Last 30s', seconds: 30 },
              { label: 'Last 1m', seconds: 60 },
              { label: 'Last 5m', seconds: 300 },
            ].map((preset) => (
              <Button
                key={preset.seconds}
                variant="secondary"
                size="sm"
                className="min-h-10"
                onClick={() => selectPrevious(preset.seconds)}
              >
                {preset.label}
              </Button>
            ))}
          </div>

          <div className="mt-4 rounded-lg bg-(--color-bg-subtle) p-3">
            {selection ? (
              <dl className="space-y-2 text-xs">
                <div className="flex justify-between gap-3">
                  <dt className="text-(--color-fg-subtle)">Starts</dt>
                  <dd className="text-right font-medium tabular-nums">{localTime(selection.start)}</dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-(--color-fg-subtle)">Ends</dt>
                  <dd className="text-right font-medium tabular-nums">{localTime(selection.end)}</dd>
                </div>
                <div className="flex justify-between gap-3 border-t border-(--color-border) pt-2">
                  <dt className="text-(--color-fg-subtle)">Duration</dt>
                  <dd className="font-semibold tabular-nums">{durationLabel(selectionSeconds)}</dd>
                </div>
              </dl>
            ) : (
              <p className="text-xs leading-relaxed text-(--color-fg-subtle)">
                Drag over the timeline, or choose one of the quick durations above.
              </p>
            )}
          </div>

          <Button
            className="mt-4 min-h-11 w-full"
            disabled={!canExport || createExport.isPending}
            onClick={handleExport}
          >
            {createExport.isPending ? (
              <Loader2 className="size-4 animate-spin" aria-hidden />
            ) : (
              <FolderLock className="size-4" aria-hidden />
            )}
            Save permanently
          </Button>

          {createExport.isSuccess ? (
            <p className="mt-3 flex items-center gap-1.5 text-xs text-(--color-success)">
              <CheckCircle2 className="size-4" aria-hidden /> Clip queued successfully
            </p>
          ) : null}
          {createExport.isError ? (
            <p className="mt-3 text-xs text-(--color-danger)">{errorText(createExport.error)}</p>
          ) : null}
          <p className="mt-3 text-[11px] leading-relaxed text-(--color-fg-subtle)">
            The cut begins at the nearest keyframe and may include a few extra seconds. Video is
            copied without re-encoding.
          </p>
        </aside>
      </div>

      {playbackState === 'error' || playbackState === 'ended' ? (
        <Alert variant={playbackState === 'error' ? 'danger' : 'info'} title={playbackDetail ?? ''}>
          {playbackState === 'ended'
            ? 'Pick another time on the timeline below.'
            : 'Try a different browser, or download the segment instead.'}
        </Alert>
      ) : null}

      <section className="rounded-(--radius-card) border border-(--color-border) bg-(--color-surface-raised) p-3 shadow-(--shadow-card) sm:p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-sm font-semibold">Recording timeline</h2>
            <p className="mt-0.5 text-xs text-(--color-fg-subtle)">
              Tap to play · drag to select · pinch controls are replaced by reliable zoom buttons
            </p>
          </div>
          {selection ? (
            <Badge variant="success">{durationLabel(selectionSeconds)} selected</Badge>
          ) : null}
        </div>
        <TimelineBar
          range={range}
          onRangeChange={setRange}
          timeline={timeline.data}
          playheadAt={playheadAt}
          onSeek={(at) => {
            if (followingPlayer.current) return
            handleSeek(at)
          }}
          selection={selection}
          onSelectionChange={setSelection}
        />
        {timeline.isFetching ? (
          <p className="mt-2 flex items-center gap-2 text-xs text-(--color-fg-subtle)">
            <Loader2 className="size-3.5 animate-spin" aria-hidden /> Loading this time range…
          </p>
        ) : null}
      </section>

      {timeline.isError ? (
        <Alert variant="danger" title="Could not load the timeline.">
          {errorText(timeline.error)}
        </Alert>
      ) : null}

      <section className="space-y-3 rounded-(--radius-card) border border-(--color-border) bg-(--color-surface-raised) p-3 sm:p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-sm font-semibold">Saved clips</h2>
            <p className="mt-0.5 text-xs text-(--color-fg-subtle)">
              Permanent exports for {cameraName}. Download them whenever you need them.
            </p>
          </div>
          {!viewer ? (
            <Button asChild variant="ghost" size="sm">
              <Link to="/admin/clips">
                <Scissors className="size-4" aria-hidden /> Manage all clips
              </Link>
            </Button>
          ) : null}
        </div>

        {exports.isPending ? (
          <Skeleton className="h-16" />
        ) : jobs.length > 0 ? (
          <ul className="space-y-2">
            {jobs.map((job) => (
              <ExportRow
                key={job.id}
                job={job}
                onDelete={(id) => deleteExport.mutate(id)}
                onToggleProtection={(id, protectedClip) =>
                  updateExport.mutate({ id, protectedClip })
                }
                viewer={viewer}
              />
            ))}
          </ul>
        ) : (
          <p className="rounded-lg bg-(--color-bg-subtle) px-3 py-4 text-sm text-(--color-fg-subtle)">
            No saved clips for this camera yet.
          </p>
        )}
      </section>
    </div>
  )
}

export function TimelinePage() {
  const cameras = useCameras()
  const [cameraId, setCameraId] = useState('')

  useEffect(() => {
    if (!cameraId && cameras.data && cameras.data.length > 0) {
      setCameraId(cameras.data[0].id)
    }
  }, [cameras.data, cameraId])

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <PageHeader
        title="Timeline"
        description="Scrub through recorded footage. Shaded bands are movement; breaks in the bar are periods with no recording."
      />

      <CameraPicker cameras={cameras.data ?? []} cameraId={cameraId} onChange={setCameraId} />

      {cameras.isError ? (
        <Alert variant="danger" title="Could not load cameras.">
          {errorText(cameras.error)}
        </Alert>
      ) : null}

      {cameras.isPending ? (
        <Skeleton className="h-96" />
      ) : cameras.data && cameras.data.length === 0 ? (
        <EmptyState
          icon={Video}
          title="No cameras yet"
          description="Add a camera and let it record; its footage will show up here."
          action={
            <Button asChild>
              <Link to="/admin/cameras">Add camera</Link>
            </Button>
          }
        />
      ) : cameraId ? (
        <CameraTimeline
          key={cameraId}
          cameraId={cameraId}
          cameraName={cameras.data?.find((camera) => camera.id === cameraId)?.name ?? 'Camera'}
        />
      ) : null}
    </div>
  )
}

export function ViewerTimelinePage() {
  const cameras = useViewerCameras()
  const [cameraId, setCameraId] = useState('')

  useEffect(() => {
    if (!cameraId && cameras.data && cameras.data.length > 0) {
      setCameraId(cameras.data[0].id)
    }
  }, [cameras.data, cameraId])

  return (
    <div className="min-h-dvh overflow-x-hidden bg-(--color-bg)">
      <ViewerNav current="playback" />

      <main className="mx-auto max-w-6xl space-y-6 px-4 py-5 pb-[max(env(safe-area-inset-bottom,0px),1.5rem)] sm:px-6 lg:px-8">
        <PageHeader
          title="Playback"
          description="Review recordings, save a permanent clip and download it without opening settings."
        />

        {cameras.error instanceof ApiError && cameras.error.isUnauthorized ? (
          <ViewerSignIn />
        ) : cameras.isError ? (
          <Alert variant="danger" title="Could not load cameras">
            The recorder continues running on the server.
          </Alert>
        ) : null}

        {!cameras.isError ? <ViewerSystemStrip compact /> : null}

        {cameras.isError ? null : cameras.isPending ? (
          <Skeleton className="h-96" />
        ) : cameras.data && cameras.data.length === 0 ? (
          <EmptyState
            icon={Video}
            title="No enabled cameras"
            description="An administrator can discover or enable cameras in Sentinel settings."
          />
        ) : (
          <>
            <CameraPicker cameras={cameras.data ?? []} cameraId={cameraId} onChange={setCameraId} />
            {cameraId ? (
              <CameraTimeline
                key={cameraId}
                cameraId={cameraId}
                cameraName={cameras.data?.find((camera) => camera.id === cameraId)?.name ?? 'Camera'}
                viewer
              />
            ) : null}
          </>
        )}
      </main>
    </div>
  )
}
