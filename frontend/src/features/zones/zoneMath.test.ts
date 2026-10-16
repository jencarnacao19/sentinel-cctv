import { describe, expect, it } from 'vitest'

import type { EditorZone } from '@/features/zones/types'
import {
  clamp01,
  distance,
  mergeZones,
  pointInPolygon,
  polygonKey,
  sanitizeServerZones,
  zonesSignature,
  zonesToPayload,
} from '@/features/zones/zoneMath'

describe('clamp01', () => {
  it('clamps out-of-range values into 0-1', () => {
    expect(clamp01(-0.5)).toBe(0)
    expect(clamp01(1.5)).toBe(1)
    expect(clamp01(0.42)).toBeCloseTo(0.42)
  })

  it('treats NaN as 0 rather than propagating it', () => {
    expect(clamp01(Number.NaN)).toBe(0)
  })
})

describe('sanitizeServerZones', () => {
  it('accepts a well-formed list of polygons untouched', () => {
    const raw = [
      [
        [0, 0],
        [1, 0],
        [0.5, 1],
      ],
    ]
    const result = sanitizeServerZones(raw)
    expect(result.dropped).toBe(0)
    expect(result.zones).toEqual([[{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 0.5, y: 1 }]])
  })

  it('does not crash on a completely malformed payload and drops nothing usable', () => {
    expect(sanitizeServerZones(null)).toEqual({ zones: [], dropped: 0 })
    expect(sanitizeServerZones(undefined)).toEqual({ zones: [], dropped: 0 })
    expect(sanitizeServerZones('not an array')).toEqual({ zones: [], dropped: 0 })
    expect(sanitizeServerZones(42)).toEqual({ zones: [], dropped: 0 })
  })

  it('drops a polygon with too few points but keeps the valid ones, reporting the count', () => {
    const raw = [
      [
        [0, 0],
        [1, 1],
      ], // only two points -- not a polygon
      [
        [0, 0],
        [1, 0],
        [1, 1],
      ],
    ]
    const result = sanitizeServerZones(raw)
    expect(result.zones).toHaveLength(1)
    expect(result.dropped).toBe(1)
  })

  it('drops a polygon containing an out-of-range or non-numeric coordinate', () => {
    const raw = [
      [
        [0, 0],
        [1.5, 0], // out of 0-1 range
        [0, 1],
      ],
      [
        [0, 0],
        ['bad', 0],
        [0, 1],
      ],
      [
        [0, 0],
        [1, 0],
        [1, 1],
      ],
    ]
    const result = sanitizeServerZones(raw)
    expect(result.zones).toHaveLength(1)
    expect(result.dropped).toBe(2)
  })

  it('drops entries beyond the 32-zone limit without touching the ones that fit', () => {
    const validPolygon = [
      [0, 0],
      [1, 0],
      [1, 1],
    ]
    const raw = Array.from({ length: 40 }, () => validPolygon)
    const result = sanitizeServerZones(raw)
    expect(result.zones).toHaveLength(32)
    expect(result.dropped).toBe(8)
  })

  it('never throws regardless of what garbage is nested inside', () => {
    const raw = [null, 1, 'x', { not: 'a polygon' }, [{ x: 1 }, [1, 2, 3]]]
    expect(() => sanitizeServerZones(raw)).not.toThrow()
    expect(sanitizeServerZones(raw).zones).toEqual([])
  })
})

