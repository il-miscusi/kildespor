/**
 * Query Provider
 *
 * Wraps the app in React Query's QueryClientProvider so that
 * any client component can use useQuery(), useMutation(), etc.
 *
 * This must be a client component ('use client') because React Query
 * uses browser-only APIs (subscriptions, timers, etc.).
 *
 * Place this high in the component tree (inside the locale layout)
 * so all pages and components have access to the query client.
 */
'use client'

import { QueryClientProvider } from '@tanstack/react-query'
import { getQueryClient } from '@/lib/query-client'

export function QueryProvider({ children }: { children: React.ReactNode }) {
  const queryClient = getQueryClient()

  return (
    <QueryClientProvider client={queryClient}>
      {children}
    </QueryClientProvider>
  )
}
