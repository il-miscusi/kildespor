"""Offline tests for website discovery and the identity gate (no network)."""
from __future__ import annotations

import hashlib
import time
import unittest
from unittest.mock import patch

from kildespor import http as khttp
from kildespor import website
from kildespor.http import Robots, Snapshot
from kildespor.model import CompanyRecord

ORG = "938702675"
OTHER = "911237814"     # an unrelated company (accountant, agency, sister)
OTHER2 = "911277409"
OTHER3 = "911443708"


class FakeCaches:
    """The caches interface with a shared-phone/e-mail screen loaded."""

    def __init__(self, shared_phones=(), shared_domains=(), social=()):
        self.phones = set(shared_phones)
        self.domains = set(shared_domains)
        self.social = list(social)

    def available(self):
        return ["shared-phones.tsv.gz", "shared-email-domains.tsv.gz"]

    def shared_phone(self, number):
        return number[-8:] in self.phones

    def shared_email_domain(self, domain):
        return domain in self.domains

    def candidates(self, orgnr):
        return [dict(item) for item in self.social if item.get("orgnr", ORG) == orgnr]

    def register_contacts(self, orgnr):
        return {"phones": [], "emails": [], "websites": []}

    def _load(self, key, builder):
        index = {}
        for item in self.social:
            index.setdefault(item.get("orgnr", ORG), []).append((item["url"], item["kind"], item.get("platform"), item["source"], item.get("match_rule") or "", "", ""))
        return index

    _build_candidates = None


class NoScreenCaches(FakeCaches):
    def available(self):
        return []


def make_rec(name="BERG BYGG AS", phone="55 12 34 56", email=None, street="Strandgaten 1", postcode="5013", town="BERGEN",
             form="AS", roles=(), group=False, hjemmeside=None):
    rec = CompanyRecord(ORG)
    rec.context["entity"] = {
        "organisasjonsnummer": ORG, "navn": name, "telefon": phone, "epostadresse": email, "hjemmeside": hjemmeside,
        "organisasjonsform": {"kode": form},
        "forretningsadresse": {"adresse": [street], "postnummer": postcode, "poststed": town, "kommune": town},
        "erIKonsern": group,
    }
    rec.context["roles"] = [{"name": n, "role_code": c, "holder_type": "person"} for c, n in roles]
    rec.context["subunits"] = []
    return rec


def page(url: str, body: str) -> tuple[Snapshot, object, str]:
    raw = body.encode()
    snap = Snapshot(url, url, 200, "2026-10-17T00:00:00Z", hashlib.sha256(raw).hexdigest(), "text/html; charset=utf-8", raw)
    soup, _html, text = website.parse(snap)
    return snap, soup, text


def run(rec: CompanyRecord, pages: dict[str, str], cache=None, guesses=None) -> list[tuple[str, str]]:
    """Run research_website against fixed pages; return [(identity_method, site url)] published."""
    published: list[tuple[str, str]] = []
    fixtures = {url: page(url, body) for url, body in pages.items()}

    def fetch(self, url):  # noqa: ANN001
        return fixtures.get(url)

    with patch.object(website, "caches", cache if cache is not None else FakeCaches()), \
         patch.object(website, "resolve_public", side_effect=lambda host: any(host in u for u in pages)), \
         patch.object(website, "_prefetch_dns", lambda hosts: None), \
         patch.object(website.SiteVisit, "fetch_page", fetch), \
         patch.object(website, "_publish_site", side_effect=lambda r, v, home, isnap, identity, *a, **k: published.append((identity, home.final_url))):
        if guesses is not None:
            with patch.object(website, "guess_domains", return_value=guesses):
                website.research_website(None, rec, time.monotonic() + 60)
        else:
            website.research_website(None, rec, time.monotonic() + 60)
    return published


def html(title: str, body: str, extra_head: str = "") -> str:
    return f"<html><head><title>{title}</title>{extra_head}</head><body>{body}</body></html>"


