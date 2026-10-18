import { ApiError, NetworkError } from '@/api/client'

/**
 * What to tell a person about a failed request, or `null` when nothing failed.
 *
 * Sentinel's own message when the server sent one, a fixed sentence when the
 * server could not be reached, and `fallback` for anything else -- one rule
 * for every screen, so the same failure never reads two different ways.
 */
export function errorText(error: unknown, fallback = 'Something went wrong.'): string | null {
  if (!error) return null
  if (error instanceof NetworkError) return 'Could not reach the Sentinel server.'
  if (error instanceof ApiError) return error.message
  return fallback
}
