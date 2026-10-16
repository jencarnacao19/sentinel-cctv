import { EyeOff, Loader2, Maximize2, Minimize2, Move, VideoOff, Volume2, VolumeX } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { HlsPlayer, prefersNativeHls } from '@/features/live/hls'
import { isMseSupported, MsePlayer, type PlayerState, websocketUrl } from '@/features/live/mse'
import { PtzControls } from '@/features/live/PtzControls'
import { isWebRtcSupported, WebRtcPlayer } from '@/features/live/webrtc'
import { useFullscreen } from '@/features/timeline/useFullscreen'
import {
  viewerAuthHeaders,
  viewerWebsocketPath,
  viewerWebsocketProtocols,
} from '@/features/viewer/access'
import { cn } from '@/lib/utils'

interface Props {
  cameraId: string
  cameraName: string
  /** Play the low-resolution substream. Used by multi-camera grids. */
  sub?: boolean
  className?: string
  showControls?: boolean
  /** Use the isolated read-only viewer socket rather than an admin session. */
  viewer?: boolean
  /** Show ONVIF controls only after the backend positively detected PTZ. */
  ptz?: boolean
  ptzZoom?: boolean
  /** When set, no player or camera connection is created. */
  privacyMessage?: string | null
  /** Draw the player flush inside a tile that already has its own corners. */
  flush?: boolean
}

/**
 * A single state panel over the video.
 *
 * Every state gets its own words and its own symbol. Collapsing them into one
 * red "error" is what makes an operator power-cycle a camera that was simply
 * reconnecting, so a stream that is coming back on its own shows a spinner and
 * says so, while one that has genuinely stopped shows a struck-through camera.
 */
function StreamState({
  icon,
  title,
  detail,
  action,
}: {
  icon: React.ReactNode
  title: string
  detail?: string | null
  action?: React.ReactNode
}) {
  return (
    <div
      className="absolute inset-0 flex flex-col items-center justify-center gap-2.5 bg-black/70 px-4 text-center"
      role="status"
    >
      <span className="text-(--color-media-fg-muted) [&_svg]:size-5" aria-hidden="true">
        {icon}
      </span>
      <p className="text-xs font-medium text-(--color-media-fg)">{title}</p>
      {detail ? <p className="max-w-xs text-[11px] leading-4 text-(--color-media-fg-subtle)">{detail}</p> : null}
      {action}
    </div>
  )
}