class RegisterContactRule(unittest.TestCase):
    def test_registered_phone_on_brand_site_is_accepted(self) -> None:
        rec = make_rec(name="HANDVERKER MOXNESS AS", phone="41 22 33 44")
        pages = {"https://snekkersentralen.no/": html("Snekkersentralen", "Vi bygger terrasser. Ring 412 23 344 i dag. Strandgaten 1, 5013 Bergen")}
        out = run(rec, pages, guesses=["snekkersentralen.no"])
        self.assertEqual(out, [("register_contact_on_site", "https://snekkersentralen.no/")])

    def test_a_lone_phone_match_is_not_enough(self) -> None:
        rec = make_rec(name="HANDVERKER MOXNESS AS", phone="41 22 33 44")
        pages = {"https://snekkersentralen.no/": html("Snekkersentralen", "Vi bygger terrasser. Ring 412 23 344 i dag.")}
        self.assertEqual(run(rec, pages, guesses=["snekkersentralen.no"]), [])

    def test_accountants_page_with_shared_phone_is_refused(self) -> None:
        rec = make_rec(name="OLSEN HOLDING AS", phone="22 00 11 22", email="post@regnskapshuset.no")
        body = "Regnskapshuset AS - regnskap for små bedrifter. Tlf 22 00 11 22. post@regnskapshuset.no"
        pages = {"https://regnskapshuset.no/": html("Regnskapshuset", body)}
        out = run(rec, pages, cache=FakeCaches(shared_phones={"22001122"}), guesses=[])
        self.assertEqual(out, [])

    def test_accountants_own_org_number_vetoes_contact_match(self) -> None:
        rec = make_rec(name="BERG BYGG AS", phone="22 00 11 22", email="berg@regnskapsbyraa.no")
        body = f"Regnskapsbyrået. Tlf 22 00 11 22. berg@regnskapsbyraa.no. Org.nr {OTHER} MVA"
        pages = {"https://regnskapsbyraa.no/": html("Regnskapsbyrået", body)}
        self.assertEqual(run(rec, pages, guesses=[]), [])

    def test_sister_company_site_is_refused(self) -> None:
        # A holding company shares phone and address with its operating sister (Karthik's vetnes.no case).
        rec = make_rec(name="VETNES INVEST AS", phone="70 11 22 33", street="Vetnesvegen 2", postcode="6200", town="STRANDA")
        body = "Vetnes Bygg - tømrer på Sunnmøre. Tlf 70 11 22 33, Vetnesvegen 2, 6200 Stranda. © 2025 Vetnes Bygg AS"
        pages = {"https://vetnes.no/": html("Vetnes Bygg AS", body)}
        self.assertEqual(run(rec, pages, guesses=["vetnes.no"]), [])

    def test_holding_company_contact_match_needs_its_full_name(self) -> None:
        rec = make_rec(name="VETNES INVEST AS", phone="70 11 22 33")
        pages = {"https://vetnes.no/": html("Vetnes", "Velkommen. Tlf 70 11 22 33")}
        self.assertEqual(run(rec, pages, guesses=["vetnes.no"]), [])

    def test_housing_coop_never_uses_contact_rules(self) -> None:
        rec = make_rec(name="SOLBAKKEN BORETTSLAG", form="BRL", phone="22 00 11 22")
        pages = {"https://solbakken.no/": html("Solbakken borettslag", "Kontakt forvaltning: 22 00 11 22. 5013 Bergen")}
        self.assertEqual(run(rec, pages, guesses=["solbakken.no"]), [])

    def test_without_screen_a_phone_counts_only_on_the_registered_email_domain(self) -> None:
        rec = make_rec(name="FJORD MARIN AS", phone="41 22 33 44", email="post@fjordmarin.no")
        pages = {"https://fjordmarin.com/": html("Båtservice", "Ring 41 22 33 44")}
        self.assertEqual(run(rec, pages, cache=NoScreenCaches(), guesses=["fjordmarin.com"]), [])
        rec = make_rec(name="FJORD MARIN AS", phone="41 22 33 44", email="post@fjordmarin.no")
        pages = {"https://fjordmarin.no/": html("Båtservice", "Ring 41 22 33 44")}
        out = run(rec, pages, cache=NoScreenCaches(), guesses=["fjordmarin.no"])
        self.assertEqual([m for m, _ in out], ["register_contact_on_site"])

    def test_foreign_country_domain_needs_the_number(self) -> None:
        rec = make_rec(name="LAS VENTURES AS", phone="41 22 33 44")
        pages = {"https://lasventures.in/": html("LAS Ventures", "Ring 41 22 33 44. 5013 Bergen")}
        self.assertEqual(run(rec, pages, guesses=["lasventures.in"]), [])


