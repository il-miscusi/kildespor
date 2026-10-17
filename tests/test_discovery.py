"""Offline tests for website discovery and the identity gate (no network)."""
from __future__ import annotations

import hashlib
import json
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
        self.holders = {}
        self.contacts = {}

    def available(self):
        return ["shared-phones.tsv.gz", "shared-email-domains.tsv.gz"]

    def shared_phone(self, number):
        return number[-8:] in self.phones

    def shared_email_domain(self, domain):
        return domain in self.domains

    def email_domain_holders(self, domain):
        return self.holders.get(domain, 7 if domain in self.domains else 0)

    def candidates(self, orgnr):
        return [dict(item) for item in self.social if item.get("orgnr", ORG) == orgnr]

    def register_contacts(self, orgnr):
        return {"phones": [], "emails": [], "websites": []}

    def _load(self, key, builder):
        if key == "register-contacts":
            return self.contacts
        index = {}
        for item in self.social:
            index.setdefault(item.get("orgnr", ORG), []).append((item["url"], item["kind"], item.get("platform"), item["source"], item.get("match_rule") or "", "", ""))
        return index

    _build_candidates = None
    _build_contacts = None


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

    website._CONTACT_INDEX = None
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
        # A brand that shares no word with the legal name cannot pass on contact details alone.
        self.assertEqual(run(rec, pages, guesses=["snekkersentralen.no"]), [])
        pages = {"https://moxnessbygg.no/": html("Moxness Bygg", "Vi bygger terrasser. Ring 412 23 344 i dag.")}
        out = run(rec, pages, guesses=["moxnessbygg.no"])
        self.assertEqual(out, [("register_contact_on_site", "https://moxnessbygg.no/")])

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
        pages = {"https://fjordmarin.no/": html("Båtservice | Fjord Marin", "Ring 41 22 33 44")}
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
            cands = website.website_candidates(rec)
        # Still tried, last, and labelled so that its contact details never count as proof.
        self.assertEqual(cands[-1], ("https://forvalter.no/", "shared_email_domain"))

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
    SOCIAL = [
        {"url": "https://www.facebook.com/bergbygg", "kind": "social", "platform": "facebook", "source": "wikidata", "match_rule": "wikidata_p2333_exact_orgnr", "source_record": "https://www.wikidata.org/wiki/Q1#P2013"},
        {"url": "https://www.instagram.com/bergbygg", "kind": "social", "platform": "instagram", "source": "overture", "match_rule": "overture_name_postcode", "source_record": "overture:2026-09:x"},
        {"url": "https://www.x.com/bergbygg", "kind": "social", "platform": "x", "source": "overture", "match_rule": "overture_email_exact", "source_record": "overture:2026-09:w"},
        {"url": "https://www.instagram.com/shared", "kind": "social", "platform": "instagram", "source": "wikidata", "match_rule": "wikidata_p2333_exact_orgnr", "source_record": "https://www.wikidata.org/wiki/Q1#P2003"},
        {"url": "https://www.instagram.com/shared", "kind": "social", "platform": "instagram", "source": "wikidata", "match_rule": "wikidata_p2333_exact_orgnr", "source_record": "https://www.wikidata.org/wiki/Q2#P2003", "orgnr": OTHER},
    ]

    class Fetcher:
        def __init__(self, entity=None, refuse=False):
            self.entity, self.refuse, self.urls = entity, refuse, []

        def get(self, url, **kwargs):
            self.urls.append(url)
            if self.refuse:
                raise khttp.RobotsDisallowed(url)
            body = json.dumps({"entities": {"Q1": self.entity}}).encode()
            return Snapshot(url, url, 200, "2026-10-17T12:00:00Z", hashlib.sha256(body).hexdigest(), "application/json", body)

    @staticmethod
    def entity(orgnr=ORG):
        def claim(value):
            return {"rank": "normal", "mainsnak": {"snaktype": "value", "datavalue": {"value": value}}}
        return {"claims": {"P2333": [claim(orgnr)], "P2013": [claim("bergbygg")], "P2003": [claim("shared")]}}

    def publish(self, fetcher):
        rec = make_rec()
        website._cache_social_owners = None
        with patch.object(website, "caches", FakeCaches(social=self.SOCIAL)):
            website._publish_cache_socials(rec, fetcher, time.monotonic() + 60)
        website._cache_social_owners = None
        return rec

    def test_live_wikidata_item_confirms_the_profile(self) -> None:
        fetcher = self.Fetcher(self.entity())
        rec = self.publish(fetcher)
        claims = [c for c in rec.claims if c["field"] == "social_profile"]
        self.assertEqual([c["value"]["url"] for c in claims], ["https://www.facebook.com/bergbygg"])
        ev = rec.evidence[claims[0]["evidence_ids"][0]]
        self.assertIsNotNone(ev["content_sha256"])
        self.assertIn("P2333", ev["claim_span"])
        self.assertTrue(fetcher.urls[0].startswith("https://www.wikidata.org/wiki/Special:EntityData/Q1.json"))

    def test_item_without_our_number_publishes_nothing(self) -> None:
        rec = self.publish(self.Fetcher(self.entity(orgnr=OTHER)))
        self.assertEqual([c for c in rec.claims if c["field"] == "social_profile"], [])

    def test_ended_or_deprecated_statements_are_not_current(self) -> None:
        ended = {"P582": [{"snaktype": "value", "datavalue": {"value": {"time": "+2020-01-01T00:00:00Z"}}}]}
        entity = self.entity()
        entity["claims"]["P2013"][0]["qualifiers"] = ended
        rec = self.publish(self.Fetcher(entity))
        self.assertEqual([c for c in rec.claims if c["field"] == "social_profile"], [])
        entity = self.entity()
        entity["claims"]["P2333"][0]["qualifiers"] = ended
        self.assertEqual([c for c in self.publish(self.Fetcher(entity)).claims if c["field"] == "social_profile"], [])
        entity = self.entity()
        entity["claims"]["P2333"][0]["rank"] = "deprecated"
        self.assertEqual([c for c in self.publish(self.Fetcher(entity)).claims if c["field"] == "social_profile"], [])

    def test_refused_or_absent_confirmation_publishes_nothing(self) -> None:
        rec = self.publish(self.Fetcher(refuse=True))
        self.assertEqual([c for c in rec.claims if c["field"] == "social_profile"], [])
        rec = self.publish(None)
        self.assertEqual([c for c in rec.claims if c["field"] == "social_profile"], [])


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


