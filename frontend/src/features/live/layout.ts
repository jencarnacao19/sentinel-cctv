import type { SegmentedOption } from '@/components/ui/segmented'

export type Layout = 'auto' | '1' | '2x2' | '3x3' | '4x4'

/**
 * Grid density for the camera wall.
 *
 * Every layout is one column on a phone until there is room for a second that
 * is still worth looking at. The dense layouts used to start at `grid-cols-2`
 * from zero width, which on a 390px screen produced 180px-wide feeds -- narrower
 * than the fullscreen button drawn on top of them. Two columns begin at 480px,
 * which is a large phone in landscape or a small tablet, and no earlier.
 *
 * Shared by the administrator wall and the viewer wall, which had drifted into
 * two copies with different breakpoints and different labels.
 */
const COLUMNS: Record<Layout, string> = {
  auto: '',
  '1': 'grid-cols-1',
  '2x2': 'grid-cols-1 sm:grid-cols-2',
  '3x3': 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3',
  '4x4': 'grid-cols-1 min-[480px]:grid-cols-2 sm:grid-cols-3 lg:grid-cols-4',
}

/** What a person watching can pick; Default follows the administrator's grid. */
export type WallChoice = 'default' | Exclude<Layout, 'auto'>

export const WALL_OPTIONS: SegmentedOption<WallChoice>[] = [
  { value: 'default', label: "The administrator's default grid", children: 'Default' },
  { value: '1', label: 'One camera per row', children: '1' },
  { value: '2x2', label: 'Two columns', children: '2' },
  { value: '3x3', label: 'Three columns', children: '3' },
  { value: '4x4', label: 'Four columns', children: '4' },
]

/** Auto picks the tightest grid that still fits the cameras you have. */
export function autoColumns(count: number): string {
  if (count <= 1) return COLUMNS['1']
  if (count <= 4) return COLUMNS['2x2']
  if (count <= 9) return COLUMNS['3x3']
  return COLUMNS['4x4']
}

export function columnsFor(layout: Layout, count: number): string {
  return layout === 'auto' ? autoColumns(count) : COLUMNS[layout]
}