class OrganisationNumberContexts(unittest.TestCase):
    def test_customer_list_number_is_not_ownership(self) -> None:
        rec = make_rec(name="TEST AS", phone=None)
        body = f"Våre kunder: TEST AS, org.nr {ORG[:3]} {ORG[3:6]} {ORG[6:]}. Nettstedet drives av OTHER AS, org.nr {OTHER}."
        pages = {"https://test.no/": html("OTHER AS", body)}
        self.assertEqual(run(rec, pages, guesses=["test.no"]), [])

    def test_agency_footer_number_does_not_veto_our_site(self) -> None:
        rec = make_rec(name="BERG BYGG AS", phone="55 12 34 56")
        body = f"Berg Bygg - tømrer. Tlf 55 12 34 56. Org.nr {ORG} MVA. Nettside levert av Webbyrå AS, org.nr {OTHER}"
        pages = {"https://bergbygg.no/": html("Berg Bygg AS", body)}
        out = run(rec, pages, guesses=["bergbygg.no"])
        self.assertEqual([m for m, _ in out], ["organisation_number_on_site"])

    def test_agency_credit_with_our_number_on_a_client_site_is_not_ours(self) -> None:
        rec = make_rec(name="WEBBYRÅ AS", phone=None)
        body = f"Kafé Sentrum. Org.nr {OTHER} MVA. Nettside levert av Webbyrå AS, org.nr {ORG}"
        pages = {"https://webbyra.no/": html("Kafé Sentrum", body)}
        self.assertEqual(run(rec, pages, guesses=["webbyra.no"]), [])

    def test_number_context_kinds(self) -> None:
        kinds = {n: k for n, (k, _) in website.number_contexts(
            f"Org.nr {ORG} MVA | Revisor: Revisjon AS org.nr {OTHER} | Levert av Byrå AS {OTHER2} | Liste {OTHER3}").items()}
        self.assertEqual(kinds, {ORG: "owner", OTHER: "mention", OTHER2: "credit", OTHER3: "bare"})


class NameAndRegisterSignalRule(unittest.TestCase):
    def test_name_in_title_and_postcode_town(self) -> None:
        rec = make_rec(name="BERG BYGG AS", phone=None)
        pages = {"https://bergbygg.no/": html("Berg Bygg AS | Tømrer i Bergen", "Vi holder til i Strandgaten 9, 5013 Bergen.")}
        self.assertEqual([m for m, _ in run(rec, pages, guesses=["bergbygg.no"])], ["name_and_register_signal"])

    def test_name_with_daglig_leder(self) -> None:
        rec = make_rec(name="BERG BYGG AS", phone=None, postcode="9999", roles=[("DAGL", "Kari Nordmann")])
        pages = {"https://bergbygg.com/": html("Hjem", "<h1>Berg Bygg</h1> Daglig leder Kari Nordmann")}
        self.assertEqual([m for m, _ in run(rec, pages, guesses=["bergbygg.com"])], ["name_and_register_signal"])

    def test_longer_name_is_another_company(self) -> None:
        rec = make_rec(name="BERG BYGG AS", phone=None)
        pages = {"https://bergbygg.no/": html("Berg Bygg og Anlegg", "5013 Bergen")}
        self.assertEqual(run(rec, pages, guesses=["bergbygg.no"]), [])

    def test_name_without_register_signal_is_refused(self) -> None:
        rec = make_rec(name="BERG BYGG AS", phone=None)
        pages = {"https://bergbygg.no/": html("Berg Bygg AS", "Vi bygger hus i hele landet.")}
        self.assertEqual(run(rec, pages, guesses=["bergbygg.no"]), [])

    def test_group_umbrella_site_stays_out(self) -> None:
        rec = make_rec(name="BERG BYGG AS", phone=None, group=True)
        rec.context["group_orgs"] = {ORG, OTHER}
        pages = {"https://bergbygg.no/": html("Berg Bygg AS", f"5013 Bergen. Berg Gruppen AS org.nr {OTHER}")}
        self.assertEqual(run(rec, pages, guesses=["bergbygg.no"]), [])