describe('zonesToPayload', () => {
  it('sends only enabled zones, rounded to 4 decimals', () => {
    const zones: EditorZone[] = [
      {
        id: 'a',
        name: 'Enabled',
        enabled: true,
        points: [
          { x: 0.123456, y: 0.987654 },
          { x: 0, y: 1 },
          { x: 0.5, y: 0.5 },
        ],
      },
      {
        id: 'b',
        name: 'Disabled',
        enabled: false,
        points: [
          { x: 0, y: 0 },
          { x: 1, y: 0 },
          { x: 1, y: 1 },
        ],
      },
    ]
    const payload = zonesToPayload(zones)
    expect(payload).toEqual([
      [
        [0.1235, 0.9877],
        [0, 1],
        [0.5, 0.5],
      ],
    ])
  })

  it('clamps a stray out-of-range coordinate rather than sending it as-is', () => {
    const zones: EditorZone[] = [
      {
        id: 'a',
        name: 'Zone',
        enabled: true,
        points: [
          { x: -0.2, y: 0 },
          { x: 1, y: 0 },
          { x: 1.5, y: 1 },
        ],
      },
    ]
    expect(zonesToPayload(zones)).toEqual([
      [
        [0, 0],
        [1, 0],
        [1, 1],
      ],
    ])
  })
})

describe('mergeZones', () => {
  it('names and enables every server zone when nothing is stored locally yet', () => {
    const serverZones = [
      [{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 1, y: 1 }],
      [{ x: 0, y: 0 }, { x: 0.5, y: 0 }, { x: 0.5, y: 0.5 }],
    ]
    const merged = mergeZones(null, serverZones)
    expect(merged).toHaveLength(2)
    expect(merged[0]?.name).toBe('Zone 1')
    expect(merged[0]?.enabled).toBe(true)
    expect(merged[1]?.name).toBe('Zone 2')
  })

  it('keeps local names and imports only zones the local copy does not already know', () => {
    const knownPoints = [{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 1, y: 1 }]
    const stored: EditorZone[] = [
      { id: 'local-1', name: 'Driveway', enabled: true, points: knownPoints },
    ]
    const newServerPolygon = [{ x: 0, y: 0 }, { x: 0.2, y: 0 }, { x: 0.2, y: 0.2 }]
    const merged = mergeZones(stored, [knownPoints, newServerPolygon])

    expect(merged).toHaveLength(2)
    expect(merged[0]).toEqual(stored[0])
    expect(merged[1]?.name).toBe('Imported zone 1')
    expect(merged[1]?.points).toEqual(newServerPolygon)
  })
})

describe('pointInPolygon', () => {
  const square = [
    { x: 0, y: 0 },
    { x: 1, y: 0 },
    { x: 1, y: 1 },
    { x: 0, y: 1 },
  ]

  it('reports a point inside the polygon as inside', () => {
    expect(pointInPolygon({ x: 0.5, y: 0.5 }, square)).toBe(true)
  })

  it('reports a point outside the polygon as outside', () => {
    expect(pointInPolygon({ x: 2, y: 2 }, square)).toBe(false)
  })
})

describe('distance and polygonKey', () => {
  it('measures straight-line distance', () => {
    expect(distance({ x: 0, y: 0 }, { x: 3, y: 4 })).toBe(5)
  })

  it('produces the same key for equal point lists and a different one otherwise', () => {
    const a = [{ x: 0.1, y: 0.2 }, { x: 0.3, y: 0.4 }]
    const b = [{ x: 0.1, y: 0.2 }, { x: 0.3, y: 0.4 }]
    const c = [{ x: 0.1, y: 0.2 }, { x: 0.3, y: 0.5 }]
    expect(polygonKey(a)).toBe(polygonKey(b))
    expect(polygonKey(a)).not.toBe(polygonKey(c))
  })
})

describe('zonesSignature', () => {
  it('changes when a name or the enabled flag changes, not just the points', () => {
    const zone: EditorZone = {
      id: 'a',
      name: 'Front yard',
      enabled: true,
      points: [{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 1, y: 1 }],
    }
    const base = zonesSignature([zone])
    expect(zonesSignature([{ ...zone, name: 'Back yard' }])).not.toBe(base)
    expect(zonesSignature([{ ...zone, enabled: false }])).not.toBe(base)
    expect(zonesSignature([zone])).toBe(base)
  })
})
