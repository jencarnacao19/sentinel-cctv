import { describe, expect, it } from 'vitest'

import type { EditorZone } from '@/features/zones/types'
import {
  clamp01,
  defaultZoneName,
  distance,
  pointInPolygon,
  sanitizeServerZones,
  zoneProblems,
  zonesSignature,
  zonesToPayload,
} from '@/features/zones/zoneMath'

const TRIANGLE: [number, number][] = [
  [0, 0],
  [1, 0],
  [1, 1],
]

function wire(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 'zgate',
    name: 'Gate',
    kind: 'detect',
    enabled: true,
    sensitivity: 70,
    points: TRIANGLE,
    ...overrides,
  }
}

function zone(overrides: Partial<EditorZone> = {}): EditorZone {
  return {
    id: 'zgate',
    saved: true,
    name: 'Gate',
    kind: 'detect',
    enabled: true,
    sensitivity: null,
    points: [
      { x: 0, y: 0 },
      { x: 1, y: 0 },
      { x: 1, y: 1 },
    ],
    ...overrides,
  }
}

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
  it('reads a well-formed zone with its name, kind and sensitivity', () => {
    const result = sanitizeServerZones([wire()])
    expect(result.dropped).toBe(0)
    expect(result.zones).toEqual([
      {
        id: 'zgate',
        saved: true,
        name: 'Gate',
        kind: 'detect',
        enabled: true,
        sensitivity: 70,
        points: [
          { x: 0, y: 0 },
          { x: 1, y: 0 },
          { x: 1, y: 1 },
        ],
      },
    ])
  })

  it('keeps disabled zones, so they survive being switched off for a while', () => {
    expect(sanitizeServerZones([wire({ enabled: false })]).zones[0]?.enabled).toBe(false)
  })

  it('gives an ignore zone no sensitivity of its own', () => {
    const result = sanitizeServerZones([wire({ kind: 'ignore', sensitivity: 70 })])
    expect(result.zones[0]?.sensitivity).toBeNull()
  })

  it('does not crash on a completely malformed payload and drops nothing usable', () => {
    expect(sanitizeServerZones(null)).toEqual({ zones: [], dropped: 0 })
    expect(sanitizeServerZones(undefined)).toEqual({ zones: [], dropped: 0 })
    expect(sanitizeServerZones('not an array')).toEqual({ zones: [], dropped: 0 })
    expect(sanitizeServerZones(42)).toEqual({ zones: [], dropped: 0 })
  })

  it('drops unusable zones but keeps the valid ones, reporting the count', () => {
    const raw = [
      wire({ points: [[0, 0], [1, 1]] }), // only two points -- not a polygon
      wire({ points: [[0, 0], [1.5, 0], [0, 1]] }), // out of range
      wire({ kind: 'other' }),
      wire({ name: '  ' }),
      wire({ id: '' }),
      wire(),
    ]
    const result = sanitizeServerZones(raw)
    expect(result.zones).toHaveLength(1)
    expect(result.dropped).toBe(5)
  })

  it('drops entries beyond the 32-zone limit without touching the ones that fit', () => {
    const raw = Array.from({ length: 40 }, (_, index) => wire({ id: `z${index}` }))
    const result = sanitizeServerZones(raw)
    expect(result.zones).toHaveLength(32)
    expect(result.dropped).toBe(8)
  })

  it('never throws regardless of what garbage is nested inside', () => {
    const raw = [null, 1, 'x', { not: 'a zone' }, [[0, 0], [1, 0], [1, 1]]]
    expect(() => sanitizeServerZones(raw)).not.toThrow()
    expect(sanitizeServerZones(raw).zones).toEqual([])
  })
})

describe('zonesToPayload', () => {
  it('sends every zone, rounded to 4 decimals, with its kind and enabled flag', () => {
    const payload = zonesToPayload([
      zone({
        sensitivity: 80,
        points: [
          { x: 0.123456, y: 0.987654 },
          { x: 0, y: 1 },
          { x: 0.5, y: 0.5 },
        ],
      }),
      zone({ id: 'ztree', name: 'Tree', kind: 'ignore', enabled: false }),
    ])
    expect(payload).toEqual([
      {
        id: 'zgate',
        name: 'Gate',
        kind: 'detect',
        enabled: true,
        sensitivity: 80,
        points: [
          [0.1235, 0.9877],
          [0, 1],
          [0.5, 0.5],
        ],
      },
      { id: 'ztree', name: 'Tree', kind: 'ignore', enabled: false, sensitivity: null, points: TRIANGLE },
    ])
  })

  it('leaves the id off a zone drawn in this session so the server assigns one', () => {
    const [payload] = zonesToPayload([zone({ id: 'temporary', saved: false })])
    expect(payload).not.toHaveProperty('id')
  })

  it('clamps a stray out-of-range coordinate rather than sending it as-is', () => {
    const [payload] = zonesToPayload([
      zone({
        points: [
          { x: -0.2, y: 0 },
          { x: 1, y: 0 },
          { x: 1.5, y: 1 },
        ],
      }),
    ])
    expect(payload?.points).toEqual(TRIANGLE)
  })
})

describe('zoneProblems', () => {
  it('finds nothing wrong with distinct, named zones', () => {
    expect(zoneProblems([zone(), zone({ id: 'ztree', name: 'Tree' })])).toEqual([])
  })

  it('refuses two zones with the same name, ignoring case and spaces', () => {
    expect(zoneProblems([zone(), zone({ id: 'z2', name: ' gate ' })])).toHaveLength(1)
  })

  it('refuses a zone without a name', () => {
    expect(zoneProblems([zone({ name: '   ' })])).toEqual(['Every zone needs a name.'])
  })
})

describe('defaultZoneName', () => {
  it('picks the first free name for the kind being drawn', () => {
    expect(defaultZoneName('detect', [zone({ name: 'Zone 1' })])).toBe('Zone 2')
    expect(defaultZoneName('ignore', [])).toBe('Ignored area 1')
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

describe('distance', () => {
  it('measures straight-line distance', () => {
    expect(distance({ x: 0, y: 0 }, { x: 3, y: 4 })).toBe(5)
  })
})

describe('zonesSignature', () => {
  it('changes when the name, kind, sensitivity or enabled flag changes, not just the points', () => {
    const base = zonesSignature([zone()])
    expect(zonesSignature([zone({ name: 'Back yard' })])).not.toBe(base)
    expect(zonesSignature([zone({ enabled: false })])).not.toBe(base)
    expect(zonesSignature([zone({ kind: 'ignore' })])).not.toBe(base)
    expect(zonesSignature([zone({ sensitivity: 90 })])).not.toBe(base)
    expect(zonesSignature([zone()])).toBe(base)
  })
})
