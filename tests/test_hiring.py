"""Offline tests for hiring precision (audit of 23 Oct, rules R1-R7): no network.

"Check each job is a single ad posted by this company, not a careers page, a search-results link
or the parent's vacancies." Run with ``python -m unittest tests.test_hiring``.
"""
from __future__ import annotations

import hashlib
import json
import time
import unittest
from pathlib import Path
from datetime import date, timedelta
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from kildespor import harvest, jobs
from kildespor.contract import internal, publish
from kildespor.envelope import build_envelope
from kildespor.http import Snapshot
from kildespor.model import FAMILIES, CompanyRecord
from kildespor.refresh import diff
from kildespor.synthesis import build_synthesis
from kildespor.website import SiteVisit, parse

TODAY = date.today()
FUTURE = (TODAY + timedelta(days=30)).strftime("%d.%m.%Y")
PAST = (TODAY - timedelta(days=5)).strftime("%d.%m.%Y")
LONG_AGO = (TODAY - timedelta(days=130)).strftime("%d.%m.%Y")
NAV_FEED = "https://pam-stilling-feed.nav.no/api/v1/feed (last 45 days, 3 pages, 100 active ads)"


def snap(url: str, body: str, status: int = 200, final_url: str | None = None, ctype: str = "text/html; charset=utf-8") -> Snapshot:
    data = body.encode("utf-8")
    return Snapshot(requested_url=url, final_url=final_url or url, status=status, retrieved_at="2026-10-23T00:00:00Z", sha256=hashlib.sha256(data).hexdigest(), content_type=ctype, body=data, redirect_chain=[url] if final_url and final_url != url else [])


def record(name: str = "FJORDBYGG AS", org: str = "923609016") -> CompanyRecord:
    rec = CompanyRecord(org)
    rec.context["entity"] = {"navn": name}
    return rec


class Fetcher:
    """Fixture pages by URL: (body, status, final URL after redirects). Unknown URLs are 404."""

    def __init__(self, pages: dict[str, tuple[str, int, str | None] | str]) -> None:
        self.pages = {k: (v, 200, None) if isinstance(v, str) else v for k, v in pages.items()}
        self.count = 0
        self.cap: int | None = None
        self.cap_start = 0
        self.requested: list[str] = []
        parser = RobotFileParser()
        parser.parse(["User-agent: *", "Allow: /"])

    def thread_requests(self) -> int:
        return self.count

    def begin_company(self, cap: int | None) -> None:
        self.cap, self.cap_start = cap, self.count

    def raise_company_cap(self, extra: int) -> None:
        if self.cap is not None:
            self.cap = max(self.cap, self.count - self.cap_start + extra)

    def get(self, url: str, **_kwargs) -> Snapshot:
        if self.cap is not None and self.count - self.cap_start >= self.cap:
            raise RuntimeError("per-company request cap reached")
        self.count += 1
        self.requested.append(url)
        body, status, final = self.pages.get(url, ("not found", 404, None))
        return snap(url, body, status, final)


def harvest_site(rec: CompanyRecord, home_url: str, home_html: str, pages: dict, relationship: str | None = None) -> Fetcher:
    fetcher = Fetcher(pages)
    visit = SiteVisit(fetcher, rec, time.monotonic() + 60)  # type: ignore[arg-type]
    home = snap(home_url, home_html)
    soup, _html, text = parse(home)
    visit.pages.append((home, soup, text))
    harvest._extract_site_facts(rec, visit, home, True, relationship)
    return fetcher


def read(url: str, html: str, names: list[str], final_url: str | None = None) -> harvest.AdPage:
    page = snap(url, html, final_url=final_url)
    soup, _html, text = parse(page)
    return harvest.read_ad_page(page, soup, text, names)


def ad_html(title: str, about: str | None = None, extra: str = "", footer: str = "") -> str:
    about_block = f"<h2>Om {about}</h2><p>Vi bygger Norge.</p>" if about else ""
    return f"""<html><head><title>{title}</title></head><body><header><a href="/">Hjem</a></header><main><h1>{title}</h1>
    <p>Vi søker en dyktig medarbeider. Søknadsfrist: {FUTURE}</p>{extra}{about_block}</main><footer>{footer}</footer></body></html>"""


