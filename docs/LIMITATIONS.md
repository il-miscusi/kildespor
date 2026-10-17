# Known gaps

- **Website discovery without a search engine.** About 11% of the eligible
  universe lists a website in the Entity Register. For the rest, the agent
  tries domains derived from the legal and subunit names and publishes one
  only when the site shows the organisation number. Brands whose domain does
  not resemble the legal name, sites on shared platforms (for example
  `name.barnehage.no`) and sites that never print their organisation number
  are missed. This is deliberate: the agent misses rather than guesses.
- **Ratings, reviews and sentiment** are not collected. No permitted source
  covers them at this scale without an account or licence.
- **Social platform content** (follower counts, posts) is not collected; only
  company-owned profile links are.
- **Job ads** come from arbeidsplassen.nav.no (which includes many FINN ads)
  and from JSON-LD on company sites. Ads only on other boards or behind
  applicant-tracking widgets are missed. Search results are capped by the
  board, so very large employers may show a subset.
- **Financial history** is what the accounts API returns (up to three fiscal
  years). Older years are not parsed from the PDF copies.
- **Brreg announcements** (`w2.brreg.no/kunngjoring/`) are not fetched
  because robots.txt disallows them; dated register activity comes from the
  roles register and the official update feed instead.
- **JavaScript-only sites** are read as served; there is no headless browser.
  If the organisation number appears only after rendering, the site is not
  verified.
