import { Monitor, Moon, Sun } from 'lucide-react'

import { Segmented, type SegmentedOption } from '@/components/ui/segmented'
import { type ThemeMode, useThemeStore } from '@/stores/theme'

const OPTIONS: SegmentedOption<ThemeMode>[] = [
  { value: 'light', label: 'Light', icon: <Sun aria-hidden="true" /> },
  { value: 'dark', label: 'Dark', icon: <Moon aria-hidden="true" /> },
  { value: 'system', label: 'Match the system', icon: <Monitor aria-hidden="true" /> },
]

export function ThemeToggle() {
  const mode = useThemeStore((state) => state.mode)
  const setMode = useThemeStore((state) => state.setMode)

  return (
    <Segmented
      ariaLabel="Colour theme"
      size="compact"
      value={mode}
      onChange={setMode}
      options={OPTIONS}
    />
  )
}
