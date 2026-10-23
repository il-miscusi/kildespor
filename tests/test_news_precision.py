"""News precision rules (organiser audit 23 Oct): offline fixtures taken from the audited v1 items
(campaign/audit/oct23/C_news_labels.tsv). No network.

Run with ``python -m unittest tests.test_news_precision``.
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
from kildespor.contract import publish
from kildespor.http import Snapshot
from kildespor.model import CompanyRecord
from kildespor.website import SiteVisit, parse

TODAY = date.today()
RECENT = (TODAY - timedelta(days=20)).isoformat()
OLDER = (TODAY - timedelta(days=60)).isoformat()


def snap(url: str, body: str, ctype: str = "text/html; charset=utf-8", status: int = 200) -> Snapshot:
    data = body.encode("utf-8")
    return Snapshot(requested_url=url, final_url=url, status=status, retrieved_at="2026-10-23T00:00:00Z", sha256=hashlib.sha256(data).hexdigest(), content_type=ctype, body=data)


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def item(title: str, url: str = "https://www.fjordbygg.no/nyheter/sak", published: str = f"{RECENT}T10:00:00+02:00", categories: tuple[str, ...] = (), method: str = "wordpress_posts_api") -> harvest.NewsItem:
    return harvest.NewsItem(title, url, published, published[:10], snap(url, ""), title, method, "company_website", categories=categories)


def wp_posts(*posts: tuple[str, str, list[str], str]) -> list[harvest.NewsItem]:
    """(title, slug, category slugs, local datetime) -> wordpress_items over a posts API response with class_list."""
    body = json.dumps([
        {"date": when, "date_gmt": when, "link": f"https://www.fjordbygg.no/{slug}/", "title": {"rendered": title},
         "class_list": ["post", "type-post", "hentry"] + [f"category-{c}" for c in cats]}
        for title, slug, cats, when in posts
    ])
    return harvest.wordpress_items(snap("https://www.fjordbygg.no/wp-json/wp/v2/posts", body, "application/json"), "fjordbygg.no")


class CategoryRuleTests(unittest.TestCase):
    """R1: the post's own WordPress categories (class_list, same request) mark page parts and catalogue posts."""

    def test_class_list_categories_are_read(self) -> None:
        items = wp_posts(("Nordic Door er kåret til Årets Mulighetsbedrift 2025", "kaaret", ["privat", "proff"], f"{RECENT}T18:56:47"))
        self.assertEqual(items[0].categories, ("privat", "proff"))
        self.assertIn("category privat, proff", items[0].span)

    def test_blocked_categories(self) -> None:
        for cats in (("frontpage",), ("frontpagemobile",), ("produkter",), ("produkter", "nyhet"), ("produktopplevelse", "aktuelt"), ("outlet",),
                     ("aktive-investeringsprosjekter",), ("avsluttede-investeringsprosjekter", "nyheter"), ("behandlinger",), ("guides", "knowledge")):
            self.assertIsNotNone(harvest.news_drop_reason(item("Easy Sport Small sykkel", categories=cats)), cats)

    def test_news_and_mixed_categories_pass(self) -> None:
        for cats in (("nyheter",), ("news", "uncategorized"), ("kosmetiske-behandlinger",), ("events",), ("privat", "proff"), ("messe",)):
            self.assertIsNone(harvest.news_drop_reason(item("Vår kosmetiske avdeling flytter til nye lokaler", categories=cats)), cats)

    def test_feed_categories_count_too(self) -> None:
        rss = f"""<rss><channel><item><title>Pumpeutleie med kort leveringstid</title><link>https://pumpeteknikk.as/pumpeutleie/</link>
        <category><![CDATA[Produkter]]></category><pubDate>{date.fromisoformat(RECENT).strftime('%a, %d %b %Y')} 08:00:00 +0000</pubDate></item></channel></rss>"""
        items = harvest.feed_items(snap("https://pumpeteknikk.as/feed/", rss, "application/rss+xml"), "pumpeteknikk.as")
        self.assertEqual(items[0].categories, ("produkter",))
        self.assertIsNotNone(harvest.news_drop_reason(items[0]))


