"""Offline tests for the per-site harvest (kildespor/harvest.py): no network.

Run with ``python -m unittest tests.test_harvest``.
"""
from __future__ import annotations

import hashlib
import json
import time
import unittest
from datetime import date, timedelta
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from kildespor import harvest
from kildespor.http import Snapshot
from kildespor.model import CompanyRecord
from kildespor.website import SiteVisit, parse

TODAY = date.today()
RECENT = (TODAY - timedelta(days=20)).isoformat()
OLDER = (TODAY - timedelta(days=60)).isoformat()
ANCIENT = (TODAY - timedelta(days=1200)).isoformat()


def snap(url: str, body: str, ctype: str = "text/html; charset=utf-8", status: int = 200) -> Snapshot:
    data = body.encode("utf-8")
    return Snapshot(requested_url=url, final_url=url, status=status, retrieved_at="2026-10-17T00:00:00Z", sha256=hashlib.sha256(data).hexdigest(), content_type=ctype, body=data)


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def record(name: str = "FJORDBYGG AS", org: str = "923609016") -> CompanyRecord:
    rec = CompanyRecord(org)
    rec.context["entity"] = {"navn": name}
    return rec


class SocialTests(unittest.TestCase):
    def test_canonical_forms(self) -> None:
        cp = harvest.canonical_profile
        self.assertEqual(cp("https://www.facebook.com/FjordBygg/?ref=footer"), ("facebook", "https://facebook.com/FjordBygg", "FjordBygg"))
        self.assertEqual(cp("https://m.facebook.com/profile.php?id=100064123456789&ref=x")[1], "https://facebook.com/profile.php?id=100064123456789")
        self.assertEqual(cp("https://no.linkedin.com/company/fjordbygg-as/about/")[1], "https://linkedin.com/company/fjordbygg-as")
        self.assertEqual(cp("https://www.youtube.com/@fjordbygg/videos")[1], "https://youtube.com/@fjordbygg")
        self.assertEqual(cp("https://twitter.com/fjordbygg")[1], "https://x.com/fjordbygg")
        self.assertEqual(cp("https://www.tiktok.com/@fjordbygg")[1], "https://tiktok.com/@fjordbygg")
        self.assertEqual(cp("https://www.instagram.com/fjordbygg/")[1], "https://instagram.com/fjordbygg")
        self.assertEqual(cp("https://no.pinterest.com/fjordbygg/")[1], "https://pinterest.com/fjordbygg")
        self.assertEqual(cp("https://vimeo.com/fjordbygg")[1], "https://vimeo.com/fjordbygg")
        self.assertEqual(cp("https://www.threads.net/@fjordbygg")[1], "https://threads.net/@fjordbygg")
        self.assertEqual(cp("https://github.com/fjordbygg")[1], "https://github.com/fjordbygg")
        self.assertEqual(cp("https://www.facebook.com/pages/category/Construction-Company/Fjordbygg-AS-104512345678901/")[2], "pages/category/Construction-Company/Fjordbygg-AS-104512345678901")
        self.assertEqual(cp("https://linkedin.com/company/the-well---en-verden-av-velv%C3%A6re")[1], "https://linkedin.com/company/the-well---en-verden-av-velv%C3%A6re")

    def test_non_profiles_rejected(self) -> None:
        cp = harvest.canonical_profile
        for url in (
            "https://www.facebook.com/sharer/sharer.php?u=https://fjordbygg.no",
            "https://www.facebook.com/plugins/page.php?href=x",
            "https://developers.facebook.com/fjordbygg",
            "https://www.linkedin.com/in/kari-nordmann",
            "https://www.linkedin.com/shareArticle?mini=true",
            "https://twitter.com/intent/tweet?text=x",
            "https://twitter.com/fjordbygg/status/123",
            "https://www.youtube.com/watch?v=abc",
            "https://youtu.be/abc",
            "https://www.youtube.com/embed/abc",
            "https://www.instagram.com/p/Cx123/",
            "https://vimeo.com/123456789",
            "https://player.vimeo.com/video/1",
            "https://www.facebook.com/wix",
            "https://www.facebook.com/123456789/posts/1",
            "https://github.com/fjordbygg/website",
        ):
            self.assertIsNone(cp(url), url)

    def test_profiles_on_page_sources_and_ownership(self) -> None:
        rec = record()
        html = """<html><head><script type="application/ld+json">{"@type":"Organization","name":"Fjordbygg","sameAs":["https://www.instagram.com/fjordbygg_as/","https://www.linkedin.com/company/12345678/"]}</script>
        <script>window.__DATA__={"social":{"tiktok":"https:\\/\\/www.tiktok.com\\/@fjordbygg","other":"https:\\/\\/www.facebook.com\\/somepartner"}}</script></head>
        <body><footer>
          <a href="https://www.facebook.com/fjordbyggas">Facebook</a>
          <a href="https://www.facebook.com/sharer/sharer.php?u=x">Del</a>
          <a href="https://www.linkedin.com/in/ola-nordmann">Ola</a>
          <a href="https://www.facebook.com/bygghandelen">Vår leverandør</a>
          <p>Nettside levert av <a href="https://www.facebook.com/webbyraaet">Byrå</a></p>
          <div class="fb-page" data-href="https://www.facebook.com/fjordbygg.bergen"></div>
          <iframe src="https://www.facebook.com/plugins/page.php?href=https%3A%2F%2Fwww.facebook.com%2Ffjordbygg.stavanger&tabs=timeline"></iframe>
          <a href="https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv">YouTube</a>
        </footer></body></html>"""
        page = snap("https://www.fjordbygg.no/", html)
        contains, prefixes = harvest.identity_tokens(["FJORDBYGG AS"], page.final_url)
        found = {f.url: f.via for f in harvest.profiles_on_page(page, soup_of(html), html, contains, prefixes, rec)}
        self.assertEqual(found.get("https://facebook.com/fjordbyggas"), "anchor")
        self.assertEqual(found.get("https://facebook.com/fjordbygg.bergen"), "data_href")
        self.assertEqual(found.get("https://facebook.com/fjordbygg.stavanger"), "embed_iframe")
        self.assertEqual(found.get("https://instagram.com/fjordbygg_as"), "json_ld_sameAs")
        self.assertEqual(found.get("https://linkedin.com/company/12345678"), "json_ld_sameAs")  # id-only account
        self.assertEqual(found.get("https://tiktok.com/@fjordbygg"), "script_data")
        self.assertIn("https://youtube.com/channel/UCabcdefghijklmnopqrstuv", found)
        self.assertNotIn("https://facebook.com/bygghandelen", found)  # another company's page
        self.assertNotIn("https://facebook.com/webbyraaet", found)  # the web agency's credit
        self.assertNotIn("https://facebook.com/somepartner", found)
        self.assertFalse(any("linkedin.com/in" in u for u in found))

    def _profiles(self, html: str, names: list[str] = ["FJORDBYGG AS"]) -> dict[str, str]:
        rec = record(names[0])
        page = snap("https://www.fjordbygg.no/", html)
        contains, prefixes = harvest.identity_tokens(names, page.final_url)
        own_names = [" ".join(harvest._fold_a(t) for t in harvest.name_tokens(n)) for n in names]
        own_words = {"fjordbygg"}
        return {f.url: f.via for f in harvest.profiles_on_page(page, soup_of(html), html, contains, prefixes, rec, "fjordbygg as", own_names, own_words)}

    def test_sister_company_link_is_not_ours(self) -> None:
        """Codex review rc1 finding 4: a link labelled as a sister company, or an account that is our
        name plus another entity's word, is not the company's profile."""
        html = """<html><body><main><p>Vårt søsterselskap: <a href="https://www.linkedin.com/company/fjordbygg-utleie/">Fjordbygg Utleie</a></p>
        <div><span>Morselskap</span> <a href="https://www.facebook.com/fjordbyggkonsernet">Fjordbygg-konsernet</a></div></main>
        <footer><a href="https://www.linkedin.com/company/fjordbygg/">LinkedIn</a>
        <a href="https://www.facebook.com/fjordbygg.eiendom">Facebook</a></footer></body></html>"""
        found = self._profiles(html)
        self.assertEqual(list(found), ["https://linkedin.com/company/fjordbygg"])

    def test_former_name_never_approves_a_profile(self) -> None:
        """Codex review rc2 N1: Nordlys Produksjon AS was Solglimt AS; facebook.com/solglimt on its
        site may be the successor's, so it is not published. Its current-name account is."""
        home_html = """<html><head><title>Nordlys Produksjon</title></head><body><footer>
        <a href="https://www.facebook.com/solglimt">Facebook</a><a href="https://www.instagram.com/nordlysproduksjon">Instagram</a>
        Org.nr 923 609 016</footer></body></html>"""
        fetcher = FakeFetcher({}, robots="User-agent: *\nAllow: /\n")
        rec = record("NORDLYS PRODUKSJON AS")
        rec.context["entity"]["historiskeNavn"] = [{"navn": "SOLGLIMT AS", "fraDato": "2015-01-01", "tilDato": "2024-01-01"}]
        visit = SiteVisit(fetcher, rec, time.monotonic() + 60)  # type: ignore[arg-type]
        home = snap("https://www.fjordbygg.no/", home_html)
        soup, _html, text = parse(home)
        visit.pages.append((home, soup, text))
        harvest._extract_site_facts(rec, visit, home, True, None)
        self.assertEqual([c["value"]["url"] for c in rec.claims if c["field"] == "social_profile"], ["https://instagram.com/nordlysproduksjon"])

    def test_id_only_accounts_need_the_site_social_block(self) -> None:
        """Codex review rc1 finding 4: a numeric Facebook/YouTube account in body content is not
        published; in the footer social block or the site's own Organization sameAs it is."""
        html = """<html><head><script type="application/ld+json">{"@type":"Organization","name":"Fjordbygg AS","sameAs":["https://www.linkedin.com/company/7654321/"]}</script>
        <script type="application/ld+json">{"@type":"WebPage","name":"Fjordbygg AS","sameAs":["https://www.facebook.com/profile.php?id=100099999999999"]}</script></head>
        <body><article><p>Les om prosjektet <a href="https://www.facebook.com/profile.php?id=100012345678901">her</a> og
        <a href="https://www.youtube.com/channel/UCbodybodybodybodybody12">se videoen</a>.</p></article>
        <div class="social-icons"><a href="https://www.facebook.com/profile.php?id=100055555555555">Facebook</a></div></body></html>"""
        found = self._profiles(html)
        self.assertIn("https://facebook.com/profile.php?id=100055555555555", found)
        self.assertIn("https://linkedin.com/company/7654321", found)
        self.assertNotIn("https://facebook.com/profile.php?id=100012345678901", found)
        self.assertNotIn("https://youtube.com/channel/UCbodybodybodybodybody12", found)
        self.assertNotIn("https://facebook.com/profile.php?id=100099999999999", found)

    def test_page_title_words_are_not_identity_tokens(self) -> None:
        """Audit 23 Oct R1: a brand from the <title> or og:site_name counts only when it shares a word
        with the legal name or the domain, and page words (Hjem, Forside, Home, Nettbutikk) never do."""
        ib = harvest.identity_brands
        self.assertEqual(ib(["Hjem", "Forside", "Home page", "Nettbutikk"], ["NORDIC SUPPLY PARTNER AS"], "https://nsp.no/"), [])
        self.assertEqual(ib(["Utleiemegler Oslo"], ["ENKLERE UTLEIE AS"], "https://enklereutleie.no/"), [])
        self.assertEqual(ib(["Bruktbilforhandler i Mosjøen"], ["AUTONEXT AS"], "https://www.auto-next.no/"), [])
        self.assertEqual(ib(["Hexagon Ragasco"], ["RAGASCO AS"], "https://www.ragasco.com/"), ["Hexagon Ragasco"])
        self.assertEqual(ib(["Kviltorp Camping"], ["NAF CAMP MOLDE AS"], "https://kviltorpcamping.no/"), ["Kviltorp Camping"])
        self.assertEqual(ib(["Hjem | Blomsterdekor"], ["BLOMSTERDEKOR BERGEN AS"], "https://blomsterdekor.no/"), ["Blomsterdekor"])
        contains, prefixes = harvest.identity_tokens(["NORDIC SUPPLY PARTNER AS"], "https://nsp.no/", brands=["Hjem"])
        self.assertFalse(harvest.resembles("hjemmesideproffen", contains, prefixes))
        home_html = """<html><head><title>Hjem - Blomsterdekor Bergen</title></head><body><footer>
        <a href="https://www.facebook.com/blomsterdekortaarnplass">Facebook</a><a href="https://www.facebook.com/hjemmesideproffen">Facebook</a>
        </footer></body></html>"""
        rec = record("BLOMSTERDEKOR BERGEN AS")
        visit = SiteVisit(FakeFetcher({}, robots="User-agent: *\\nAllow: /\\n"), rec, time.monotonic() + 60)  # type: ignore[arg-type]
        home = snap("https://www.blomsterdekor.no/", home_html)
        soup, _html, text = parse(home)
        visit.pages.append((home, soup, text))
        harvest._extract_site_facts(rec, visit, home, True, None)
        self.assertEqual([c["value"]["url"] for c in rec.claims if c["field"] == "social_profile"], ["https://facebook.com/blomsterdekortaarnplass"])

    def test_place_word_alone_is_not_a_match(self) -> None:
        """Audit 23 Oct R5: Klinikk Trondheim's "trondheim" must not admit visittrondheim or a
        Trondheim web agency; EiehjelpenOslo passed for Enklere Utleie only on "oslo"."""
        contains, prefixes = harvest.identity_tokens(["KLINIKK TRONDHEIM AS"], "https://klinikktrondheim.no/", place_tokens=False)
        self.assertNotIn("trondheim", contains)
        self.assertFalse(harvest.resembles("visittrondheim", contains, prefixes))
        self.assertFalse(harvest.resembles("trondheimwebbyra", contains, prefixes))
        self.assertTrue(harvest.resembles("klinikktrondheim", contains, prefixes))
        contains, prefixes = harvest.identity_tokens(["ENKLERE UTLEIE AS"], "https://enklereutleie.no/", brands=["Utleiemegler Oslo", "Oslo"], place_tokens=False)
        self.assertFalse(harvest.owned_profile("EiehjelpenOslo", contains, prefixes))
        contains, prefixes = harvest.identity_tokens(["SPORTSPROFIL NORGE AS"], "https://sportsprofil.no/", place_tokens=False)
        self.assertTrue(harvest.resembles("sportsprofil", contains, prefixes))
        self.assertFalse(harvest.resembles("visitnorge", contains, prefixes))

    def test_own_regional_and_group_accounts_stay(self) -> None:
        contains, prefixes = harvest.identity_tokens(["CONSTO AS"], "https://consto.no/")
        self.assertFalse(harvest.names_other_company("constokonsern", contains, prefixes, {"consto"}))
        contains, prefixes = harvest.identity_tokens(["PACHAMAMA AS"], "https://pachamama.no/")
        self.assertFalse(harvest.names_other_company("pachamamaoslo", contains, prefixes, {"pachamama"}))
        contains, prefixes = harvest.identity_tokens(["FJORDBYGG EIENDOM AS"], "https://fjordbygg.no/")
        self.assertFalse(harvest.names_other_company("fjordbyggeiendom", contains, prefixes, {"fjordbygg", "eiendom"}))

    def test_sister_company_abroad_is_not_ours(self) -> None:
        contains, prefixes = harvest.identity_tokens(["HEAD ENERGY NORGRID AS", "Head Energy"], "https://www.headenergy.no/")
        own = "head energy norgrid as head energy"
        self.assertFalse(harvest.owned_profile("headenergysweden", contains, prefixes, own_text=own))
        self.assertFalse(harvest.owned_profile("company/headenergydanmarkas", contains, prefixes, own_text=own))
        self.assertFalse(harvest.owned_profile("headenergyse", contains, prefixes, own_text=own))
        self.assertTrue(harvest.owned_profile("company/head-energy-norgrid", contains, prefixes, own_text=own))
        self.assertTrue(harvest.owned_profile("headenergy", contains, prefixes, own_text=own))
        swe = harvest.identity_tokens(["NORDIC SWEDEN AS"], "https://nordicsweden.no/")
        self.assertTrue(harvest.owned_profile("nordicsweden", *swe, own_text="nordic sweden as"))

    def test_resemblance_folds_norwegian_letters(self) -> None:
        contains, prefixes = harvest.identity_tokens(["Bjørkås Rørleggerservice AS"], "https://bjorkas.no/")
        self.assertTrue(harvest.resembles("bjorkasror", contains, prefixes))
        self.assertTrue(harvest.resembles("bjoerkaasroerleggerservice", contains, prefixes))
        self.assertFalse(harvest.resembles("elektroimport", contains, prefixes))


