"""The learned news gate (kildespor/newsgate.py) and its corpus dump. No network.

Run with ``python -m pytest -q tests/test_newsgate.py``.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

from kildespor import harvest, newsgate
from kildespor.model import CompanyRecord
from kildespor.website import SiteVisit, parse

from test_news_precision import RECENT, FakeFetcher, snap


HOME = """<html><head><title>Fjordbygg</title><link rel="https://api.w.org/" href="https://www.fjordbygg.no/wp-json/">
<meta name="generator" content="WordPress 6.6"></head><body><p>Fjordbygg AS, org.nr 923 609 016</p></body></html>"""
API = "https://www.fjordbygg.no/wp-json/wp/v2/posts?per_page=20&_fields=date,date_gmt,link,title,class_list,excerpt"


def post(title: str, slug: str, cats: list[str], when: str, excerpt: str = "") -> dict:
    return {"date": when, "date_gmt": when, "link": f"https://www.fjordbygg.no/{slug}/", "title": {"rendered": title},
            "class_list": [f"category-{c}" for c in cats], "excerpt": {"rendered": f"<p>{excerpt} [&hellip;]</p>"}}


def run_site(posts: list[dict]) -> CompanyRecord:
    fetcher = FakeFetcher({API: (json.dumps(posts), "application/json"), "https://www.fjordbygg.no/feed/": ("<rss><channel></channel></rss>", "application/rss+xml")})
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