class TitleRuleTests(unittest.TestCase):
    def test_template_titles_are_not_headlines(self) -> None:  # R4
        for title in ("HeroSliderPost", "HeroSliderPost ( Cloned )", "MobilPostSlider1", "TestPostMobil", "Lorem ipsum dolor sit amet"):
            self.assertFalse(harvest.is_headline(title), title)
        self.assertTrue(harvest.is_headline("Åpningstider sommeren 2026"))
        self.assertEqual(wp_posts(("HeroSliderPost ( Cloned )", "herosliderpost-cloned", ["frontpage"], f"{RECENT}T15:54:50")), [])

    def test_job_ads(self) -> None:  # R5
        for title in ("Ledige stillinger - bli en av oss!", "Hej Sverige, vill du arbeta i Norge ?", "Bli med på suksessen: Spartveit AS søker nye teammedlemmer",
                      "Bli en del av Norges påbygger- og tilhenger spesialist ?", "Vi ansetter: prosjektleder"):
            self.assertEqual(harvest.news_drop_reason(item(title)), "job advertisement", title)
        # the path alone: a job-ad slug inside a segment under /aktuelt/
        self.assertEqual(harvest.news_drop_reason(item("Bli med i laget vårt", "https://www.romsdalsnettas.no/aktuelt/ledige-stillinger-bli-en-av-oss")), "job advertisement")
        for title in ("Ledige skoleplasser til skoleåret 2026-2027", "Ny lærling på plass", "Gratulerer til vår lærling Tone – nå med svennebrev!"):
            self.assertIsNone(harvest.news_drop_reason(item(title, "https://www.stib.no/mnedspost/ledige-skoleplasser-til-skoleret-2026-2027")), title)

    def test_guides(self) -> None:  # R6
        for title in ("Whitepaper: What to Pick for In-Store Fulfillment", "Report: Shaping Retail Automation", "GUIDE: Hva gjør de mest vellykkede investorene?",
                      "Lipødem – Hvordan kan behandling hjelpe?", "Alt du trenger å vite om laserbehandlinger", "Hva er skinboosters?",
                      "💰 How Much Does It Cost to Market Your Art Online? A Practical Guide for Artists", "Sjekkliste for utleiere om de skal leie ut selv",
                      "Brannsikkerhet på nyttårsaften – slik feirer du trygt", "Slik fungerer En Hånd i Hverdagen – praktisk seniorhjelp"):
            self.assertEqual(harvest.news_drop_reason(item(title)), "guide or explainer", title)
        for title in ("Slik løste vi vindusvask i høyden for Wangen Apartments i Aurland", "Kun 16 av 129 selskaper når dette nivået. Ly Forsikring er ett av dem",
                      "Consto med nytt oppdrag for Statnett – Samnanger Transformatorstasjon", "TGS Q3 2026 Operational Update"):
            self.assertIsNone(harvest.news_drop_reason(item(title)), title)


