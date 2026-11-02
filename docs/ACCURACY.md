# How often each source type is right

Page-by-page check of run `r4c` on 2026-11-02: 40 companies, 150 published facts. Sheet: [`accuracy/sheet.csv`](accuracy/sheet.csv). Sites, jobs and news were opened by two independent AI review agents with no access to the agent's rules; social pages were read in a browser as a logged-out visitor by the session that built this revision. No person has re-checked them yet.

Companies were drawn at random, those with a job ad or a social page first, because both are rare. At most three facts of one type per company. So the shares describe published facts, not coverage: they say how often a fact the agent printed holds up, and nothing about what it missed.

| source type | checked | right | wrong | share right | 95% lower bound | could not open |
|---|---:|---:|---:|---:|---:|---:|
| Company website | 36 | 35 | 1 | 97% | 86% | 0 |
| Social profile | 45 | 44 | 1 | 98% | 88% | 14 |
| Dated news | 34 | 27 | 7 | 79% | 63% | 0 |
| Job ad | 21 | 19 | 2 | 90% | 71% | 0 |

The share right is right / (right + wrong). A page that could not be opened counts as neither. The lower bound is a Wilson 95% interval: on a sample this small it is the number to rely on.

## What each check asked

- **Company website:** Is this the company's own site, not a parent group's, a chain's or a namesake's?
- **Social profile:** Is this page the company itself, not a parent, a group, an agency or a namesake?
- **Dated news:** Is this a real post about the company, with this publication date?
- **Job ad:** Is this one open ad posted by this company, naming the company?

## What was wrong

- **LØVSTAD NATURBARNEHAGE SA** (887446032), news: Kom å besøk oss i Løvstad naturbarnehage (2024-12-03). The page is an evergreen invitation and sales pitch for the kindergarten, not a report of an event.
- **FINEARTPRINT AS** (920298230), news: Lyst til å få mer ut av mobilbildene dine i sommer? (2025-06-18T00:28:24+00:00). The page is a how-to guide on taking phone photos for printing, published 18 June 2025.
- **PROXO REGNSKAP AS** (924923180), news: Ta det med ro - Vi tar oss av fristene! Offentlige oppgaver og frister for næringsdrivende -mai 2023 (2023-05-04T23:58:44+02:00). Generic list of public tax deadlines for May 2023 that reports nothing the company did.
- **PROXO REGNSKAP AS** (924923180), news: April 2023: Oversikt over offentlige frister for næringsdrivende (2023-03-24T11:10:07+01:00). Generic overview of public deadlines for April 2023 that reports nothing the company did.
- **TERTNES ENTREPRENØR AS** (930572098), news: Lokal entreprenør med stolt historie og vekstambisjoner søker Prosjektleder/Driftsleder Grunn/VA! (2024-03-26T21:47:21+01:00). The post is a job advertisement for a project manager.
- **TERTNES ENTREPRENØR AS** (930572098), news: Kampanje for Tertnes Entrepenør og Vitek Miljø (2024-03-04T13:41:54+01:00). The post is a service promotion with phone numbers, and the page JSON-LD gives 28 February 2024 as published, not 4 March.
- **LÆRINGSVERKSTEDET JAR IDRETTSBARNEHAGE AS** (930867098), site: https://laringsverkstedet.no/barnehage/jar-idrettsbarnehage. Page is a sub-page of the Læringsverkstedet chain site, though the register lists it for this kindergarten.
- **BISKOPSHAVN LEGESENTER AS** (934145232), news: Influensa vaksinering 2026/27 (2026-06-03T07:24:27+00:00). The page is a flu vaccine price and information notice with generic health text, and its slug sommer-2026 shows it was repurposed.
- **ADDSECURE AS** (976145178), social: https://facebook.com/AddSecure. The page is the AddSecure group: "a leading European provider", addsecure.com, posts in French; not the Norwegian company.
- **ADDSECURE AS** (976145178), jobs: https://arbeidsplassen.nav.no/stillinger/stilling/c86d12e0-4a0a-4b07-a580-4153ff82cdc8. Posted by the recruiter Bravura Norge. NAV ties it to AddSecure's sub-unit 976448677 and the ad says you are hired directly by AddSecure, so the vacancy is real, but the company did not post it. The same ad appears twice.
- **ADDSECURE AS** (976145178), jobs: https://arbeidsplassen.nav.no/stillinger/stilling/ffa23d62-9c1f-4e63-9e6d-f4daebdd5ae5. Posted by the recruiter Bravura Norge. NAV ties it to AddSecure's sub-unit 976448677 and the ad says you are hired directly by AddSecure, so the vacancy is real, but the company did not post it. The same ad appears twice.

## Fixed after this check

Job ads and lists of public deadlines posted as news are now refused by title, a vacancy posted twice is one claim, and one Facebook page under two addresses is one claim. Not fixed: evergreen posts with no such mark in the title (Løvstad, Fineartprint, a campaign post, a repurposed vaccine notice), a chain's sub-page listed in the register as a kindergarten's own site, and a foreign group's page linked from a Norwegian subsidiary's site (AddSecure), which the page's name cannot show.

These fixes are not in the table: it reports the run as it was checked.
