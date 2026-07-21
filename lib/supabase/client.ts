'use client'

import { createBrowserClient } from '@supabase/ssr'

import { requireSupabasePublicEnv } from '@/lib/supabase/env'

type BrowserSupabaseClient = ReturnType<typeof createBrowserClient>

let browserClient: BrowserSupabaseClient | undefined

export function createSupabaseBrowserClient(): BrowserSupabaseClient {
  const { url, key } = requireSupabasePublicEnv()

  return createBrowserClient(url, key)
}

export function getSupabaseBrowserClient(): BrowserSupabaseClient {
  if (!browserClient) {
    browserClient = createSupabaseBrowserClient()
  }

  return browserClient
}
