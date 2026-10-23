"""Offline tests for group, chain and foreign-group websites (no network).

The organiser's rule: a website must be the company's own, not a parent group's, and a group's
site lends this company none of its social accounts, news or jobs.
"""
from __future__ import annotations

import hashlib
import time
import unittest
from unittest.mock import patch

from kildespor import website
from kildespor.http import Snapshot

from test_discovery import ORG, FakeCaches, html, make_rec, run


def redirected(requested: str, final: str, body: str) -> tuple[Snapshot, object, str]:
    raw = body.encode()
    snap = Snapshot(requested, final, 200, "2026-10-23T00:00:00Z", hashlib.sha256(raw).hexdigest(), "text/html; charset=utf-8", raw)
    soup, _html, text = website.parse(snap)
    return snap, soup, text


def run_fixtures(rec, fixtures: dict[str, tuple[Snapshot, object, str]], cache=None) -> list[tuple[str, str]]:
    """research_website against prebuilt pages (keyed by the URL asked for); [(identity, url)] published."""
    published: list[tuple[str, str]] = []

    def fetch(self, url):  # noqa: ANN001
        return fixtures.get(url)

    website._CONTACT_INDEX = None
    with patch.object(website, "caches", cache if cache is not None else FakeCaches()), \
         patch.object(website, "resolve_public", side_effect=lambda host: any(host in u for u in fixtures)), \
         patch.object(website, "_prefetch_dns", lambda hosts: None), \
         patch.object(website, "guess_domains", return_value=[]), \
         patch.object(website.SiteVisit, "fetch_page", fetch), \
         patch.object(website, "_publish_site", side_effect=lambda r, v, home, isnap, identity, *a, **k: published.append((identity, home.final_url))):
        website.research_website(None, rec, time.monotonic() + 60)
    return published


HIPAQ_DK = html(
    "HIPAQ A/S",
    "HIPAQ er Skandinavias førende leverandør af hjælpemidler. Kontakt: +45 98 38 34 00. "
    "Copyright © HIPAQ Denmark, Vidalsvej 4, DK-9230 Svenstrup",
)


class ForeignGroupHost(unittest.TestCase):
    def test_listed_domain_redirecting_to_a_foreign_group_site_is_refused(self) -> None:
        # HIPAQ AS: Brreg lists www.hipaq.no, which redirects to the Danish group's hipaq.dk.
        rec = make_rec(name="HIPAQ AS", phone=None, hjemmeside="www.hipaq.no")
        fixtures = {"https://www.hipaq.no/": redirected("https://www.hipaq.no/", "https://hipaq.dk/?lang=no", HIPAQ_DK)}
        self.assertEqual(run_fixtures(rec, fixtures), [])
        self.assertEqual(rec.family_state["official_website"]["state"], "ambiguous")
        self.assertIn("group site of hipaq.dk", rec.family_state["official_website"]["reason"])

    def test_our_name_under_a_danish_legal_form_is_the_group(self) -> None:
        # The same page served on the listed .no domain: "HIPAQ A/S" beside DK markers.
        rec = make_rec(name="HIPAQ AS", phone=None, hjemmeside="www.hipaq.no")
        self.assertEqual(run(rec, {"https://www.hipaq.no/": HIPAQ_DK}, guesses=[]), [])
        self.assertIn("HIPAQ A/S", rec.family_state["official_website"]["reason"])
        rec = make_rec(name="WORK SYSTEM NORWAY AS", phone=None, hjemmeside="worksystem.no")
        body = html("Work System Norway", "Work System Norway AS er en del av Work System Sweden AB.")
        self.assertEqual([m for m, _ in run(rec, {"https://worksystem.no/": body}, guesses=[])], ["registry_listed_name_on_site"])

    def test_norwegian_a_s_and_our_own_number_keep_the_site(self) -> None:
        # Norwegian sites still write "A/S"; without a Danish marker the site stays ours.
        rec = make_rec(name="ROAD SUPPLY AS", phone=None, hjemmeside="roadsupply.no")
        body = html("Road Supply A/S", "Road Supply A/S, Industriveien 4, 2020 Skedsmokorset")
        self.assertEqual([m for m, _ in run(rec, {"https://roadsupply.no/": body}, guesses=[])], ["registry_listed_name_on_site"])
        # Our own number on the page settles it before any group test.
        rec = make_rec(name="HIPAQ AS", phone=None, hjemmeside="www.hipaq.no")
        body = HIPAQ_DK.replace("Copyright", f"HIPAQ AS org.nr {ORG} MVA. Copyright")
        self.assertEqual([m for m, _ in run(rec, {"https://www.hipaq.no/": body}, guesses=[])], ["registry_listed_and_organisation_number_on_site"])

    def test_same_domain_redirect_and_dotcom_are_not_foreign(self) -> None:
        rec = make_rec(name="SWIMEYE AS", phone=None, hjemmeside="swimeye.no")
        fixtures = {"https://swimeye.no/": redirected("https://swimeye.no/", "https://swimeye.com/", html("SwimEye", "SwimEye drowning detection"))}
        self.assertEqual([m for m, _ in run_fixtures(rec, fixtures)], ["registry_listed_name_on_site"])


if __name__ == "__main__":
    unittest.main()