class DeclaredSites(unittest.TestCase):
    def test_register_site_js_shell_with_name_in_metadata(self) -> None:
        rec = make_rec(name="FIRST FLUSH AS", phone=None, hjemmeside="ffteas.com")
        head = '<meta property="og:site_name" content="First Flush Tea">'
        pages = {"https://ffteas.com/": html("Loading", "<div id=root></div>", head)}
        out = run(rec, pages, guesses=[])
        self.assertEqual(out, [("registry_listed_name_in_metadata", "https://ffteas.com/")])

    def test_register_site_domain_spells_short_name(self) -> None:
        rec = make_rec(name="TLH AS", phone=None, hjemmeside="www.tlh.com")
        pages = {"https://www.tlh.com/": html("Welcome", "<div id=app></div>")}
        self.assertEqual([m for m, _ in run(rec, pages, guesses=[])], ["registry_listed_domain_spells_name"])

    def test_register_site_showing_another_primary_number(self) -> None:
        rec = make_rec(name="TLH AS", phone=None, hjemmeside="www.tlh.com")
        pages = {"https://www.tlh.com/": html("TLH", f"TLH Holding. Org.nr {OTHER} MVA")}
        self.assertEqual(run(rec, pages, guesses=[]), [])

    def test_unreachable_register_site_is_not_a_removal(self) -> None:
        rec = make_rec(name="TLH AS", phone=None, hjemmeside="www.tlh.com")
        run(rec, {}, guesses=[])
        self.assertNotIn("official_website", rec.checked_fields)


class CandidatesAndBudget(unittest.TestCase):
    def test_candidates_include_former_names_email_and_subunits(self) -> None:
        rec = make_rec(name="NYTT NAVN AS", email="post@gammelt.no")
        rec.context["entity"]["historiskeNavn"] = [{"navn": "GAMMELT FIRMA AS", "tilDato": "2024-01-01"}]
        rec.context["subunits"] = [{"name": "SJØBUA KAFE", "email": "hei@sjobua.no", "phones": []}]
        origins = dict((website.registered_domain(website.urlsplit(u).hostname), o) for u, o in website.website_candidates(rec))
        self.assertEqual(origins["gammelt.no"], "registry_email_domain")
        self.assertEqual(origins["sjobua.no"], "subunit_email_domain")
        self.assertEqual(origins["nyttnavn.no"], "name_derived_domain")
        self.assertEqual(origins["gammeltfirma.no"], "former_name_domain")
        self.assertEqual(origins["sjobuakafe.no"], "subunit_name_domain")

    def test_shared_email_domain_is_no_candidate(self) -> None:
        rec = make_rec(name="NYTT NAVN AS", email="kunde@forvalter.no")
        with patch.object(website, "caches", FakeCaches(shared_domains={"forvalter.no"})):
            urls = [u for u, _ in website.website_candidates(rec)]
        self.assertFalse(any("forvalter.no" in u for u in urls))

    def test_long_and_generic_names_are_guessed(self) -> None:
        self.assertIn("vetnes.no", website.guess_domains("VETNES INVEST AS", limit=12))
        self.assertTrue(website.guess_domains("ÅSEN & ØVRE EIENDOM NORGE AS", limit=12))

    def test_raise_company_cap(self) -> None:
        fetcher = khttp.Fetcher()
        fetcher.begin_company(16)
        fetcher._local.count = fetcher._local.cap_start + 10
        fetcher.raise_company_cap(16)
        self.assertEqual(fetcher._local.cap, 26)