class CareersTests(unittest.TestCase):
    SITE = "fjordbygg.no"

    def test_careers_links(self) -> None:
        f = harvest.is_careers_link
        self.assertEqual(f("https://www.fjordbygg.no/karriere", "Karriere", self.SITE), 0)
        self.assertEqual(f("https://www.fjordbygg.no/om-oss/ledige-stillinger/", "Jobb hos oss", self.SITE), 0)
        self.assertEqual(f("https://karriere.fjordbygg.no/", "", self.SITE), 0)
        self.assertEqual(f("https://02365.no/jobb-som-sjafor/", "", "02365.no"), 0)
        self.assertEqual(f("https://abchar.no/bli-med-paa-laget", "", "abchar.no"), 0)
        self.assertIsNone(f("https://freedomfromfear.no/bli-med-meg-til-det-hellige-fjell", "", "freedomfromfear.no"))
        self.assertIsNone(f("https://www.narvikgolf.no/bli-medlem", "Bli medlem", "narvikgolf.no"))
        self.assertEqual(f("https://www.fjordbygg.no/page/42", "Ledige stillinger", self.SITE), 2)
        self.assertEqual(f("https://fjordbygg.teamtailor.com/jobs", "Se stillinger", self.SITE), 1)
        self.assertEqual(f("https://www.finn.no/job/fulltime/ad.html?finnkode=123456", "", self.SITE), 1)
        # A blog post that mentions work is not a careers page (spedify-lanserer-reparasjonstjenester).
        self.assertIsNone(f("https://spedify.no/spedify-lanserer-reparasjonstjenester", "Spedify lanserer reparasjonstjenester - nå jobber vi med", "spedify.no"))
        self.assertIsNone(f("https://www.fjordbygg.no/nyheter/vi-jobber-med-baerekraft", "Slik jobber vi med bærekraft", self.SITE))
        self.assertIsNone(f("https://www.teamtailor.com/", "Powered by Teamtailor", self.SITE))
        self.assertIsNone(f("https://www.finn.no/", "Finn", self.SITE))

    def test_more_careers_link_forms(self) -> None:
        f = harvest.is_careers_link
        self.assertEqual(f("https://teknomed.no/stilling-ledig/", "Stilling ledig", "teknomed.no"), 0)
        self.assertEqual(f("http://www.bygdesnekkern.no/ansatte/", "Jobb hos oss", "bygdesnekkern.no"), 2)  # the page must read as careers
        self.assertIsNone(f("https://www.fjordbygg.no/blogg/jobb-hos-oss", "Jobb hos oss", self.SITE))
        tokens, _ = harvest.identity_tokens(["WORK SYSTEM NORWAY AS"], "https://www.worksystem.no/")
        self.assertEqual(f("https://talangpunkten.se/career/work-system/lang/no", "Karriere", "worksystem.no", tokens), 1)
        self.assertIsNone(f("https://talangpunkten.se/career/annet-firma/", "Karriere", "worksystem.no", tokens))
        self.assertIsNone(f("https://talangpunkten.se/career/work-system/", "Les mer", "worksystem.no", tokens))
        self.assertIsNone(f("https://www.facebook.com/worksystem/jobs", "Karriere", "worksystem.no", tokens))

    def test_roles_on_own_careers_page(self) -> None:
        rec = record()
        future = (TODAY + timedelta(days=30)).strftime("%d.%m.%Y")
        past = (TODAY - timedelta(days=3)).strftime("%d.%m.%Y")
        html = f"""<html><head><title>Ledige stillinger - Fjordbygg</title>
        <script type="application/ld+json">{{"@type":"JobPosting","title":"Prosjektleder","datePosted":"{RECENT}","validThrough":"{(TODAY + timedelta(days=10)).isoformat()}","hiringOrganization":{{"@type":"Organization","name":"Fjordbygg AS"}},"jobLocation":{{"@type":"Place","address":{{"addressLocality":"Bergen"}}}}}}</script>
        </head><body><h1>Ledige stillinger</h1><ul>
          <li><a href="/karriere/stilling/tomrer-bergen"><h3>Tømrer</h3></a><p>Søknadsfrist: {future}</p><p>Sted: Bergen</p></li>
          <li><a href="/karriere/stilling/laerling-2026">Lærling tømrerfaget</a></li>
          <li><a href="/karriere/stilling/gammel">Gammel stilling</a><span>Søknadsfrist {past}</span></li>
          <li><a href="/karriere/team/mot-anna">Møt Anna, prosjektleder</a></li>
          <li><a href="/personvern">Personvern</a></li>
        </ul></body></html>"""
        page = snap("https://www.fjordbygg.no/karriere/", html)
        roles = harvest.roles_on_page(page, soup_of(html), rec, own_site=True, tenant_ok=True)
        titles = {r.title: r for r in roles}
        self.assertIn("Prosjektleder", titles)
        self.assertEqual(titles["Prosjektleder"].location, "Bergen")
        self.assertIn("Tømrer", titles)
        self.assertEqual(titles["Tømrer"].url, "https://www.fjordbygg.no/karriere/stilling/tomrer-bergen")
        self.assertEqual(titles["Tømrer"].location, "Bergen")
        self.assertIn("Lærling tømrerfaget", titles)
        self.assertNotIn("Gammel stilling", titles)  # deadline passed
        self.assertFalse(any("Anna" in t for t in titles))

    def test_card_anchor_title_is_its_first_line(self) -> None:
        html = """<html><body><h1>Ledige stillinger</h1>
        <a class="vacancy-card" href="/karriere/ledige-stillinger/2026/10/planleggingsleder/"><div><p class="vacancy-card__title">Planleggingsleder</p>
        <ul><li><span>Råde</span></li><li><span>Bygg, AF Bygg Øst</span></li><li><span>14.11.2026</span></li></ul></div></a>
        <a href="/karriere/ledige-stillinger/2026/10/bas/"><div><p>BAS for rørleggerfaget</p><p>Oslo</p></div></a></body></html>"""
        page = snap("https://www.afgruppen.no/karriere/ledige-stillinger/", html)
        roles = harvest.roles_on_page(page, soup_of(html), record("AF GRUPPEN ASA"), own_site=True, tenant_ok=True)
        self.assertEqual([r.title for r in roles], ["Planleggingsleder", "BAS for rørleggerfaget"])

    def test_rejected_posting_is_not_readded_by_its_anchor(self) -> None:
        """Codex review rc1 finding 5: a JobPosting naming another employer, or one already expired,
        is never republished from its anchor, in this pass or a later one."""
        html = """<html><head><script type="application/ld+json">{"@type":"JobPosting","title":"Sjåfør","url":"/karriere/stilling/123456-sjafor","hiringOrganization":{"@type":"Organization","name":"Annet Firma AS"}}</script></head>
        <body><h1>Ledige stillinger</h1><a href="/karriere/stilling/123456-sjafor">Sjåfør</a><a href="/karriere/stilling/123457-lager">Lagermedarbeider</a></body></html>"""
        page = snap("https://www.fjordbygg.no/karriere/", html)
        rejected: set[str] = set()
        self.assertEqual(harvest.roles_on_page(page, soup_of(html), record(), own_site=True, tenant_ok=True, rejected=rejected), [])
        self.assertIn("fjordbygg.no/karriere/stilling/123456-sjafor", rejected)
        later = '<html><body><a href="/karriere/stilling/123456-sjafor">Sjåfør</a><a href="/karriere/stilling/9-tomrer">Tømrer</a><a href="/karriere/stilling/8-maler">Maler</a></body></html>'
        roles = harvest.roles_on_page(snap("https://www.fjordbygg.no/karriere/alle", later), soup_of(later), record(), own_site=True, tenant_ok=True, rejected=rejected)
        self.assertEqual([r.title for r in roles], ["Tømrer", "Maler"])

    def test_anchor_starting_with_a_refused_title_is_refused(self) -> None:
        """Codex review rc2, finding 5 partial: an expired posting with no url, title "Snekker",
        does not come back through an anchor "Snekker – Bergen"."""
        html = """<html><head><script type="application/ld+json">{"@type":"JobPosting","title":"Snekker","validThrough":"2020-01-01","hiringOrganization":{"@type":"Organization","name":"Fjordbygg AS"}}</script></head>
        <body><a href="/jobb/stilling/11-snekker-bergen">Snekker – Bergen</a><a href="/jobb/stilling/12-snekkerlaerling">Snekkerlærling</a><a href="/jobb/stilling/13-maler">Maler</a></body></html>"""
        roles = harvest.roles_on_page(snap("https://www.fjordbygg.no/jobb/", html), soup_of(html), record(), own_site=True, tenant_ok=True, rejected=set())
        self.assertEqual([r.title for r in roles], ["Snekkerlærling", "Maler"])

    def test_page_naming_another_employer_lends_anchors_nothing(self) -> None:
        html = """<html><head><script type="application/ld+json">{"@type":"JobPosting","title":"Sjåfør","hiringOrganization":{"@type":"Organization","name":"Annet Firma AS"}}</script></head>
        <body><a href="/karriere/stilling/2-tomrer">Tømrer</a><a href="/karriere/stilling/3-maler">Maler</a></body></html>"""
        roles = harvest.roles_on_page(snap("https://www.fjordbygg.no/karriere/", html), soup_of(html), record(), own_site=True, tenant_ok=True, rejected=set())
        self.assertEqual(roles, [])

    def test_expired_posting_is_not_readded(self) -> None:
        html = """<html><head><script type="application/ld+json">{"@type":"JobPosting","title":"Prosjektleder","url":"/jobb/stilling/1-prosjektleder","validThrough":"2020-01-01","hiringOrganization":{"@type":"Organization","name":"Fjordbygg AS"}}</script></head>
        <body><a href="/jobb/stilling/1-prosjektleder">Prosjektleder</a><a href="/jobb/stilling/2-tomrer">Tømrer</a><a href="/jobb/stilling/3-maler">Maler</a></body></html>"""
        roles = harvest.roles_on_page(snap("https://www.fjordbygg.no/jobb/", html), soup_of(html), record(), own_site=True, tenant_ok=True, rejected=set())
        self.assertEqual([r.title for r in roles], ["Tømrer", "Maler"])

    def test_open_applications_and_host_digits_are_not_roles(self) -> None:
        html = """<html><body><h1>Ledige stillinger</h1>
        <a href="/ledige-stillinger/spontansoknad/">Spontansøknad</a>
        <a href="/ledige-stillinger/karriere-instalco/">Hva vil det si å jobbe i et Instalco-selskap?</a>
        <a href="/ledige-stillinger/123456-rorlegger">Rørlegger</a><a href="/ledige-stillinger/123457-servicetekniker">Servicetekniker</a></body></html>"""
        roles = harvest.roles_on_page(snap("https://total-vvs.no/ledige-stillinger/", html), soup_of(html), record("TOTAL VVS AS"), own_site=True, tenant_ok=True, rejected=set())
        self.assertEqual([r.title for r in roles], ["Rørlegger", "Servicetekniker"])
        taxi = '<html><body><a href="https://02365.no/bedrifter/">Bedrifter</a><a href="https://02365.no/bestilling/">Bestilling</a><a href="https://02365.no/tjenester/">Tjenester</a></body></html>'
        self.assertEqual(harvest.roles_on_page(snap("https://02365.no/jobb-som-sjafor/", taxi), soup_of(taxi), record("CHRISTIANIA TAXI AS"), own_site=True, tenant_ok=True, rejected=set()), [])

    def test_single_team_link_is_not_a_role(self) -> None:
        html = '<html><body><h1>Karriere</h1><a href="/karriere/stilling/historien-om-oss">Historien om oss</a></body></html>'
        page = snap("https://www.fjordbygg.no/karriere/", html)
        self.assertEqual(harvest.roles_on_page(page, soup_of(html), record(), own_site=True, tenant_ok=True), [])

    def test_vacant_premises_and_menu_links_are_not_roles(self) -> None:
        html = """<html><body><nav class="mega-menu"><a href="/eiendom/ledige-lokaler/leie-kontor-i-bergen">Leie kontor i Bergen</a>
        <a href="/eiendom/ledige-lokaler/leie-kontor-i-oslo">Leie kontor i Oslo</a></nav><h1>Jobb hos oss</h1>
        <a href="/jobb/stilling/123456-okonomisjef">Økonomisjef</a><a href="/jobb/stilling/123457-jurist">Jurist</a>
        <a href="https://www.entra.no/meny/stilling/9999999-fra-menyen">Fra menyen</a></body></html>"""
        page = snap("https://www.entra.no/om-entra/jobb-hos-entra", html)
        menu = {"entra.no/meny/stilling/9999999-fra-menyen"}
        roles = harvest.roles_on_page(page, soup_of(html), record("ENTRA ASA"), own_site=True, tenant_ok=True, site_wide=menu)
        self.assertEqual([r.title for r in roles], ["Økonomisjef", "Jurist"])

    def test_job_feed_and_tracking_params(self) -> None:
        rss = f"""<rss xmlns:tt="https://teamtailor.com/locations"><channel><title>Fjordbygg</title>
        <item><title>Tømrer</title><link>https://karriere.fjordbygg.no/jobs/123-tomrer</link><pubDate>{date.fromisoformat(RECENT).strftime('%a, %d %b %Y')} 08:00:00 +0000</pubDate><tt:locations><tt:location><tt:city>Bergen</tt:city></tt:location></tt:locations></item>
        </channel></rss>"""
        roles = harvest.rss_roles(snap("https://karriere.fjordbygg.no/jobs.rss", rss, "application/rss+xml"), "https://karriere.fjordbygg.no/")
        self.assertEqual((roles[0].title, roles[0].location), ("Tømrer", "Bergen"))
        self.assertEqual(harvest._clean_url("https://jobs.zaptec.com/?_gl=1*ys5oo6&_ga=2.81&lang=no#top"), "https://jobs.zaptec.com/?lang=no")

    def test_foreign_subsidiary_roles(self) -> None:
        self.assertTrue(harvest.foreign_page("https://career-lithuania.kitron.com/jobs/8313064-x", "kitron asa"))
        self.assertTrue(harvest.foreign_page("https://career-denmark.kitron.com/jobs/8450175-procestekniker", "kitron asa"))
        self.assertFalse(harvest.foreign_page("https://career.kitron.com/jobs/1-operator-arendal", "kitron asa"))
        self.assertFalse(harvest.foreign_page("https://www.strongpoint.com/events/join-baltic-retail-forum-in-vilnius/", "strongpoint asa"))

    def test_ats_tenant(self) -> None:
        self.assertEqual(harvest.ats_tenant("https://fjordbygg.teamtailor.com/jobs"), "fjordbygg")
        self.assertEqual(harvest.ats_tenant("https://jobs.lever.co/fjordbygg/"), "fjordbygg")
        self.assertEqual(harvest.ats_tenant("https://candidate.hr-manager.net/vacancies/list.aspx?customer=fjordbygg"), "fjordbygg")
        self.assertEqual(harvest.ats_tenant("https://equinor.wd3.myworkdayjobs.com/EQNR"), "equinor")

    def test_ats_page_of_other_tenant_yields_no_roles(self) -> None:
        html = '<html><body><a href="https://andre.teamtailor.com/jobs/123456-sjafor">Sjåfør</a><a href="https://andre.teamtailor.com/jobs/123457-lager">Lagermedarbeider</a></body></html>'
        page = snap("https://andre.teamtailor.com/jobs", html)
        self.assertEqual(harvest.roles_on_page(page, soup_of(html), record(), own_site=False, tenant_ok=False), [])
        roles = harvest.roles_on_page(page, soup_of(html), record(), own_site=False, tenant_ok=True)
        self.assertEqual([r.title for r in roles], ["Sjåfør", "Lagermedarbeider"])


