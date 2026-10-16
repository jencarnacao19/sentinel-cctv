import { useQuery } from '@tanstack/react-query'

import { queryKeys, systemApi } from '@/api/endpoints'
import { PageHeader } from '@/components/layout/PageHeader'
import { Alert } from '@/components/ui/alert'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { formatDuration } from '@/lib/utils'

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-2.5">
      <dt className="text-sm text-(--color-fg-muted)">{label}</dt>
      <dd className="tabular text-sm font-medium">{value}</dd>
    </div>
  )
}

export function SystemPage() {
  const info = useQuery({ queryKey: queryKeys.systemInfo, queryFn: systemApi.info })

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <PageHeader title="System" description="Details about this Sentinel installation." />

      {info.isError ? (
        <Alert variant="danger" title="Could not load system information.">
          The server may be restarting. This page retries automatically.
        </Alert>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Server</CardTitle>
        </CardHeader>
        <CardContent>
          {info.isPending ? (
            <div className="space-y-3">
              {Array.from({ length: 5 }, (_, index) => (
                <Skeleton key={index} className="h-5" />
              ))}
            </div>
          ) : info.data ? (
            <dl className="divide-y divide-(--color-border)">
              <Row label="Version" value={info.data.version} />
              <Row label="Uptime" value={formatDuration(info.data.uptime_seconds)} />
              <Row label="Timezone" value={info.data.timezone} />
              <Row label="Platform" value={info.data.platform} />
              <Row label="Python" value={info.data.python_version} />
              <Row
                label="Setup"
                value={info.data.setup_completed ? 'Complete' : 'Not finished'}
              />
              <Row
                label="Signed in as"
                value={`${info.data.authenticated_as.name} (${info.data.authenticated_as.kind})`}
              />
            </dl>
          ) : null}
        </CardContent>
      </Card>
    </div>
  )
}