class CareersPageIsNeverAClaim(unittest.TestCase):
    """R1: a careers page, ATS landing, list or login page or recruiter page is never a hiring claim."""

    HOME = '<html><head><title>Total VVS</title></head><body><nav><a href="/ledige-stillinger/">Ledige stillinger</a></nav><footer>Org.nr 923 609 016</footer></body></html>'

    def test_careers_page_without_ads_is_context_not_a_claim(self) -> None:
        careers = "<html><head><title>Ledige stillinger | Total VVS</title></head><body><h1>Ledige stillinger</h1><p>Vi har mange spennende prosjekter.</p></body></html>"
        rec = record("TOTAL VVS AS")
        harvest_site(rec, "https://total-vvs.no/", self.HOME, {"https://total-vvs.no/ledige-stillinger/": careers})
        self.assertFalse(rec.claims_for("hiring"))
        self.assertEqual(rec.context["career_pages"], ["https://total-vvs.no/ledige-stillinger/"])
        # the list was read in full and is empty, but NAV has not answered yet: absence not established
        self.assertEqual(rec.family_state["hiring"]["state"], "failed")
        jobs.record_nav_hiring(rec, "not_available", "no active ad in NAV's vacancy feed names this organisation number", [NAV_FEED])
        state = rec.family_state["hiring"]
        self.assertEqual(state["state"], "not_available")
        self.assertIn("careers page https://total-vvs.no/ledige-stillinger/ lists no open ad", state["reason"])
        self.assertIn("no active ad in NAV", state["reason"])
        self.assertEqual(set(state["checked_sources"]), {"https://total-vvs.no/ledige-stillinger/", NAV_FEED})

    def test_no_openings_text_sets_not_available_explicitly(self) -> None:
        """R7: "Ingen ledige stillinger på dette tidspunktet" is the company's own word."""
        careers = "<html><head><title>Ledige stillinger</title></head><body><h1>Ledige stillinger</h1><p>Ingen ledige stillinger på dette tidspunktet.</p></body></html>"
        rec = record("TOTAL VVS AS")
        harvest_site(rec, "https://total-vvs.no/", self.HOME, {"https://total-vvs.no/ledige-stillinger/": careers})
        self.assertEqual(rec.family_state["hiring"]["state"], "not_available")
        self.assertIn("says there are no open positions", rec.family_state["hiring"]["reason"])
        jobs.record_nav_hiring(rec, "failed", "NAV vacancy feed unavailable")
        self.assertEqual(rec.family_state["hiring"]["state"], "not_available")  # NAV's failure does not undo it

    def test_publish_drops_careers_pages_and_titleless_jobs(self) -> None:
        env = {
            "organisation_number": "923609016",
            "sections": {"hiring_and_activity": {"families": {"hiring": {"state": "not_available", "reason": "careers page https://x.no/jobb lists no open ad", "checked_sources": ["https://x.no/jobb"]}}}},
            "claims": [
                {"id": "cl-1", "key": "careers_page:x", "family": "hiring", "field": "careers_page", "value": "https://x.no/jobb", "availability": "available", "evidence_ids": ["ev-1"]},
                {"id": "cl-2", "key": "job_posting:y", "family": "hiring", "field": "job_posting", "value": {"url": "https://x.no/jobb/123456"}, "availability": "available", "evidence_ids": ["ev-1"]},
            ],
            "evidence": [{"id": "ev-1", "source_url": "https://x.no/jobb", "content_sha256": "a" * 64, "retrieved_at": "t"}],
        }
        out = publish(env)
        hiring = [c for c in out["claims"] if c["field"] in ("hiring_signal", "careers_page", "job_posting")]
        self.assertEqual([(c["field"], c["availability"]) for c in hiring], [("hiring_signal", "not_available")])
        self.assertEqual(hiring[0]["checked_sources"], ["https://x.no/jobb"])
        self.assertFalse(any("signal_type" in c for c in out["claims"]))


