import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { ZoneCanvas } from '@/features/zones/ZoneCanvas'
import type { EditorZone, Point } from '@/features/zones/types'
import { makeZoneId } from '@/features/zones/zoneMath'

/**
 * A minimal, faithful stand-in for how `ZonesPage` actually drives
 * `ZoneCanvas`: real `useState`, real reducers for each callback, not mocks
 * that merely record they were called. The resulting zones are rendered as
 * JSON so the test can read them back without reaching into React internals.
 */
function Harness() {
  const [zones, setZones] = useState<EditorZone[]>([])
  const [drawingZoneId, setDrawingZoneId] = useState<string | null>(null)
  const [activeZoneId, setActiveZoneId] = useState<string | null>(null)

  function updateZone(zoneId: string, updater: (zone: EditorZone) => EditorZone) {
    setZones((current) => current.map((zone) => (zone.id === zoneId ? updater(zone) : zone)))
  }

  return (
    <div>
      <ZoneCanvas
        cameraName="Front door"
        previewUrl={null}
        previewLoading={false}
        previewError={null}
        zones={zones}
        drawingZoneId={drawingZoneId}
        activeZoneId={activeZoneId}
        validationMessage={null}
        onSelectZone={setActiveZoneId}
        onStartZone={() => {
          const id = makeZoneId()
          setZones((current) => [
            ...current,
            { id, name: `Zone ${current.length + 1}`, enabled: true, points: [] },
          ])
          setDrawingZoneId(id)
          setActiveZoneId(id)
        }}
        onAddPoint={(zoneId, point: Point) =>
          updateZone(zoneId, (zone) => ({ ...zone, points: [...zone.points, point] }))
        }
        onMovePoint={(zoneId, index, point: Point) =>
          updateZone(zoneId, (zone) => ({
            ...zone,
            points: zone.points.map((p, i) => (i === index ? point : p)),
          }))
        }
        onDeletePoint={(zoneId, index) =>
          updateZone(zoneId, (zone) => ({
            ...zone,
            points: zone.points.filter((_, i) => i !== index),
          }))
        }
        onFinishZone={(zoneId) =>
          setDrawingZoneId((current) => (current === zoneId ? null : current))
        }
        onCancelZone={(zoneId) => {
          setZones((current) => current.filter((zone) => zone.id !== zoneId))
          setDrawingZoneId(null)
        }}
        onDeleteZone={(zoneId) => setZones((current) => current.filter((zone) => zone.id !== zoneId))}
      />
      <pre data-testid="zones-json">{JSON.stringify(zones)}</pre>
    </div>
  )
}

function readZones(): EditorZone[] {
  return JSON.parse(screen.getByTestId('zones-json').textContent ?? '[]') as EditorZone[]
}

/** The canvas normalises against its own bounding box; jsdom reports every
 * element's box as all-zero, so every coordinate-sensitive test has to give
 * it a real one to divide by. */
function mockCanvasRect(element: Element, width = 400, height = 200): void {
  vi.spyOn(element, 'getBoundingClientRect').mockReturnValue({
    left: 0,
    top: 0,
    right: width,
    bottom: height,
    width,
    height,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  })
}

describe('ZoneCanvas — mouse drawing', () => {
  it('places vertices at the clicked position, normalised to 0-1, and closes on the first point', async () => {
    const user = userEvent.setup()
    render(<Harness />)

    const canvas = screen.getByRole('group')
    mockCanvasRect(canvas)

    await user.click(screen.getByRole('button', { name: 'New zone' }))

    fireEvent.click(canvas, { clientX: 40, clientY: 20 })
    fireEvent.click(canvas, { clientX: 360, clientY: 20 })
    fireEvent.click(canvas, { clientX: 200, clientY: 180 })
    // Close to the first vertex (40, 20): within the closing threshold.
    fireEvent.click(canvas, { clientX: 45, clientY: 25 })

    const zones = readZones()
    expect(zones).toHaveLength(1)
    expect(zones[0]?.points).toEqual([
      { x: 0.1, y: 0.1 },
      { x: 0.9, y: 0.1 },
      { x: 0.5, y: 0.9 },
    ])
    // Drawing ended: the toolbar is back to offering a new zone.
    expect(screen.getByRole('button', { name: 'New zone' })).toBeInTheDocument()
  })

  it('does not close the polygon on a click that only happens to be far from the first vertex', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    const canvas = screen.getByRole('group')
    mockCanvasRect(canvas)

    await user.click(screen.getByRole('button', { name: 'New zone' }))
    fireEvent.click(canvas, { clientX: 10, clientY: 10 })
    fireEvent.click(canvas, { clientX: 300, clientY: 10 })
    fireEvent.click(canvas, { clientX: 300, clientY: 150 })
    // Far from vertex 1 -- adds a fourth point instead of closing.
    fireEvent.click(canvas, { clientX: 150, clientY: 150 })

    expect(readZones()[0]?.points).toHaveLength(4)
  })

  it('drags an existing vertex to a new normalised position', () => {
    render(<Harness />)
    const canvas = screen.getByRole('group')
    mockCanvasRect(canvas)

    fireEvent.click(screen.getByRole('button', { name: 'New zone' }))
    fireEvent.click(canvas, { clientX: 40, clientY: 20 })
    fireEvent.click(canvas, { clientX: 360, clientY: 20 })
    fireEvent.click(canvas, { clientX: 200, clientY: 180 })
    fireEvent.click(screen.getByRole('button', { name: 'Finish zone' }))

    const firstVertex = screen.getByRole('button', { name: /Point 1 of 3/ })
    fireEvent.mouseDown(firstVertex, { clientX: 40, clientY: 20, button: 0 })
    fireEvent.mouseMove(window, { clientX: 80, clientY: 60 })
    fireEvent.mouseUp(window, { clientX: 80, clientY: 60 })

    expect(readZones()[0]?.points[0]).toEqual({ x: 0.2, y: 0.3 })
  })
})

