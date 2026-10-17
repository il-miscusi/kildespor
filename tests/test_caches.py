"""Offline tests for kildespor.caches against a tiny fixture directory."""
from __future__ import annotations

import gzip
import tempfile
import threading
import unittest
from pathlib import Path

from kildespor import caches


def _write(path: Path, header: str, lines: list[str]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(header + "\n")
        for line in lines:
            fh.write(line + "\n")


class CachesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        head = "orgnr\tkind\tplatform\turl\tmatch_rule\tsource_record\trelease_date\tlicence"
        _write(d / "candidates-wikidata.tsv.gz", head, [
            "923609016\twebsite\t\thttps://equinor.com\twikidata_p2333_exact_orgnr\thttps://www.wikidata.org/wiki/Q1776022#P856\t2026-10-17\tCC0-1.0",
            "923609016\tsocial\tfacebook\thttps://facebook.com/Equinor\twikidata_p2333_exact_orgnr\thttps://www.wikidata.org/wiki/Q1776022#P2013\t2026-10-17\tCC0-1.0",
        ])
        # an older six-column file must still load
        _write(d / "candidates-overture.tsv.gz", head, [
            "923609016\tsocial\tinstagram\thttps://instagram.com/equinor\toverture_email_exact\toverture:2026-09-23.0:abc",
            "short\trow",
        ])
        _write(d / "candidates-noindex.tsv.gz", "orgnr\trank\tdomain\tpage_url\trule\tdomain_orgnrs\tretrieved_at", [
            "923609016\t1\tstyret.com\thttps://equinor.styret.com/om\tsingle\t3\t2026-10-17T10:00:00Z",
            "923609016\t2\tequinor.no\tHTTPS://www.Equinor.no:443/kontakt/\tsingle\t1\t2026-10-17T11:00:00Z",
            "923609016\t3\teqn.no\thttps://eqn.no/\traw\t1\t2026-10-17T12:00:00Z",
            "932688573\t1\tbjørnstad.no\thttps://bjørnstad.no/personvern\tsingle\t1\t2026-10-17T09:00:00Z",
            "932688573\t2\txn--trgaset-r1a.no\thttp://xn--trgaset-r1a.no/\tld\t1\t2026-10-17T09:30:00Z",
            "932688573\t3\tbad\tftp://bad/\tsingle\t1\t2026-10-17T09:30:00Z",
        ])
        _write(d / "shared-phones.tsv.gz", "phone8\tcount", ["22334455\t12"])
        _write(d / "shared-email-domains.tsv.gz", "domain\tcount", ["obos.no\t300", "gmail.com\tgeneric"])
        _write(d / "register-contacts.tsv.gz", "orgnr\tphones\temails\twebsites", [
            "923609016\t51990000|99887766\tpost@equinor.com\thttps://www.equinor.no",
        ])
        caches.reset(d)

    def tearDown(self) -> None:
        caches.reset(Path(caches.__file__).with_name("data"))
        self.tmp.cleanup()

    def test_candidates(self) -> None:
        got = caches.candidates("923 609 016")
        self.assertEqual(len(got), 6)
        self.assertEqual({c["source"] for c in got}, {"wikidata", "overture", "noindex"})
        web = [c for c in got if c["kind"] == "website"][0]
        self.assertIsNone(web["platform"])
        self.assertEqual(web["release_date"], "2026-10-17")
        self.assertEqual(web["licence"], "CC0-1.0")
        self.assertTrue(web["source_record"].startswith("https://www.wikidata.org/wiki/Q"))
        insta = [c for c in got if c["platform"] == "instagram"][0]
        self.assertIsNone(insta["release_date"])
        self.assertIsNone(insta["licence"])
        self.assertEqual(caches.candidates("999999999"), [])
        got[0]["url"] = "mutated"
        self.assertNotEqual(caches.candidates("923609016")[0]["url"], "mutated")

    def test_noindex(self) -> None:
        got = caches.candidates("923609016")
        self.assertEqual([c["source"] for c in got], ["overture", "wikidata", "wikidata", "noindex", "noindex", "noindex"])
        idx = [c for c in got if c["source"] == "noindex"]
        # the sole owner's domain first; the shared platform (3 owners) and the raw-only number last, by rank
        self.assertEqual([c["url"] for c in idx], ["https://www.equinor.no/", "https://equinor.styret.com/", "https://eqn.no/"])
        first = idx[0]
        self.assertEqual(first["kind"], "website")
        self.assertIsNone(first["platform"])
        self.assertEqual(first["match_rule"], "single")
        self.assertEqual(first["source_record"], "https://www.equinor.no/kontakt/")
        self.assertEqual((first["rank"], first["domain"], first["domain_orgnrs"]), (2, "equinor.no", 1))
        self.assertEqual(first["retrieved_at"], "2026-10-17T11:00:00Z")
        self.assertEqual(first["release_date"], "2026-10-17")
        self.assertEqual(idx[2]["match_rule"], "raw")
        # IDN hosts stay usable by the agent's own normalisation, in Unicode or punycode
        from kildespor.website import normalise_site_url, registered_domain
        from urllib.parse import urlsplit
        idn = caches.candidates("932688573")
        self.assertEqual([c["url"] for c in idn], ["https://bjørnstad.no/", "http://xn--trgaset-r1a.no/"])
        self.assertEqual(registered_domain(urlsplit(normalise_site_url(idn[0]["url"])).hostname), "bjørnstad.no")
        self.assertEqual(registered_domain(urlsplit(normalise_site_url(idn[1]["url"])).hostname), "xn--trgaset-r1a.no")
        self.assertEqual(caches.describe()["candidates-noindex.tsv.gz"], 5)

    def test_shared(self) -> None:
        self.assertTrue(caches.shared_phone("+47 22 33 44 55"))
        self.assertTrue(caches.shared_phone("0047-22334455"))
        self.assertFalse(caches.shared_phone("22 33 44 56"))
        self.assertFalse(caches.shared_phone("123"))
        self.assertTrue(caches.shared_email_domain("styret@obos.no"))
        self.assertTrue(caches.shared_email_domain("mail.obos.no"))
        self.assertTrue(caches.shared_email_domain("GMAIL.COM"))
        self.assertFalse(caches.shared_email_domain("equinor.com"))
        self.assertFalse(caches.shared_email_domain(""))
        self.assertEqual(caches.email_domain_holders("a@mail.obos.no"), 300)
        self.assertEqual(caches.email_domain_holders("gmail.com"), caches.GENERIC)
        self.assertEqual(caches.phone_holders("22334455"), 12)
        self.assertEqual(caches.phone_holders("99999999"), 0)

    def test_register_contacts(self) -> None:
        got = caches.register_contacts("923609016")
        self.assertEqual(got["phones"], ["51990000", "99887766"])
        self.assertEqual(got["emails"], ["post@equinor.com"])
        self.assertEqual(got["websites"], ["https://www.equinor.no"])
        self.assertEqual(caches.register_contacts("1"), {"phones": [], "emails": [], "websites": []})

    def test_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            caches.reset(empty)
            self.assertEqual(caches.candidates("923609016"), [])
            self.assertFalse(caches.shared_phone("22334455"))
            self.assertFalse(caches.shared_email_domain("obos.no"))
            self.assertEqual(caches.register_contacts("923609016")["phones"], [])
            self.assertEqual(caches.available(), [])

    def test_damaged_file(self) -> None:
        (Path(self.tmp.name) / "candidates-wikidata.tsv.gz").write_bytes(b"not gzip")
        caches.reset(self.tmp.name)
        self.assertEqual(len([c for c in caches.candidates("923609016") if c["source"] != "noindex"]), 1)

    def test_threads(self) -> None:
        caches.reset(self.tmp.name)
        results: list[int] = []
        threads = [threading.Thread(target=lambda: results.append(len(caches.candidates("923609016")))) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, [6] * 16)


if __name__ == "__main__":
    unittest.main()