class AvailabilityStates(unittest.TestCase):
    """Only a careers page found: never available; not_available, blocked or failed by what was read."""

    def test_settle_rules(self) -> None:
        rec = record()
        jobs.record_site_hiring(rec, "not_available", "careers page https://x.no/jobb lists no open ad", ["https://x.no/jobb"])
        jobs.record_nav_hiring(rec, "not_available", "no NAV ad", [NAV_FEED])
        self.assertEqual(rec.family_state["hiring"]["state"], "not_available")
        rec = record()
        jobs.record_nav_hiring(rec, "not_available", "no NAV ad", [NAV_FEED])
        jobs.record_site_hiring(rec, "blocked", "the ad list sits behind a login", ["https://x.recman.no/"])
        self.assertEqual(rec.family_state["hiring"]["state"], "blocked")  # the order does not matter
        rec = record()
        jobs.record_site_hiring(rec, "not_available", "careers page lists no open ad", ["https://x.no/jobb"])
        jobs.record_nav_hiring(rec, "failed", "NAV vacancy feed unavailable")
        self.assertEqual(rec.family_state["hiring"]["state"], "failed")
        rec = record()
        jobs.record_nav_hiring(rec, "not_available", "no NAV ad", [NAV_FEED])
        self.assertEqual(rec.family_state["hiring"]["state"], "not_available")  # no careers page: NAV alone

    def test_blocked_careers_page(self) -> None:
        home = '<html><body><nav><a href="/karriere/">Karriere</a></nav></body></html>'
        rec = record()
        harvest_site(rec, "https://www.fjordbygg.no/", home, {"https://www.fjordbygg.no/karriere/": ("Forbidden", 403, None)})
        self.assertFalse(rec.claims_for("hiring"))
        self.assertEqual(rec.family_state["hiring"]["state"], "blocked")

    def test_ats_list_redirecting_to_a_login_is_blocked(self) -> None:
        """Flexibemanning: recman.no/index.php redirected to recman.io/login."""
        home = '<html><body><nav><a href="https://flexibemanning.recman.no/index.php">Ledige stillinger</a></nav></body></html>'
        login = "<html><head><title>Logg inn - Recman</title></head><body><h1>Logg inn</h1><input type='password'></body></html>"
        rec = record("FLEXIBEMANNING AS", "915432687")
        harvest_site(rec, "https://flexibemanning.no/", home, {"https://flexibemanning.recman.no/index.php": (login, 200, "https://app.recman.io/login")})
        self.assertFalse(rec.claims_for("hiring"))
        self.assertEqual(rec.family_state["hiring"]["state"], "blocked")

    def test_ad_pages_refusing_access_are_blocked_not_published(self) -> None:
        """Haugesund Sparebank's ad pages answered 403: an unread ad is never published."""
        home = '<html><body><nav><a href="/karriere/">Karriere</a></nav></body></html>'
        careers = """<html><head><title>Karriere</title></head><body><h1>Ledige stillinger</h1>
        <a href="/karriere/stilling/12345-radgiver">Rådgiver bedrift</a><a href="/karriere/stilling/12346-kunderadgiver">Kunderådgiver</a></body></html>"""
        rec = record("HAUGESUND SPAREBANK", "937894538")
        pages = {
            "https://www.hsb.no/karriere/": careers,
            "https://www.hsb.no/karriere/stilling/12345-radgiver": ("Forbidden", 403, None),
            "https://www.hsb.no/karriere/stilling/12346-kunderadgiver": ("Forbidden", 403, None),
        }
        harvest_site(rec, "https://www.hsb.no/", home, pages)
        self.assertFalse(rec.claims_for("hiring"))
        self.assertEqual(rec.family_state["hiring"]["state"], "blocked")
        self.assertNotIn("job_posting:site", rec.checked_fields)  # unread ads can be neither published nor closed

    def test_unread_ads_past_the_budget_are_not_published(self) -> None:
        home = '<html><body><nav><a href="/karriere/">Karriere</a></nav></body></html>'
        listed = harvest.MAX_AD_FETCHES + 4
        links = "".join(f'<a href="/karriere/stilling/{100000 + i}-rolle">Rolle nummer {i}</a>' for i in range(listed))
        careers = f"<html><head><title>Karriere</title></head><body><h1>Ledige stillinger</h1>{links}</body></html>"
        pages = {"https://www.fjordbygg.no/karriere/": careers}
        for i in range(listed):
            pages[f"https://www.fjordbygg.no/karriere/stilling/{100000 + i}-rolle"] = ad_html(f"Rolle nummer {i}", "Fjordbygg")
        rec = record()
        fetcher = harvest_site(rec, "https://www.fjordbygg.no/", home, pages)
        ads_read = [u for u in fetcher.requested if "/stilling/" in u]
        self.assertEqual(len(ads_read), harvest.MAX_AD_FETCHES)
        self.assertEqual(len(rec.claims_for("hiring")), harvest.MAX_AD_FETCHES)
        self.assertNotIn("job_posting:site", rec.checked_fields)