class LabelsAndBareNumbers(unittest.TestCase):
    def test_org_colon_and_vat_prefix_are_ownership_labels(self) -> None:
        kinds = {n: k for n, (k, _) in website.number_contexts(f"To salg & Service AS Org: {ORG[:3]} {ORG[3:6]} {ORG[6:]} | Rotsund NO{OTHER}").items()}
        self.assertEqual(kinds, {ORG: "owner", OTHER: "owner"})

    def test_bare_number_with_name_is_ours(self) -> None:
        rec = make_rec(name="AURINKO FILM AS", phone=None)
        pages = {"https://aurinkofilm.com/": html("Aurinko Film AS | Arctic Stories", f"Contact us. Aurinko Film AS {ORG}")}
        self.assertEqual([m for m, _ in run(rec, pages, guesses=["aurinkofilm.com"])], ["organisation_number_with_register_signal"])

    def test_bare_number_alone_is_not(self) -> None:
        rec = make_rec(name="AURINKO FILM AS", phone=None)
        pages = {"https://aurinkofilm.com/": html("Stories", f"Partnere: Nordlys AS {ORG}")}
        self.assertEqual(run(rec, pages, guesses=["aurinkofilm.com"]), [])


class SharedDomains(unittest.TestCase):
    def test_group_domain_with_name_in_title_is_found(self) -> None:
        rec = make_rec(name="SOLVANG ASA", phone="51 84 84 00", email="post@solvangship.no", street="Strandkaien 36", postcode="4005", town="STAVANGER")
        pages = {"https://solvangship.no/": html("Solvang ASA - shipping", "Strandkaien 36, 4005 Stavanger. Telephone (+47) 51 84 84 00")}
        out = run(rec, pages, cache=FakeCaches(shared_domains={"solvangship.no"}), guesses=[])
        self.assertEqual(out, [("name_and_register_signal", "https://solvangship.no/")])

    def test_accountant_domain_contact_match_proves_nothing(self) -> None:
        rec = make_rec(name="BERG BYGG AS", phone="22 00 11 22", email="berg@regnskap.no")
        pages = {"https://regnskap.no/": html("Regnskap", "Tlf 22 00 11 22. berg@regnskap.no. Strandgaten 1, 5013 Bergen")}
        self.assertEqual(run(rec, pages, cache=FakeCaches(shared_domains={"regnskap.no"}), guesses=[]), [])


