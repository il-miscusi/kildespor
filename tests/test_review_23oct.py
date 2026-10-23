"""Offline tests for the five defects review A reproduced on 23 Oct, plus the time budget and the
finn.no rule: no network. Run with ``python -m unittest tests.test_review_23oct``."""
from __future__ import annotations

import hashlib
import time
import unittest
from unittest.mock import patch

from kildespor import harvest, jobs
from kildespor.envelope import build_envelope
from kildespor.http import Snapshot
from kildespor.model import CompanyRecord
from kildespor.refresh import diff, news_checked_key
from kildespor.runner import default_budget, drop_unsupported, validate

ORG = "938702675"
RAW = b"<html><h1>TEST AS</h1><p>Org.nr. 938702675</p></html>"
SNAP = Snapshot("https://test.no/", "https://test.no/", 200, "2026-10-23T00:00:00Z", hashlib.sha256(RAW).hexdigest(), "text/html", RAW)


def record() -> CompanyRecord:
    rec = CompanyRecord(ORG)
    rec.context["entity"] = {"navn": "TEST AS", "organisasjonsnummer": ORG}
    return rec


def envelope(rec: CompanyRecord) -> dict:
    return build_envelope(rec, run={"run_id": "offline"}, synthesis={}, changes=[], refresh_meta={})


class SpanSupport(unittest.TestCase):
    def test_claim_whose_span_is_not_in_its_source_is_refused(self) -> None:
        rec = record()
        ev = rec.evidence_from_snapshot(SNAP, source_class="company_owned_website", span="INVENTED NAME AS", method="offline_fixture")
        rec.add_claim(family="legal_identity", field_name="legal_name", value="INVENTED NAME AS", evidence_ids=[ev], method="offline_fixture")
        rec.set_state("legal_identity", "available", "fixture")
        self.assertFalse(validate([envelope(rec)], [ORG])["passed"])
        drop_unsupported(rec)
        rec.finalize_states()
        self.assertFalse(rec.claims)
        self.assertEqual(rec.family_state["legal_identity"]["state"], "failed")
        self.assertTrue(validate([envelope(rec)], [ORG])["passed"])

    def test_quoted_span_present_in_source_is_kept(self) -> None:
        rec = record()
        ev = rec.evidence_from_snapshot(SNAP, source_class="company_owned_website", span='<h1> = "TEST AS"', method="offline_fixture")
        self.assertEqual(rec.evidence[ev]["span_check"], "verbatim")
        rec.add_claim(family="public_brand", field_name="public_brand_name", value="TEST AS", evidence_ids=[ev], method="offline_fixture")
        drop_unsupported(rec)
        self.assertEqual(len(rec.claims), 1)


    def test_register_locators_are_not_checked_as_quotes(self) -> None:
        body = b'{"rollegrupper": [{"roller": [{"type": {"kode": "DAGL"}, "person": {"navn": {"fornavn": "Kari", "etternavn": "Nordmann"}}}]}]}'
        snap = Snapshot("https://data.brreg.no/enhetsregisteret/api/enheter/938702675/roller", "https://data.brreg.no/enhetsregisteret/api/enheter/938702675/roller", 200, "2026-10-23T00:00:00Z", hashlib.sha256(body).hexdigest(), "application/json", body)
        rec = record()
        ev = rec.evidence_from_snapshot(snap, source_class="official_roles", span="DAGL/DAGL: Kari Nordmann", method="brreg_roles_api")
        self.assertNotIn("span_check", rec.evidence[ev])

    def test_snippet_across_page_elements_is_found(self) -> None:
        raw = "<html><head><title>Nydals AS</title><style>.a{x:1}</style></head><body><div>Org.nr: 853&nbsp;097 012</div><nav>Meny Hjem</nav></body></html>".encode()
        snap = Snapshot("https://nydals.no/", "https://nydals.no/", 200, "2026-10-23T00:00:00Z", hashlib.sha256(raw).hexdigest(), "text/html", raw)
        rec = record()
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span="Nydals AS Org. nr: 853 097 012 Meny Hjem", method="html_metadata")
        self.assertEqual(rec.evidence[ev]["span_check"], "verbatim")
        # Identity evidence is the identity gate's to check, not the span replay's.
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span="Org.nr 853 097 012 (from markup)", method="registry_listed_and_organisation_number_on_site")
        self.assertNotIn("span_check", rec.evidence[ev])