class AdPageRefusals(unittest.TestCase):
    """R2: a role is published only after its own ad page is read and shows one open vacancy."""

    NAMES = ["FJORDBYGG AS"]

    def test_open_ad_passes(self) -> None:
        ad = read("https://www.fjordbygg.no/karriere/stilling/1-tomrer", ad_html("Tømrer", "Fjordbygg AS"), self.NAMES)
        self.assertIsNone(ad.refused)
        self.assertEqual(ad.employers, ["Fjordbygg AS"])

    def test_filled_expired_and_closed(self) -> None:
        url = "https://www.fjordbygg.no/karriere/stilling/1-tomrer"
        self.assertIn("filled", read(url, ad_html("Elektromontør Vats", extra="<p>Stillingen er nå besatt.</p>"), self.NAMES).refused)
        self.assertIn("filled", read(url, ad_html("Snekker", extra="<p>Fristen er utløpt</p>"), self.NAMES).refused)
        self.assertIn("filled", read(url, ad_html("Snekker", extra="<p>Annonsen er utløpt</p>"), self.NAMES).refused)
        self.assertIn("filled", read(url, ad_html("Developer", extra="<p>This job is no longer accepting applications</p>"), self.NAMES).refused)
        badge = read("https://www.finn.no/job/ad/466429350", ad_html("Montør", extra="<span class='badge'>Besatt</span>"), ["NORDIC DOOR AS"])
        self.assertIn("Besatt", badge.refused)
        past = f"<html><body><h1>Snekker</h1><p>Søknadsfrist: {PAST}</p></body></html>"
        self.assertIn("deadline", read(url, past, self.NAMES).refused)
        ld = f'<html><head><script type="application/ld+json">{{"@type":"JobPosting","title":"Snekker","validThrough":"2020-01-01"}}</script></head><body><h1>Snekker</h1></body></html>'
        self.assertIn("validThrough", read(url, ld, self.NAMES).refused)

    def test_finn_is_never_fetched(self) -> None:
        # finn.no's terms forbid automated access; the fetcher refuses it before any request.
        from kildespor.http import Fetcher, TermsForbidden

        with self.assertRaises(TermsForbidden):
            Fetcher().get("https://www.finn.no/job/ad/460758926")
        self.assertIsNone(harvest.ats_of("https://www.finn.no/job/ad/460758926"))

    def test_open_applications_are_not_vacancies(self) -> None:
        """Leirvik's webcruiter "Open søknad innan produksjon", published by v1 as "Operatørar"."""
        url = "https://candidate.webcruiter.com/nb-no/Home/job?advertId=4566579776"
        for title in ("Open søknad innan produksjon", "Open søknadsportal for Ingeniørar og Økonomar", "Åpen søknad", "Talentbank", "Registrer CV"):
            self.assertIn("open application", read(url, ad_html(title), ["LEIRVIK AS"]).refused, title)

    def test_error_and_login_pages(self) -> None:
        url = "https://www.fjordbygg.no/karriere/stilling/1-tomrer"
        self.assertEqual(read(url, "<html><head><title>404 - Siden finnes ikke</title></head><body><h1>Siden finnes ikke</h1></body></html>", self.NAMES).refused, "error page")
        self.assertIn("login", read(url, "<html><head><title>Logg inn</title></head><body><h1>Logg inn</h1></body></html>", self.NAMES).refused)


class SearchAndLoginUrls(unittest.TestCase):
    """R5: search and landing URLs are never publishable, also after redirects."""

    def test_ad_urls(self) -> None:
        ok = harvest.ad_url_ok
        self.assertTrue(ok("https://www.finn.no/job/ad/466429350"))
        self.assertTrue(ok("https://www.finn.no/job/fulltime/ad.html?finnkode=466429350"))
        self.assertFalse(ok("https://www.finn.no/job/search?orgId=123"))
        self.assertFalse(ok("https://www.finn.no/job/fulltime/search.html?q=nordic+door"))
        self.assertTrue(ok("https://arbeidsplassen.nav.no/stillinger/stilling/7f4b1c2e-1a2b-4c3d-9e8f-001122334455"))
        self.assertFalse(ok("https://arbeidsplassen.nav.no/stillinger?q=leirvik"))
        self.assertFalse(ok("https://flexibemanning.recman.no/index.php"))
        self.assertFalse(ok("https://app.recman.io/login"))
        self.assertFalse(ok("https://web103.reachmee.com/ext/I003/1009/main?site=7&lang=NO"))
        self.assertFalse(ok("https://web103.reachmee.com/ext/I003/1009/profile?site=7"))
        self.assertFalse(ok("https://www.fjordbygg.no/sok?q=stilling"))
        self.assertTrue(ok("https://www.fjordbygg.no/karriere/stilling/123456-tomrer"))

    def test_redirect_to_login_is_refused(self) -> None:
        login = "<html><head><title>Recman</title></head><body><h1>Velkommen</h1></body></html>"
        ad = read("https://flexibemanning.recman.no/job.php?job_id=123456", login, ["FLEXIBEMANNING AS"], final_url="https://app.recman.io/login")
        self.assertIn("not an ad URL after redirects", ad.refused)

    def test_search_link_on_a_careers_page_is_never_read_or_published(self) -> None:
        home = '<html><body><nav><a href="/karriere/">Karriere</a></nav></body></html>'
        careers = """<html><head><title>Karriere</title></head><body><h1>Ledige stillinger</h1>
        <a href="https://www.finn.no/job/search?orgId=1234567">Se alle våre stillinger på FINN</a>
        <a href="https://www.finn.no/job/fulltime/search.html?finnkode=123456">Montør</a></body></html>"""
        rec = record("NORDIC DOOR AS", "966820152")
        fetcher = harvest_site(rec, "https://www.nordicdoor.no/", home, {"https://www.nordicdoor.no/karriere/": careers})
        self.assertFalse(rec.claims_for("hiring"))
        self.assertFalse(any("finn.no" in u for u in fetcher.requested))


