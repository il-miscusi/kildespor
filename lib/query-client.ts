/**
 * Query Client Singleton
 *
 * Creates a single QueryClient instance shared across the app.
 * Uses a global variable to persist the client between HMR refreshes
 * in development, preventing redundant re-renders and cache resets.
 *
 * Default options:
 *   - staleTime: 5 min — data is considered fresh, no automatic refetch
 *   - gcTime: 30 min — unused queries stay cached in memory
 *
 * Override per-query by passing options to useQuery():
 *   useQuery({ queryKey: ['todos'], queryFn: fetchTodos, staleTime: 0 })
 */
import { QueryClient } from '@tanstack/react-query'

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5 * 60 * 1000,
        gcTime: 30 * 60 * 1000,
      },
    },
  })
}

let browserQueryClient: QueryClient | undefined

export function getQueryClient() {
  if (typeof window === 'undefined') {
    // Server: always make a new query client
    return makeQueryClient()
  }
  // Browser: make a new client if we don't already have one
  if (!browserQueryClient) {
    browserQueryClient = makeQueryClient()
  }
  return browserQueryClient
}