class NewsTests(unittest.TestCase):
    SITE = "fjordbygg.no"

    def test_dates(self) -> None:
        self.assertEqual(harvest.text_date("Publisert 14. oktober 2026 av Kari"), "2026-10-14")
        self.assertEqual(harvest.text_date("14.10.2026"), "2026-10-14")
        self.assertEqual(harvest.text_date("October 14, 2026"), "2026-10-14")
        self.assertEqual(harvest.stamp("2025-09-22T20:00:00+02:00"), ("2025-09-22T20:00:00+02:00", "2025-09-22"))
        self.assertEqual(harvest.stamp("Mon, 22 Sep 2025 18:00:00 +0000"), ("2025-09-22T20:00:00+02:00", "2025-09-22"))
        self.assertEqual(harvest.stamp("2025-09-22"), ("2025-09-22", "2025-09-22"))

    def test_listing_items(self) -> None:
        d1 = date.fromisoformat(RECENT)
        html = f"""<html><body><nav><ul><li><a href="/nyheter/">Nyheter</a></li></ul></nav><main>
          <article><h2><a href="/nyheter/fjordbygg-vinner-kontrakt">Fjordbygg vinner stor kontrakt i Bergen</a></h2><time datetime="{RECENT}T09:30:00+02:00">{RECENT}</time></article>
          <div class="news-card"><a href="/nyheter/ny-avdeling-pa-voss"><h3>Ny avdeling åpner på Voss</h3></a><span>{d1.day}. {['januar','februar','mars','april','mai','juni','juli','august','september','oktober','november','desember'][d1.month-1]} {d1.year}</span></div>
          <article><h2><a href="https://e24.no/annen-sak">Annet selskap i nyhetene</a></h2><time datetime="{RECENT}">x</time></article>
          <article><h2><a href="/nyheter/kategori/bygg">Kategori bygg og anlegg</a></h2><time datetime="{RECENT}">x</time></article>
        </main></body></html>"""
        page = snap("https://www.fjordbygg.no/nyheter/", html)
        items = {i.title: i for i in harvest.listing_items(page, soup_of(html), self.SITE, require_news_path=False)}
        self.assertEqual(items["Fjordbygg vinner stor kontrakt i Bergen"].published, f"{RECENT}T09:30:00+02:00")
        self.assertEqual(items["Ny avdeling åpner på Voss"].date, RECENT)
        self.assertNotIn("Annet selskap i nyhetene", items)  # another site's story
        self.assertEqual(len(items), 2)

    def test_article_meta(self) -> None:
        html = """<html><head><title>Bedre sosial funksjon etter hjerneskade - Sunnaas sykehus</title>
        <meta property="article:published_time" content="2025-09-22T20:00:00+02:00"></head>
        <body><article><h1>Bedre sosial funksjon etter hjerneskade</h1><p>Tekst</p></article></body></html>"""
        page = snap("https://www.sunnaas.no/fag-og-forskning/nyheter-rkr/bedre-sosial-funksjon", html)
        item = harvest.article_meta(page, soup_of(html), ["Sunnaas sykehus"])
        self.assertEqual((item.title, item.published), ("Bedre sosial funksjon etter hjerneskade", "2025-09-22T20:00:00+02:00"))

    def test_article_title_skips_a_section_banner(self) -> None:
        html = """<html><head><title>Spedify lanserer reparasjonstjenester</title><meta property="og:title" content="Spedify lanserer reparasjonstjenester">
        <meta property="article:published_time" content="2026-09-01T10:00:00+02:00"></head>
        <body><h1>Nyheter, innsikt og erfaringer fra drift, fulfillment og vareflyt</h1></body></html>"""
        item = harvest.article_meta(snap("https://spedify.no/spedify-3pl-blogg/spedify-lanserer-reparasjonstjenester", html), soup_of(html), ["Spedify"])
        self.assertEqual(item.title, "Spedify lanserer reparasjonstjenester")

    def test_feed_and_wordpress(self) -> None:
        d = date.fromisoformat(RECENT)
        rss = f"""<?xml version="1.0"?><rss><channel><title>Fjordbygg</title>
        <item><title><![CDATA[Fjordbygg &amp; partnere bygger skole]]></title><link>https://www.fjordbygg.no/fjordbygg-bygger-skole/</link><pubDate>{d.strftime('%a, %d %b %Y')} 08:00:00 +0000</pubDate></item>
        <item><title>Gammel nyhet fra lenge siden</title><link>https://www.fjordbygg.no/gammel/</link><pubDate>Mon, 01 Jan 2018 08:00:00 +0000</pubDate></item>
        </channel></rss>"""
        items = harvest.feed_items(snap("https://www.fjordbygg.no/feed/", rss, "application/rss+xml"), self.SITE)
        self.assertEqual(items[0].title, "Fjordbygg & partnere bygger skole")
        self.assertTrue(items[0].published.startswith(RECENT + "T1"))
        posts = json.dumps([{"date": f"{RECENT}T10:00:00", "date_gmt": f"{RECENT}T08:00:00", "link": "https://www.fjordbygg.no/ny-kontrakt/", "title": {"rendered": "Ny kontrakt med Bergen kommune"}}])
        wp = harvest.wordpress_items(snap("https://www.fjordbygg.no/wp-json/wp/v2/posts", posts, "application/json"), self.SITE)
        self.assertEqual(wp[0].published, f"{RECENT}T10:00:00+02:00")

    def test_shop_pages_are_not_news(self) -> None:
        self.assertTrue(harvest.NOT_NEWS_PATH.search("/nettbutikk/74-hjem-fritid/2702-brannslukker"))
        self.assertTrue(harvest.NOT_NEWS_PATH.search("/blog/category/tips"))
        self.assertIsNone(harvest.NOT_NEWS_PATH.search("/nyheter/fjordbygg-vinner-kontrakt"))
        self.assertTrue(harvest.NOT_NEWS_PATH.search("/jobs/8432212-warehouse-lead"))
        self.assertTrue(harvest.NOT_NEWS_PATH.search("/oppskrifter/salater/proteinrik-pastasalat"))

    def test_sitemap_parse(self) -> None:
        xml = f"""<urlset xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"><url><loc>https://www.fjordbygg.no/karriere/</loc></url>
        <url><loc>https://www.fjordbygg.no/nyheter/ny-sak</loc><lastmod>{RECENT}</lastmod><news:news><news:publication_date>{RECENT}T08:00:00+02:00</news:publication_date><news:title>Ny sak om bygg</news:title></news:news></url></urlset>"""
        pages, children = harvest.parse_sitemap(xml)
        self.assertEqual(children, [])
        self.assertEqual(pages[1].news_title, "Ny sak om bygg")
        index = "<sitemapindex><sitemap><loc>https://x.no/post-sitemap.xml</loc></sitemap><sitemap><loc>https://x.no/page-sitemap.xml</loc></sitemap><sitemap><loc>https://x.no/product-sitemap.xml</loc></sitemap></sitemapindex>"
        _pages, children = harvest.parse_sitemap(index)
        self.assertEqual([harvest._child_rank(c) for c in children], [0, 1, 9])


