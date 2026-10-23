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

from test_discovery import ORG, FakeCaches, html, make_rec, page, run


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


class HeldCacheSites(unittest.TestCase):
    NOINDEX = [{"url": "https://hjernenett.no/", "kind": "website", "platform": None, "source": "noindex", "match_rule": "orgnr_on_own_pages", "source_record": "noindex:2026-10-17:hjernenett.no"}]
    PORTAL = html("Hjernenett", f"Webinarer om migrene og depresjon. Hjernenett drives av H. Lundbeck AS, org.nr {ORG}")

    def test_index_site_never_replaces_an_unread_register_site(self) -> None:
        # H. LUNDBECK AS: lundbeck.no failed on TLS; the product portal hjernenett.no prints our number.
        rec = make_rec(name="H. LUNDBECK AS", phone=None, hjemmeside="www.lundbeck.no")
        out = run(rec, {"https://hjernenett.no/": self.PORTAL}, cache=FakeCaches(social=self.NOINDEX), guesses=[])
        self.assertEqual(out, [])
        self.assertEqual(rec.family_state["official_website"]["state"], "failed")
        self.assertIn("could not be read", rec.family_state["official_website"]["reason"])
        self.assertNotIn("official_website", rec.checked_fields)  # an unreachable site proves nothing

    def test_index_site_is_still_published_when_the_register_site_was_read(self) -> None:
        rec = make_rec(name="H. LUNDBECK AS", phone=None, hjemmeside="www.lundbeck.no")
        pages = {"https://www.lundbeck.no/": html("Parkert", "Annen Bedrift AS, org.nr 911237814 MVA"), "https://hjernenett.no/": self.PORTAL}
        out = run(rec, pages, cache=FakeCaches(social=self.NOINDEX), guesses=[])
        self.assertEqual(out, [("organisation_number_on_site", "https://hjernenett.no/")])


def run_labelled(rec, pages: dict[str, str]) -> list[tuple[str, tuple[str, str] | None]]:
    """research_website on fixed pages; [(identity_method, group label)] for what would be published."""
    published: list[tuple[str, tuple[str, str] | None]] = []
    fixtures = {url: page(url, body) for url, body in pages.items()}

    def fetch(self, url):  # noqa: ANN001
        return fixtures.get(url)

    website._CONTACT_INDEX = None
    with patch.object(website, "caches", FakeCaches()), \
         patch.object(website, "resolve_public", side_effect=lambda host: any(host in u for u in pages)), \
         patch.object(website, "_prefetch_dns", lambda hosts: None), \
         patch.object(website, "guess_domains", return_value=[]), \
         patch.object(website.SiteVisit, "fetch_page", fetch), \
         patch.object(website, "_publish_site", side_effect=lambda r, v, home, isnap, identity, *a: published.append((identity, a[4]))):
        website.research_website(None, rec, time.monotonic() + 60)
    return published


PARENT, SISTER, ROOT = "911237814", "911277409", "911443708"
CONSTO_PAGE = html(
    "Consto - et av Norges ledende konsern",
    "Consto bygger og rehabiliterer i hele landet. Våre selskaper: Consto AS, Consto Bergen AS, Consto Anlegg Nord AS, Consto Øst AS.",
)


def consto_tree(org_name: str) -> dict[str, dict]:
    """Barlindhaug AS -> Consto AS -> {us, Consto Bergen AS}."""
    return {
        ROOT: {"organisation_number": ROOT, "name": "BARLINDHAUG AS", "parent": None},
        PARENT: {"organisation_number": PARENT, "name": "CONSTO AS", "parent": ROOT},
        ORG: {"organisation_number": ORG, "name": org_name, "parent": PARENT},
        SISTER: {"organisation_number": SISTER, "name": "CONSTO BERGEN AS", "parent": PARENT},
    }