class EmployerAttribution(unittest.TestCase):
    """R3/R4: an ad belongs to this orgnr only if the ad names it, a subunit, or its legal or brand name."""

    LISTING = """<html><head><title>Ledige stillinger | AF Gruppen</title></head><body><h1>Ledige stillinger</h1>
    <a href="/karriere/ledige-stillinger/2026/10/kalkulator/">Kalkulatør</a>
    <a href="/karriere/ledige-stillinger/2026/10/hms-leder/">HMS-leder konsern</a>
    <a href="/karriere/ledige-stillinger/2026/10/elektriker/">Elektriker</a>
    <a href="/karriere/ledige-stillinger/2026/10/prosjektleder/">Prosjektleder</a>
    <a href="/karriere/ledige-stillinger/2026/10/ingen-arbeidsgiver/">Anleggsleder</a></body></html>"""
    FOOTER = "AF Gruppen ASA · Org.nr. 938 702 675 · <h2>Om AF Gruppen</h2>"
    BASE = "https://www.afgruppen.no/karriere/ledige-stillinger/2026/10/"

    def pages(self) -> dict:
        return {
            "https://www.afgruppen.no/karriere/ledige-stillinger/": self.LISTING,
            self.BASE + "kalkulator/": ad_html("AF Anlegg søker kalkulatør", "AF Anlegg", footer=self.FOOTER),
            self.BASE + "hms-leder/": ad_html("HMS-leder konsern", "AF Gruppen", footer=self.FOOTER),
            self.BASE + "elektriker/": ad_html("Elektriker", "AF Elkraft AS", footer=self.FOOTER),
            self.BASE + "prosjektleder/": ad_html("Prosjektleder", "Aeron AS", footer=self.FOOTER),
            self.BASE + "ingen-arbeidsgiver/": ad_html("Anleggsleder", None, footer=self.FOOTER),
        }

    HOME = '<html><head><title>AF Gruppen</title></head><body><nav><a href="/karriere/ledige-stillinger/">Ledige stillinger</a></nav></body></html>'

    def test_group_list_keeps_only_the_parents_own_ads(self) -> None:
        rec = record("AF GRUPPEN ASA", "938702675")
        harvest_site(rec, "https://www.afgruppen.no/", self.HOME, self.pages())
        titles = [c["value"]["title"] for c in rec.claims_for("hiring")]
        self.assertEqual(titles, ["HMS-leder konsern"])  # units, subsidiaries and the unnamed ad on a group list are refused
        self.assertEqual(rec.family_state["hiring"]["state"], "available")

    def test_subsidiary_never_takes_ads_from_the_group_site(self) -> None:
        rec = record("AF ELKRAFT AS", "916036930")
        rec.context["group_root"] = "938702675"
        fetcher = harvest_site(rec, "https://www.afgruppen.no/", self.HOME, self.pages(), relationship="group_site")
        self.assertFalse(rec.claims_for("hiring"))
        self.assertFalse(any("/2026/10/" in u for u in fetcher.requested))  # not even read
        self.assertIn("group site", rec.family_state["hiring"]["reason"])

    def test_own_site_ad_naming_no_employer_is_accepted(self) -> None:
        home = '<html><body><nav><a href="/karriere/">Karriere</a></nav><footer>Org.nr 923 609 016</footer></body></html>'
        careers = """<html><head><title>Karriere</title></head><body><h1>Ledige stillinger</h1>
        <a href="/karriere/stilling/1-tomrer">Tømrer</a><a href="/karriere/stilling/2-maler">Maler</a></body></html>"""
        pages = {
            "https://www.fjordbygg.no/karriere/": careers,
            "https://www.fjordbygg.no/karriere/stilling/1-tomrer": ad_html("Tømrer", footer="Org.nr 923 609 016"),
            "https://www.fjordbygg.no/karriere/stilling/2-maler": ad_html("Maler", "Fjordbygg"),
        }
        rec = record()
        harvest_site(rec, "https://www.fjordbygg.no/", home, pages)
        self.assertEqual(sorted(c["value"]["title"] for c in rec.claims_for("hiring")), ["Maler", "Tømrer"])
        self.assertIn("job_posting:site", rec.checked_fields)

    def test_orgnr_and_employer_fields(self) -> None:
        rec = record("NORDIC DOOR AS", "966820152")
        rec.context["subunits"] = [{"name": "NORDIC DOOR AS AVD GJØVIK", "organisation_number": "971727470"}]
        ours = read("https://www.finn.no/job/ad/1", ad_html("Montør", extra="<p>Org.nr. 966 820 152</p>"), ["NORDIC DOOR AS"])
        self.assertEqual(ours.numbers, {"966820152"})
        self.assertTrue(harvest.ad_employer_is_ours(ours, rec))
        subunit = read("https://www.finn.no/job/ad/2", ad_html("Montør", extra="<p>Organisasjonsnummer: 971727470</p>"), ["NORDIC DOOR AS"])
        self.assertTrue(harvest.ad_employer_is_ours(subunit, rec))
        other = read("https://www.finn.no/job/ad/3", ad_html("Montør", extra="<p>Org.nr. 938 702 675</p>"), ["NORDIC DOOR AS"])
        self.assertFalse(harvest.ad_employer_is_ours(other, rec))
        field = read("https://candidate.webcruiter.com/job?advertId=1", ad_html("Montør", extra="<dl><dt>Arbeidsgiver</dt><dd>Annet Firma AS</dd></dl>"), ["NORDIC DOOR AS"])
        self.assertEqual(field.employers, ["Annet Firma AS"])
        self.assertFalse(harvest.ad_employer_is_ours(field, rec))
        named = read("https://candidate.webcruiter.com/job?advertId=2", ad_html("Montør", extra="<p>Arbeidsgiver: Nordic Door AS</p>"), ["NORDIC DOOR AS"])
        self.assertTrue(harvest.ad_employer_is_ours(named, rec))
        nothing = read("https://www.finn.no/job/ad/4", ad_html("Montør", footer="Org.nr. 938 702 675"), ["NORDIC DOOR AS"])
        self.assertIsNone(harvest.ad_employer_is_ours(nothing, rec))  # a footer's number is the site owner's
        place = read("https://www.nordicdoor.no/jobb/stilling/5", ad_html("Montør", "Gjøvik"), ["NORDIC DOOR AS"])
        self.assertEqual(place.employers, [])  # "Om Gjøvik" is a town, not an employer