class NavNameTests(unittest.TestCase):
    def test_brand_names_join_the_prefilter(self) -> None:
        from kildespor.jobs import search_names
        rec = record("NORDIC DOOR AS")
        rec.context["subunits"] = [{"name": "NORDIC DOOR AS AVD BERGEN", "organisation_number": "912345678"}]
        rec.context["site_brand_names"] = ["Nordic Door Proff", "Hjem", "NORDIC DOOR AS"]
        self.assertEqual(search_names(rec), ["NORDIC DOOR AS", "NORDIC DOOR AS AVD BERGEN", "Nordic Door Proff"])


class FakeFetcher:
    """Serves fixture pages; counts requests like the real fetcher's thread counter."""

    def __init__(self, pages: dict[str, tuple[str, str]], robots: str = "") -> None:
        self.pages = pages
        self.count = 0
        self.cap: int | None = None
        self.cap_start = 0
        parser = RobotFileParser()
        parser.parse(robots.splitlines())
        self._robots = {"https://www.fjordbygg.no": parser}
        self.requested: list[str] = []

    def thread_requests(self) -> int:
        return self.count

    def begin_company(self, cap: int | None) -> None:
        self.cap, self.cap_start = cap, self.count

    def get(self, url: str, **_kwargs) -> Snapshot:
        if self.cap is not None and self.count - self.cap_start >= self.cap:
            raise RuntimeError("per-company request cap reached")
        self.count += 1
        self.requested.append(url)
        body, ctype = self.pages.get(url, ("not found", "text/html"))
        return snap(url, body, ctype, 200 if url in self.pages else 404)


