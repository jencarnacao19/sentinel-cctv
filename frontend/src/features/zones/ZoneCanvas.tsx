/**
 * The drawing surface: a preview frame with an SVG overlay for the polygons
 * and a native `<button>` per vertex.
 *
 * Vertices are real buttons, not SVG shapes, on purpose. It means focus,
 * `:focus-visible` styling, `Tab` order and touch hit-testing all come from
 * the browser for free instead of being reimplemented on top of canvas pixel
 * coordinates -- the single biggest simplifier in this file. The SVG layer
 * underneath is purely decorative (`pointer-events: none`, `aria-hidden`) and
 * only draws the polygons themselves.
 *
 * Placing/closing a zone is a plain `onClick` (fires for mouse and touch
 * alike, so no separate touch handler is needed there). Dragging an existing
 * vertex is the one gesture that needs continuous tracking, done with
 * `mousemove`/`touchmove` listeners on `window` -- attached only for the
 * duration of the gesture -- rather than the Pointer Events API, which this
 * project's test environment (jsdom) does not implement at all; Mouse and
 * Touch events do the same job and keep the drag path exercisable by a test.
 */

import { Check, Plus, X } from 'lucide-react'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'

import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { clamp01, distance, MAX_POINTS, MIN_POINTS, pointInPolygon } from '@/features/zones/zoneMath'
import type { EditorZone, Point } from '@/features/zones/types'
import { cn } from '@/lib/utils'

export interface SelectedPoint {
  zoneId: string
  pointIndex: number
}

interface ZoneCanvasProps {
  cameraName: string
  previewUrl: string | null
  previewLoading: boolean
  previewError: string | null
  zones: EditorZone[]
  drawingZoneId: string | null
  activeZoneId: string | null
  validationMessage: string | null
  onSelectZone: (zoneId: string | null) => void
  onStartZone: () => void
  onAddPoint: (zoneId: string, point: Point) => void
  onMovePoint: (zoneId: string, pointIndex: number, point: Point) => void
  onDeletePoint: (zoneId: string, pointIndex: number) => void
  onFinishZone: (zoneId: string) => void
  onCancelZone: (zoneId: string) => void
  onDeleteZone: (zoneId: string) => void
  /** The server rejects a 33rd zone outright (`MotionZonesUpdate`); disabling
   * "New zone" at the same limit means the operator finds out before drawing
   * one, not after losing the work to a failed save. */
  maxZonesReached?: boolean
}

/** How close (in device pixels) a click has to land to the first vertex of
 * an in-progress zone to count as "close it" rather than "add another
 * point". Generous on purpose -- a fingertip on a tablet is not a mouse. */
const CLOSE_THRESHOLD_PX = 18

const NUDGE_STEP = 0.005
const NUDGE_STEP_LARGE = 0.02
const CURSOR_STEP = 0.02
const CURSOR_STEP_LARGE = 0.06

