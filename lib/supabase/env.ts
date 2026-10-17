export type SupabasePublicEnv = {
  url: string | undefined
  key: string | undefined
  configured: boolean
}

export function getSupabasePublicEnv(): SupabasePublicEnv {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL
  const key =
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY ??
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY

  return {
    url,
    key,
    configured: Boolean(url && key),
  }
}

export function requireSupabasePublicEnv(): { url: string; key: string } {
  const env = getSupabasePublicEnv()

  if (!env.url || !env.key) {
    throw new Error(
      'Missing Supabase env. Expected NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY or NEXT_PUBLIC_SUPABASE_ANON_KEY.',
    )
  }

  return { url: env.url, key: env.key }
}