class EndToEndTests(unittest.TestCase):
    def test_harvest_publishes_one_claim_per_fact(self) -> None:
        home_html = f"""<html><head><title>Fjordbygg - tømrer i Bergen</title><meta property="og:site_name" content="Fjordbygg">
        <meta name="description" content="Fjordbygg bygger hus, hytter og skoler på Vestlandet siden 1985."></head><body>
        <nav><a href="/karriere/">Karriere</a><a href="/nyheter/">Nyheter</a><a href="/kontakt/">Kontakt</a></nav>
        <section class="news"><article><a href="/nyheter/fjordbygg-vinner-kontrakt"><h3>Fjordbygg vinner stor kontrakt i Bergen</h3></a><time datetime="{RECENT}">x</time></article></section>
        <a href="/nyheter/spedify-lanserer-reparasjonstjenester">Vi lanserer reparasjoner - nå jobber vi også med service</a>
        <footer><a href="https://www.facebook.com/fjordbygg/">Facebook</a><a href="https://www.instagram.com/fjordbygg.as/">Instagram</a>
        <a href="https://www.linkedin.com/company/fjordbygg/">LinkedIn</a><a href="https://www.facebook.com/sharer/sharer.php?u=x">Del</a>
        <a href="tel:+4755123456">55 12 34 56</a> Org.nr 923 609 016</footer></body></html>"""
        careers_html = """<html><head><title>Karriere | Fjordbygg</title></head><body><h1>Jobb hos oss</h1>
        <a href="https://fjordbygg.teamtailor.com/jobs/123456-tomrer">Tømrer</a>
        <a href="https://fjordbygg.teamtailor.com/jobs/123457-prosjektleder">Prosjektleder</a>
        <a href="https://fjordbygg.teamtailor.com/jobs">Se alle stillinger</a></body></html>"""
        ats_html = """<html><head><title>Jobs at Fjordbygg</title></head><body>
        <a href="https://fjordbygg.teamtailor.com/jobs/123456-tomrer"><span>Tømrer</span></a>
        <a href="https://fjordbygg.teamtailor.com/jobs/123458-lærling">Lærling</a></body></html>"""
        article_html = f"""<html><head><title>Fjordbygg vinner stor kontrakt i Bergen | Fjordbygg</title>
        <meta property="article:published_time" content="{RECENT}T09:15:00+02:00"></head><body><article><h1>Fjordbygg vinner stor kontrakt i Bergen</h1></article></body></html>"""
        sitemap = f"""<urlset><url><loc>https://www.fjordbygg.no/karriere/</loc></url><url><loc>https://www.fjordbygg.no/nyheter/eldre-sak-om-hytter</loc><lastmod>{OLDER}</lastmod></url></urlset>"""
        listing_html = f"""<html><body><h1>Nyheter</h1><ul>
        <li><a href="/nyheter/fjordbygg-vinner-kontrakt">Fjordbygg vinner stor kontrakt i Bergen</a> <span>{RECENT[8:10]}.{RECENT[5:7]}.{RECENT[:4]}</span></li>
        <li><a href="/nyheter/eldre-sak-om-hytter">Eldre sak om hytter på fjellet</a> <span>{OLDER[8:10]}.{OLDER[5:7]}.{OLDER[:4]}</span></li>
        <li><a href="/nyheter/veldig-gammel-sak">Veldig gammel sak fra arkivet</a> <span>{ANCIENT[8:10]}.{ANCIENT[5:7]}.{ANCIENT[:4]}</span></li></ul></body></html>"""
        contact_html = '<html><body><a href="https://www.youtube.com/@fjordbygg">YouTube</a><a href="mailto:post@fjordbygg.no">post@fjordbygg.no</a></body></html>'
        pages = {
            "https://www.fjordbygg.no/sitemap.xml": (sitemap, "application/xml"),
            "https://www.fjordbygg.no/karriere/": (careers_html, "text/html"),
            "https://fjordbygg.teamtailor.com/jobs": (ats_html, "text/html"),
            "https://www.fjordbygg.no/nyheter/": (listing_html, "text/html"),
            "https://www.fjordbygg.no/kontakt/": (contact_html, "text/html"),
            "https://www.fjordbygg.no/nyheter/fjordbygg-vinner-kontrakt": (article_html, "text/html"),
        }
        fetcher = FakeFetcher(pages, robots="User-agent: *\nAllow: /\n")
        rec = record()
        visit = SiteVisit(fetcher, rec, time.monotonic() + 60)  # type: ignore[arg-type]
        home = snap("https://www.fjordbygg.no/", home_html)
        visit.pages.append((home, *parse(home)[0:1], parse(home)[2]))
        harvest._extract_site_facts(rec, visit, home, True, None)

        socials = sorted(c["value"]["url"] for c in rec.claims if c["field"] == "social_profile")
        self.assertEqual(socials, ["https://facebook.com/fjordbygg", "https://instagram.com/fjordbygg.as", "https://linkedin.com/company/fjordbygg", "https://youtube.com/@fjordbygg"])
        careers = [c["value"] for c in rec.claims if c["field"] == "careers_page"]
        self.assertIn("https://www.fjordbygg.no/karriere/", careers)
        self.assertNotIn("https://www.fjordbygg.no/nyheter/spedify-lanserer-reparasjonstjenester", careers)
        jobs = {c["value"]["title"]: c for c in rec.claims if c["field"] == "job_posting"}
        self.assertEqual(sorted(jobs), ["Lærling", "Prosjektleder", "Tømrer"])
        news = {c["value"]["title"]: c["value"] for c in rec.claims if c["field"] == "company_news"}
        self.assertEqual(news["Fjordbygg vinner stor kontrakt i Bergen"]["published"], f"{RECENT}T09:15:00+02:00")
        self.assertIn("Eldre sak om hytter på fjellet", news)
        self.assertNotIn("Veldig gammel sak fra arkivet", news)  # older than 24 months
        article_claim = next(c for c in rec.claims if c["field"] == "company_news" and c["value"]["title"].startswith("Fjordbygg vinner"))
        self.assertEqual(rec.evidence[article_claim["evidence_ids"][0]]["source_url"], "https://www.fjordbygg.no/nyheter/fjordbygg-vinner-kontrakt")
        for claim in rec.claims:
            self.assertEqual(len(claim["evidence_ids"]), 1, claim)
        self.assertLessEqual(rec.context["harvest"]["requests"], harvest.HARVEST_REQUESTS)
        self.assertEqual(rec.family_state["hiring"]["state"], "available")
        self.assertIn("job_posting:site", rec.checked_fields)  # careers page and its ATS list were read
        if hasattr(rec, "pages_checked"):
            purposes = {p["url"]: p["purpose"] for p in rec.pages_checked}
            self.assertEqual(purposes["https://www.fjordbygg.no/"], "home")
            self.assertEqual(purposes["https://www.fjordbygg.no/karriere/"], "careers")
            self.assertEqual(purposes["https://www.fjordbygg.no/nyheter/fjordbygg-vinner-kontrakt"], "news_article")
            self.assertEqual(purposes["https://www.fjordbygg.no/nyheter/"], "news_listing")


