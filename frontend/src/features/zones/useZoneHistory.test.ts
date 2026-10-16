import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { useZoneHistory } from '@/features/zones/useZoneHistory'

/**
 * `ZoneCanvas.test.tsx` deliberately drives its harness with plain `useState`
 * (see the comment there), so undo/redo -- one of the house-rule requirements
 * for this feature -- has never actually been exercised by a test. This file
 * is that coverage, isolated from React rendering entirely so the history
 * stack's own invariants (what grows it, what it discards, what a boundary
 * does) are pinned down independently of any canvas behaviour.
 */
describe('useZoneHistory', () => {
  it('starts at the given state with nothing to undo or redo', () => {
    const { result } = renderHook(() => useZoneHistory<number[]>([1]))
    expect(result.current.state).toEqual([1])
    expect(result.current.canUndo).toBe(false)
    expect(result.current.canRedo).toBe(false)
  })

  it('commit applies the updater and pushes the previous state onto the undo stack', () => {
    const { result } = renderHook(() => useZoneHistory<number[]>([1]))

    act(() => result.current.commit((current) => [...current, 2]))
    expect(result.current.state).toEqual([1, 2])
    expect(result.current.canUndo).toBe(true)
    expect(result.current.canRedo).toBe(false)

    act(() => result.current.commit((current) => [...current, 3]))
    expect(result.current.state).toEqual([1, 2, 3])
    expect(result.current.canUndo).toBe(true)
  })

  it('undo and redo walk the history in both directions', () => {
    const { result } = renderHook(() => useZoneHistory<number[]>([1]))
    act(() => result.current.commit((current) => [...current, 2]))
    act(() => result.current.commit((current) => [...current, 3]))

    act(() => result.current.undo())
    expect(result.current.state).toEqual([1, 2])
    expect(result.current.canUndo).toBe(true)
    expect(result.current.canRedo).toBe(true)

    act(() => result.current.undo())
    expect(result.current.state).toEqual([1])
    expect(result.current.canUndo).toBe(false)
    expect(result.current.canRedo).toBe(true)

    act(() => result.current.redo())
    expect(result.current.state).toEqual([1, 2])
    act(() => result.current.redo())
    expect(result.current.state).toEqual([1, 2, 3])
    expect(result.current.canRedo).toBe(false)
  })

  it('undo with nothing behind it and redo with nothing ahead of it are no-ops', () => {
    const { result } = renderHook(() => useZoneHistory<number[]>([1]))

    act(() => result.current.undo())
    expect(result.current.state).toEqual([1])
    expect(result.current.canUndo).toBe(false)

    act(() => result.current.commit((current) => [...current, 2]))
    act(() => result.current.redo())
    expect(result.current.state).toEqual([1, 2])
    expect(result.current.canRedo).toBe(false)
  })

  it('a new commit after an undo discards the abandoned redo branch for good', () => {
    const { result } = renderHook(() => useZoneHistory<number[]>([1]))
    act(() => result.current.commit((current) => [...current, 2]))
    act(() => result.current.commit((current) => [...current, 3]))
    // Back to [1, 2]; [1, 2, 3] is now sitting in the redo branch.
    act(() => result.current.undo())

    act(() => result.current.commit((current) => [...current, 99]))
    expect(result.current.state).toEqual([1, 2, 99])
    expect(result.current.canRedo).toBe(false)

    // Undoing the new edit must land back on [1, 2], never on the
    // abandoned [1, 2, 3] -- a redo branch a fresh edit did not come from is
    // not history any more, it is a stale draft.
    act(() => result.current.undo())
    expect(result.current.state).toEqual([1, 2])
  })

  it('resetTo replaces the state and clears all history, for switching cameras', () => {
    const { result } = renderHook(() => useZoneHistory<number[]>([1]))
    act(() => result.current.commit((current) => [...current, 2]))
    act(() => result.current.commit((current) => [...current, 3]))
    act(() => result.current.undo())

    act(() => result.current.resetTo([42]))
    expect(result.current.state).toEqual([42])
    expect(result.current.canUndo).toBe(false)
    expect(result.current.canRedo).toBe(false)
  })
})