def nav_job(rec: CompanyRecord, title: str, uuid: str, ev: str) -> None:
    rec.add_claim(family="hiring", field_name="job_posting", value={"title": title, "url": f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}", "employer_organisation_number": "971727470", "board": "arbeidsplassen.nav.no"},
                  key=["nav", uuid], evidence_ids=[ev], method="nav_feed_entry_employer_orgnr")


def site_job(rec: CompanyRecord, title: str, url: str, ev: str, method: str = "ad_page_read") -> None:
    rec.add_claim(family="hiring", field_name="job_posting", value={"title": title, "url": url, "source": "webcruiter"}, key=["job", url], evidence_ids=[ev], method=method)


def leirvik() -> tuple[CompanyRecord, str]:
    rec = CompanyRecord("948158263")
    ev = rec.add_evidence(source_url="https://data.brreg.no/enhetsregisteret/api/enheter/948158263", source_class="official_registry", retrieved_at="2026-10-23T00:00:00Z", content_sha256="a" * 64, claim_span='navn = "LEIRVIK AS"', extraction_method="t")
    rec.add_claim(family="legal_identity", field_name="legal_name", value="LEIRVIK AS", evidence_ids=[ev], method="t")
    return rec, ev


def envelope_for(rec: CompanyRecord, previous: dict | None, run_id: str) -> dict:
    changes, meta = diff(rec, previous, run_id)
    return build_envelope(rec, run={"run_id": run_id, "completed_at": "2026-10-23T00:00:00Z"}, synthesis=build_synthesis(rec, changes, baseline=bool(meta.get("baseline"))), changes=changes, refresh_meta=meta)