if __name__ == "__main__":
    unittest.main()


class SiteJobsCheckedTests(unittest.TestCase):
    def test_failed_careers_fetch_does_not_close_site_jobs(self) -> None:
        home_html = '<html><head><title>Fjordbygg</title></head><body><a href="/karriere/">Karriere</a><footer>Org.nr 923 609 016</footer></body></html>'
        fetcher = FakeFetcher({}, robots="User-agent: *\nAllow: /\n")  # every page 404s
        rec = record()
        visit = SiteVisit(fetcher, rec, time.monotonic() + 60)  # type: ignore[arg-type]
        home = snap("https://www.fjordbygg.no/", home_html)
        soup, _html, text = parse(home)
        visit.pages.append((home, soup, text))
        harvest._extract_site_facts(rec, visit, home, True, None)
        self.assertNotIn("job_posting:site", rec.checked_fields)
        self.assertEqual([c["value"] for c in rec.claims if c["field"] == "careers_page"], ["https://www.fjordbygg.no/karriere/"])


class SitemapProvenanceTests(unittest.TestCase):
    def test_sitemap_facts_cite_the_sitemap_file_they_came_from(self) -> None:
        """Codex review rc1 finding 8: a news item from a child news sitemap cites that child file,
        and a careers page found in the page sitemap cites the page sitemap, never the index."""
        index = "<sitemapindex><sitemap><loc>https://www.fjordbygg.no/news-sitemap.xml</loc></sitemap><sitemap><loc>https://www.fjordbygg.no/page-sitemap.xml</loc></sitemap></sitemapindex>"
        news = f"""<urlset xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"><url><loc>https://www.fjordbygg.no/nyheter/ny-kontrakt-i-bergen</loc>
        <news:news><news:publication_date>{RECENT}T08:00:00+02:00</news:publication_date><news:title>Fjordbygg får ny kontrakt i Bergen</news:title></news:news></url></urlset>"""
        pages_xml = "<urlset><url><loc>https://www.fjordbygg.no/karriere/</loc></url></urlset>"
        fetcher = FakeFetcher({
            "https://www.fjordbygg.no/sitemap.xml": (index, "application/xml"),
            "https://www.fjordbygg.no/news-sitemap.xml": (news, "application/xml"),
            "https://www.fjordbygg.no/page-sitemap.xml": (pages_xml, "application/xml"),
        }, robots="User-agent: *\nAllow: /\n")
        rec = record()
        visit = SiteVisit(fetcher, rec, time.monotonic() + 60)  # type: ignore[arg-type]
        home = snap("https://www.fjordbygg.no/", "<html><head><title>Fjordbygg</title></head><body>Org.nr 923 609 016</body></html>")
        soup, _html, text = parse(home)
        visit.pages.append((home, soup, text))
        harvest._extract_site_facts(rec, visit, home, True, None)
        sources = {c["field"]: rec.evidence[c["evidence_ids"][0]]["source_url"] for c in rec.claims if c["field"] in ("company_news", "careers_page")}
        self.assertEqual(sources["company_news"], "https://www.fjordbygg.no/news-sitemap.xml")
        self.assertEqual(sources["careers_page"], "https://www.fjordbygg.no/page-sitemap.xml")


