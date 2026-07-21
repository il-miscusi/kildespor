import { cookies } from 'next/headers'
import { createServerClient } from '@supabase/ssr'

import { requireSupabasePublicEnv } from '@/lib/supabase/env'

export async function createSupabaseServerClient() {
  const { url, key } = requireSupabasePublicEnv()
  const cookieStore = await cookies()

  return createServerClient(url, key, {
    cookies: {
      getAll() {
        return cookieStore.getAll()
      },
      setAll(cookiesToSet) {
        try {
          cookiesToSet.forEach(({ name, value, options }) => {
            cookieStore.set(name, value, options)
          })
        } catch {
          // Server Components cannot set cookies. Middleware, Route Handlers,
          // and Server Actions can use the same helper with writable cookies.
        }
      },
    },
  })
}
