import { RefreshCw } from 'lucide-react'
import { Component, type ErrorInfo, type ReactNode } from 'react'

import { Button } from '@/components/ui/button'

interface Props {
  children: ReactNode
  fallback?: (error: Error, reset: () => void) => ReactNode
}

interface State {
  error: Error | null
}

/**
 * Keeps a render error from blanking the whole app.
 *
 * A CCTV console that goes white is worse than one showing a degraded panel:
 * the operator cannot tell whether the cameras stopped or the page did.
 */
export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('Sentinel UI error:', error, info.componentStack)
  }

  reset = (): void => {
    this.setState({ error: null })
  }

  override render(): ReactNode {
    const { error } = this.state
    if (!error) return this.props.children
    if (this.props.fallback) return this.props.fallback(error, this.reset)

    return (
      <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 px-6 text-center">
        <h2 className="text-lg font-semibold">Something went wrong in the interface.</h2>
        <p className="max-w-md text-sm text-(--color-fg-muted)">
          Recording and motion detection run on the server and are unaffected by this error.
        </p>
        <pre className="max-w-full overflow-x-auto rounded-lg bg-(--color-bg-subtle) px-3 py-2 text-left text-xs text-(--color-fg-muted)">
          {error.message}
        </pre>
        <Button onClick={this.reset} variant="secondary">
          <RefreshCw aria-hidden="true" />
          Try again
        </Button>
      </div>
    )
  }
}
