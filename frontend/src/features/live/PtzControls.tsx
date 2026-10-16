import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  CircleStop,
  Move,
  X,
  ZoomIn,
  ZoomOut,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { camerasApi } from '@/api/endpoints'
import { Button } from '@/components/ui/button'
import { viewerApi } from '@/features/viewer/access'
import type { PtzVector } from '@/types/api'

interface Props {
  cameraId: string
  cameraName: string
  viewer: boolean
  zoomSupported: boolean
}

const PAN_SPEED = 0.65
const ZOOM_SPEED = 0.55
const REFRESH_MS = 450

export function PtzControls({ cameraId, cameraName, viewer, zoomSupported }: Props) {
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const repeatRef = useRef<number | null>(null)
  const movingRef = useRef(false)

  const clearRepeat = useCallback(() => {
    if (repeatRef.current !== null) {
      window.clearInterval(repeatRef.current)
      repeatRef.current = null
    }
  }, [])

  const move = useCallback(
    async (vector: PtzVector) => {
      try {
        if (viewer) {
          await viewerApi.post<void>(`/viewer/cameras/${encodeURIComponent(cameraId)}/ptz/move`, vector)
        } else {
          await camerasApi.movePtz(cameraId, vector)
        }
        setError(null)
      } catch (reason) {
        movingRef.current = false
        clearRepeat()
        setError(reason instanceof Error ? reason.message : 'PTZ control failed.')
      }
    },
    [cameraId, clearRepeat, viewer],
  )

  const stop = useCallback(async (force = false) => {
    clearRepeat()
    if (!movingRef.current && !force) return
    movingRef.current = false
    try {
      if (viewer) {
        await viewerApi.post<void>(`/viewer/cameras/${encodeURIComponent(cameraId)}/ptz/stop`)
      } else {
        await camerasApi.stopPtz(cameraId)
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not stop PTZ movement.')
    }
  }, [cameraId, clearRepeat, viewer])

  const start = useCallback(
    (vector: PtzVector) => {
      if (movingRef.current) return
      movingRef.current = true
      setError(null)
      void move(vector)
      repeatRef.current = window.setInterval(() => {
        if (movingRef.current) void move(vector)
      }, REFRESH_MS)
    },
    [move],
  )

  useEffect(() => {
    const stopIfHidden = () => {
      if (document.visibilityState === 'hidden') void stop()
    }
    const stopOnBlur = () => void stop()
    document.addEventListener('visibilitychange', stopIfHidden)
    window.addEventListener('blur', stopOnBlur)
    return () => {
      document.removeEventListener('visibilitychange', stopIfHidden)
      window.removeEventListener('blur', stopOnBlur)
      void stop()
    }
  }, [stop])

  function holdHandlers(vector: PtzVector) {
    return {
      onPointerDown: (event: React.PointerEvent<HTMLButtonElement>) => {
        event.preventDefault()
        event.currentTarget.setPointerCapture?.(event.pointerId)
        start(vector)
      },
      onPointerUp: () => void stop(),
      onPointerCancel: () => void stop(),
      onLostPointerCapture: () => void stop(),
      onKeyDown: (event: React.KeyboardEvent<HTMLButtonElement>) => {
        if ((event.key === 'Enter' || event.key === ' ') && !event.repeat) {
          event.preventDefault()
          start(vector)
        }
      },
      onKeyUp: (event: React.KeyboardEvent<HTMLButtonElement>) => {
        if (event.key === 'Enter' || event.key === ' ') void stop()
      },
      onContextMenu: (event: React.MouseEvent) => event.preventDefault(),
    }
  }

  const holdClass =
    'size-10 touch-none select-none bg-black/80 text-white hover:bg-(--color-primary) hover:text-white active:scale-95'

  return (
    <>
      <Button
        variant="ghost"
        size="icon"
        className="size-8 bg-black/60 text-white hover:bg-black/80 hover:text-white"
        onClick={() => {
          if (open) void stop(true)
          setOpen((value) => !value)
        }}
        aria-label={`${open ? 'Close' : 'Open'} PTZ controls for ${cameraName}`}
        aria-expanded={open}
      >
        <Move aria-hidden="true" />
      </Button>

      {open ? (
        <div
          className="absolute right-0 bottom-11 z-20 w-52 rounded-xl border border-white/15 bg-black/95 p-2.5 text-white shadow-lg"
          role="group"
          aria-label={`PTZ controls for ${cameraName}`}
        >
          <div className="mb-2 flex items-center justify-between gap-2 px-1">
            <span className="text-[11px] font-semibold tracking-wide uppercase">PTZ</span>
            <button
              type="button"
              className="rounded p-1 text-white/70 hover:bg-white/10 hover:text-white"
              onClick={() => {
                void stop(true)
                setOpen(false)
              }}
              aria-label="Close PTZ controls"
            >
              <X className="size-4" aria-hidden="true" />
            </button>
          </div>

          <div
            className={zoomSupported ? 'grid grid-cols-[1fr_auto] items-center gap-2' : ''}
          >
            <div className="grid grid-cols-3 gap-1">
              <span />
              <Button
                variant="ghost"
                size="icon"
                className={holdClass}
                aria-label="Tilt up"
                {...holdHandlers({ pan: 0, tilt: PAN_SPEED, zoom: 0 })}
              >
                <ChevronUp aria-hidden="true" />
              </Button>
              <span />
              <Button
                variant="ghost"
                size="icon"
                className={holdClass}
                aria-label="Pan left"
                {...holdHandlers({ pan: -PAN_SPEED, tilt: 0, zoom: 0 })}
              >
                <ChevronLeft aria-hidden="true" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className={holdClass}
                aria-label="Stop PTZ"
                onClick={() => void stop(true)}
              >
                <CircleStop aria-hidden="true" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className={holdClass}
                aria-label="Pan right"
                {...holdHandlers({ pan: PAN_SPEED, tilt: 0, zoom: 0 })}
              >
                <ChevronRight aria-hidden="true" />
              </Button>
              <span />
              <Button
                variant="ghost"
                size="icon"
                className={holdClass}
                aria-label="Tilt down"
                {...holdHandlers({ pan: 0, tilt: -PAN_SPEED, zoom: 0 })}
              >
                <ChevronDown aria-hidden="true" />
              </Button>
              <span />
            </div>

            {zoomSupported ? (
              <div className="flex flex-col gap-1 border-l border-white/15 pl-2">
              <Button
                variant="ghost"
                size="icon"
                className={holdClass}
                aria-label="Zoom in"
                {...holdHandlers({ pan: 0, tilt: 0, zoom: ZOOM_SPEED })}
              >
                <ZoomIn aria-hidden="true" />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className={holdClass}
                aria-label="Zoom out"
                {...holdHandlers({ pan: 0, tilt: 0, zoom: -ZOOM_SPEED })}
              >
                <ZoomOut aria-hidden="true" />
              </Button>
              </div>
            ) : null}
          </div>

          <p className="mt-2 px-1 text-[10px] text-white/60">Hold to move · release to stop</p>
          {error ? <p className="mt-1 px-1 text-[10px] text-red-300">{error}</p> : null}
        </div>
      ) : null}
    </>
  )
}