class ParentVacanciesAbroad(unittest.TestCase):
    """Organiser's point 4, "not the parent's vacancies": an ad page that names no employer counts
    only for a workplace in Norway. pnoinnovation.com, verified for 954803694, lists the PNO
    group's vacancies (Lyon, a German Werkstudent, EU projects) and names no employer on them.
    Fixtures: the live ad pages of 23 Oct 2026, trimmed to their main text."""

    FIX = Path(__file__).resolve().parent / "fixtures" / "ats"
    HOME = '<html><head><title>PNO Innovation</title></head><body><nav><a href="/careers/">Careers</a></nav><footer>PNO Innovation AS, Oslo</footer></body></html>'

    def site(self, ads: dict[str, str]) -> CompanyRecord:
        links = "".join(f'<a href="{url}">{url.rsplit("/", 2)[-2]}</a>' for url in ads)
        careers = f"<html><head><title>Careers | PNO</title></head><body><h1>Careers</h1>{links}</body></html>"
        rec = record("PNO INNOVATION AS", "954803694")
        rec.context["entity"]["forretningsadresse"] = {"kommune": "OSLO", "poststed": "OSLO"}
        harvest_site(rec, "https://www.pnoinnovation.com/", self.HOME, {"https://www.pnoinnovation.com/careers/": careers, **ads})
        return rec

    def test_group_vacancies_abroad_are_refused(self) -> None:
        rec = self.site({
            "https://www.pnoinnovation.com/position/werkstudent-m-w-d/": (self.FIX / "pno-werkstudent-m-w-d.html").read_text(encoding="utf-8"),
            "https://www.pnoinnovation.com/position/product-owner/": (self.FIX / "pno-product-owner.html").read_text(encoding="utf-8"),
        })
        self.assertEqual(rec.claims_for("hiring"), [])
        self.assertEqual(rec.context["harvest"]["ads_refused"], 2)

    @staticmethod
    def ad(title: str, text: str, ld: str = "") -> str:
        return f"<html><head><title>{title}</title>{ld}</head><body><main><h1>{title}</h1><p>{text}</p></main></body></html>"

    def test_norwegian_workplace_signals(self) -> None:
        base = "https://www.pnoinnovation.com/position/"
        rec = self.site({
            base + "a-oslo/": self.ad("Innovation Consultant", "We are looking for a consultant to join our team. Location: Oslo"),
            base + "b-nowhere/": self.ad("Senior Consultant", "We are looking for a senior consultant to join our growing team of experts."),
            base + "c-norsk/": self.ad("Rådgiver", "Vi søker en rådgiver som har erfaring med søknader og vil jobbe med oss."),
            base + "d-ld-no/": self.ad("Analyst", "Join us.", '<script type="application/ld+json">{"@type":"JobPosting","title":"Analyst","jobLocation":{"@type":"Place","address":{"addressCountry":"NO","addressLocality":"Oslo"}}}</script>'),
            base + "e-ld-de/": self.ad("Werkstudent", "Werde Teil unseres Teams.", '<script type="application/ld+json">{"@type":"JobPosting","title":"Werkstudent","jobLocation":{"@type":"Place","address":{"addressCountry":"DE","addressLocality":"Hamburg"}}}</script>'),
            base + "f-lyon/": self.ad("Consultant R&D Tax Credit (Lyon)", "Rejoignez notre équipe à Lyon, France."),
        })
        # kept: a location field in Oslo, a Norwegian-language ad, a JobPosting placed in Norway;
        # refused: no place at all, a JobPosting in Hamburg, an ad in Lyon
        kept = sorted(c["value"]["url"].rsplit("/", 2)[-2] for c in rec.claims_for("hiring"))
        self.assertEqual(kept, ["a-oslo", "c-norsk", "d-ld-no"])

    def test_common_word_town_names_count_only_in_a_location_field(self) -> None:
        base = "https://www.pnoinnovation.com/position/"
        rec = self.site({
            base + "a-fulltime/": self.ad("Consultant", "A full time role for an experienced consultant who wants to grow with us."),
            base + "b-ski/": self.ad("Sales Engineer", "We are hiring a sales engineer. Benefits include a ski cabin and a long lunch time."),
            base + "c-field/": self.ad("Accountant", "We are hiring an accountant. Location: Ski"),
        })
        # "full time", "ski cabin", "lunch time" name no place; "Location: Ski" does
        self.assertEqual(sorted(c["value"]["url"].rsplit("/", 2)[-2] for c in rec.claims_for("hiring")), ["c-field"])
        self.assertFalse(harvest._names_norwegian_place("full-time position, flexible time", rec, field=False))
        self.assertTrue(harvest._names_norwegian_place("Time, Rogaland", rec))

    def test_ad_naming_this_employer_needs_no_place(self) -> None:
        named = self.ad("Consultant", "Join us.").replace("</main>", "<h2>Om PNO Innovation</h2><p>Advisers in Europe.</p></main>")
        rec = self.site({"https://www.pnoinnovation.com/position/g-named/": named, "https://www.pnoinnovation.com/position/h-named/": named.replace("Consultant", "Adviser")})
        self.assertEqual(len(rec.claims_for("hiring")), 2)