export function ZoneCanvas(props: ZoneCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const vertexRefs = useRef(new Map<string, HTMLButtonElement>())
  const pendingFocusRef = useRef<SelectedPoint | null>(null)
  const dragCleanupRef = useRef<(() => void) | null>(null)
  const rafRef = useRef<number | null>(null)
  const pendingMoveRef = useRef<Point | null>(null)

  const [cursor, setCursor] = useState<Point>({ x: 0.5, y: 0.5 })
  const [canvasFocused, setCanvasFocused] = useState(false)
  const [selectedPoint, setSelectedPoint] = useState<SelectedPoint | null>(null)
  const [dragPreview, setDragPreview] = useState<SelectedPoint & { point: Point } | null>(null)
  const [liveMessage, setLiveMessage] = useState('')

  // Reset the keyboard cursor to the centre every time a fresh zone starts,
  // rather than wherever it was left over the previous one.
  useEffect(() => {
    if (props.drawingZoneId) setCursor({ x: 0.5, y: 0.5 })
  }, [props.drawingZoneId])

  // A point deleted from the text list (not via this canvas) can leave the
  // local "selected vertex" pointing at an index that no longer exists.
  useEffect(() => {
    if (!selectedPoint) return
    const zone = props.zones.find((z) => z.id === selectedPoint.zoneId)
    if (!zone || !zone.points[selectedPoint.pointIndex]) setSelectedPoint(null)
  }, [props.zones, selectedPoint])

  // Keeps DOM focus following "the vertex that just became the sensible one
  // to be on" after a keyboard delete, so repeated Delete presses do not
  // require re-tabbing in from scratch each time.
  useLayoutEffect(() => {
    const pending = pendingFocusRef.current
    if (!pending) return
    pendingFocusRef.current = null
    const element = vertexRefs.current.get(`${pending.zoneId}:${pending.pointIndex}`)
    if (element) {
      element.focus()
      setSelectedPoint(pending)
    }
  }, [props.zones])

  useEffect(() => () => dragCleanupRef.current?.(), [])

  function computeNormalizedPoint(clientX: number, clientY: number): Point {
    const rect = containerRef.current?.getBoundingClientRect()
    if (!rect || rect.width === 0 || rect.height === 0) return { x: 0.5, y: 0.5 }
    return { x: clamp01((clientX - rect.left) / rect.width), y: clamp01((clientY - rect.top) / rect.height) }
  }

  function scheduleDragPreview(zoneId: string, pointIndex: number, point: Point) {
    pendingMoveRef.current = point
    if (rafRef.current !== null) return
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null
      if (pendingMoveRef.current) setDragPreview({ zoneId, pointIndex, point: pendingMoveRef.current })
    })
  }

  function cancelScheduledDrag() {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current)
      rafRef.current = null
    }
    pendingMoveRef.current = null
  }

  function beginVertexSelection(zoneId: string, pointIndex: number) {
    setSelectedPoint({ zoneId, pointIndex })
    props.onSelectZone(zoneId)
  }

  function handleVertexMouseDown(zoneId: string, pointIndex: number, event: React.MouseEvent<HTMLButtonElement>) {
    if (event.button !== 0) return
    event.stopPropagation()
    event.currentTarget.focus()
    beginVertexSelection(zoneId, pointIndex)

    const handleMove = (moveEvent: MouseEvent) => {
      scheduleDragPreview(zoneId, pointIndex, computeNormalizedPoint(moveEvent.clientX, moveEvent.clientY))
    }
    const handleUp = (upEvent: MouseEvent) => {
      cleanup()
      const finalPoint = computeNormalizedPoint(upEvent.clientX, upEvent.clientY)
      setDragPreview(null)
      props.onMovePoint(zoneId, pointIndex, finalPoint)
    }
    const cleanup = () => {
      window.removeEventListener('mousemove', handleMove)
      window.removeEventListener('mouseup', handleUp)
      cancelScheduledDrag()
      dragCleanupRef.current = null
    }
    dragCleanupRef.current = cleanup
    window.addEventListener('mousemove', handleMove)
    window.addEventListener('mouseup', handleUp)
  }

  function handleVertexTouchStart(zoneId: string, pointIndex: number, event: React.TouchEvent<HTMLButtonElement>) {
    event.stopPropagation()
    const touchId = event.touches[0]?.identifier
    if (touchId === undefined) return
    beginVertexSelection(zoneId, pointIndex)

    const findTouch = (list: TouchList) => Array.from(list).find((touch) => touch.identifier === touchId)

    const handleMove = (moveEvent: TouchEvent) => {
      const touch = findTouch(moveEvent.touches)
      if (!touch) return
      // Otherwise the page scrolls or the browser tries to pinch-zoom while
      // the operator is trying to drag a point on a tablet.
      moveEvent.preventDefault()
      scheduleDragPreview(zoneId, pointIndex, computeNormalizedPoint(touch.clientX, touch.clientY))
    }
    const handleEnd = (endEvent: TouchEvent) => {
      cleanup()
      const touch = findTouch(endEvent.changedTouches)
      setDragPreview(null)
      if (touch) props.onMovePoint(zoneId, pointIndex, computeNormalizedPoint(touch.clientX, touch.clientY))
    }
    const cleanup = () => {
      window.removeEventListener('touchmove', handleMove)
      window.removeEventListener('touchend', handleEnd)
      window.removeEventListener('touchcancel', handleEnd)
      cancelScheduledDrag()
      dragCleanupRef.current = null
    }
    dragCleanupRef.current = cleanup
    window.addEventListener('touchmove', handleMove, { passive: false })
    window.addEventListener('touchend', handleEnd)
    window.addEventListener('touchcancel', handleEnd)
  }

  function handleVertexKeyDown(zoneId: string, pointIndex: number, event: React.KeyboardEvent<HTMLButtonElement>) {
    const zone = props.zones.find((z) => z.id === zoneId)
    const point = zone?.points[pointIndex]
    if (!zone || !point) return
    const step = event.shiftKey ? NUDGE_STEP_LARGE : NUDGE_STEP

    if (event.key === 'ArrowUp' || event.key === 'ArrowDown' || event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault()
      event.stopPropagation()
      const delta: Point = {
        x: event.key === 'ArrowLeft' ? -step : event.key === 'ArrowRight' ? step : 0,
        y: event.key === 'ArrowUp' ? -step : event.key === 'ArrowDown' ? step : 0,
      }
      props.onMovePoint(zoneId, pointIndex, { x: clamp01(point.x + delta.x), y: clamp01(point.y + delta.y) })
      return
    }

    if (event.key === 'Delete' || event.key === 'Backspace') {
      event.preventDefault()
      event.stopPropagation()
      if (zone.points.length <= MIN_POINTS) {
        // Left for the page to turn into the "delete the whole zone
        // instead" message -- this component only reports the attempt.
        props.onDeletePoint(zoneId, pointIndex)
        return
      }
      pendingFocusRef.current = { zoneId, pointIndex: Math.max(0, pointIndex - 1) }
      setLiveMessage(`Point removed. ${zone.points.length - 1} points remain in ${zone.name}.`)
      props.onDeletePoint(zoneId, pointIndex)
    }
  }

  function handleBackgroundClick(event: React.MouseEvent<HTMLDivElement>) {
    event.currentTarget.focus()
    const rect = containerRef.current?.getBoundingClientRect()
    if (!rect) return
    const point = computeNormalizedPoint(event.clientX, event.clientY)

    if (props.drawingZoneId) {
      const zone = props.zones.find((z) => z.id === props.drawingZoneId)
      if (!zone) return
      if (zone.points.length >= MIN_POINTS) {
        const first = zone.points[0]
        const clickPx = { x: event.clientX - rect.left, y: event.clientY - rect.top }
        const firstPx = { x: first.x * rect.width, y: first.y * rect.height }
        if (distance(clickPx, firstPx) <= CLOSE_THRESHOLD_PX) {
          props.onFinishZone(zone.id)
          setLiveMessage(`Zone finished with ${zone.points.length} points.`)
          return
        }
      }
      if (zone.points.length < MAX_POINTS) {
        props.onAddPoint(zone.id, point)
        setLiveMessage(`Point ${zone.points.length + 1} placed in ${zone.name}.`)
      }
      return
    }

    for (let index = props.zones.length - 1; index >= 0; index -= 1) {
      const zone = props.zones[index]
      if (zone.points.length >= 3 && pointInPolygon(point, zone.points)) {
        props.onSelectZone(zone.id)
        return
      }
    }
    props.onSelectZone(null)
    setSelectedPoint(null)
  }

  function handleContainerKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (props.drawingZoneId) {
      const zone = props.zones.find((z) => z.id === props.drawingZoneId)
      if (!zone) return
      const step = event.shiftKey ? CURSOR_STEP_LARGE : CURSOR_STEP

      switch (event.key) {
        case 'ArrowUp':
          event.preventDefault()
          setCursor((current) => ({ x: current.x, y: clamp01(current.y - step) }))
          return
        case 'ArrowDown':
          event.preventDefault()
          setCursor((current) => ({ x: current.x, y: clamp01(current.y + step) }))
          return
        case 'ArrowLeft':
          event.preventDefault()
          setCursor((current) => ({ x: clamp01(current.x - step), y: current.y }))
          return
        case 'ArrowRight':
          event.preventDefault()
          setCursor((current) => ({ x: clamp01(current.x + step), y: current.y }))
          return
        case 'Enter':
        case ' ':
          event.preventDefault()
          if (zone.points.length < MAX_POINTS) {
            props.onAddPoint(zone.id, cursor)
            setLiveMessage(`Point ${zone.points.length + 1} placed in ${zone.name}.`)
          }
          return
        case 'f':
        case 'F':
          event.preventDefault()
          if (zone.points.length >= MIN_POINTS) {
            props.onFinishZone(zone.id)
            setLiveMessage(`Zone finished with ${zone.points.length} points.`)
          }
          return
        case 'Escape':
          event.preventDefault()
          props.onCancelZone(zone.id)
          setLiveMessage('Zone drawing cancelled.')
          return
        default:
          return
      }
    }

    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      if (props.maxZonesReached) {
        setLiveMessage('At most 32 zones are allowed. Delete one before starting another.')
        return
      }
      props.onStartZone()
      setLiveMessage('New zone started. Use the arrow keys to move the marker and Enter to place a point.')
      return
    }
    if ((event.key === 'Delete' || event.key === 'Backspace') && props.activeZoneId) {
      event.preventDefault()
      props.onDeleteZone(props.activeZoneId)
      setLiveMessage('Zone deleted.')
    }
  }

  const drawingZone = props.drawingZoneId
    ? props.zones.find((zone) => zone.id === props.drawingZoneId)
    : undefined

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {!props.drawingZoneId ? (
          <Button
            type="button"
            size="sm"
            onClick={props.onStartZone}
            disabled={props.maxZonesReached}
            title={props.maxZonesReached ? 'At most 32 zones are allowed.' : undefined}
          >
            <Plus aria-hidden="true" />
            New zone
          </Button>
        ) : (
          <>
            <Button
              type="button"
              size="sm"
              onClick={() => {
                props.onFinishZone(props.drawingZoneId!)
                setLiveMessage(`Zone finished with ${drawingZone?.points.length ?? 0} points.`)
              }}
              disabled={(drawingZone?.points.length ?? 0) < MIN_POINTS}
            >
              <Check aria-hidden="true" />
              Finish zone
            </Button>
            <Button
              type="button"
              size="sm"
              variant="secondary"
              onClick={() => {
                props.onCancelZone(props.drawingZoneId!)
                setLiveMessage('Zone drawing cancelled.')
              }}
            >
              <X aria-hidden="true" />
              Cancel
            </Button>
            <span className="text-xs text-(--color-fg-subtle)">
              Click to add points, or click the first point to close the shape.
            </span>
          </>
        )}
      </div>

      {props.validationMessage ? <Alert variant="warning">{props.validationMessage}</Alert> : null}

      <div
        ref={containerRef}
        role="group"
        aria-roledescription="zone drawing surface"
        aria-label={
          props.drawingZoneId
            ? `Drawing a new zone over the preview from ${props.cameraName}. Arrow keys move the placement marker, Enter places a point, F finishes the zone, Escape cancels it.`
            : `Ignore-zone canvas for ${props.cameraName}. Press Enter to start a new zone. Tab to an existing point to move it with the arrow keys or delete it with Delete.`
        }
        tabIndex={0}
        onFocus={() => setCanvasFocused(true)}
        onBlur={() => setCanvasFocused(false)}
        onClick={handleBackgroundClick}
        onKeyDown={handleContainerKeyDown}
        className={cn(
          'relative touch-none overflow-hidden rounded-(--radius-card) border border-(--color-border) bg-black',
          'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-(--color-accent)',
        )}
      >
        {props.previewLoading ? (
          <Skeleton className="aspect-video w-full rounded-none" />
        ) : props.previewUrl ? (
          <img
            src={props.previewUrl}
            alt={`Preview from ${props.cameraName}, for drawing ignore zones`}
            className="block w-full max-w-full select-none"
            draggable={false}
          />
        ) : (
          <div className="flex aspect-video w-full items-center justify-center px-6 text-center text-sm text-(--color-fg-subtle)">
            {props.previewError ?? 'No preview available for this camera.'}
          </div>
        )}

        <svg
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 size-full"
          viewBox="0 0 1 1"
          preserveAspectRatio="none"
        >
          {props.zones.map((zone) => {
            if (zone.points.length < 2) return null
            const isActive = zone.id === props.activeZoneId || zone.id === props.drawingZoneId
            const pointsAttr = zone.points
              .map((point, index) => {
                const shown =
                  dragPreview && dragPreview.zoneId === zone.id && dragPreview.pointIndex === index
                    ? dragPreview.point
                    : point
                return `${shown.x},${shown.y}`
              })
              .join(' ')
            const color = zone.enabled ? 'var(--color-motion)' : 'var(--color-fg-subtle)'
            return (
              <polygon
                key={zone.id}
                points={pointsAttr}
                fill={color}
                fillOpacity={isActive ? 0.3 : 0.15}
                stroke={color}
                strokeOpacity={0.85}
                strokeWidth={isActive ? 2 : 1}
                strokeDasharray={zone.id === props.drawingZoneId ? '0.012 0.01' : undefined}
                vectorEffect="non-scaling-stroke"
              />
            )
          })}
        </svg>

        {props.drawingZoneId && canvasFocused ? (
          <div
            aria-hidden="true"
            className="pointer-events-none absolute z-10 size-5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-dashed border-(--color-accent)"
            style={{ left: `${cursor.x * 100}%`, top: `${cursor.y * 100}%` }}
          />
        ) : null}

        {props.zones.flatMap((zone) =>
          zone.points.map((point, index) => {
            const key = `${zone.id}:${index}`
            const shown =
              dragPreview && dragPreview.zoneId === zone.id && dragPreview.pointIndex === index
                ? dragPreview.point
                : point
            const isSelected = selectedPoint?.zoneId === zone.id && selectedPoint.pointIndex === index
            return (
              <button
                key={key}
                ref={(element) => {
                  if (element) vertexRefs.current.set(key, element)
                  else vertexRefs.current.delete(key)
                }}
                type="button"
                style={{ left: `${shown.x * 100}%`, top: `${shown.y * 100}%` }}
                className={cn(
                  'absolute z-10 size-4 -translate-x-1/2 -translate-y-1/2 touch-none rounded-full border-2',
                  'cursor-grab bg-(--color-surface) shadow-sm active:cursor-grabbing',
                  zone.enabled ? 'border-(--color-accent)' : 'border-(--color-fg-subtle)',
                  isSelected && 'outline-2 outline-offset-2 outline-(--color-accent)',
                )}
                aria-label={`Point ${index + 1} of ${zone.points.length} in ${zone.name}`}
                onMouseDown={(event) => handleVertexMouseDown(zone.id, index, event)}
                onTouchStart={(event) => handleVertexTouchStart(zone.id, index, event)}
                onClick={(event) => {
                  event.stopPropagation()
                  beginVertexSelection(zone.id, index)
                }}
                onFocus={() => beginVertexSelection(zone.id, index)}
                onKeyDown={(event) => handleVertexKeyDown(zone.id, index, event)}
              />
            )
          }),
        )}

        {selectedPoint
          ? (() => {
              const zone = props.zones.find((z) => z.id === selectedPoint.zoneId)
              const point = zone?.points[selectedPoint.pointIndex]
              if (!zone || !point) return null
              return (
                <button
                  type="button"
                  style={{ left: `${point.x * 100}%`, top: `${point.y * 100}%` }}
                  className="absolute z-20 flex size-6 -translate-x-1/2 translate-y-[-160%] items-center justify-center rounded-full bg-(--color-danger) text-(--color-danger-fg) shadow"
                  aria-label={`Delete point ${selectedPoint.pointIndex + 1} of ${zone.name}`}
                  onClick={(event) => {
                    event.stopPropagation()
                    props.onDeletePoint(zone.id, selectedPoint.pointIndex)
                  }}
                >
                  <X className="size-3.5" aria-hidden="true" />
                </button>
              )
            })()
          : null}
      </div>

      <p aria-live="polite" className="sr-only">
        {liveMessage}
      </p>
    </div>
  )
}