export function VideoPlayer({
  cameraId,
  cameraName,
  sub = false,
  className,
  showControls = true,
  viewer = false,
  ptz = false,
  ptzZoom = false,
  privacyMessage,
  flush = false,
}: Props) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const [state, setState] = useState<PlayerState>('connecting')
  /** Retries since the stream last worked, reported by the player so the
   *  overlay can say what it is doing rather than only that it failed. */
  const [attempt, setAttempt] = useState(0)
  const [detail, setDetail] = useState<string | undefined>()
  const [muted, setMuted] = useState(true)
  const fullscreen = useFullscreen({ containerRef, videoRef })

  useEffect(() => {
    if (privacyMessage) return
    const video = videoRef.current
    if (!video) return

    let disposed = false
    let usingWebRtc = false
    let active: MsePlayer | WebRtcPlayer | HlsPlayer | null = null
    const events = {
      onStateChange: (next: PlayerState, message?: string, tries?: number) => {
        setState(next)
        setDetail(message)
        // Only 'reconnecting' carries an attempt; anything else clears it so a
        // later panel cannot show a stale count from a previous outage.
        setAttempt(tries ?? 0)
      },
    }
    const query = sub ? '?sub=true' : ''

    let usingHls = false
    const startHls = () => {
      if (disposed || usingHls) return
      usingHls = true
      active?.stop()
      active = new HlsPlayer(
        video,
        `/api/v1/streams/${cameraId}/${viewer ? 'viewer-hls-session' : 'hls-session'}${query}`,
        events,
        viewer ? viewerAuthHeaders() : undefined,
      )
      active.start()
    }

    const startWebRtc = () => {
      if (disposed || usingWebRtc) return
      usingWebRtc = true
      active?.stop()
      active = new WebRtcPlayer(
        video,
        `/api/v1/streams/${cameraId}/${viewer ? 'viewer-webrtc' : 'webrtc'}${query}`,
        { ...events, onUnsupported: startHls },
        viewer ? viewerAuthHeaders() : undefined,
      )
      active.start()
    }

    if (prefersNativeHls(video)) {
      startHls()
    } else if (isMseSupported()) {
      active = new MsePlayer(
        video,
        websocketUrl(
          viewer
            ? viewerWebsocketPath(cameraId, sub)
            : `/api/v1/streams/${cameraId}/ws${query}`,
        ),
        { ...events, onUnsupported: startWebRtc },
        viewer ? viewerWebsocketProtocols() : undefined,
      )
      active.start()
    } else if (isWebRtcSupported()) {
      startWebRtc()
    } else {
      startHls()
    }

    return () => {
      disposed = true
      active?.stop()
    }
  }, [cameraId, privacyMessage, sub, viewer])

  const isLive = state === 'playing'
  /**
   * Inside a `CameraTile` the surrounding figure already owns the 16:9 box and
   * the corners, so the player fills it instead of drawing a second one. Used
   * standalone it draws its own.
   */
  const box = flush
    ? 'absolute inset-0'
    : 'relative aspect-video overflow-hidden rounded-(--radius-tile) border border-(--color-border)'

  if (privacyMessage) {
    return (
      <div
        className={cn(
          'flex flex-col items-center justify-center gap-2 bg-(--color-bg-subtle) px-5 text-center',
          box,
          className,
        )}
        role="status"
        aria-label={`Live view of ${cameraName} paused for privacy`}
      >
        <EyeOff className="size-6 text-(--color-warning)" aria-hidden="true" />
        <p className="text-sm font-semibold">Paused for privacy</p>
        <p className="max-w-md text-xs leading-4 text-(--color-fg-muted)">{privacyMessage}</p>
      </div>
    )
  }

  return (
    <div
      ref={containerRef}
      className={cn(
        'group bg-black',
        box,
        className,
        fullscreen.mode === 'theatre' &&
          'fixed inset-0 z-[100] flex h-[100dvh] w-[100dvw] items-center justify-center rounded-none border-0 ' +
            'pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)] ' +
            'pl-[env(safe-area-inset-left)] pr-[env(safe-area-inset-right)]',
      )}
    >
      <video
        ref={videoRef}
        className="size-full object-contain"
        // Muted by default because browsers refuse to autoplay with sound, and a
        // wall of nine cameras all playing audio would be unusable anyway.
        muted={muted}
        playsInline
        autoPlay
        aria-label={`Live view of ${cameraName}`}
      />

      {!isLive ? (
        state === 'connecting' ? (
          <StreamState
            icon={<Loader2 className="animate-spin" />}
            title="Connecting…"
            detail="Negotiating the local video transport."
          />
        ) : state === 'reconnecting' ? (
          // Deliberately not the dead panel: this stream is coming back on
          // its own, and showing the same icon as a terminal failure would
          // send an operator to reload a page that did not need it.
          <StreamState
            icon={<Loader2 className="animate-spin" />}
            title={detail ?? 'The live stream dropped. Reconnecting…'}
            detail={attempt > 1 ? `Attempt ${attempt}` : 'Recording on the server is unaffected.'}
          />
        ) : state === 'stalled' ? (
          // The old copy asked for a gesture the interface never offered.
          <StreamState
            icon={<VideoOff />}
            title={detail ?? 'The live stream stalled.'}
            action={
              <button
                type="button"
                onClick={() => void videoRef.current?.play().catch(() => undefined)}
                className="mt-1 min-h-9 rounded-(--radius-control) border border-(--color-media-border) px-3 text-xs font-medium text-(--color-media-fg) hover:bg-white/10"
              >
                Resume
              </button>
            }
          />
        ) : (
          <StreamState
            icon={<VideoOff />}
            title={
              detail ??
              (state === 'closed'
                ? 'The live stream ended.'
                : !isMseSupported() && !isWebRtcSupported()
                  ? 'This browser cannot play the live stream.'
                  : 'Live view unavailable.')
            }
            detail="Recorded footage for this camera is still available under Timeline."
          />
        )
      ) : null}

      <div className="pointer-events-none absolute top-2 left-2 flex flex-wrap items-center gap-1.5">
        {isLive ? (
          <span className="material-media flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[10px] font-semibold text-(--color-media-fg)">
            <span
              className="pulse-recording size-1.5 rounded-full bg-(--color-recording)"
              aria-hidden="true"
            />
            Live
          </span>
        ) : null}
        {/* Says the camera can be driven before anyone goes looking for the
            control, which on a phone is hidden until the tile is touched. */}
        {ptz ? (
          <span className="material-media flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold text-(--color-media-fg-muted)">
            <Move className="size-3" aria-hidden="true" />
            PTZ
          </span>
        ) : null}
      </div>

      {showControls ? (
        <div className="absolute right-2 bottom-2 flex gap-1 opacity-100 transition-opacity sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100">
          {ptz ? (
            <PtzControls
              cameraId={cameraId}
              cameraName={cameraName}
              viewer={viewer}
              zoomSupported={ptzZoom}
            />
          ) : null}
          <Button
            variant="ghost"
            size="icon"
            className="material-media size-10 rounded-full text-(--color-media-fg) hover:bg-white/10 hover:text-(--color-media-fg) sm:size-8"
            onClick={() => setMuted((value) => !value)}
            aria-label={muted ? `Unmute ${cameraName}` : `Mute ${cameraName}`}
          >
            {muted ? <VolumeX aria-hidden="true" /> : <Volume2 aria-hidden="true" />}
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="material-media size-10 rounded-full text-(--color-media-fg) hover:bg-white/10 hover:text-(--color-media-fg) sm:size-8"
            onClick={fullscreen.toggle}
            aria-label={
              fullscreen.isFullscreen
                ? `Exit fullscreen for ${cameraName}`
                : `Show ${cameraName} fullscreen`
            }
            aria-pressed={fullscreen.isFullscreen}
          >
            {fullscreen.isFullscreen ? (
              <Minimize2 aria-hidden="true" />
            ) : (
              <Maximize2 aria-hidden="true" />
            )}
          </Button>
        </div>
      ) : null}
    </div>
  )
}