class OtherEntityTests(unittest.TestCase):
    """R12: group press on the company's own site names the parent, not the company."""

    def rec(self, name: str, parents: list[tuple[str, str]] = ()) -> CompanyRecord:
        rec = CompanyRecord("985628572")
        rec.context["entity"] = {"navn": name}
        rec.claims = [{"field": "group_relationship", "value": {"relationship": rel, "name": parent}} for rel, parent in parents]
        return rec

    def test_umbrella_brand_word_is_the_group(self) -> None:
        entity = harvest.entity_words(self.rec("NETSOLUTION VIKEN AS"), ["Aderian Netsolution"], "https://netsolution.no/")
        self.assertEqual(entity.other, ["aderian"])
        for title in ("Searchlight Capital Partners Announces Investment in Aderian Group", "Aderian AI Tour 2026 – Norge",
                      "Aderian i Norge – Sterke lokale teknologimiljøer – samlet i en nordisk plattform"):
            self.assertEqual(harvest.news_drop_reason(item(title), entity), "about the parent group, not the company", title)
        self.assertIsNone(harvest.news_drop_reason(item("NæringsArena: Netsolution Techtalk 2026"), entity))
        self.assertIsNone(harvest.news_drop_reason(item("Aderian Netsolution åpner kontor i Drammen"), entity))

    def test_register_parent(self) -> None:
        entity = harvest.entity_words(self.rec("CONSTO AS", [("parent", "BARLINDHAUG AS")]), ["Consto AS"], "https://consto.no/")
        self.assertTrue(harvest.names_other_entity("Barlindhaug kjøper eiendom i Tromsø", entity))
        self.assertFalse(harvest.names_other_entity("Consto og Barlindhaug styrker samarbeidet", entity))
        # a parent whose only distinctive word is a place stays a full-name match: the place alone is not the parent
        entity = harvest.entity_words(self.rec("ROMSDALSNETT AS", [("parent", "RAUMA ENERGI HOLDING AS")]), ["Romsdalsnett AS"], "https://www.romsdalsnettas.no/")
        self.assertFalse(harvest.names_other_entity("Strømbrudd i Rauma torsdag", entity))
        self.assertTrue(harvest.names_other_entity("Rauma Energi investerer i ny kraftstasjon", entity))
        # generic or tiny parent names are not matched ("L T L AS", "CITY HOLDING AS")
        entity = harvest.entity_words(self.rec("NORDIC DOOR AS", [("parent", "L T L AS"), ("ultimate_parent", "CITY HOLDING AS")]), ["Nordic Door"], "https://www.nordicdoor.no/")
        self.assertEqual(entity.other, [])


class CopyBurstTests(unittest.TestCase):
    def test_three_within_ten_minutes_is_a_copy_date(self) -> None:  # R3: Total VVS re-posting Instalco stories
        items = [item("Instalcoselskap signerer nytt rehabiliteringsprosjekt i Oslo", published="2026-02-11T09:38:23+00:00", method="article_page_meta"),
                 item("Lysteknikk tildelt kontrakt for Nye Aker Sykehus", published="2026-02-11T09:33:58+00:00", method="article_page_meta"),
                 item("Bærekraftvurdert Instalco-prosjekter", published="2026-02-11T09:31:11+00:00", method="article_page_meta"),
                 item("Total VVS vinner kontrakt", published="2026-02-11T12:00:00+00:00", method="article_page_meta")]
        self.assertEqual(harvest.copy_bursts(items), {0, 1, 2})

    def test_an_evenings_posts_are_not_a_burst(self) -> None:  # Teknisk Senter, 14 Oct: 18:41, 19:52, 20:00, 20:21
        stamps = ["2026-10-14T18:41:19+02:00", "2026-10-14T19:52:21+02:00", "2026-10-14T20:00:54+02:00", "2026-10-14T20:21:54+02:00"]
        self.assertEqual(harvest.copy_bursts([item(f"Kveldsklubb nummer {i}", published=s) for i, s in enumerate(stamps)]), set())
        # bare dates carry no time of day and never form a burst
        self.assertEqual(harvest.copy_bursts([item(f"Sak nummer {i}", published=RECENT) for i in range(4)]), set())

    def test_mixed_offsets_compare_as_instants(self) -> None:
        items = [item("Første sak fra migreringen", published="2025-01-24T23:05:19+01:00"), item("Andre sak fra migreringen", published="2025-01-24T22:05:36+00:00"),
                 item("Tredje sak fra migreringen", published="2025-01-24T23:05:55")]
        self.assertEqual(harvest.copy_bursts(items), {0, 1, 2})


