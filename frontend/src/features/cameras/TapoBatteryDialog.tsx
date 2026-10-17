import {
  Download,
  Maximize2,
  Minimize2,
  Play,
  Square,
  Volume2,
  VolumeX,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { apiFetch } from '@/api/client'
import { camerasApi } from '@/api/endpoints'
import { TimedRecordingPanel } from '@/features/cameras/TimedRecordingPanel'
import { isHlsSupported, prefersNativeHls } from '@/features/live/hls'
import { useFullscreen } from '@/features/timeline/useFullscreen'
import { errorText } from '@/lib/errors'
import { cn } from '@/lib/utils'
import { Alert } from '@/components/ui/alert'
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
import {
  useSyncTapoBattery,
  useTapoBatteryStatus,
} from '@/features/cameras/useTapoBattery'
import type { Camera } from '@/types/api'

interface Props {
  camera: Camera
  open: boolean
  onOpenChange: (open: boolean) => void
}

type MpegtsModule = typeof import('mpegts.js')
type MpegtsPlayer = ReturnType<MpegtsModule['default']['createPlayer']>
type MpegtsLibrary = MpegtsModule['default']

let mpegtsLibrary: Promise<MpegtsLibrary | null> | null = null

function loadMpegts(): Promise<MpegtsLibrary | null> {
  mpegtsLibrary ??= import('mpegts.js')
    .then((module) => module.default)
    .catch(() => null)
  return mpegtsLibrary
}

/**
 * What the player's error actually means, in the operator's terms.
 */
function describeStreamError(type: string, detail: string, info?: unknown): string {
  const metadata = typeof info === 'object' && info !== null ? (info as Record<string, unknown>) : null
  const candidate = metadata?.code ?? metadata?.status
  const candidateText =
    typeof candidate === 'string' || typeof candidate === 'number' ? String(candidate) : ''
  const status =
    typeof candidate === 'number'
      ? candidate
      : Number(
          candidateText.match(/\b(\d{3})\b/)?.[1] ?? /\b(\d{3})\b/.exec(detail ?? '')?.[1],
        )
  if (status === 422) {
    return 'The camera rejected its saved credentials. Save the current password of its owning Tapo account in Configure.'
  }
  if (status === 424) {
    return 'The camera could not deliver video. Wake it in the Tapo app and check the saved address and credentials.'
  }
  if (status >= 500) return 'The connection to Sentinel failed before playable video arrived.'
  if (status === 409) {
    return 'This camera already has a live view or a timed recording open. Close that first.'
  }
  if (status === 423) return 'Cameras are paused by privacy mode.'
  if (status === 429) return 'Too many camera operations in a short time. Try again shortly.'
  if (status === 401 || status === 403) return 'Your session has expired. Sign in again.'
  if (type === 'NetworkError') return 'The live stream could not be reached.'
  return 'The Direct Stream ended or the camera stopped responding.'
}

const FAILED = 'The local battery-camera operation failed.'

export function TapoBatteryDialog({ camera, open, onOpenChange }: Props) {
  const status = useTapoBatteryStatus(camera.id, open)
  const sync = useSyncTapoBattery(camera.id)
  const videoRef = useRef<HTMLVideoElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const playerRef = useRef<MpegtsPlayer | null>(null)
  const nativeStopRef = useRef<string | null>(null)
  const generationRef = useRef(0)
  const playbackTimeoutRef = useRef<number | null>(null)
  const [streaming, setStreaming] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [streamError, setStreamError] = useState<string | null>(null)
  const [playbackBlocked, setPlaybackBlocked] = useState(false)
  const [muted, setMuted] = useState(true)
  const [activeTab, setActiveTab] = useState<'live' | 'recording' | 'clips'>('live')
  const fullscreen = useFullscreen({ containerRef, videoRef })

  const stopStream = useCallback(() => {
    generationRef.current += 1
    if (playbackTimeoutRef.current != null) {
      window.clearTimeout(playbackTimeoutRef.current)
      playbackTimeoutRef.current = null
    }
    const player = playerRef.current
    playerRef.current = null
    if (player) {
      player.pause()
      player.unload()
      player.detachMediaElement()
      player.destroy()
    }
    const stopUrl = nativeStopRef.current
    nativeStopRef.current = null
    if (stopUrl) {
      void fetch(stopUrl, { method: 'DELETE', credentials: 'same-origin', keepalive: true })
        .catch(() => undefined)
    }
    const video = videoRef.current
    if (video?.hasAttribute('src')) {
      video.pause()
      video.removeAttribute('src')
      video.load()
    }
    setStreaming(false)
    setConnecting(false)
    setPlaybackBlocked(false)
  }, [])

  useEffect(() => {
    if (open) void loadMpegts()
    if (!open) {
      stopStream()
      setActiveTab('live')
    }
    return () => stopStream()
  }, [open, stopStream])

  async function startStream() {
    stopStream()
    const generation = generationRef.current
    setStreamError(null)
    setPlaybackBlocked(false)
    setConnecting(true)
    playbackTimeoutRef.current = window.setTimeout(() => {
      if (generation !== generationRef.current) return
      setStreamError('No playable video arrived from the camera. The local stream was closed.')
      stopStream()
    }, 40_000)
    try {
      const video = videoRef.current
      if (!video) return
      const native = prefersNativeHls(video)
      const mpegts = native ? null : await loadMpegts()
      if (generation !== generationRef.current) return
      if (native || (!mpegts?.isSupported() && isHlsSupported(video))) {
        const prefix = `/api/v1/cameras/${encodeURIComponent(camera.id)}/tapo-battery/hls/`
        const session = await apiFetch<{ url: string; stop_url: string }>(
          `/cameras/${encodeURIComponent(camera.id)}/tapo-battery/hls-session`,
          { method: 'POST', timeoutMs: 38_000 },
        )
        if (!session.stop_url?.startsWith(prefix) || session.url !== `${session.stop_url}/playlist.m3u8`) {
          throw new Error('Sentinel returned an invalid live-stream address.')
        }
        if (generation !== generationRef.current) {
          void fetch(session.stop_url, { method: 'DELETE', credentials: 'same-origin' }).catch(() => undefined)
          return
        }
        nativeStopRef.current = session.stop_url
        setStreaming(true)
        video.src = session.url
        video.load()
        void video.play().catch((error: unknown) => handlePlayFailure(error, generation))
        return
      }
      if (!mpegts?.isSupported()) throw new Error('This browser cannot play the local Direct Stream.')
      const player = mpegts.createPlayer(
        { type: 'mpegts', isLive: true, url: camerasApi.tapoBatteryLiveUrl(camera.id) },
        {
          enableWorker: false,
          enableStashBuffer: false,
          autoCleanupSourceBuffer: true,
          autoCleanupMaxBackwardDuration: 20,
          autoCleanupMinBackwardDuration: 10,
          liveBufferLatencyChasing: true,
          liveBufferLatencyMaxLatency: 3,
          liveBufferLatencyMinRemain: 0.8,
        },
      )
      playerRef.current = player
      player.attachMediaElement(video)
      player.on(mpegts.Events.ERROR, (type: string, detail: string, info: unknown) => {
        if (generation !== generationRef.current) return
        setStreamError(describeStreamError(type, detail, info))
        stopStream()
      })
      setStreaming(true)
      player.load()
      void Promise.resolve(player.play()).catch((error: unknown) => handlePlayFailure(error, generation))
    } catch (error) {
      if (generation !== generationRef.current) return
      setStreamError(errorText(error, FAILED) ?? 'The local stream could not be opened.')
      stopStream()
    }
  }

  function handlePlayFailure(error: unknown, generation: number) {
    if (generation !== generationRef.current) return
    // DOMException may come from a different browser realm than Error.
    const name = typeof error === 'object' && error !== null && 'name' in error ? error.name : null
    if (name === 'NotAllowedError') {
      setConnecting(false)
      setPlaybackBlocked(true)
      return
    }
    if (name === 'AbortError') return
    setStreamError('The received video could not be played. The camera connection or video format failed.')
    stopStream()
  }

  function resumePlayback() {
    const video = videoRef.current
    const generation = generationRef.current
    if (!video) return
    // This function runs inside the real button click. Remove the overlay
    // before awaiting anything so Safari and the Home Assistant WebView keep
    // the user activation attached to the native media element.
    setPlaybackBlocked(false)
    if (playbackTimeoutRef.current != null) window.clearTimeout(playbackTimeoutRef.current)
    playbackTimeoutRef.current = window.setTimeout(() => {
      if (generation !== generationRef.current) return
      if (videoRef.current && videoRef.current.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
        setStreamError('No playable video arrived from the camera. The local stream was closed.')
        stopStream()
      }
    }, 12_000)
    void video
      .play()
      .catch((error: unknown) => handlePlayFailure(error, generation))
  }

  function markPlaying() {
    if (playbackTimeoutRef.current != null) {
      window.clearTimeout(playbackTimeoutRef.current)
      playbackTimeoutRef.current = null
    }
    setStreaming(true)
    setConnecting(false)
    setPlaybackBlocked(false)
  }

  const value = status.data
  const statusError = errorText(status.error, FAILED)
  const syncError = errorText(sync.error, FAILED)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className={cn(
          'sm:max-w-2xl',
          // A fixed child is clipped by the dialog sheet's overflow and, on
          // desktop, uses its transformed parent as the containing block. When
          // native fullscreen is refused, expand that parent too so the theatre
          // fallback really covers an iPhone/HA viewport rather than 92% of it.
          fullscreen.mode === 'theatre' &&
            'inset-0 max-h-none w-full max-w-none overflow-visible rounded-none border-0 ' +
              'sm:inset-0 sm:max-h-none sm:w-full sm:max-w-none sm:translate-x-0 sm:translate-y-0 sm:rounded-none',
        )}
      >
        <DialogHeader>
          <DialogTitle>{camera.name}</DialogTitle>
          <DialogDescription>
            {value?.model ?? camera.model ?? 'Tapo battery camera'}. Event clips and local video
            on demand; no continuous recorder.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-5">
          <div
            role="tablist"
            aria-label="Battery camera views"
            className="grid grid-cols-3 border-b border-(--color-border)"
          >
            {([
              ['live', 'Live'],
              ['recording', 'Timed recording'],
              ['clips', 'Event clips'],
            ] as const).map(([tab, label]) => (
              <button
                key={tab}
                type="button"
                role="tab"
                aria-selected={activeTab === tab}
                onClick={() => {
                  if (tab !== activeTab) stopStream()
                  setActiveTab(tab)
                }}
                className={cn(
                  'min-h-11 border-b-2 px-2 text-sm font-medium transition-colors',
                  activeTab === tab
                    ? 'border-(--color-accent) text-(--color-fg)'
                    : 'border-transparent text-(--color-fg-muted) hover:text-(--color-fg)',
                )}
              >
                {label}
              </button>
            ))}
          </div>

          {activeTab === 'live' && (
            <section aria-label="Live view" className="space-y-4">
              <div
                ref={containerRef}
                className={cn(
                  'relative aspect-video w-full overflow-hidden rounded-(--radius-tile) bg-black',
                  fullscreen.mode === 'theatre' &&
                    'fixed inset-0 z-[100] flex h-[100dvh] w-[100dvw] items-center justify-center rounded-none ' +
                      'pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] ' +
                      'pl-[env(safe-area-inset-left)] pr-[env(safe-area-inset-right)]',
                )}
              >
                <video
                  ref={videoRef}
                  className={cn('h-full w-full object-contain', streaming ? 'block' : 'hidden')}
                  controls
                  muted={muted}
                  playsInline
                  onPlaying={markPlaying}
                  onError={() => {
                    if (!nativeStopRef.current) return
                    setStreamError('The native video stream ended or could not be decoded.')
                    stopStream()
                  }}
                  onEnded={() => {
                    if (!nativeStopRef.current && !playerRef.current) return
                    setStreamError('The camera ended the live stream.')
                    stopStream()
                  }}
                />

                {connecting && (
                  <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-(--color-media-bg) p-6 text-center">
                    <div>
                      <p className="text-sm font-semibold text-(--color-media-fg)">Waking camera</p>
                      <p className="mt-1 text-xs text-(--color-media-fg-muted)">
                        Opening the Direct Stream over the local network.
                      </p>
                    </div>
                    <Button size="sm" variant="secondary" onClick={stopStream}>
                      Cancel
                    </Button>
                  </div>
                )}

                {streaming && playbackBlocked && (
                  <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3 bg-black/75 p-6 text-center">
                    <div className="max-w-sm">
                      <p className="text-sm font-semibold text-(--color-media-fg)">
                        Playback needs a tap
                      </p>
                      <p className="mt-1 text-xs leading-relaxed text-(--color-media-fg-muted)">
                        This browser needs one tap before it can play camera video.
                      </p>
                    </div>
                    <Button type="button" onClick={resumePlayback}>
                      <Play aria-hidden="true" />
                      Tap to play
                    </Button>
                  </div>
                )}

                {!streaming && !connecting && streamError && (
                  <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-(--color-media-bg) p-6 text-center">
                    <div className="max-w-sm">
                      <p className="text-sm font-semibold text-(--color-media-fg)">Live view unavailable</p>
                      <p className="mt-1 text-xs leading-relaxed text-(--color-media-fg-muted)">
                        {streamError}
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      <Button size="sm" variant="secondary" onClick={() => void startStream()}>
                        Try again
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setStreamError(null)}>
                        Dismiss
                      </Button>
                    </div>
                  </div>
                )}

                {!streaming && !connecting && !streamError && (
                  <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-(--color-media-bg) p-6 text-center">
                    <div>
                      <p className="text-sm font-semibold text-(--color-media-fg)">Camera in standby</p>
                      <p className="mt-1 text-xs text-(--color-media-fg-muted)">
                        Live video opens only when requested and closes when this view stops.
                      </p>
                    </div>
                    <Button type="button" onClick={() => void startStream()}>
                      <Play aria-hidden="true" />
                      Start live view
                    </Button>
                  </div>
                )}

                {streaming && (
                  <div className="material-media absolute inset-x-2 top-2 z-10 flex items-center justify-between rounded-(--radius-md) border border-(--color-media-border) px-2 py-1.5 text-(--color-media-fg)">
                    <span className="text-xs font-medium">Live over local network</span>
                    <div className="flex items-center gap-1">
                      <Button
                        size="icon"
                        variant="ghost"
                        onClick={() => setMuted((value) => !value)}
                        aria-label={muted ? 'Unmute battery camera' : 'Mute battery camera'}
                        className="min-h-9 min-w-9"
                      >
                        {muted ? <VolumeX aria-hidden="true" /> : <Volume2 aria-hidden="true" />}
                      </Button>
                      <Button
                        size="icon"
                        variant="ghost"
                        onClick={fullscreen.toggle}
                        aria-label={fullscreen.isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}
                        aria-pressed={fullscreen.isFullscreen}
                        className="min-h-9 min-w-9"
                      >
                        {fullscreen.isFullscreen ? (
                          <Minimize2 aria-hidden="true" />
                        ) : (
                          <Maximize2 aria-hidden="true" />
                        )}
                      </Button>
                      <Button
                        size="sm"
                        variant="danger"
                        onClick={stopStream}
                        className="min-h-9"
                      >
                        <Square aria-hidden="true" />
                        Stop stream
                      </Button>
                    </div>
                  </div>
                )}
              </div>

              {statusError ? (
                <Alert variant="warning" title="Camera status unavailable">
                  <span>{statusError}</span>{' '}
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => void status.refetch()}
                    loading={status.isFetching}
                  >
                    Retry
                  </Button>
                </Alert>
              ) : null}

              {value ? (
                <dl className="divide-y divide-(--color-border) border-y border-(--color-border)">
                  <div className="flex items-center justify-between gap-4 py-2.5 text-sm">
                    <dt className="text-(--color-fg-muted)">Battery</dt>
                    <dd className="tabular font-medium">
                      {value.battery_percent != null ? `${value.battery_percent}%` : 'Unavailable'}
                      {value.charging ? ', charging' : ''}
                    </dd>
                  </div>
                  <div className="flex items-center justify-between gap-4 py-2.5 text-sm">
                    <dt className="text-(--color-fg-muted)">Device</dt>
                    <dd className="truncate font-medium">
                      {value.model ?? camera.model ?? 'Tapo battery camera'}
                      {value.firmware_version ? `, firmware ${value.firmware_version}` : ''}
                    </dd>
                  </div>
                  <div className="flex items-center justify-between gap-4 py-2.5 text-sm">
                    <dt className="text-(--color-fg-muted)">Connection</dt>
                    <dd className="font-medium">Local network</dd>
                  </div>
                </dl>
              ) : null}
            </section>
          )}

          {activeTab === 'recording' && (
            <section aria-label="Timed recording">
              <TimedRecordingPanel
                camera={camera}
                open={open}
                batteryPercent={value?.battery_percent ?? null}
              />
            </section>
          )}

          {activeTab === 'clips' && (
            <section aria-label="Event clips" className="space-y-4">
              <div>
                <h3 className="text-sm font-semibold">Import event clips</h3>
                <p className="mt-1 text-sm leading-5 text-(--color-fg-muted)">
                  Copy the last 24 hours of clips from the camera or hub into Sentinel's timeline.
                </p>
              </div>

              {sync.data ? (
                <Alert variant={sync.data.failed > 0 ? 'warning' : 'success'}>
                  Imported {sync.data.imported}; {sync.data.already_present} already present
                  {sync.data.failed > 0 ? `; ${sync.data.failed} failed` : ''}.
                </Alert>
              ) : null}

              <Button
                type="button"
                onClick={() => void sync.mutateAsync().catch(() => undefined)}
                loading={sync.isPending}
              >
                <Download aria-hidden="true" />
                Sync last 24 hours
              </Button>

              {syncError ? <Alert variant="danger">{syncError}</Alert> : null}
            </section>
          )}
        </DialogBody>

        <DialogFooter>
          <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
