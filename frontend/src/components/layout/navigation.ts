import {
  Activity,
  Clapperboard,
  Cpu,
  Gauge,
  HardDrive,
  Hexagon,
  History,
  House,
  LayoutGrid,
  Settings,
  Video,
  Waypoints,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

export interface NavItem {
  to: string
  label: string
  /** Shortened for the mobile tab bar, where a label has about seven characters. */
  shortLabel?: string
  icon: LucideIcon
  section: 'Observe' | 'Configure' | 'Server'
  /** Appears in the phone tab bar. Everything else lives behind "More". */
  primary?: boolean
}

/**
 * The application's destinations.
 *
 * An entry appears here only once its page does something real -- Sentinel
 * never shows a nav item that leads to a "coming soon" screen.
 *
 * The icons are monochrome line symbols from one family, drawn at one weight.
 * They replace a set of hand-picked Unicode glyphs (⌂ ◉ ◷ ▣ ◇ ◍ ▦ ⬡ ▤ ⌘ ⚙︎)
 * which rendered at a different size, weight and baseline in every font on
 * every platform, and which a screen reader announced as "house", "fisheye" and
 * "white square containing black small square".
 *
 * `primary` marks the four destinations that earn a permanent place in the
 * phone tab bar. Everything else is one tap away behind "More", which is the
 * ordinary iOS arrangement and keeps the frequent screens under the thumb.
 */
export const NAV_ITEMS: NavItem[] = [
  { to: '/admin', label: 'Dashboard', icon: Gauge, section: 'Observe', primary: true },
  { to: '/admin/live', label: 'Live', icon: Video, section: 'Observe', primary: true },
  {
    to: '/admin/timeline',
    label: 'Timeline',
    icon: History,
    section: 'Observe',
    primary: true,
  },
  { to: '/admin/clips', label: 'Saved clips', shortLabel: 'Clips', icon: Clapperboard, section: 'Observe' },
  { to: '/admin/events', label: 'Events', icon: Activity, section: 'Observe' },
  {
    to: '/admin/health',
    label: 'Camera health',
    shortLabel: 'Health',
    icon: Waypoints,
    section: 'Observe',
  },
  {
    to: '/admin/cameras',
    label: 'Cameras',
    icon: LayoutGrid,
    section: 'Configure',
    primary: true,
  },
  { to: '/admin/zones', label: 'Motion zones', shortLabel: 'Zones', icon: Hexagon, section: 'Configure' },
  { to: '/admin/storage', label: 'Storage', icon: HardDrive, section: 'Configure' },
  { to: '/admin/home-assistant', label: 'Home Assistant', shortLabel: 'HA', icon: House, section: 'Configure' },
  { to: '/admin/system', label: 'System', icon: Cpu, section: 'Server' },
  { to: '/admin/settings', label: 'Settings', icon: Settings, section: 'Server' },
]

export const NAV_SECTIONS = ['Observe', 'Configure', 'Server'] as const

/** The page title shown in the mobile navigation bar for a given path. */
export function titleForPath(pathname: string): string {
  // Longest match first, so `/admin/live` never resolves to `/admin`.
  const match = [...NAV_ITEMS]
    .sort((a, b) => b.to.length - a.to.length)
    .find((item) => pathname === item.to || pathname.startsWith(`${item.to}/`))
  return match?.label ?? 'Sentinel'
}