class TopicLibraryTests(unittest.TestCase):
    def test_posts_filed_by_topic(self) -> None:  # hjernenett.no: therapy areas, no news category
        posts = wp_posts(*[(f"Foredrag om tema nummer {i}", f"foredrag-{i}", [cat], f"{RECENT}T10:0{i}:00") for i, cat in enumerate(["migrene", "schizofreni", "depresjon", "unni"])])
        self.assertTrue(harvest.topic_library(posts))
        self.assertFalse(harvest.topic_library(posts[:3]))  # three topics are not enough
        news = wp_posts(("Nyhet fra oss", "nyhet", ["nyheter"], f"{RECENT}T10:00:00"))
        self.assertFalse(harvest.topic_library(posts + news))  # a news category exists
        self.assertFalse(harvest.topic_library(wp_posts(("Åpningstider påske 2026", "apningstider", ["privat", "proff"], f"{RECENT}T10:00:00"))))
        self.assertTrue(harvest.news_path("https://gamlegymnaset.no/2026/09/16/sommarljom-2026/"))
        self.assertFalse(harvest.news_path("https://hjernenett.no/prolactin/"))


class ListingTests(unittest.TestCase):
    SITE = "sanomega.as"

    def test_truncated_card_titles(self) -> None:  # R9
        url = "https://www.sanomega.as/blogs/news/astaxanthin-naturens-kraftigste-antioksidant-og-immunstyrker"
        self.assertTrue(harvest.truncated_title("Astaxanthin: Naturens kraftigste antioksidant o...", url))
        self.assertTrue(harvest.truncated_title("Brukerhistorie fra en av våre kunder: «Grunnsme...", "https://www.sanomega.as/blogs/news/brukerhistorie-fra-en-av-vare-kunder-grunnsmerten-ble-borte-av-magnesium"))
        self.assertFalse(harvest.truncated_title("Behinde the scene - dumperkjerre gone selfie……", "https://www.spartveit.no/blog/pne-dager-x"))
        self.assertFalse(harvest.truncated_title("Trump i alle kanaler….🤔", "https://stavangeram.com/en/trump/"))

    def test_card_title_and_date_evidence(self) -> None:  # R9 + R10
        html = f"""<html><body><main>
        <article class="card"><a href="/blogs/news/astaxanthin-naturens-kraftigste-antioksidant-og-immunstyrker" title="Astaxanthin: Naturens kraftigste antioksidant og immunstyrker">
          <h3>Astaxanthin: Naturens kraftigste antioksidant o...</h3></a><time datetime="{RECENT}T12:43:20Z">{RECENT}</time></article>
        <article class="card"><a href="/blogs/news/bedre-cellemiljo-og-mindre-betennelse-hos-voksne"><h3>Bedre cellemiljø og mindre betennelse hos voksne o...</h3></a><span>{RECENT[8:10]}.{RECENT[5:7]}.{RECENT[:4]}</span></article>
        </main></body></html>"""
        items = harvest.listing_items(snap("https://www.sanomega.as/blogs/news", html), soup_of(html), self.SITE, require_news_path=False)
        self.assertEqual(items[0].title, "Astaxanthin: Naturens kraftigste antioksidant og immunstyrker")
        self.assertEqual(items[0].span, f'Astaxanthin: Naturens kraftigste antioksidant og immunstyrker | <time datetime="{RECENT}T12:43:20Z">')
        self.assertTrue(items[1].span.endswith(f"| {RECENT[8:10]}.{RECENT[5:7]}.{RECENT[:4]}"))
        self.assertTrue(harvest.truncated_title(items[1].title, items[1].url))  # still cut: read its page, else not published

    def test_menus_footers_and_update_times(self) -> None:  # R11
        html = f"""<html><body>
        <div class="mega-menu"><ul><li><a href="/nyheter/fjordbygg-vinner-kontrakt">Fjordbygg vinner stor kontrakt</a><time datetime="{RECENT}">x</time></li></ul></div>
        <ul><li class="menu-item"><a href="/nyheter/ny-avdeling-pa-voss">Ny avdeling åpner på Voss</a><time datetime="{RECENT}">x</time></li></ul>
        <footer><article><a href="/nyheter/sommerfest-for-ansatte">Sommerfest for alle ansatte</a><time datetime="{RECENT}">x</time></article></footer>
        <main><article class="post hentry"><h2><a href="/nyheter/nytt-tak-pa-skistua">Nytt tak på historiske Skistua</a></h2>
          <time class="updated" datetime="{RECENT}T09:00:00+02:00">{RECENT}</time><time class="entry-date published" datetime="{OLDER}T08:00:00+02:00">{OLDER}</time></article>
        <article class="post"><h2><a href="/nyheter/vi-er-blitt-miljofyrtarn">Vi er blitt Miljøfyrtårn</a></h2><time class="entry-date published updated" datetime="{OLDER}T08:00:00+02:00">x</time></article>
        </main></body></html>"""
        items = {i.title: i for i in harvest.listing_items(snap("https://www.fjordbygg.no/nyheter/", html), soup_of(html), "fjordbygg.no", require_news_path=False)}
        self.assertEqual(sorted(items), ["Nytt tak på historiske Skistua", "Vi er blitt Miljøfyrtårn"])
        self.assertEqual(items["Nytt tak på historiske Skistua"].published, f"{OLDER}T08:00:00+02:00")
        self.assertEqual(items["Vi er blitt Miljøfyrtårn"].date, OLDER)
        self.assertTrue(harvest._update_date(soup_of('<time class="updated" datetime="2026-10-01">1. okt</time>').time))
        self.assertFalse(harvest._update_date(soup_of('<time class="entry-date published updated" datetime="2026-10-01">1. okt</time>').time))

    def test_webpage_date_needs_an_article_page(self) -> None:  # R11: Yoast's WebPage datePublished on a service page
        ld = f'<script type="application/ld+json">{{"@context":"https://schema.org","@type":"WebPage","@id":"https://x.no/tjenester/","url":"https://x.no/tjenester/","datePublished":"{RECENT}T10:00:00+02:00"}}</script>'
        service = f"<html><head>{ld}</head><body><h1>Våre tjenester innen bygg</h1></body></html>"
        self.assertIsNone(harvest.page_article(soup_of(service), "https://x.no/tjenester/"))
        self.assertIsNone(harvest.article_meta(snap("https://x.no/tjenester/", service), soup_of(service), ["X"]))
        article = f'<html><head><meta property="og:type" content="article">{ld}</head><body><h1>Våre tjenester innen bygg</h1></body></html>'
        self.assertIsNotNone(harvest.page_article(soup_of(article), "https://x.no/tjenester/"))


