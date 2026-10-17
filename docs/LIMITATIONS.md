# Known gaps

- **Website discovery without a search engine.** About 11% of the eligible
  universe lists a website in the Entity Register. For the rest, the agent
  tries domains derived from the legal, former and subunit names, the
  registered e-mail domain, NAV employer homepages and declared candidate
  caches (`CACHE.md`), and publishes a site only on one of three proofs
  printed on the site itself (organisation number, registered contact plus
  a word of the name, or the full legal name plus a register signal). Sites
  that print none of these, brands sharing no word with the legal name, and
  group or chain sites are missed or reported `ambiguous`. This is
  deliberate: the agent misses rather than guesses.
- **Ratings, reviews and sentiment** are not collected. No permitted source
  covers them at this scale without an account or licence.
- **Social platform content** (follower counts, posts) is not collected; only
  company-owned profile links are.
- **Job ads** come from NAV's vacancy feed, from the verified site's careers
  page and from the applicant-tracking pages it links to. FINN-only ads and
  ads on boards the company site does not link to are missed; NAV's site
  search endpoint is not used because it rate-limits automated clients.
- **Financial history** is what the accounts API returns (up to three fiscal
  years). Older years are not parsed from the PDF copies.
- **Brreg announcements** (`w2.brreg.no/kunngjoring/`) are not fetched
  because robots.txt disallows them; dated register activity comes from the
  roles register and the official update feed instead.
- **JavaScript-only sites** are read as served; there is no headless browser.
  If the organisation number appears only after rendering, the site is not
  verified.
