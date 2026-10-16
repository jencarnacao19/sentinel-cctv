// Applied before first paint so the stored theme never flashes the wrong colour.
// Kept in a same-origin file so production can use a CSP without unsafe inline scripts.
;(() => {
  try {
    // Identity is not known before authentication. Never paint the previous
    // person's cached theme while another user signs in on a shared device.
    const stored = 'system'
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
    const dark = stored === 'dark' || ((stored === 'system' || !stored) && prefersDark)
    document.documentElement.classList.toggle('dark', dark)
  } catch {
    // Private mode: keep the dark default already present on <html>.
  }
})()
