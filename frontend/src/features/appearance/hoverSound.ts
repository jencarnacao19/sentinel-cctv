import { useThemeStore } from '@/stores/theme'
import { isMobileInterface } from '@/lib/device'

const INTERACTIVE = 'button, a[href], summary, select, [role="button"], [role="tab"], [role="menuitem"], [role="radio"], [role="switch"]'

export function hoverTarget(event: PointerEvent): Element | null {
  if (event.pointerType !== 'mouse' || !(event.target instanceof Element)) return null
  const target = event.target.closest(INTERACTIVE)
  if (!target || target.matches(':disabled, [aria-disabled="true"]') || target.closest('[data-ui-sound="off"], [inert]')) return null
  if (event.relatedTarget instanceof Node && target.contains(event.relatedTarget)) return null
  return target
}

/** Reuse the tiny audio buffer; start each hover immediately, never queue it. */
export function createHoverClick(context: AudioContext) {
  const buffer = context.createBuffer(1, Math.ceil(context.sampleRate * 0.012), context.sampleRate)
  const samples = buffer.getChannelData(0)
  for (let i = 0; i < samples.length; i++) samples[i] = (Math.random() * 2 - 1) * Math.exp(-i / (samples.length / 5))
  const filter = context.createBiquadFilter()
  const gain = context.createGain()
  filter.type = 'lowpass'
  filter.frequency.value = 1900
  gain.gain.value = 0.045
  filter.connect(gain).connect(context.destination)
  const active = new Set<AudioBufferSourceNode>()
  return {
    play() {
      // Bound pathological bursts by dropping the oldest sound, not delaying
      // the newest interaction. Normally a voice ends after just 12 ms.
      if (active.size >= 8) {
        const oldest = active.values().next().value!
        oldest.stop()
        oldest.disconnect()
        active.delete(oldest)
      }
      const source = context.createBufferSource()
      source.buffer = buffer
      source.connect(filter)
      source.onended = () => { active.delete(source); source.disconnect() }
      active.add(source)
      source.start()
    },
    dispose() {
      for (const source of active) { source.stop(); source.disconnect() }
      active.clear()
      filter.disconnect()
      gain.disconnect()
    },
  }
}

/** One quiet, 12ms filtered click. No audio file, tracker, timer loop or camera audio changes. */
export function installHoverSound(): () => void {
  // No listeners or AudioContext on mobile; don't alter the account's desktop
  // sound preference and never touch camera media elements.
  if (isMobileInterface()) return () => {}
  let context: AudioContext | null = null
  let click: ReturnType<typeof createHoverClick> | null = null
  const allowed = () => {
    const state = useThemeStore.getState()
    return !isMobileInterface() && state.ready && state.policyKnown && state.soundAllowed && state.hover_sound &&
      !document.hidden && window.matchMedia('(any-hover: hover) and (any-pointer: fine)').matches
  }
  const close = () => {
    const previous = context
    context = null
    click?.dispose()
    click = null
    if (previous) void previous.close().catch(() => undefined)
  }
  const unlock = (event: Event) => {
    if (!event.isTrusted || !allowed() || !window.AudioContext) return
    try {
      if (!context) {
        context = new AudioContext({ latencyHint: 'interactive' })
        click = createHoverClick(context)
      }
      if (context.state === 'suspended') void context.resume().catch(() => undefined)
    } catch { close() }
  }
  const over = (event: PointerEvent) => {
    if (!event.isTrusted || !allowed() || !hoverTarget(event) || context?.state !== 'running') return
    try {
      click?.play()
    } catch { close() }
  }
  const unsubscribe = useThemeStore.subscribe(() => { if (!allowed()) close() })
  const visibility = () => { if (document.hidden) close() }
  document.addEventListener('pointerdown', unlock, true)
  document.addEventListener('click', unlock)
  document.addEventListener('keydown', unlock, true)
  document.addEventListener('pointerover', over, { passive: true })
  document.addEventListener('visibilitychange', visibility)
  return () => {
    unsubscribe()
    document.removeEventListener('pointerdown', unlock, true)
    document.removeEventListener('click', unlock)
    document.removeEventListener('keydown', unlock, true)
    document.removeEventListener('pointerover', over)
    document.removeEventListener('visibilitychange', visibility)
    close()
  }
}