class DuplicateAds(unittest.TestCase):
    """R6: one vacancy on the company's ATS and in NAV's feed is one fact, kept as the NAV entry."""

    def test_nav_and_site_twins_merge(self) -> None:
        rec, ev = leirvik()
        site_ev = rec.add_evidence(source_url="https://candidate.webcruiter.com/job?advertId=4566000001", source_class="company_ats_page", retrieved_at="t", content_sha256="b" * 64, claim_span="ad page", extraction_method="ad_page_read")
        nav_job(rec, "Sveiser", "aaaaaaaa-0001", ev)
        site_job(rec, "Sveiser ", "https://candidate.webcruiter.com/job?advertId=4566000001", site_ev)
        site_job(rec, "Platearbeider", "https://candidate.webcruiter.com/job?advertId=4566000002", site_ev)
        jobs.merge_duplicate_ads(rec)
        found = {c["value"]["title"].strip(): c for c in rec.claims if c["field"] == "job_posting"}
        self.assertEqual(sorted(found), ["Platearbeider", "Sveiser"])
        nav = found["Sveiser"]
        self.assertEqual(nav["value"]["also_at"], ["https://candidate.webcruiter.com/job?advertId=4566000001"])
        self.assertIn(site_ev, nav["evidence_ids"])
        self.assertTrue(nav["value"]["url"].startswith("https://arbeidsplassen.nav.no/"))

    def test_merged_twin_is_not_a_closed_job(self) -> None:
        rec, ev = leirvik()
        nav_job(rec, "Sveiser", "aaaaaaaa-0001", ev)
        site_job(rec, "Sveiser", "https://candidate.webcruiter.com/job?advertId=4566000001", ev)
        rec.checked_fields |= {"job_posting", "job_posting:site"}
        rec.finalize_states()
        first = publish(envelope_for(rec, None, "r1"))  # an older run published both
        rec2, ev2 = leirvik()
        nav_job(rec2, "Sveiser", "aaaaaaaa-0001", ev2)
        site_job(rec2, "Sveiser", "https://candidate.webcruiter.com/job?advertId=4566000001", ev2)
        jobs.merge_duplicate_ads(rec2)
        rec2.checked_fields |= {"job_posting", "job_posting:site"}
        rec2.finalize_states()
        second = envelope_for(rec2, internal(json.loads(json.dumps(first))), "r2")
        self.assertNotIn("closed_job", {c["change_type"] for c in second["changes"]})


class RefreshFromV1Baseline(unittest.TestCase):
    """A v1 baseline published careers pages (and unread role links) as hiring_signal: reading it back
    drops them silently, neither carried forward as stale nor reported as closed jobs."""

    def v1(self) -> dict:
        return {
            "organisation_number": "948158263",
            "run": {"run_id": "rc4b", "completed_at": "2026-10-17T20:00:00Z"},
            "claims": [
                {"id": "cl-cp", "key": 'careers_page:"https://www.leirvik.no/karriere"', "family": "hiring", "field": "hiring_signal", "value": "https://www.leirvik.no/karriere",
                 "availability": "available", "evidence_ids": ["ev-1"], "extraction_method": "careers_page_on_verified_site", "internal_field": "careers_page", "signal_type": "careers_page"},
                {"id": "cl-ats", "key": 'careers_page:"https://candidate.webcruiter.com/x"', "family": "hiring", "field": "hiring_signal", "value": "https://candidate.webcruiter.com/x",
                 "availability": "available", "evidence_ids": ["ev-1"], "extraction_method": "ats_page_linked_from_verified_site", "signal_type": "careers_page"},
                {"id": "cl-role", "key": 'job_posting:["job","candidate.webcruiter.com/job"]', "family": "hiring", "field": "hiring_signal", "value": "https://candidate.webcruiter.com/job?advertId=4566579776",
                 "detail": {"title": "Operatørar", "url": "https://candidate.webcruiter.com/job?advertId=4566579776"}, "title": "Operatørar",
                 "availability": "available", "evidence_ids": ["ev-1"], "extraction_method": "careers_page_role_link", "internal_field": "job_posting", "signal_type": "job_posting"},
            ],
            "evidence": [{"id": "ev-1", "source_url": "https://www.leirvik.no/karriere", "content_sha256": "c" * 64, "retrieved_at": "2026-10-17T20:00:00Z"}],
        }

    def test_careers_pages_are_dropped_on_read_back(self) -> None:
        back = internal(json.loads(json.dumps(self.v1())))
        self.assertEqual([c["id"] for c in back["claims"]], ["cl-role"])

    def test_no_carry_and_no_closed_job(self) -> None:
        rec, _ev = leirvik()
        for family in FAMILIES:
            if not rec.claims_for(family):
                rec.set_state(family, "not_available", "checked, nothing found")
        rec.checked_fields |= {"job_posting", "job_posting:site"}
        rec.finalize_states()
        changes, _meta = diff(rec, internal(json.loads(json.dumps(self.v1()))), "r2")
        self.assertEqual([c for c in changes if c["field"] in ("job_posting", "careers_page")], [])
        self.assertFalse(any(c.get("stale") for c in rec.claims))
        self.assertFalse(rec.claims_for("hiring"))

    def test_unread_role_is_not_carried_when_the_site_was_not_rechecked(self) -> None:
        rec, _ev = leirvik()
        rec.finalize_states()
        diff(rec, internal(json.loads(json.dumps(self.v1()))), "r2")
        self.assertFalse(rec.claims_for("hiring"))  # job_posting:site not checked, still never carried


if __name__ == "__main__":
    unittest.main()