class PublicationDateTests(unittest.TestCase):
    """Codex review rc1 finding 9: only publication-specific dates date a news item."""

    def test_date_near_the_headline_is_not_a_publication_date(self) -> None:
        html = """<html><head><title>Prosjektet på Voss</title></head><body><article><h1>Prosjektet på Voss</h1>
        <p>Seminaret arrangeres 16. oktober 2026. Prosjektet ble ferdig i 2019.</p><time datetime="2026-10-16">16. oktober</time></article></body></html>"""
        self.assertIsNone(harvest.article_meta(snap("https://www.fjordbygg.no/nyheter/prosjektet-pa-voss", html), soup_of(html), ["Fjordbygg"]))

    def test_byline_time_is_a_publication_date(self) -> None:
        html = f"""<html><head><title>Ny kontrakt i Bergen</title></head><body><article><header><h1>Ny kontrakt i Bergen</h1>
        <div class="byline">Publisert <time datetime="{RECENT}T09:00:00+02:00">{RECENT}</time></div></header><p>Tekst</p></article></body></html>"""
        item = harvest.article_meta(snap("https://www.fjordbygg.no/nyheter/ny-kontrakt", html), soup_of(html), ["Fjordbygg"])
        self.assertEqual(item.published, f"{RECENT}T09:00:00+02:00")

    def test_date_block_and_dateline_are_publication_dates(self) -> None:
        html = f"""<html><head><title>Zaptec Pro er klar for AFIR</title></head><body><h1>Zaptec Pro er klar for AFIR</h1>
        <dl><div class="date"><dd><time class="text-xs" datetime="{RECENT}T08:00:00.000+02:00">x</time></dd></div></dl>
        <div class="event-date"><time datetime="2026-12-01">1. desember</time></div><p>Tekst</p></body></html>"""
        item = harvest.article_meta(snap("https://www.zaptec.com/info-hub/industry-news/afir", html), soup_of(html), ["Zaptec"])
        self.assertEqual(item.published, f"{RECENT}T08:00:00+02:00")
        d = date.fromisoformat(RECENT)
        html2 = f"""<html><head><title>Ny avdeling på Voss</title></head><body><h1>Ny avdeling på Voss</h1>
        <p class="post-date">Publisert {d.day}.{d.month}.{d.year}</p><p>Arrangementet er 1.12.2026.</p></body></html>"""
        item2 = harvest.article_meta(snap("https://www.fjordbygg.no/nyheter/ny-avdeling", html2), soup_of(html2), ["Fjordbygg"])
        self.assertEqual(item2.date, RECENT)
        html3 = """<html><head><title>Seminar om bygg</title></head><body><h1>Seminar om bygg</h1>
        <div class="event-date">16.10.2025</div></body></html>"""
        self.assertIsNone(harvest.article_meta(snap("https://www.fjordbygg.no/nyheter/seminar", html3), soup_of(html3), ["Fjordbygg"]))

    def test_related_article_date_does_not_date_this_page(self) -> None:
        """Codex review rc2 N5: a page embedding a related article's JSON-LD takes its date only
        from the object bound to the page."""
        page = "https://www.fjordbygg.no/nyheter/prosjektet-pa-voss"
        html = f"""<html><head><title>Prosjektet på Voss</title>
        <script type="application/ld+json">{{"@type":"NewsArticle","headline":"Prosjektet på Voss","datePublished":"2019-05-02T10:00:00+02:00","mainEntityOfPage":{{"@id":"{page}/"}}}}</script>
        <script type="application/ld+json">{{"@type":"NewsArticle","headline":"Ny kontrakt i Bergen","datePublished":"{RECENT}T10:00:00+02:00"}}</script>
        </head><body><article><h1>Prosjektet på Voss</h1></article></body></html>"""
        self.assertIsNone(harvest.article_meta(snap(page, html), soup_of(html), ["Fjordbygg"]))  # 2019: outside the window
        unbound = html.replace(f',"mainEntityOfPage":{{"@id":"{page}/"}}', "")
        self.assertIsNone(harvest.article_meta(snap(page, unbound), soup_of(unbound), ["Fjordbygg"]))  # two unbound: none
        sole = f"""<html><head><title>Ny kontrakt i Bergen</title><script type="application/ld+json">{{"@type":"NewsArticle","headline":"Ny kontrakt i Bergen","datePublished":"{RECENT}T10:00:00+02:00"}}</script></head>
        <body><h1>Ny kontrakt i Bergen</h1></body></html>"""
        self.assertEqual(harvest.article_meta(snap(page, sole), soup_of(sole), ["Fjordbygg"]).date, RECENT)

    def test_update_date_in_a_byline_is_not_a_publication_date(self) -> None:
        """Codex review rc2, finding 9 (partial): "Sist oppdatert ..." inside class="byline"."""
        d = date.fromisoformat(RECENT)
        html = f"""<html><head><title>Prosjektet på Voss</title></head><body><article><h1>Prosjektet på Voss</h1>
        <div class="byline">Sist oppdatert {d.day}.{d.month}.{d.year}</div>
        <div class="byline"><span>Oppdatert</span> <time datetime="{RECENT}T10:00:00+02:00">{RECENT}</time></div></article></body></html>"""
        self.assertIsNone(harvest.article_meta(snap("https://www.fjordbygg.no/nyheter/voss", html), soup_of(html), ["Fjordbygg"]))
        listing = f"""<html><body><ul><li><a href="/nyheter/prosjektet-pa-voss">Prosjektet på Voss er ferdig</a><span>Endret {d.day}.{d.month}.{d.year}</span></li></ul></body></html>"""
        self.assertEqual(harvest.listing_items(snap("https://www.fjordbygg.no/nyheter/", listing), soup_of(listing), "fjordbygg.no", require_news_path=False), [])

    def test_atom_updated_alone_is_not_published(self) -> None:
        atom = f"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Oppdatert side om tjenester</title>
        <link href="https://www.fjordbygg.no/tjenester-oppdatert"/><updated>{RECENT}T10:00:00Z</updated></entry>
        <entry><title>Publisert nyhet om bygg</title><link href="https://www.fjordbygg.no/nyheter/publisert"/><published>{RECENT}T10:00:00Z</published><updated>{RECENT}T11:00:00Z</updated></entry></feed>"""
        items = harvest.feed_items(snap("https://www.fjordbygg.no/feed.atom", atom, "application/atom+xml"), "fjordbygg.no")
        self.assertEqual([i.title for i in items], ["Publisert nyhet om bygg"])

    def test_future_dates_are_never_publication_dates(self) -> None:
        tomorrow = (date.today() + timedelta(days=2)).isoformat()
        self.assertFalse(harvest._news_window(tomorrow))
        self.assertTrue(harvest._news_window(RECENT))

    def test_listing_date_inside_a_teaser_sentence_is_ignored(self) -> None:
        html = """<html><body><ul><li><a href="/nyheter/seminar-om-bygg">Seminar om bygg og anlegg</a>
        <p>Velkommen til seminar 16. oktober 2025 hos oss i Bergen, med foredrag og mat.</p></li></ul></body></html>"""
        page = snap("https://www.fjordbygg.no/nyheter/", html)
        self.assertEqual(harvest.listing_items(page, soup_of(html), "fjordbygg.no", require_news_path=False), [])


class VendorContactTests(unittest.TestCase):
    def test_vendor_contacts_are_not_the_companys(self) -> None:
        """Codex review rc2 N4: the agency's mailto/tel in a "levert av" credit line are not
        published as the company's website_email/website_phone."""
        home_html = """<html><head><title>Fjordbygg</title></head><body>
        <div class="contact"><a href="tel:+4755123456">55 12 34 56</a> <a href="mailto:post@fjordbygg.no">post@fjordbygg.no</a></div>
        <footer><p>Nettside levert av Webbyrå AS – <a href="mailto:hei@webbyraa.no">hei@webbyraa.no</a>, <a href="tel:+4722998877">22 99 88 77</a></p>
        <p>Design: <a href="mailto:kontakt@designer.no">kontakt@designer.no</a></p></footer></body></html>"""
        fetcher = FakeFetcher({}, robots="User-agent: *\nAllow: /\n")
        rec = record()
        visit = SiteVisit(fetcher, rec, time.monotonic() + 60)  # type: ignore[arg-type]
        home = snap("https://www.fjordbygg.no/", home_html)
        soup, _html, text = parse(home)
        visit.pages.append((home, soup, text))
        harvest._extract_site_facts(rec, visit, home, True, None)
        self.assertEqual(sorted(c["value"] for c in rec.claims if c["field"] == "website_email"), ["post@fjordbygg.no"])
        self.assertEqual([c["value"] for c in rec.claims if c["field"] == "website_phone"], ["+47 55 12 34 56"])