class NavFeedCompleteness(unittest.TestCase):
    def test_partial_feed_reports_failed_not_absent(self) -> None:
        rec = record()
        feed = jobs.NavFeed(None)
        feed.token = "offline"
        feed.active = {"unrelated": {"business": "UNRELATED AS", "title": "Developer"}}
        feed.error = "feed page returned HTTP 503 after 1 pages"
        feed.done.set()
        jobs.research_jobs(None, rec, time.monotonic() + 10, feed)
        self.assertEqual(rec.family_state["hiring"]["state"], "failed")
        self.assertNotIn("job_posting", rec.checked_fields)

    def test_page_cap_with_pages_left_is_an_error(self) -> None:
        class Response:
            ok, status = True, 200

            def text(self):
                return "token"

            def json(self):
                return {"items": [], "next_url": "/api/v1/feed?page=more"}

        class Fetcher:
            def get(self, *args, **kwargs):
                return Response()

        feed = jobs.NavFeed(Fetcher())
        with patch.object(jobs, "MAX_PAGES", 2):
            feed._read()
        self.assertIn("cap", feed.error or "")


class NewsDates(unittest.TestCase):
    def items(self, stamps: list[str]) -> list[harvest.NewsItem]:
        raw = b"<html></html>"
        snap = Snapshot("https://test.no/news/", "https://test.no/news/", 200, "2026-10-23T00:00:00Z", hashlib.sha256(raw).hexdigest(), "text/html", raw)
        return [harvest.NewsItem(f"TEST AS news {i}", f"https://test.no/news/{i}", t, t[:10], snap, f"TEST AS news {i}", "article_page_meta", "company_website") for i, t in enumerate(stamps)]

    def test_posts_released_at_one_instant_are_not_an_import(self) -> None:
        self.assertEqual(harvest.copy_bursts(self.items(["2026-10-22T12:00:00Z"] * 3)), set())

    def test_posts_stamped_seconds_apart_are_an_import(self) -> None:
        stamps = ["2025-01-24T23:05:19+01:00", "2025-01-24T23:05:36+01:00", "2025-01-24T23:05:55+01:00"]
        self.assertEqual(harvest.copy_bursts(self.items(stamps)), {0, 1, 2})

    def test_older_post_missing_from_a_bounded_feed_is_not_removed(self) -> None:
        previous = record()
        previous.add_claim(family="public_activity", field_name="company_news", key=["news", "old"],
                           value={"title": "Older announcement", "url": "https://test.no/news/old", "published": "2026-09-01"},
                           evidence_ids=["old-evidence"], method="rss_atom_feed")
        current = record()
        current.checked_fields.add("company_news")
        changes, _ = diff(current, envelope(previous), "bounded")
        self.assertFalse([c for c in changes if c["change_type"] == "removed_activity"])
        current = record()
        current.checked_fields.add(news_checked_key("https://www.test.no/news/old/"))
        changes, _ = diff(current, envelope(previous), "revisited")
        self.assertTrue([c for c in changes if c["change_type"] == "removed_activity"])


class Budget(unittest.TestCase):
    def test_default_budget_scales_with_the_batch(self) -> None:
        self.assertEqual(default_budget(100), 960.0)
        self.assertEqual(default_budget(1000), 1500.0)
        self.assertEqual(default_budget(10000), 2700.0)


if __name__ == "__main__":
    unittest.main()
