import { ApiError, NetworkError } from '@/api/client'

/** What importing a configuration file did, camera by camera. */
export interface ImportSummary {
  cameras_added: string[]
  cameras_skipped: { name: string; reason: string }[]
  settings_applied: string[]
  settings_skipped: { name: string; reason: string }[]
  warnings: string[]
  passwords_restored: boolean
}

export function errorText(error: unknown, fallback: string): string | null {
  if (!error) return null
  if (error instanceof NetworkError) return 'Could not reach the Sentinel server.'
  if (error instanceof ApiError) return error.message
  return fallback
}

/** The server's own explanation of a refused upload or download. */
export async function failure(response: Response, fallback: string): Promise<ApiError> {
  const payload: unknown = await response.json().catch(() => null)
  const detail =
    payload && typeof payload === 'object' && 'detail' in payload ? String(payload.detail) : fallback
  return new ApiError(response.status, detail)
}
