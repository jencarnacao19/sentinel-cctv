import { PageHeader } from '@/components/layout/PageHeader'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { AccountsManager } from '@/features/accounts/AccountsManager'

export function UsersPage() {
  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <PageHeader title="Users" description="Who can sign in to this server." />

      <Card>
        <CardHeader>
          <CardTitle>Viewer accounts</CardTitle>
          <CardDescription>
            For the people who watch: they sign in at this server&apos;s address to see the
            cameras, recordings and saved clips, and cannot change anything.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <AccountsManager />
        </CardContent>
      </Card>
    </div>
  )
}
