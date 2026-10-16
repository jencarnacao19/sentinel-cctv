import { create } from 'zustand'

export type ThemeMode = 'light' | 'dark' | 'system'
export type Palette = 'slate' | 'ocean' | 'forest' | 'sand'
export interface Preferences { mode: ThemeMode; palette: Palette; hover_sound: boolean }
export const DEFAULT_PREFERENCES: Preferences = { mode: 'system', palette: 'slate', hover_sound: true }
export const PALETTES: { value: Palette; label: string; colour: string }[] = [
  { value: 'slate', label: 'Slate', colour: '#718096' },
  { value: 'ocean', label: 'Ocean', colour: '#247caa' },
  { value: 'forest', label: 'Forest', colour: '#348268' },
  { value: 'sand', label: 'Sand', colour: '#a57846' },
]

export function validPreferences(value: unknown): value is Preferences {
  if (!value || typeof value !== 'object') return false
  const p = value as Record<string, unknown>
  return Object.keys(p).length === 3 && ['light', 'dark', 'system'].some((mode) => mode === p.mode) &&
    PALETTES.some((palette) => palette.value === p.palette) && typeof p.hover_sound === 'boolean'
}

export function resolveTheme(mode: ThemeMode): 'light' | 'dark' {
  return mode === 'system' ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : mode
}

function applyTheme(p: Preferences) {
  document.documentElement.classList.toggle('dark', resolveTheme(p.mode) === 'dark')
  document.documentElement.dataset.palette = p.palette
  document.documentElement.style.colorScheme = resolveTheme(p.mode)
}

type Writer = (preferences: Preferences) => Promise<unknown> | void
let writer: Writer | null = null
let epoch = 0
let queue: Promise<void> | null = null

interface ThemeState extends Preferences {
  ready: boolean
  scope: string
  saveState: 'loading' | 'saved' | 'saving' | 'error'
  revision: number
  soundAllowed: boolean
  policyKnown: boolean
  setMode: (mode: ThemeMode) => void
  setPalette: (palette: Palette) => void
  setHoverSound: (enabled: boolean) => void
  retry: () => void
}

export const useThemeStore = create<ThemeState>(() => ({
  ...DEFAULT_PREFERENCES, ready: false, scope: 'Loading preferences', saveState: 'loading', revision: 0,
  soundAllowed: false, policyKnown: false,
  setMode: (mode) => change({ mode }),
  setPalette: (palette) => change({ palette }),
  setHoverSound: (hover_sound) => change({ hover_sound }),
  retry: () => change({}),
}))

export function preferencesSnapshot(): Preferences {
  const { mode, palette, hover_sound } = useThemeStore.getState()
  return { mode, palette, hover_sound }
}

function change(patch: Partial<Preferences>) {
  const state = useThemeStore.getState()
  if (!state.ready || !writer) return
  const preferences = { ...preferencesSnapshot(), ...patch }
  if (!validPreferences(preferences)) return
  const revision = state.revision + 1
  const activeEpoch = epoch
  const save = writer
  useThemeStore.setState({ ...preferences, revision, saveState: 'saving' })
  applyTheme(preferences)
  // Local storage must finish during the click, before navigation can reset
  // the store. Serialize asynchronous account/HA saves without delaying the
  // first write by a microtask.
  const run = () => {
    if (activeEpoch !== epoch) return
    return save(preferences)
  }
  const done = () => {
    if (activeEpoch === epoch && revision === useThemeStore.getState().revision) {
      useThemeStore.setState({ saveState: 'saved' })
    }
  }
  const failed = () => {
    if (activeEpoch === epoch && revision === useThemeStore.getState().revision) {
      useThemeStore.setState({ saveState: 'error' })
    }
  }
  let result: Promise<unknown> | void
  try { result = queue ? queue.then(run) : run() } catch { failed(); return }
  if (!result) { done(); return }
  const work = Promise.resolve(result).then(done, failed)
  queue = work
  void work.then(() => { if (queue === work) queue = null })
}

export function resetPreferences() {
  epoch += 1
  writer = null
  queue = null
  useThemeStore.setState({ ...DEFAULT_PREFERENCES, ready: false, scope: 'Loading preferences', saveState: 'loading', revision: 0, soundAllowed: false, policyKnown: false })
  applyTheme(DEFAULT_PREFERENCES)
}

export function activatePreferences(scope: string, preferences: Preferences, save: Writer) {
  epoch += 1
  writer = save
  queue = null
  useThemeStore.setState({ ...preferences, scope, ready: true, saveState: 'saved', revision: 0 })
  applyTheme(preferences)
}

export function refreshPreferences(preferences: Preferences) {
  const state = useThemeStore.getState()
  if (!state.ready || state.saveState !== 'saved') return
  useThemeStore.setState(preferences)
  applyTheme(preferences)
}

export function initTheme(): () => void {
  applyTheme(preferencesSnapshot())
  const media = window.matchMedia('(prefers-color-scheme: dark)')
  const onChange = () => applyTheme(preferencesSnapshot())
  media.addEventListener('change', onChange)
  return () => media.removeEventListener('change', onChange)
}
