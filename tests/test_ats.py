"""Offline tests for the ATS connectors, the tenant index and Jobbnorge (no network).

Fixtures in ``tests/fixtures/ats/`` are live responses read on 23 Oct 2026, trimmed to a few
entries (descriptions cut). Run with ``python -m pytest -q tests/test_ats.py``.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from kildespor import ats, harvest, jobs, model
from kildespor.http import Snapshot
from kildespor.model import CompanyRecord
from kildespor.website import SiteVisit, parse

FIX = Path(__file__).resolve().parent / "fixtures" / "ats"


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def snap(url: str, body: str, status: int = 200, ctype: str = "application/json; charset=utf-8", final_url: str | None = None) -> Snapshot:
    data = body.encode("utf-8")
    return Snapshot(requested_url=url, final_url=final_url or url, status=status, retrieved_at="2026-10-23T00:00:00Z", sha256=hashlib.sha256(data).hexdigest(), content_type=ctype, body=data)


def record(name: str, org: str = "986885358", **entity: object) -> CompanyRecord:
    rec = CompanyRecord(org)
    rec.context["entity"] = {"navn": name, **entity}
    return rec


class Fetcher:
    """Fixture responses by URL: (body, content type). Unknown URLs are 404."""

    def __init__(self, pages: dict[str, tuple[str, str] | str]) -> None:
        self.pages = {k: (v, "text/html; charset=utf-8") if isinstance(v, str) else v for k, v in pages.items()}
        self.count = 0
        self.cap: int | None = None
        self.cap_start = 0
        self.requested: list[str] = []

    def thread_requests(self) -> int:
        return self.count

    def begin_company(self, cap: int | None) -> None:
        self.cap, self.cap_start = cap, self.count

    def raise_company_cap(self, extra: int) -> None:
        if self.cap is not None:
            self.cap = max(self.cap, self.count - self.cap_start + extra)

    def get(self, url: str, **_kwargs: object) -> Snapshot:
        self.count += 1
        self.requested.append(url)
        if url not in self.pages:
            return snap(url, "not found", 404, "text/html")
        body, ctype = self.pages[url]
        return snap(url, body, 200, ctype)


def harvest_site(rec: CompanyRecord, home_url: str, home_html: str, pages: dict) -> Fetcher:
    fetcher = Fetcher(pages)
    visit = SiteVisit(fetcher, rec, time.monotonic() + 60)  # type: ignore[arg-type]
    home = snap(home_url, home_html, ctype="text/html; charset=utf-8")
    soup, _html, text = parse(home)
    visit.pages.append((home, soup, text))
    harvest._extract_site_facts(rec, visit, home, True, None)
    return fetcher


def job_claims(rec: CompanyRecord) -> list[dict]:
    return [c for c in rec.claims if c["field"] == "job_posting"]


class Detection(unittest.TestCase):
    def test_tenant_urls(self) -> None:
        t = ats.tenant_from_url
        self.assertEqual(t("https://opplandelektro.teamtailor.com/jobs/7829376-prosjektleder").list_url, "https://opplandelektro.teamtailor.com/jobs.json")
        self.assertIsNone(t("https://www.teamtailor.com/en/"))  # the vendor's own site
        self.assertIsNone(t("https://app.teamtailor.com/login"))
        self.assertEqual(t("https://boards.greenhouse.io/embed/job_board/js?for=acme").list_url, "https://boards-api.greenhouse.io/v1/boards/acme/jobs")
        self.assertEqual(t("https://job-boards.eu.greenhouse.io/acme/jobs/123").list_url, "https://boards-api.eu.greenhouse.io/v1/boards/acme/jobs")
        self.assertEqual(t("https://jobs.lever.co/acme/1f2e").list_url, "https://api.lever.co/v0/postings/acme?mode=json")
        self.assertEqual(t("https://jobs.eu.lever.co/acme").list_url, "https://api.eu.lever.co/v0/postings/acme?mode=json")
        self.assertEqual(t("https://jobs.ashbyhq.com/Acme/abc").list_url, "https://api.ashbyhq.com/posting-api/job-board/Acme")
        self.assertEqual(t("https://acme.jobs.personio.de/job/123").list_url, "https://acme.jobs.personio.de/xml")
        self.assertEqual(t("https://acme.recruitee.com/o/tomrer").list_url, "https://acme.recruitee.com/api/offers/")
        self.assertEqual(t("https://apply.workable.com/acme/j/ABC123/").list_url, "https://apply.workable.com/api/v1/widget/accounts/acme")
        self.assertIsNone(t("https://apply.workable.com/j/ABC123"))  # names no account
        for skipped in ("https://candidate.webcruiter.com/nb-no/Ad/123", "https://www.finn.no/job/ad/1", "https://jobs.smartrecruiters.com/Acme/1", "https://acme.recman.no/job.php?job_id=1"):
            self.assertIsNone(t(skipped), skipped)

    def test_custom_domain_teamtailor_site(self) -> None:
        html = '<html><head><link rel="alternate" type="application/rss+xml" title="Jobs" href="https://karriere.dips.com/jobs.rss" /><link href="https://assets-aws.teamtailor-cdn.com/x.css"></head><body></body></html>'
        self.assertEqual(ats.tenant_for_page("https://karriere.dips.com/", html).list_url, "https://karriere.dips.com/jobs.json")
        # Teamtailor's images on a company page, but no feed of its own: not a career site
        self.assertIsNone(ats.tenant_for_page("https://www.dips.com/", '<img src="https://images.teamtailor-cdn.com/a.jpg">'))


class Parsers(unittest.TestCase):
    def test_teamtailor_feed(self) -> None:
        tl = ats.parse_list("teamtailor", "https://opplandelektro.teamtailor.com/jobs.json", fixture("teamtailor-opplandelektro.json"))
        self.assertIsNone(tl.error)
        self.assertEqual(tl.name, "Oppland Elektro")
        ad = tl.ads[0]
        self.assertEqual((ad.title, ad.employer, ad.location, ad.country), ("Prosjektleder og faglig ansvarlig bygg", "Oppland Elektro", "Østre Gausdal", "NO"))
        self.assertEqual(ad.url, "https://opplandelektro.teamtailor.com/jobs/7829376-prosjektleder-og-faglig-ansvarlig-bygg")
        self.assertEqual(ad.published, "2026-06-01T15:27:33+02:00")
        empty = ats.parse_list("teamtailor", "https://karriere.dips.com/jobs.json", fixture("teamtailor-dips-empty.json"))
        self.assertEqual((empty.error, empty.ads, empty.name), (None, [], "DIPS AS"))
        html = ats.parse_list("teamtailor", "https://jobs.zaptec.com/jobs.json", "<!DOCTYPE html><html></html>")
        self.assertIsNotNone(html.error)  # Homerun answers its own HTML: not a feed

    def test_other_systems(self) -> None:
        gh = ats.parse_list("greenhouse", "https://boards-api.greenhouse.io/v1/boards/gitlab/jobs", fixture("greenhouse-gitlab.json"))
        self.assertTrue(gh.ads and all(a.employer == "GitLab" and a.url.startswith("https://job-boards.greenhouse.io/gitlab/jobs/") for a in gh.ads))
        lv = ats.parse_list("lever", "https://api.lever.co/v0/postings/spotify?mode=json", fixture("lever-spotify.json"))
        self.assertTrue(lv.ads and all(a.employer is None and a.url.startswith("https://jobs.lever.co/spotify/") and a.published.endswith("Z") for a in lv.ads))
        ash = ats.parse_list("ashby", "https://api.ashbyhq.com/posting-api/job-board/ramp", fixture("ashby-ramp.json"))
        self.assertTrue(ash.ads and all(a.url.startswith("https://jobs.ashbyhq.com/ramp/") for a in ash.ads))
        pe = ats.parse_list("personio", "https://personio.jobs.personio.de/xml", fixture("personio-personio.xml"))
        self.assertEqual([(a.title, a.employer, a.url) for a in pe.ads], [("Staff Software Engineer, Data Platform", "Personio SE & Co. KG", "https://personio.jobs.personio.de/job/1834171")])
        rc = ats.parse_list("recruitee", "https://channable.recruitee.com/api/offers/", fixture("recruitee-channable.json"))
        self.assertTrue(rc.ads and all(a.employer == "Channable" for a in rc.ads))
        wk = ats.parse_list("workable", "https://apply.workable.com/api/v1/widget/accounts/huggingface", fixture("workable-huggingface.json"))
        self.assertTrue(wk.ads and all(a.employer == "Hugging Face" and a.url.startswith("https://apply.workable.com/j/") for a in wk.ads))

    def test_screening(self) -> None:
        body = json.dumps({"version": "https://jsonfeed.org/version/1.1", "title": "Fjordbygg", "items": [
            {"title": "Åpen søknad", "url": "https://fjordbygg.teamtailor.com/jobs/1-apen-soknad", "_jobposting": {"hiringOrganization": {"name": "Fjordbygg"}}},
            {"title": "Talent pool: ingeniører", "url": "https://fjordbygg.teamtailor.com/jobs/2-talent", "_jobposting": {"hiringOrganization": {"name": "Fjordbygg"}}},
            {"title": "Tømrer", "url": "https://fjordbygg.teamtailor.com/jobs/3-tomrer", "_jobposting": {"validThrough": "2020-01-01", "hiringOrganization": {"name": "Fjordbygg"}}},
            {"title": "Intern rekruttering", "url": "https://fjordbygg.teamtailor.com/jobs/internal/4-x", "_jobposting": {"hiringOrganization": {"name": "Fjordbygg"}}},
            {"title": "Prosjektleder", "url": "https://fjordbygg.teamtailor.com/jobs/5-prosjektleder", "_jobposting": {"hiringOrganization": {"name": "Fjordbygg"}}},
        ]})
        tl = ats.parse_list("teamtailor", "https://fjordbygg.teamtailor.com/jobs.json", body)
        self.assertEqual([a.title for a in tl.open_ads()], ["Prosjektleder"])
        self.assertEqual(len([a for a in tl.ads if a.refused]), 4)


class EmployerRule(unittest.TestCase):
    def test_single_employer_tenant(self) -> None:
        tl = ats.parse_list("teamtailor", "https://opplandelektro.teamtailor.com/jobs.json", fixture("teamtailor-opplandelektro.json"))
        kept, _ = ats.select(record("OPPLAND ELEKTRO AS"), tl, own_tenant=True)
        self.assertEqual(len(kept), 1)
        kept, refused = ats.select(record("OPPLAND ELEKTRO GJØVIK AS"), tl, own_tenant=False)
        self.assertEqual(kept, [])
        self.assertIn("another employer", next(iter(refused.values())))

    def test_group_tenant_keeps_only_this_entity(self) -> None:
        tl = ats.parse_list("teamtailor", "https://career.kitron.com/jobs.json", fixture("teamtailor-kitron-group.json"))
        # KITRON ASA: every ad names a unit ("Kitron Lithuania", "Kitron Global"), none the ASA
        kept, refused = ats.select(record("KITRON ASA", "976605713"), tl, own_tenant=True)
        self.assertEqual(kept, [])
        self.assertTrue(all("another employer" in r for r in refused.values()))
        kept, _ = ats.select(record("KITRON SWEDEN AS", "000000000"), tl, own_tenant=False)
        self.assertEqual({a.employer for a in kept}, {"Kitron Sweden"})

    def test_list_naming_no_employer(self) -> None:
        tl = ats.parse_list("lever", "https://api.lever.co/v0/postings/spotify?mode=json", fixture("lever-spotify.json"))
        kept, _ = ats.select(record("SPOTIFY NORWAY AS"), tl, own_tenant=True)
        self.assertEqual(len(kept), len(tl.ads))
        kept, refused = ats.select(record("SPOTIFY NORWAY AS"), tl, own_tenant=False)
        self.assertEqual(kept, [])
        self.assertTrue(all("names no employer" in r for r in refused.values()))

    def test_exact_names_only(self) -> None:
        body = json.dumps({"version": "https://jsonfeed.org/version/1.1", "title": "Leirvik", "items": [
            {"title": "Sveiser", "url": "https://leirvik.teamtailor.com/jobs/9-sveiser", "_jobposting": {"hiringOrganization": {"name": "Leirvik"}}}]})
        tl = ats.parse_list("teamtailor", "https://leirvik.teamtailor.com/jobs.json", body)
        # "Leirvik" is LEIRVIK AS's name, not LEIRVIK SVEIS AS's: never accepted for the latter
        self.assertEqual(ats.select(record("LEIRVIK SVEIS AS"), tl, own_tenant=True)[0], [])
        self.assertEqual(len(ats.select(record("LEIRVIK AS"), tl, own_tenant=False)[0]), 1)
        # ... unless the verified website uses it as its brand
        rec = record("LEIRVIK SVEIS AS")
        rec.context["site_brand_names"] = ["Leirvik"]
        self.assertEqual(len(ats.select(rec, tl, own_tenant=True)[0]), 1)

    def test_name_shared_inside_a_group_is_refused(self) -> None:
        body = json.dumps({"version": "https://jsonfeed.org/version/1.1", "title": "Kitron", "items": [
            {"title": "Operatør", "url": "https://career.kitron.com/jobs/1-operator", "_jobposting": {"hiringOrganization": {"name": "Kitron"}}}]})
        tl = ats.parse_list("teamtailor", "https://career.kitron.com/jobs.json", body)
        rec = record("KITRON AS", "111111111")
        self.assertEqual(len(ats.select(rec, tl, own_tenant=True)[0]), 1)
        rec.context["group_tree"] = {"976605713": {"name": "KITRON ASA"}, "111111111": {"name": "KITRON AS"}}
        kept, refused = ats.select(rec, tl, own_tenant=True)
        self.assertEqual(kept, [])
        self.assertIn("also the name of 976605713", next(iter(refused.values())))


class HarvestIntegration(unittest.TestCase):
    HOME = ('<html><head><title>Oppland Elektro</title></head><body><nav><a href="/karriere/">Karriere</a></nav>'
            "<footer>Oppland Elektro AS, org.nr 986 885 358</footer></body></html>")

    def test_careers_page_linking_a_tenant_reads_its_list_once(self) -> None:
        careers = ('<html><head><title>Karriere | Oppland Elektro</title></head><body><h1>Jobb hos oss</h1>'
                   '<a href="https://opplandelektro.teamtailor.com/jobs/7829376-prosjektleder-og-faglig-ansvarlig-bygg">Prosjektleder</a>'
                   '<a href="https://opplandelektro.teamtailor.com/">Se alle stillinger</a></body></html>')
        rec = record("OPPLAND ELEKTRO AS")
        model.SNAPSHOT_DIR = None
        fetcher = harvest_site(rec, "https://www.opplandelektro.no/", self.HOME, {
            "https://www.opplandelektro.no/karriere/": careers,
            "https://opplandelektro.teamtailor.com/jobs.json": (fixture("teamtailor-opplandelektro.json"), "application/feed+json; charset=utf-8"),
        })
        claims = job_claims(rec)
        self.assertEqual([c["value"]["title"] for c in claims], ["Prosjektleder og faglig ansvarlig bygg"])
        value = claims[0]["value"]
        self.assertEqual((value["employer"], value["source"], value["published"]), ("Oppland Elektro", "teamtailor", "2026-06-01T15:27:33+02:00"))
        self.assertEqual(claims[0]["extraction_method"], "ats_api_teamtailor_list")
        ev = rec.evidence[claims[0]["evidence_ids"][0]]
        self.assertEqual(ev["span_check"], "verbatim")
        self.assertIn("Teamtailor career-site job feed", ev["access_policy"])
        self.assertEqual(ev["source_url"], "https://opplandelektro.teamtailor.com/jobs.json")
        # one list request, no ad pages, no HTML list page
        self.assertEqual([u for u in fetcher.requested if "teamtailor" in u], ["https://opplandelektro.teamtailor.com/jobs.json"])
        self.assertEqual(rec.family_state["hiring"]["state"], "available")

    def test_own_teamtailor_career_site(self) -> None:
        home = '<html><head><title>DIPS</title></head><body><a href="https://karriere.dips.com">Jobb i DIPS</a><footer>DIPS AS</footer></body></html>'
        career = ('<html><head><title>Karriere - DIPS</title><link rel="alternate" type="application/rss+xml" title="Jobs" href="https://karriere.dips.com/jobs.rss" />'
                  '<link rel="stylesheet" href="https://assets-aws.teamtailor-cdn.com/assets/packs/css/careersite.css"></head><body><h1>Bli med i DIPS</h1></body></html>')
        feed = json.dumps({"version": "https://jsonfeed.org/version/1.1", "title": "DIPS AS", "items": [
            {"id": "a", "title": "Systemutvikler", "url": "https://karriere.dips.com/jobs/123456-systemutvikler", "date_published": "2026-10-01T09:00:00+02:00",
             "_jobposting": {"hiringOrganization": {"name": "DIPS AS"}, "jobLocation": [{"address": {"addressLocality": "Bodø", "addressCountry": "NO"}}]}}]})
        rec = record("DIPS AS", "979543883")
        fetcher = harvest_site(rec, "https://www.dips.com/", home, {
            "https://karriere.dips.com": career, "https://karriere.dips.com/": career,
            "https://karriere.dips.com/jobs.json": (feed, "application/feed+json; charset=utf-8"),
        })
        self.assertEqual([c["value"]["title"] for c in job_claims(rec)], ["Systemutvikler"])
        self.assertIn("https://karriere.dips.com/jobs.json", fetcher.requested)
        self.assertNotIn("https://karriere.dips.com/jobs.rss", fetcher.requested)

    def test_unreadable_list_falls_back_to_the_html_path(self) -> None:
        careers = ('<html><head><title>Karriere</title></head><body><h1>Jobb hos oss</h1>'
                   '<a href="https://opplandelektro.teamtailor.com/">Se alle stillinger</a></body></html>')
        rec = record("OPPLAND ELEKTRO AS")
        fetcher = harvest_site(rec, "https://www.opplandelektro.no/", self.HOME, {"https://www.opplandelektro.no/karriere/": careers})
        self.assertIn("https://opplandelektro.teamtailor.com/jobs.json", fetcher.requested)
        self.assertIn("https://opplandelektro.teamtailor.com/", fetcher.requested)  # the HTML page, after the 404
        self.assertEqual(job_claims(rec), [])


class TenantIndex(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "ats-tenants.tsv.gz"
        rows = ["\t".join(ats.INDEX_COLUMNS), "986885358\tteamtailor\thttps://opplandelektro.teamtailor.com/jobs.json\tregister_domain_cname\tkarriere.opplandelektro.no CNAME ext.teamtailor.com\tOppland Elektro\t2026-10-23"]
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write("\n".join(rows) + "\n")
        self.saved = ats._INDEX
        ats._INDEX = ats.load_index(path)

    def tearDown(self) -> None:
        ats._INDEX = self.saved
        self.tmp.cleanup()

    def test_index_tenant_is_read_live_and_checked(self) -> None:
        rec = record("OPPLAND ELEKTRO AS")
        fetcher = Fetcher({"https://opplandelektro.teamtailor.com/jobs.json": (fixture("teamtailor-opplandelektro.json"), "application/feed+json")})
        count, sources = ats.research_index(fetcher, rec, time.monotonic() + 30)
        self.assertEqual((count, sources), (1, ["https://opplandelektro.teamtailor.com/jobs.json"]))
        claim = job_claims(rec)[0]
        ev = rec.evidence[claim["evidence_ids"][0]]
        self.assertEqual(ev["span_check"], "verbatim")
        self.assertIn("declared ATS index (register_domain_cname", ev["access_policy"])
        # read once per run: the site harvest's read (or an earlier lookup) is not repeated
        self.assertEqual(ats.research_index(fetcher, rec, time.monotonic() + 30), (0, []))
        self.assertEqual(fetcher.count, 1)

    def test_index_row_never_overrides_the_employer_check(self) -> None:
        rec = record("OPPLAND BYGG AS")  # wrongly tied: the live list names another employer
        rec.organisation_number = "986885358"
        fetcher = Fetcher({"https://opplandelektro.teamtailor.com/jobs.json": (fixture("teamtailor-opplandelektro.json"), "application/feed+json")})
        self.assertEqual(ats.research_index(fetcher, rec, time.monotonic() + 30)[0], 0)
        self.assertEqual(job_claims(rec), [])

    def test_missing_index_is_empty(self) -> None:
        self.assertEqual(ats.load_index(Path(self.tmp.name) / "absent.tsv.gz"), {})


class Jobbnorge(unittest.TestCase):
    def board(self) -> jobs.JobbnorgeList:
        board = jobs.JobbnorgeList(None)  # type: ignore[arg-type]
        body = fixture("jobbnorge-sample.json")
        board.snap = snap(jobs.JOBBNORGE, body)
        for job in json.loads(body)["jobs"]:
            board.by_key.setdefault(jobs.name_key(board.employer_of(job)), []).append(job)
        board.count = 3
        board.done.set()
        return board

    @staticmethod
    def firda(municipality: str = "STRYN", postcode: str = "6783") -> CompanyRecord:
        return record("FIRDA BILLAG BUSS AS", "915455662", forretningsadresse={"kommune": municipality, "postnummer": postcode})

    def test_name_and_registered_place(self) -> None:
        rec = self.firda()
        count, sources = jobs.research_jobbnorge(rec, self.board())
        self.assertEqual(count, 1)
        claim = job_claims(rec)[0]
        self.assertEqual((claim["value"]["employer"], claim["value"]["board"]), ("Firda Billag Buss AS", "jobbnorge.no"))
        self.assertTrue(claim["value"]["url"].startswith("https://www.jobbnorge.no/ledige-stillinger/stilling/"))
        self.assertEqual(rec.evidence[claim["evidence_ids"][0]]["span_check"], "verbatim")
        self.assertEqual(sources, [jobs.JOBBNORGE + " (3 open ads)"])

    def test_name_alone_is_not_enough(self) -> None:
        self.assertEqual(jobs.research_jobbnorge(self.firda("BERGEN", "5003"), self.board())[0], 0)
        # Jobbnorge regards the department "Firda Billag Buss AS" as the employer: never the parent's ad
        self.assertEqual(jobs.research_jobbnorge(record("FIRDA BILLAG AS", forretningsadresse={"kommune": "STRYN"}), self.board())[0], 0)
        # "AV & PÅ ELEKTRO AS" is not "Elektro AS", and "Linea AS" is not "LINEA BYGG AS"
        self.assertEqual(jobs.research_jobbnorge(record("AV & PÅ ELEKTRO AS", forretningsadresse={"kommune": "VEFSN"}), self.board())[0], 0)
        self.assertEqual(jobs.research_jobbnorge(record("LINEA BYGG AS", forretningsadresse={"kommune": "VEFSN"}), self.board())[0], 0)
        self.assertEqual(jobs.research_jobbnorge(record("LINEA AS", forretningsadresse={"kommune": "VEFSN"}), self.board())[0], 1)

    def test_settles_hiring_available(self) -> None:
        rec = self.firda()
        feed = jobs.NavFeed(Fetcher({}))  # type: ignore[arg-type]
        feed.jobbnorge = self.board()
        jobs._research_ats(Fetcher({}), rec, time.monotonic() + 30, feed)  # type: ignore[arg-type]
        self.assertEqual(rec.family_state["hiring"]["state"], "available")
        jobs.record_nav_hiring(rec, "not_available", "no active ad in NAV's vacancy feed names this organisation number")
        self.assertEqual(rec.family_state["hiring"]["state"], "available")


if __name__ == "__main__":
    unittest.main()
