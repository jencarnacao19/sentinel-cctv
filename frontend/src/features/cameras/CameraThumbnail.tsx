import { BatteryCharging, EyeOff, Video, VideoOff } from 'lucide-react'
import { useEffect, useState } from 'react'

import { camerasApi } from '@/api/endpoints'
import { STATUS_LABEL } from '@/features/cameras/cameraKind'
import { cn } from '@/lib/utils'
import type { Camera } from '@/types/api'

interface Props {
  camera: Camera
  privacyMessage?: string | null
  className?: string
}

/**
 * A camera's latest frame, or an honest placeholder.
 *
 * Two things were wrong before. The snapshot had no `onError`, so a camera that
 * reported itself online while its snapshot endpoint failed rendered the
 * browser's broken-image glyph with the alt text spelled out beside it -- the
 * words "Latest frame from Garagem Exterior" in a grey box, which looks like a
 * bug in the product rather than a camera that did not answer. And the URL was
 * rebuilt with `Date.now()` on every render, so any unrelated state change
 * refetched every thumbnail on the page.
 *
 * The fallback is a flat single-colour camera glyph on the subtle surface. No
 * gradient and no shimmer: a placeholder that draws attention is a placeholder
 * competing with the eight real frames next to it.
 */
export function CameraThumbnail({ camera, privacyMessage, className }: Props) {
  const [failed, setFailed] = useState(false)
  // Computed once per camera rather than per render. Bumping this is the only
  // way to refetch, which makes refreshing deliberate instead of accidental.
  const [cacheBuster, setCacheBuster] = useState(() => Date.now())

  useEffect(() => {
    setFailed(false)
    setCacheBuster(Date.now())
    // `last_seen_at` moves when Test succeeds or the camera is probed, which is
    // the natural moment to try the preview again. Without it a single failed
    // snapshot left the placeholder for the life of the page.
  }, [camera.id, camera.status, camera.last_seen_at])

  function retry() {
    setFailed(false)
    setCacheBuster(Date.now())
  }

  const showSnapshot = !privacyMessage && camera.kind === 'rtsp' && camera.status === 'online'

  return (
    <div
      className={cn(
        // A fixed identifier at every width. Letting it fill the row on a phone
        // gave each camera a 16:9 hero image and pushed the next camera off the
        // screen, so a page of eight became a page of one and a half.
        'relative flex aspect-video w-28 shrink-0 items-center justify-center',
        'overflow-hidden rounded-(--radius-sm) bg-(--color-bg-subtle) sm:w-40',
        className,
      )}
    >
      {privacyMessage ? (
        <Placeholder icon={<EyeOff aria-hidden="true" />} label="Paused for privacy" warning />
      ) : camera.kind === 'tapo_battery' ? (
        <Placeholder
          icon={<BatteryCharging aria-hidden="true" />}
          label="On demand only"
          accent
        />
      ) : showSnapshot && !failed ? (
        <img
          // The key restarts the element when the URL changes, so a retry after
          // a failure actually re-requests rather than reusing a cached error.
          key={cacheBuster}
          src={camerasApi.snapshotUrl(camera.id, { width: 320, cacheKey: cacheBuster })}
          alt=""
          aria-hidden="true"
          className="size-full object-cover"
          loading="lazy"
          decoding="async"
          onError={() => setFailed(true)}
        />
      ) : failed ? (
        // Retryable: a snapshot can fail because the camera was mid-reconnect,
        // and there is no reason to make the operator reload the page for it.
        // Retryable, but quietly: on a page of eight cameras a row of alarm
        // icons would read as eight faults. The state is named plainly and the
        // retry is a secondary affordance on the tile itself.
        <button
          type="button"
          onClick={retry}
          aria-label={`Retry the preview for ${camera.name}`}
          className="size-full cursor-pointer transition-colors hover:bg-(--color-surface-hover)"
        >
          <Placeholder icon={<Video aria-hidden="true" />} label="No recent frame" />
        </button>
      ) : (
        <Placeholder
          icon={
            camera.status === 'online' ? (
              <Video aria-hidden="true" />
            ) : (
              <VideoOff aria-hidden="true" />
            )
          }
          label={TILE_LABEL[camera.status]}
        />
      )}
    </div>
  )
}

/**
 * What the tile says when there is no frame to show.
 *
 * Shares the camera list's wording so a row cannot describe the same camera two
 * different ways. Only `online` differs, and deliberately: an online camera
 * with no thumbnail is not offline, it simply has not produced a frame yet.
 */
const TILE_LABEL: Record<Camera['status'], string> = {
  ...STATUS_LABEL,
  online: 'No preview',
}

function Placeholder({
  icon,
  label,
  warning = false,
  accent = false,
}: {
  icon: React.ReactNode
  label: string
  warning?: boolean
  accent?: boolean
}) {
  return (
    <div className="flex flex-col items-center gap-1.5 px-3 text-center">
      <span
        className={cn(
          '[&_svg]:size-6',
          warning
            ? 'text-(--color-warning)'
            : accent
              ? 'text-(--color-accent)'
              : 'text-(--color-fg-subtle)',
        )}
      >
        {icon}
      </span>
      <span className="text-[11px] text-(--color-fg-muted)">{label}</span>
    </div>
  )
}
