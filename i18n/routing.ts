/**
 * i18n Routing Configuration
 *
 * Defines the supported locales and default locale for the app.
 * To add a new language:
 *   1. Add the locale code to the `locales` array (e.g., 'es', 'fr')
 *   2. Create a corresponding messages file (e.g., `messages/es.json`)
 *   3. Update the proxy matcher if needed (e.g., add '/(es)/:path*')
 */
import { defineRouting } from 'next-intl/routing'

export const routing = defineRouting({
  locales: ['en'],
  defaultLocale: 'en',
})
