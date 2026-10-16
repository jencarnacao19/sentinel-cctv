import { describe, expect, it } from 'vitest'

import { cn, formatBytes, formatDuration } from '@/lib/utils'

describe('cn', () => {
  it('merges conditional classes', () => {
    const enabled = false
    expect(cn('a', enabled && 'b', 'c')).toBe('a c')
  })

  it('lets the later Tailwind utility win', () => {
    expect(cn('p-2', 'p-4')).toBe('p-4')
  })
})

describe('formatBytes', () => {
  it.each([
    [0, '0 B'],
    [999, '999 B'],
    [1000, '1.0 kB'],
    [1_500_000, '1.5 MB'],
    [2_000_000_000, '2.0 GB'],
    [3_500_000_000_000, '3.5 TB'],
  ])('formats %i as %s', (input, expected) => {
    expect(formatBytes(input)).toBe(expected)
  })

  it('returns a dash for missing values', () => {
    expect(formatBytes(null)).toBe('—')
    expect(formatBytes(undefined)).toBe('—')
    expect(formatBytes(Number.NaN)).toBe('—')
  })
})

describe('formatDuration', () => {
  it.each([
    [0, '0s'],
    [45, '45s'],
    [90, '1m 30s'],
    [3661, '1h 01m'],
    [90_061, '1d 01h'],
  ])('formats %i seconds as %s', (input, expected) => {
    expect(formatDuration(input)).toBe(expected)
  })

  it('returns a dash for missing or negative values', () => {
    expect(formatDuration(null)).toBe('—')
    expect(formatDuration(-5)).toBe('—')
  })
})