class FakeFetcher:
    def __init__(self, pages: dict[str, tuple[str, str]]) -> None:
        self.pages = pages
        self.count = 0
        self.cap: int | None = None
        self.cap_start = 0
        parser = RobotFileParser()
        parser.parse(["User-agent: *", "Allow: /"])
        self._robots = {"https://www.fjordbygg.no": parser}

    def thread_requests(self) -> int:
        return self.count

    def begin_company(self, cap: int | None) -> None:
        self.cap, self.cap_start = cap, self.count

    def get(self, url: str, **_kwargs) -> Snapshot:
        self.count += 1
        body, ctype = self.pages.get(url, ("not found", "text/html"))
        return snap(url, body, ctype, 200 if url in self.pages else 404)


class PublishTests(unittest.TestCase):
    HOME = """<html><head><title>Fjordbygg</title><link rel="https://api.w.org/" href="https://www.fjordbygg.no/wp-json/">
    <meta name="generator" content="WordPress 6.6"></head><body><p>Fjordbygg AS, org.nr 923 609 016</p></body></html>"""
    API = "https://www.fjordbygg.no/wp-json/wp/v2/posts?per_page=20&_fields=date,date_gmt,link,title,class_list"

    def run_site(self, posts: list[dict]) -> CompanyRecord:
        fetcher = FakeFetcher({self.API: (json.dumps(posts), "application/json"), "https://www.fjordbygg.no/feed/": ("<rss><channel></channel></rss>", "application/rss+xml")})
        rec = CompanyRecord("923609016")
        rec.context["entity"] = {"navn": "FJORDBYGG AS"}
        visit = SiteVisit(fetcher, rec, time.monotonic() + 60)  # type: ignore[arg-type]
        home = snap("https://www.fjordbygg.no/", self.HOME)
        parsed = parse(home)
        visit.pages.append((home, parsed[0], parsed[2]))
        harvest._extract_site_facts(rec, visit, home, True, None)
        return rec

    def post(self, title: str, slug: str, cats: list[str], when: str) -> dict:
        return {"date": when, "date_gmt": when, "link": f"https://www.fjordbygg.no/{slug}/", "title": {"rendered": title}, "class_list": [f"category-{c}" for c in cats]}

    def test_only_news_is_published(self) -> None:
        rec = self.run_site([
            self.post("Fjordbygg vinner stor kontrakt i Bergen", "kontrakt", ["nyheter"], f"{RECENT}T10:00:00"),
            self.post("HeroSliderPost", "herosliderpost", ["frontpage"], f"{RECENT}T11:00:00"),
            self.post("Easy Sport Small", "easy-sport-small", ["produkter"], f"{RECENT}T12:00:00"),
            self.post("Ledige stillinger - bli en av oss!", "ledige-stillinger", ["nyheter"], f"{OLDER}T09:00:00"),
        ])
        news = [c["value"]["title"] for c in rec.claims if c["field"] == "company_news"]
        self.assertEqual(news, ["Fjordbygg vinner stor kontrakt i Bergen"])
        self.assertEqual(rec.family_state["public_activity"]["state"], "available")

    def test_no_news_left_names_what_was_checked(self) -> None:
        rec = self.run_site([
            self.post("Utnytt plassen hjemme med skyvedør", "skyvedor", ["privat"], f"{OLDER}T10:08:48"),
            self.post("Nordic Door – Det trygge valget!", "trygge-valget", ["privat"], f"{OLDER}T10:08:39"),
            self.post("Oppdag vårt utvalg av ståldører", "staldorer", ["proff"], f"{OLDER}T10:08:26"),
            self.post("Hva er skinboosters?", "skinboosters", ["ansikt"], f"{RECENT}T14:27:21"),
        ])
        self.assertEqual([c for c in rec.claims if c["field"] == "company_news"], [])
        state = rec.family_state["public_activity"]
        self.assertEqual(state["state"], "not_available")
        self.assertIn("the WordPress post API (4 dated post(s))", state["reason"])
        self.assertIn("3 a copy or import date", state["reason"])
        self.assertIn("1 guide or explainer", state["reason"])

        # Register updates make the family available; the published dated-news row still says not_available, and why.
        rec.add_claim(family="public_activity", field_name="registry_update", value={"event": "x", "date": RECENT}, evidence_ids=["ev-x"], method="official_register")
        rec.finalize_states()
        envelope = {"organisation_number": rec.organisation_number, "claims": rec.claims, "evidence": [],
                    "sections": {"hiring_and_activity": {"families": {"public_activity": rec.family_state["public_activity"]}}}}
        row = next(c for c in publish(envelope)["claims"] if c["field"] == "dated_news")
        self.assertEqual(row["availability"], "not_available")
        self.assertTrue(row["reason"].startswith("no dated news about the company on its own website; checked the homepage, the WordPress post API"))


if __name__ == "__main__":
    unittest.main()