class ServiceProviders(unittest.TestCase):
    def test_small_accountant_lending_its_contacts_is_refused(self) -> None:
        # Domain held by fewer than five entities, so the screen does not catch it.
        rec = make_rec(name="NORDLYS TRANSPORT AS", phone="22 00 11 22", email="nordlys@lilleregnskap.no")
        pages = {"https://lilleregnskap.no/": html("Lille Regnskap - regnskapsfører i Bergen", "Tlf 22 00 11 22. nordlys@lilleregnskap.no. Strandgaten 1, 5013 Bergen")}
        self.assertEqual(run(rec, pages, guesses=[]), [])


class GroupDomains(unittest.TestCase):
    def test_group_domain_is_ours_for_a_group_member(self) -> None:
        rec = make_rec(name="AF GRUPPEN ASA", email="post@afgruppen.no", group=True)
        rec.context["group_orgs"] = {ORG} | {str(911111110 + i) for i in range(20)}
        cache = FakeCaches(shared_domains={"afgruppen.no"})
        cache.holders = {"afgruppen.no": 17}
        with patch.object(website, "caches", cache):
            cands = dict(website.website_candidates(rec))
        self.assertEqual(cands["https://afgruppen.no/"], "registry_email_domain")


class RobotsSitemaps(unittest.TestCase):
    def test_sitemap_lines_are_kept_for_harvest(self) -> None:
        robots = Robots(["User-agent: *", "Disallow: /admin", "Sitemap: https://x.no/custom-map.xml", "", "sitemap: https://x.no/news.xml", "Sitemap: /relative.xml"], "KildesporBot")
        self.assertEqual(robots.site_maps(), ["https://x.no/custom-map.xml", "https://x.no/news.xml"])
        self.assertTrue(robots.allows("https://x.no/"))
        self.assertIsNone(Robots(["User-agent: *", "Disallow:"], "KildesporBot").site_maps())
        self.assertIsNone(Robots([], disallow_all=True).site_maps())


class DeclaredSiteOwnershipChanges(unittest.TestCase):
    def test_short_live_page_with_sitemap_alone_is_not_ours(self) -> None:
        rec = make_rec(name="FJORD KONSULT AS", phone=None, hjemmeside="oldsite.no")
        pages = {"https://oldsite.no/": html("Welcome", "<div id=root></div>")}
        self.assertEqual(run(rec, pages, guesses=[]), [])
        self.assertFalse(hasattr(website, "_sitemap_size"))

    def test_successor_site_under_former_name_beats_loose_name_match(self) -> None:
        # NORDLYS HOLDING AS was NORDLYS AS; the new NORDLYS AS runs nordlys.no.
        rec = make_rec(name="NORDLYS HOLDING AS", phone=None, hjemmeside="nordlys.no")
        rec.context["entity"]["historiskeNavn"] = [{"navn": "NORDLYS AS", "tilDato": "2023-05-01"}]
        pages = {"https://nordlys.no/": html("Nordlys | Elektro i Bergen", "Nordlys leverer elektrotjenester. 5013 Bergen")}
        self.assertEqual(run(rec, pages, guesses=[]), [])

    def test_holding_needs_its_whole_name_on_its_declared_site(self) -> None:
        rec = make_rec(name="NORDLYS HOLDING AS", phone=None, hjemmeside="nordlys.no")
        pages = {"https://nordlys.no/": html("Velkommen", "Nordlys leverer elektrotjenester.")}
        self.assertEqual(run(rec, pages, guesses=[]), [])

    def test_declared_site_naming_another_owner_is_refused(self) -> None:
        rec = make_rec(name="FJORD KONSULT AS", phone=None, hjemmeside="fjordkonsult.no")
        pages = {"https://fjordkonsult.no/": html("Fjord Konsult", "Fjord Konsult er nå en del av oss. © 2025 Vestland Rådgivning AS")}
        self.assertEqual(run(rec, pages, guesses=[]), [])


