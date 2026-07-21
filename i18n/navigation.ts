/**
 * i18n Navigation Helpers
 *
 * Locale-aware wrappers around Next.js navigation APIs.
 * Use these instead of the standard Next.js equivalents
 * to ensure links and redirects include the correct locale prefix.
 *
 * Usage:
 *   import { Link, useRouter } from '@/i18n/navigation'
 *   <Link href="/about">About</Link>  →  renders /en/about
 *   router.push('/about')              →  navigates to /en/about
 */
import { createNavigation } from 'next-intl/navigation'
import { routing } from './routing'

export const { Link, redirect, usePathname, useRouter, getPathname } =
  createNavigation(routing)
