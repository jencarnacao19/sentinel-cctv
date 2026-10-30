/**
 * The last frame each camera showed on this device, kept so a reopened wall
 * paints at once instead of a grid of black boxes while the streams connect.
 *
 * It lives only in this browser's storage, never on the server, and signing
 * out removes it.
 */

const PREFIX = 'sentinel.poster.'
const WIDTH = 480

export function posterKey(cameraId: string, sub: boolean): string {
  return `${PREFIX}${cameraId}${sub ? '.sub' : ''}`
}

export function readPoster(key: string): string | undefined {
  try {
    return window.localStorage.getItem(key) ?? undefined
  } catch {
    return undefined
  }
}

export function savePoster(key: string, video: HTMLVideoElement): void {
  if (!video.videoWidth) return
  try {
    const canvas = document.createElement('canvas')
    canvas.width = WIDTH
    canvas.height = Math.round((WIDTH * video.videoHeight) / video.videoWidth)
    canvas.getContext('2d')?.drawImage(video, 0, 0, canvas.width, canvas.height)
    window.localStorage.setItem(key, canvas.toDataURL('image/jpeg', 0.7))
  } catch {
    // Full or blocked storage only costs the next open its instant picture.
  }
}

export function clearPosters(): void {
  try {
    for (const key of Object.keys(window.localStorage)) {
      if (key.startsWith(PREFIX)) window.localStorage.removeItem(key)
    }
  } catch {
    // Nothing stored, or storage unavailable.
  }
}
