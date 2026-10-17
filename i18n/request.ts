/**
 * i18n Request Configuration
 *
 * Loaded on every server request to determine the current locale
 * and provide the matching translation messages.
 *
 * This file is referenced by next-intl/plugin in next.config.ts
 * and should not be renamed or moved without updating that reference.
 */
import { getRequestConfig } from 'next-intl/server'
import { routing } from './routing'

export default getRequestConfig(async ({ requestLocale }) => {
  let locale = await requestLocale

  // Fallback to default locale if the requested one is not supported
  if (!locale || !routing.locales.includes(locale as (typeof routing.locales)[number])) {
    locale = routing.defaultLocale
  }

  return {
    locale,
    // Dynamically load the message catalog for the resolved locale
    messages: (await import(`../messages/${locale}.json`)).default,
  }
})
