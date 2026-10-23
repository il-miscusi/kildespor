"""The learned news gate (kildespor/newsgate.py) and its corpus dump. No network.

Run with ``python -m pytest -q tests/test_newsgate.py``.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

import requests

from kildespor import harvest, newsgate
from kildespor.contract import publish
from kildespor.model import CompanyRecord
from kildespor.website import SiteVisit, parse

from test_news_precision import RECENT, TODAY, FakeFetcher, snap

TODAY_YEAR = TODAY.year if TODAY.month >= 11 else TODAY.year - 1  # a 3 November inside the news window


HOME = """<html><head><title>Fjordbygg</title><link rel="https://api.w.org/" href="https://www.fjordbygg.no/wp-json/">
<meta name="generator" content="WordPress 6.6"></head><body><p>Fjordbygg AS, org.nr 923 609 016</p></body></html>"""
API = "https://www.fjordbygg.no/wp-json/wp/v2/posts?per_page=20&_fields=date,date_gmt,link,title,class_list,excerpt"


def post(title: str, slug: str, cats: list[str], when: str, excerpt: str = "") -> dict:
    return {"date": when, "date_gmt": when, "link": f"https://www.fjordbygg.no/{slug}/", "title": {"rendered": title},
            "class_list": [f"category-{c}" for c in cats], "excerpt": {"rendered": f"<p>{excerpt} [&hellip;]</p>"}}


class SlowApiFetcher(FakeFetcher):
    """The post API times out ``timeouts`` times, then answers."""

    def __init__(self, pages: dict[str, tuple[str, str]], timeouts: int) -> None:
        super().__init__(pages)
        self.timeouts = timeouts

    def get(self, url: str, **kwargs) -> object:
        if url == API and self.timeouts > 0:
            self.timeouts -= 1
            self.count += 1
            raise requests.exceptions.ReadTimeout("Read timed out. (read timeout=10.0)")
        return super().get(url, **kwargs)


def run_site(posts: list[dict], timeouts: int = 0) -> CompanyRecord:
    fetcher = SlowApiFetcher({API: (json.dumps(posts), "application/json"), "https://www.fjordbygg.no/feed/": ("<rss><channel></channel></rss>", "application/rss+xml")}, timeouts)
    rec = CompanyRecord("923609016")
    rec.context["entity"] = {"navn": "FJORDBYGG AS"}
    visit = SiteVisit(fetcher, rec, time.monotonic() + 60)  # type: ignore[arg-type]
    home = snap("https://www.fjordbygg.no/", HOME)
    parsed = parse(home)
    visit.pages.append((home, parsed[0], parsed[2]))
    harvest._extract_site_facts(rec, visit, home, True, None)
    return rec


class LeadTests(unittest.TestCase):
    def test_wordpress_excerpt_is_the_lead(self) -> None:
        body = json.dumps([post("Fjordbygg vinner stor kontrakt", "kontrakt", ["nyheter"], f"{RECENT}T10:00:00", "Fjordbygg skal bygge ny skole på Voss for 120 millioner kroner.")])
        items = harvest.wordpress_items(snap(API, body, "application/json"), "fjordbygg.no")
        self.assertEqual(items[0].lead, "Fjordbygg skal bygge ny skole på Voss for 120 millioner kroner.")
        self.assertEqual(items[0].date_source, "wp_date_gmt")

    def test_feed_description_is_the_lead(self) -> None:
        feed = f"""<rss><channel><item><title>Ny avdeling i Bergen</title><link>https://www.fjordbygg.no/nyheter/bergen</link>
        <pubDate>Mon, 05 Oct 2026 08:00:00 GMT</pubDate><description><![CDATA[<p>Vi åpner kontor i Bergen.</p>]]></description></item></channel></rss>"""
        items = harvest.feed_items(snap("https://www.fjordbygg.no/feed/", feed, "application/rss+xml"), "fjordbygg.no")
        self.assertEqual(items[0].lead, "Vi åpner kontor i Bergen.")
        self.assertEqual(items[0].date_source, "feed_pubdate")

    def test_article_page_signals(self) -> None:
        html = f"""<html><head><meta property="og:type" content="article"><meta property="article:published_time" content="{RECENT}T10:00:00+02:00">
        <script type="application/ld+json">{{"@type":"NewsArticle","headline":"Fjordbygg åpner i Bergen","datePublished":"{RECENT}T10:00:00+02:00"}}</script></head>
        <body><nav>Hjem Om oss</nav><article><h1>Fjordbygg åpner i Bergen</h1><p>Fjordbygg åpner nytt kontor i Bergen med ti ansatte.</p></article></body></html>"""
        page = snap("https://www.fjordbygg.no/nyheter/bergen", html)
        soup, _html, text = parse(page)
        item = harvest.article_meta(page, soup, ["Fjordbygg"], text)
        self.assertEqual(item.og_type, "article")
        self.assertTrue(item.article_time)
        self.assertIn("NewsArticle", item.ld_types)
        self.assertEqual(item.date_source, "meta_article:published_time")
        self.assertTrue(item.lead.startswith("Fjordbygg åpner nytt kontor"))

    def test_shop_page_flags(self) -> None:
        flags = newsgate.page_signals('<button class="single_add_to_cart_button">Legg i handlekurv</button>', "Easy Sport Small 12 990 kr inkl. mva", {"Product"})
        self.assertEqual(flags, ("cart", "ld_product", "price"))


class TitleTests(unittest.TestCase):
    """Card and button labels published as titles (teacher-audit sample, 23 Oct)."""

    def test_labels_are_not_headlines(self) -> None:
        for title in ("Les Nyhet", "Read more>", "Siste nytt 19.08.2026", "News - 03. November 2022", "Les hele saken", "Vis mer"):
            self.assertFalse(harvest.is_headline(title), title)
        for title in ("Lasse er ny administrasjonsleder på SAFE-huset", "Nyhet: Ny avdeling i Bergen", "NYHET! Recaro Monza CFX Reha"):
            self.assertTrue(harvest.is_headline(title), title)

    def test_card_with_a_styled_title_and_a_written_date(self) -> None:  # cchange.no/news
        html = """<html><body><div class="section"><div class="ItemListing__item ItemListing__item--news">
        <a class="ItemListing__itemContent" href="/news/you-should-read-this"><div class="ItemListing__itemInfo">
        <div class="ItemListing__itemType">News</div><div class="ItemListing__title">"You should read this"</div>
        <div class="ItemListing__date">06.10.2026</div><div class="ItemListing__ingress"><p>The email was from Vernice.</p></div></div></a></div></div></body></html>"""
        page = snap("https://www.cchange.no/news", html)
        soup, _html, _text = parse(page)
        items = harvest.listing_items(page, soup, "cchange.no", require_news_path=False)
        self.assertEqual([(i.title, i.date) for i in items], [('"You should read this"', "2026-10-06")])

    def test_heading_with_the_cards_date_and_labels(self) -> None:
        html = f"""<html><body><ul><li class="news-item"><a href="/articles/new-ceo"><h3>News New CEO appointed News - 03. November {TODAY_YEAR}</h3></a><time datetime="{TODAY_YEAR}-11-03">3.11.{TODAY_YEAR}</time></li></ul></body></html>"""
        page = snap("https://bergencarbonsolutions.com/articles", html)
        soup, _html, _text = parse(page)
        items = harvest.listing_items(page, soup, "bergencarbonsolutions.com", require_news_path=False)
        self.assertEqual([i.title for i in items], ["New CEO appointed"])


class FeatureTests(unittest.TestCase):
    def test_features_are_deterministic_and_order_free(self) -> None:
        c = {"title": "Fjordbygg vinner kontrakt for ny skole i 2026", "url": "https://www.fjordbygg.no/nyheter/2026/10/kontrakt/", "lead": "Fjordbygg skal bygge.",
             "categories": ["nyheter"], "method": "wordpress_posts_api", "own": ["fjordbygg"], "other": [], "published": f"{RECENT}T10:00:00+02:00"}
        a = newsgate.hashed(newsgate.features(c), 16)
        b = newsgate.hashed(newsgate.features(dict(reversed(list(c.items())))), 16)
        self.assertEqual(a, b)
        feats = newsgate.features(c)
        for f in ("e:title_own", "s:newspath", "s:datedpath", "c:nyheter", "t:<y>", "time:y"):
            self.assertIn(f, feats)

    def test_weights_round_trip(self) -> None:
        values = [0] * (1 << 8)
        values[3], values[200] = 90, -127
        spec = {"bits": 8, "bias": -0.5, "scale": 0.01, "threshold": 0.5, "weights": newsgate.encode_weights(values)}
        model = newsgate.Gate(spec)
        self.assertEqual(model.weights[3], 90)
        self.assertEqual(model.weights[200], -127)
        p = model.score({"title": "x"})
        self.assertTrue(0.0 < p < 1.0)


class TitleGate:
    """A stand-in model: refuses any item whose title carries one of ``words``."""

    threshold = 0.5

    def __init__(self, *words: str) -> None:
        self.words = words

    def score(self, c: dict) -> float:
        return 0.1 if any(w in c["title"].lower() for w in self.words) else 0.9


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = (newsgate._GATE, newsgate._GATE_LOADED)

    def tearDown(self) -> None:
        newsgate._GATE, newsgate._GATE_LOADED = self.saved

    def test_gate_refuses_what_the_rules_let_through(self) -> None:
        newsgate._GATE, newsgate._GATE_LOADED = TitleGate("rullestol"), True
        rec = run_site([
            post("Fjordbygg vinner stor kontrakt i Bergen", "kontrakt", ["nyheter"], f"{RECENT}T10:00:00", "Fjordbygg skal bygge."),
            post("Easy Sport Small rullestol i titan", "easy-sport", ["nyheter"], f"{RECENT}T11:00:00", "Pris 12 990 kr."),
        ])
        self.assertEqual([c["value"]["title"] for c in rec.claims if c["field"] == "company_news"], ["Fjordbygg vinner stor kontrakt i Bergen"])

    def test_without_a_model_everything_passes(self) -> None:
        newsgate._GATE, newsgate._GATE_LOADED = None, True
        self.assertEqual(newsgate.verdict({"title": "x"}), (True, None))

    def test_all_refused_is_not_available_with_the_reason(self) -> None:
        newsgate._GATE, newsgate._GATE_LOADED = TitleGate("rullestol"), True
        rec = run_site([post("Easy Sport Small rullestol i titan", "easy-sport", ["nyheter"], f"{RECENT}T11:00:00", "Pris 12 990 kr.")])
        state = rec.family_state["public_activity"]
        self.assertEqual(state["state"], "not_available")
        self.assertIn("below the news gate", state["reason"])

    def test_shipped_model_loads_and_scores(self) -> None:
        if not newsgate.MODEL_PATH.exists():
            self.skipTest("no shipped model")
        spec = json.loads(newsgate.MODEL_PATH.read_text(encoding="utf-8"))
        self.assertEqual(spec["sha256"], newsgate.spec_sha256(spec))
        model = newsgate.Gate(spec)
        good = {"title": "Fjordbygg vinner kontrakt for ny skole på Voss", "url": "https://www.fjordbygg.no/nyheter/kontrakt-skole-voss/", "method": "wordpress_posts_api",
                "categories": ["nyheter"], "lead": "Fjordbygg har signert kontrakt med Voss kommune om bygging av ny barneskole.", "own": ["fjordbygg"], "published": f"{RECENT}T10:00:00+02:00"}
        self.assertEqual(model.score(good), model.score(dict(good)))


class UnreadSourceTests(unittest.TestCase):
    """STRONGPOINT ASA, run-rev2d: the post API timed out and the company came out "not_available"."""

    POSTS = [post("Fjordbygg vinner stor kontrakt i Bergen", "kontrakt", ["nyheter"], f"{RECENT}T10:00:00", "Fjordbygg skal bygge.")]

    def test_one_timeout_is_retried(self) -> None:
        rec = run_site(self.POSTS, timeouts=1)
        self.assertEqual([c["value"]["title"] for c in rec.claims if c["field"] == "company_news"], ["Fjordbygg vinner stor kontrakt i Bergen"])

    def test_unread_api_is_failed_not_absent(self) -> None:
        rec = run_site(self.POSTS, timeouts=2)
        state = rec.family_state["public_activity"]
        self.assertEqual(state["state"], "failed")
        self.assertTrue(state["reason"].startswith(newsgate.NEWS_UNREAD))
        self.assertIn("the WordPress post API (timed out or server error)", state["reason"])
        self.assertNotIn("company_news", rec.checked_fields)

    def test_failed_row_survives_register_updates(self) -> None:
        reason = f"{newsgate.NEWS_UNREAD}: could not read the WordPress post API (timed out or server error); checked the homepage"
        env = {"organisation_number": "982769221", "claims": [
            {"id": "cl-1", "key": "registry_update:z", "family": "public_activity", "field": "registry_update", "value": {"event": "x"}, "availability": "available", "evidence_ids": []}],
            "evidence": [], "sections": {"activity": {"families": {"public_activity": {"state": "available", "reason": reason}}}}}
        row = next(c for c in publish(env)["claims"] if c["field"] == "dated_news")
        self.assertEqual(row["availability"], "failed")
        self.assertEqual(row["reason"], reason)


class DumpTests(unittest.TestCase):
    def test_dump_writes_every_candidate_with_its_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            old = os.environ.get("KILDESPOR_NEWS_DUMP")
            os.environ["KILDESPOR_NEWS_DUMP"] = folder
            try:
                rec = run_site([
                    post("Fjordbygg vinner stor kontrakt i Bergen", "kontrakt", ["nyheter"], f"{RECENT}T10:00:00", "Fjordbygg skal bygge."),
                    post("Easy Sport Small rullestol", "easy-sport-small", ["produkter"], f"{RECENT}T11:00:00"),
                ])
            finally:
                if old is None:
                    os.environ.pop("KILDESPOR_NEWS_DUMP", None)
                else:
                    os.environ["KILDESPOR_NEWS_DUMP"] = old
            rows = [json.loads(line) for name in os.listdir(folder) for line in open(os.path.join(folder, name), encoding="utf-8")]
        by_title = {r["title"]: r for r in rows}
        self.assertTrue(by_title["Fjordbygg vinner stor kontrakt i Bergen"]["published_ok"])
        self.assertEqual(by_title["Fjordbygg vinner stor kontrakt i Bergen"]["lead"], "Fjordbygg skal bygge.")
        self.assertEqual(by_title["Easy Sport Small rullestol"]["rule"], "page element, product, service, guide or portfolio category")
        self.assertEqual(by_title["Easy Sport Small rullestol"]["legal_name"], "FJORDBYGG AS")
        self.assertEqual([c["value"]["title"] for c in rec.claims if c["field"] == "company_news"], ["Fjordbygg vinner stor kontrakt i Bergen"])

    def test_no_dump_without_the_variable(self) -> None:
        self.assertEqual(os.environ.get("KILDESPOR_NEWS_DUMP", ""), "")
        newsgate.dump([{"title": "x"}])  # a no-op


if __name__ == "__main__":
    unittest.main()