class SectionSiteTests(unittest.TestCase):
    def test_section_site_never_reads_domain_wide_sources(self):
        from kildespor.harvest import Plan
        from kildespor.model import CompanyRecord

        class FakeVisit:
            section = "https://berggruppen.no/berg-bygg/"
            pages = []

            def __init__(self):
                self.fetched = []
                self.fetcher = self

            def in_section(self, url):
                from urllib.parse import urlsplit
                parts, sec = urlsplit(url), urlsplit(self.section)
                if parts.hostname != sec.hostname:
                    return True
                return (parts.path or "/").startswith(sec.path)

            def time_left(self):
                return 60.0

            def get(self, url, **kwargs):
                self.fetched.append(url)
                return None

        visit = FakeVisit()
        plan = Plan(CompanyRecord("123456785"), visit)
        self.assertIsNone(plan.raw("https://berggruppen.no/sitemap.xml"))
        self.assertIsNone(plan.raw("https://berggruppen.no/wp-json/wp/v2/posts"))
        self.assertEqual(visit.fetched, [])


class RecruiterLinkTests(unittest.TestCase):
    def test_unknown_recruiter_page_cites_the_company_page_and_is_not_fetched(self) -> None:
        home_html = """<html><head><title>Work System</title></head><body><nav><a href="https://talangpunkten.se/career/work-system/lang/no">Karriere</a></nav>
        <footer>Org.nr 913 170 296</footer></body></html>"""
        fetcher = FakeFetcher({}, robots="User-agent: *\nAllow: /\n")
        rec = record("WORK SYSTEM NORWAY AS", "913170296")
        visit = SiteVisit(fetcher, rec, time.monotonic() + 60)  # type: ignore[arg-type]
        home = snap("https://www.worksystem.no/", home_html)
        soup, _html, text = parse(home)
        visit.pages.append((home, soup, text))
        harvest._extract_site_facts(rec, visit, home, True, None)
        claim = next(c for c in rec.claims if c["field"] == "careers_page")
        self.assertEqual(claim["value"], "https://talangpunkten.se/career/work-system/lang/no")
        self.assertEqual(rec.evidence[claim["evidence_ids"][0]]["source_url"], "https://www.worksystem.no/")
        self.assertFalse(any("talangpunkten" in u for u in fetcher.requested))
        self.assertNotIn("job_posting:site", rec.checked_fields)
