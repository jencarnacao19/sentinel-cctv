/**
 * Undo/redo over the editor's zone list.
 *
 * Every discrete edit (place a vertex, drop one, nudge one, rename or toggle
 * a zone, delete a zone) goes through `commit`, which is the only thing that
 * grows the undo stack -- one entry per meaningful action, not one per pixel
 * of a drag. `ZoneCanvas` keeps in-progress drag positions in its own local
 * state and only calls `commit` when the gesture ends, which is what keeps
 * "undo" from taking fifty presses to undo one drag.
 */

import { useCallback, useState } from 'react'

interface HistoryState<T> {
  past: T[]
  present: T
  future: T[]
}

export interface ZoneHistory<T> {
  state: T
  canUndo: boolean
  canRedo: boolean
  /** Apply an edit, pushing the current state onto the undo stack and
   * clearing redo -- the normal shape of "I did something new". */
  commit: (updater: (current: T) => T) => void
  undo: () => void
  redo: () => void
  /** Replace the state with no history at all: used when a different camera
   * (or a fresh load of the same one) is hydrated, where the previous
   * camera's undo stack has nothing to do with the new one. */
  resetTo: (next: T) => void
}

export function useZoneHistory<T>(initial: T): ZoneHistory<T> {
  const [history, setHistory] = useState<HistoryState<T>>({ past: [], present: initial, future: [] })

  const commit = useCallback((updater: (current: T) => T) => {
    setHistory((current) => ({
      past: [...current.past, current.present],
      present: updater(current.present),
      future: [],
    }))
  }, [])

  const undo = useCallback(() => {
    setHistory((current) => {
      if (current.past.length === 0) return current
      const previous = current.past[current.past.length - 1]
      return {
        past: current.past.slice(0, -1),
        present: previous,
        future: [current.present, ...current.future],
      }
    })
  }, [])

  const redo = useCallback(() => {
    setHistory((current) => {
      if (current.future.length === 0) return current
      const next = current.future[0]
      return {
        past: [...current.past, current.present],
        present: next,
        future: current.future.slice(1),
      }
    })
  }, [])

  const resetTo = useCallback((next: T) => {
    setHistory({ past: [], present: next, future: [] })
  }, [])

  return {
    state: history.present,
    canUndo: history.past.length > 0,
    canRedo: history.future.length > 0,
    commit,
    undo,
    redo,
    resetTo,
  }
}
