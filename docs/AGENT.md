# Research and abstention policy

## Identity first

The organisation number is the only identity key. A legal name, brand, domain
or social handle is a candidate until it is tied back to the exact entity.

| candidate | accepted when | otherwise |
|---|---|---|
| register-listed website (entity or subunit) | it is the company's own declaration in the Entity Register, the site is reachable and not parked | `not_available` (unreachable or parked) |
| register-listed website showing other organisation numbers only (for example a housing co-operative's umbrella association) | **not published** as this entity's website; `official_website` is `ambiguous` with the other number in the reason, and the next candidate is tried | brand and profiles marked `ambiguous` |
| register-listed website that shows another group member's number and not this one | **not published** (a parent's or sister's site is not an exact match); `ambiguous` | the group's top parent is the exception: its own site lists its subsidiaries |
| employer homepage in an org-number-verified NAV job ad | treated like a register-listed site (the employer declared it) | same rules as above |
| domain of the e-mail address in the Entity Register (free-mail providers excluded) | only when the site shows this organisation number | skipped |
| domain derived from the legal or subunit name | the site (home, contact, about or privacy page) shows **this** organisation number, checked with the mod-11 checksum, and does not list three or more unrelated organisation numbers | `ambiguous` (several numbers) or skipped |
| NAV job ad | the feed's business name matches the legal or a subunit name (exactly, or as a word-boundary prefix), **and** the feed entry's own `employer.orgnr` is this entity's or one of its subunits' | not published |
| register-listed or NAV-listed site with no organisation number on it | the company name appears on the page or spells the domain | `ambiguous` (the domain may have changed hands) |
| social or video profile | linked from a verified company website (anchors or JSON-LD `sameAs`), listed as the website in the Entity Register, or entered by the employer in an org-number-verified NAV ad; share buttons, personal profiles and post links are rejected | `not_available` |

Directory and aggregator hosts (proff.no, gulesider.no, 1881.no, finn.no and
similar) are never accepted as a company website.

When the match is uncertain the agent abstains. A missing fact costs less
than a fact published under the wrong company.

## What is never done

- No fabricated or estimated values. Financial figures come only from the
  Register of Company Accounts, as filed.
- No zero in place of a missing value. Missing items are omitted, and the
  area carries an explicit availability state and reason.
- No search-engine result is used as evidence. No search engine is used.
- No scraping of LinkedIn, Meta, Glassdoor, Indeed or Google. Profiles on
  those platforms are recorded only as outbound links from the verified
  company site, or from a Wikidata statement on the item that carries this
  organisation number; the platforms themselves are not fetched.
- No language model decides identity, fills a field or writes the brief. The
  brief is a deterministic template over published claims.
- No personal data beyond names in the company-role context. Birth dates from
  the roles register are discarded.

## Availability states

| state | meaning |
|---|---|
| `available` | at least one evidence-backed claim is published |
| `not_available` | the source was checked and has nothing (the reason names what was checked) |
| `blocked` | the source refused the request or robots.txt disallows it |
| `not_applicable` | the question does not apply (e.g. deleted entity, input not an organisation number) |
| `ambiguous` | a candidate exists but could not be tied to this exact entity |
| `failed` | the source errored or the time budget ran out before the area was researched |

## Order of work

1. Official registers for every company (entity, roles, subunits, accounts,
   group, update feed). A complete output file is written right after.
2. Website discovery and verification (register, e-mail domain, name
   guesses, declared caches), then site facts from the verified site and its
   sitemap: brand, description, contacts, company-owned profiles, careers
   page and open roles (including linked applicant-tracking pages), and dated
   news (WordPress posts API, RSS/Atom, news sitemaps, listings, articles).
   Wikidata-listed profiles are published only after the live Wikidata item
   confirms this organisation number and the profile.
3. Job ads from NAV's vacancy feed (loaded once in the background), then the
   employer homepage named in a verified ad if no website was verified yet.
4. Synthesis, refresh diff, envelope, report and viewer.