describe('ZoneCanvas — keyboard operation', () => {
  it('draws and finishes a whole polygon with only the keyboard', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    const canvas = screen.getByRole('group')
    act(() => canvas.focus())

    await user.keyboard('{Enter}') // start a new zone
    await user.keyboard('{Enter}') // place point at the centre, (0.5, 0.5)
    await user.keyboard('{ArrowRight}{ArrowRight}{ArrowRight}')
    await user.keyboard('{Enter}') // (0.56, 0.5)
    await user.keyboard('{ArrowDown}{ArrowDown}')
    await user.keyboard('{Enter}') // (0.56, 0.54)
    await user.keyboard('f') // finish

    const zones = readZones()
    expect(zones).toHaveLength(1)
    expect(zones[0]?.points).toEqual([
      { x: 0.5, y: 0.5 },
      { x: 0.56, y: 0.5 },
      { x: 0.56, y: 0.54 },
    ])
    expect(screen.getByRole('button', { name: 'New zone' })).toBeInTheDocument()
  })

  it('cancels an in-progress zone on Escape, discarding it entirely', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    const canvas = screen.getByRole('group')
    act(() => canvas.focus())

    await user.keyboard('{Enter}')
    await user.keyboard('{Enter}')
    expect(readZones()).toHaveLength(1)

    await user.keyboard('{Escape}')
    expect(readZones()).toHaveLength(0)
  })

  it('deletes a zone with Delete once it has focus and is selected', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    const canvas = screen.getByRole('group')
    act(() => canvas.focus())

    await user.keyboard('{Enter}')
    await user.keyboard('{Enter}')
    await user.keyboard('{ArrowRight}')
    await user.keyboard('{Enter}')
    await user.keyboard('{ArrowDown}')
    await user.keyboard('{Enter}')
    await user.keyboard('f')
    expect(readZones()).toHaveLength(1)

    act(() => canvas.focus())
    await user.keyboard('{Delete}')
    expect(readZones()).toHaveLength(0)
  })

  it('moves a focused vertex with the arrow keys and removes it with Delete', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    const canvas = screen.getByRole('group')
    mockCanvasRect(canvas)
    canvas.focus()

    // Draw a 4-point polygon so one point can be removed and still leave a
    // valid triangle behind.
    await user.click(screen.getByRole('button', { name: 'New zone' }))
    await user.pointer({ keys: '[MouseLeft]', target: canvas, coords: { clientX: 0, clientY: 0 } })
    await user.pointer({ keys: '[MouseLeft]', target: canvas, coords: { clientX: 400, clientY: 0 } })
    await user.pointer({ keys: '[MouseLeft]', target: canvas, coords: { clientX: 400, clientY: 200 } })
    await user.pointer({ keys: '[MouseLeft]', target: canvas, coords: { clientX: 200, clientY: 100 } })
    await user.click(screen.getByRole('button', { name: 'Finish zone' }))
    expect(readZones()[0]?.points).toHaveLength(4)

    const secondVertex = screen.getByRole('button', { name: /Point 2 of 4/ })
    act(() => secondVertex.focus())
    await user.keyboard('{ArrowDown}') // nudge, no shift => 0.005 step
    expect(readZones()[0]?.points[1]).toEqual({ x: 1, y: 0.005 })

    await user.keyboard('{Delete}')
    expect(readZones()[0]?.points).toHaveLength(3)
  })
})
