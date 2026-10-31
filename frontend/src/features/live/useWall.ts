import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { queryKeys } from '@/api/endpoints'
import type { Layout, WallChoice } from '@/features/live/layout'
import { viewerApi } from '@/features/viewer/access'
import type { WallLayout } from '@/types/api'

const CHOICE_KEY = 'sentinel.wall.layout'
const CHOICES: readonly WallChoice[] = ['default', '1', '2x2', '3x3', '4x4']

/** The administrator's order, without hidden cameras; new cameras go last. */
export function arrangeCameras<T extends { id: string }>(cameras: T[], wall?: WallLayout): T[] {
  if (!wall) return cameras
  const hidden = new Set(wall.hidden)
  const rank = new Map(wall.order.map((id, index) => [id, index]))
  return cameras
    .filter((camera) => !hidden.has(camera.id))
    .map((camera, index) => ({ camera, rank: rank.get(camera.id) ?? wall.order.length + index }))
    .sort((a, b) => a.rank - b.rank)
    .map(({ camera }) => camera)
}

function readChoice(): WallChoice {
  try {
    const stored = window.localStorage.getItem(CHOICE_KEY) as WallChoice | null
    return stored && CHOICES.includes(stored) ? stored : 'default'
  } catch {
    return 'default'
  }
}

/**
 * The wall as the administrator arranged it, with the grid this device chose.
 *
 * `ready` holds the request until the caller can authenticate it: the viewer
 * wall first turns its key into a session.
 */
export function useWall<T extends { id: string }>(cameras: T[], ready = true) {
  const wall = useQuery({
    queryKey: queryKeys.wall,
    queryFn: () => viewerApi.get<WallLayout>('/viewer/wall'),
    enabled: ready,
    staleTime: 30_000,
  })
  const [choice, setChoiceState] = useState<WallChoice>(readChoice)

  const setChoice = (next: WallChoice) => {
    setChoiceState(next)
    try {
      if (next === 'default') window.localStorage.removeItem(CHOICE_KEY)
      else window.localStorage.setItem(CHOICE_KEY, next)
    } catch {
      // Without storage the choice lasts until the page closes.
    }
  }

  const layout: Layout = choice === 'default' ? (wall.data?.columns ?? 'auto') : choice
  return {
    list: arrangeCameras(cameras, wall.data),
    layout,
    choice,
    setChoice,
    // A failed read falls back to every camera rather than an empty wall.
    settled: !wall.isPending || wall.isError,
  }
}