class RobotsAndJsonLd(unittest.TestCase):
    def test_allow_homepage_over_disallow_all(self) -> None:
        robots = Robots(["User-agent: *", "Disallow: /", "Allow: /$"], "KildesporBot")
        self.assertTrue(robots.allows("https://x.no/"))
        self.assertFalse(robots.allows("https://x.no/side"))

    def test_wildcard_rules_do_not_block_homepage(self) -> None:
        robots = Robots(["User-agent: *", "Disallow: /*?", "Disallow: /wp-admin/"], "KildesporBot")
        self.assertTrue(robots.allows("https://x.no/"))
        self.assertFalse(robots.allows("https://x.no/?p=1"))

    def test_unidentified_jsonld_is_not_ours(self) -> None:
        rec = make_rec()
        self.assertFalse(website._ld_org_is_ours({"@type": "Organization"}, rec))
        self.assertFalse(website._ld_employer_is_ours({"title": "Snekker"}, rec))
        self.assertTrue(website._ld_org_is_ours({"@type": "Organization", "name": "Berg Bygg AS"}, rec))


class CacheSocialProfiles(unittest.TestCase):
    def test_exact_cache_profiles_published_name_matches_not(self) -> None:
        rec = make_rec()
        social = [
            {"url": "https://www.facebook.com/bergbygg", "kind": "social", "platform": "facebook", "source": "wikidata", "match_rule": "p2333", "source_record": "https://www.wikidata.org/wiki/Q1#P2013"},
            {"url": "https://www.instagram.com/bergbygg", "kind": "social", "platform": "instagram", "source": "overture", "match_rule": "overture_name_postcode", "source_record": "overture:2026-09:x"},
            {"url": "https://www.instagram.com/shared", "kind": "social", "platform": "instagram", "source": "overture", "match_rule": "overture_email_exact", "source_record": "overture:2026-09:y"},
            {"url": "https://www.instagram.com/shared", "kind": "social", "platform": "instagram", "source": "overture", "match_rule": "overture_email_exact", "source_record": "overture:2026-09:z", "orgnr": OTHER},
        ]
        website._cache_social_owners = None
        with patch.object(website, "caches", FakeCaches(social=social)):
            website._publish_cache_socials(rec)
        website._cache_social_owners = None
        urls = [c["value"]["url"] for c in rec.claims if c["field"] == "social_profile"]
        self.assertEqual(urls, ["https://www.facebook.com/bergbygg"])


if __name__ == "__main__":
    unittest.main()


class SuccessorsAndUmbrellas(unittest.TestCase):
    def test_declared_site_under_a_former_name_is_refused(self) -> None:
        # CMW HOLDING AS was ABCHAR AS; a new ABCHAR AS now runs abchar.no.
        rec = make_rec(name="CMW HOLDING AS", phone="45 05 19 98", email="maria@abchar.no", hjemmeside="abchar.no")
        rec.context["entity"]["historiskeNavn"] = [{"navn": "ABCHAR AS", "tilDato": "2024-01-01"}]
        pages = {"https://abchar.no/": html("ABCHAR | Pyrolysereaktor", "<div id=app></div> 45 05 19 98")}
        self.assertEqual(run(rec, pages, guesses=[]), [])

    def test_umbrella_brand_with_our_phone_needs_the_full_name(self) -> None:
        rec = make_rec(name="HEAD ENERGY NORGRID AS", phone="992 04 323")
        pages = {"https://headenergy.no/": html("Head Energy Norge | Ingeniørselskap", "Kontakt +47 992 04 323")}
        self.assertEqual(run(rec, pages, guesses=["headenergy.no"]), [])

    def test_initials_stay_together_in_guesses(self) -> None:
        self.assertIn("pe-gaarud.no", website.guess_domains("P.E. GAARUD AS"))

    def test_default_port_is_dropped_from_the_site_url(self) -> None:
        rec = make_rec(name="PEAB BYGG AS", phone=None)
        home = page("https://peab.no:443/bygg/", html("Peab Bygg", "Peab Bygg"))[0]
        visit = website.SiteVisit(None, rec, time.monotonic() + 30)
        with patch("kildespor.harvest._extract_site_facts", lambda *a, **k: None):
            website._publish_site(rec, visit, home, home, "registry_listed_name_on_site", "registry_listed", "Peab Bygg", set(), set())
        self.assertEqual(rec.claims_for("official_website")[0]["value"]["url"], "https://peab.no/")