class SisterCompanies(unittest.TestCase):
    def group_rec(self, name: str, hjemmeside: str, tree: dict | None = None):
        rec = make_rec(name=name, phone=None, hjemmeside=hjemmeside, group=bool(tree))
        if tree:
            rec.context["group_tree"] = tree
            rec.context["group_orgs"] = set(tree)
            rec.context["group_root"] = next(n for n, item in tree.items() if not item.get("parent"))
        return rec

    def test_sub_company_on_the_group_site_is_labelled(self) -> None:
        # A sister company printed by name on the page: the site is the Consto group's.
        rec = self.group_rec("CONSTO ANLEGG NORD AS", "consto.no", consto_tree("CONSTO ANLEGG NORD AS"))
        [(identity, group)] = run_labelled(rec, {"https://consto.no/": CONSTO_PAGE})
        self.assertEqual(identity, "registry_listed_name_on_site")
        self.assertIn("CONSTO BERGEN AS", group[0])
        self.assertEqual(group[1], "Consto")
        # Without Brreg's tree: several other companies under our name's first word say the same.
        rec = self.group_rec("CONSTO ANLEGG NORD AS", "consto.no")
        [(_identity, group)] = run_labelled(rec, {"https://consto.no/": CONSTO_PAGE})
        self.assertIn("other companies under the consto name", group[0])

    def test_namesake_keeps_the_group_site_unlabelled(self) -> None:
        # CONSTO AS is the namesake of consto.no: Builderr scores its Facebook and LinkedIn as correct.
        tree = consto_tree("CONSTO ANLEGG NORD AS")
        rec = self.group_rec("CONSTO AS", "consto.no", tree)
        rec.organisation_number = PARENT
        rec.context["entity"]["organisasjonsnummer"] = PARENT
        self.assertEqual(run_labelled(rec, {"https://consto.no/": CONSTO_PAGE}), [("registry_listed_name_on_site", None)])

    def test_own_site_naming_its_parent_or_printing_our_number_is_not_labelled(self) -> None:
        tree = {
            PARENT: {"organisation_number": PARENT, "name": "FJORD GRUPPEN AS", "parent": None},
            ORG: {"organisation_number": ORG, "name": "VESTLAND TAK AS", "parent": PARENT},
            SISTER: {"organisation_number": SISTER, "name": "VESTLAND BYGG AS", "parent": PARENT},
        }
        rec = self.group_rec("VESTLAND TAK AS", "taktekker.no", tree)
        body = html("Taktekker Bergen", "Vestland Tak AS legger tak i Bergen. Vi er en del av Fjord Gruppen AS.")
        self.assertEqual(run_labelled(rec, {"https://taktekker.no/": body}), [("registry_listed_name_on_site", None)])
        # A sister company named: labelled, unless our own number is printed as the site's owner.
        rec = self.group_rec("VESTLAND TAK AS", "taktekker.no", tree)
        body = html("Taktekker Bergen", "Vestland Tak AS legger tak. Søsterselskapet Vestland Bygg AS bygger hus.")
        [(_identity, group)] = run_labelled(rec, {"https://taktekker.no/": body})
        self.assertIn("VESTLAND BYGG AS", group[0])
        rec = self.group_rec("VESTLAND TAK AS", "taktekker.no", tree)
        body = body.replace("bygger hus.", f"bygger hus. Vestland Tak AS org.nr {ORG} MVA")
        self.assertEqual(run_labelled(rec, {"https://taktekker.no/": body}), [("registry_listed_and_organisation_number_on_site", None)])


class ChainSections(unittest.TestCase):
    def publish(self, name: str, home_url: str, pages: dict[str, str]):
        rec = make_rec(name=name, phone=None)
        fixtures = {url: page(url, body) for url, body in pages.items()}
        visit = website.SiteVisit(None, rec, time.monotonic() + 30)

        def fetch(self_, url):
            if not self_.in_section(url):
                return None
            got = fixtures.get(url)
            if got:
                self_.pages.append(got)
            return got

        harvested = []
        with patch.object(website, "caches", FakeCaches()), patch.object(website.SiteVisit, "fetch_page", fetch), \
             patch("kildespor.harvest._extract_site_facts", lambda r, v, home, exact, rel: harvested.append(rel)):
            home = fixtures[home_url][0]
            website._publish_site(rec, visit, home, home, "registry_listed_name_on_site", "registry_listed", "name on page", set(), set())
        return rec, harvested

    def test_store_section_of_a_chain_site_is_a_group_site(self) -> None:
        # HUSFLIDEN HOLMESTRAND SA: norskflid.no's root names no legal entity, and lacks "holmestrand".
        rec, harvested = self.publish("HUSFLIDEN HOLMESTRAND SA", "https://www.norskflid.no/butikker/holmestrand/", {
            "https://www.norskflid.no/butikker/holmestrand/": html("Husfliden Holmestrand | Norsk Flid", "Husfliden Holmestrand. Tlf 33 05 12 34"),
            "https://www.norskflid.no/": html("Hjem - Norsk Flid Husfliden", "Norsk Flid Husfliden - butikker over hele landet"),
        })
        claim = rec.claims_for("official_website")[0]
        self.assertEqual(claim["value"]["url"], "https://www.norskflid.no/butikker/holmestrand/")
        self.assertEqual(claim["relationship"], "group_site")
        self.assertEqual(harvested, ["group_site"])
        self.assertEqual(rec.context["website"]["group_brand"], "Norsk Flid Husfliden")

    def test_section_under_a_root_carrying_our_name_is_not(self) -> None:
        rec, harvested = self.publish("BERG BYGG AS", "https://bergbygg.no/no-nb/hjem/", {
            "https://bergbygg.no/no-nb/hjem/": html("Berg Bygg", "Berg Bygg AS"),
            "https://bergbygg.no/": html("Berg Bygg - tømrer i Bergen", "Velkommen"),
        })
        self.assertNotIn("relationship", rec.claims_for("official_website")[0])
        self.assertEqual(harvested, [None])


if __name__ == "__main__":
    unittest.main()