class JsonLdBinding(unittest.TestCase):
    CUSTOMER = '<script type="application/ld+json">{"@type":"Organization","name":"Berg Bygg AS","address":"Strandgaten 9, 5013 Bergen"}</script>'

    def test_customer_organisation_on_agency_page_proves_nothing(self) -> None:
        rec = make_rec(name="BERG BYGG AS", phone=None)
        pages = {"https://digitaltbyra.no/": html("Digitalt Byrå", "Kundecase: nettside for en tømrer. Strandgaten 9, 5013 Bergen", self.CUSTOMER)}
        self.assertEqual(run(rec, pages, guesses=["digitaltbyra.no"]), [])

    def test_publisher_organisation_on_the_site_counts(self) -> None:
        rec = make_rec(name="BERG BYGG AS", phone=None)
        ld = ('<script type="application/ld+json">{"@graph":[{"@type":"WebSite","url":"https://bb.no/","publisher":{"@id":"https://bb.no/#org"}},'
              '{"@type":"Organization","@id":"https://bb.no/#org","name":"Berg Bygg AS"}]}</script>')
        pages = {"https://bb.no/": html("Hjem", "Strandgaten 9, 5013 Bergen", ld)}
        self.assertEqual([m for m, _ in run(rec, pages, guesses=["bb.no"])], ["name_and_register_signal"])

    def test_unbound_names_are_not_identity_parts(self) -> None:
        soup = page("https://digitaltbyra.no/", html("Digitalt Byrå", "", self.CUSTOMER))[1]
        labels = [label for label, _ in website.identity_parts(soup, "", "digitaltbyra.no")]
        self.assertNotIn("JSON-LD name", labels)


class ContactsHeldByOthers(unittest.TestCase):
    def test_lent_email_domain_and_phone_do_not_prove_another_brand(self) -> None:
        # NORDLYS PRODUKSJON AS registers Solglimt's e-mail domain and its director's phone.
        rec = make_rec(name="NORDLYS PRODUKSJON AS", phone="91 22 33 44", email="kari@solglimt.no")
        pages = {"https://solglimt.no/": html("Solglimt", "Kontakt: kari@solglimt.no, 91 22 33 44, Strandgaten 1, 5013 Bergen")}
        self.assertEqual(run(rec, pages, guesses=[]), [])

    def test_phone_held_by_a_company_outside_the_group_is_shared(self) -> None:
        rec = make_rec(name="NORDLYS PRODUKSJON AS", phone="91 22 33 44")
        cache = FakeCaches()
        cache.contacts = {ORG: ("91223344", "", ""), OTHER: ("91223344", "", "")}
        pages = {"https://nordlysproduksjon.no/": html("Nordlys", "Ring 91 22 33 44")}
        self.assertEqual(run(rec, pages, cache=cache, guesses=["nordlysproduksjon.no"]), [])
        rec = make_rec(name="NORDLYS PRODUKSJON AS", phone="91 22 33 44")
        cache.contacts = {ORG: ("91223344", "", "")}
        self.assertEqual([m for m, _ in run(rec, pages, cache=cache, guesses=["nordlysproduksjon.no"])], ["register_contact_on_site"])


class HyphenatedNames(unittest.TestCase):
    def test_hyphenated_legal_names_are_one_entity_mention(self) -> None:
        parts = [("copyright", "Frøya Rør- og Sveiseservice AS"), ("title", "Nor-El AS - elektriker i Bergen")]
        self.assertEqual(website.entity_mentions(parts), ["froya ror sveiseservice", "nor el"])

    def test_declared_site_with_hyphenated_copyright_is_ours(self) -> None:
        rec = make_rec(name="FRØYA RØR OG SVEISESERVICE AS", phone=None, hjemmeside="froyaror.no")
        pages = {"https://froyaror.no/": html("Frøya Rør", "Rørlegger på Frøya. Kopirett - Frøya Rør- og Sveiseservice AS - 2023")}
        self.assertEqual(len(run(rec, pages, guesses=[])), 1)


class EmployerNames(unittest.TestCase):
    def test_sister_company_employer_is_not_ours(self) -> None:
        rec = make_rec(name="FJORDBYGG AS")
        job = {"@type": "JobPosting", "title": "Eiendomsforvalter", "hiringOrganization": {"@type": "Organization", "name": "Fjordbygg Eiendom AS"}}
        self.assertFalse(website._ld_employer_is_ours(job, rec))
        self.assertFalse(website._ld_employer_is_ours({**job, "hiringOrganization": "Fjordbygg Eiendom"}, rec))
        self.assertFalse(website._ld_org_is_ours({"@type": "Organization", "name": "Fjordbygg"}, make_rec(name="FJORDBYGG EIENDOM AS")))

    def test_exact_name_or_number_is_ours(self) -> None:
        rec = make_rec(name="FJORDBYGG AS")
        self.assertTrue(website._ld_employer_is_ours({"hiringOrganization": {"name": "Fjordbygg AS"}}, rec))
        self.assertTrue(website._ld_employer_is_ours({"hiringOrganization": {"name": "Fjordbygg Eiendom AS", "taxID": ORG}}, rec))
