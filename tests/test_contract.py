"""On-disk envelope shape: scalar values in the evaluator's field names, and a lossless read-back."""
import json
import unittest

from kildespor.contract import canonical_social, internal, publish


def _envelope():
    return {
        "organisation_number": "926818317",
        "sections": {"website_and_profiles": {"families": {"company_profiles": {"state": "available"}}},
                     "hiring_and_activity": {"families": {"hiring": {"state": "not_available", "reason": "none"}, "public_activity": {"state": "available"}}}},
        "claims": [
            {"id": "cl-1", "key": "official_website:\"spedify.no\"", "family": "official_website", "field": "official_website",
             "value": {"url": "https://spedify.no/", "domain": "spedify.no", "identity_method": "organisation_number_on_site"},
             "availability": "available", "confidence": 0.99, "evidence_ids": ["ev-1"]},
            {"id": "cl-2", "key": "social_profile:x", "family": "company_profiles", "field": "social_profile",
             "value": {"platform": "facebook", "url": "https://www.facebook.com/spedify.no/?ref=x", "handle": "spedify.no"},
             "availability": "available", "confidence": 0.92, "evidence_ids": ["ev-1"]},
            {"id": "cl-3", "key": "company_news:y", "family": "public_activity", "field": "company_news",
             "value": {"title": "Spedify lanserer  reparasjon", "url": "https://spedify.no/blogg/a", "published": "2026-09-22T20:00:00+02:00"},
             "availability": "available", "confidence": 0.9, "evidence_ids": ["ev-2"]},
            {"id": "cl-4", "key": "registry_update:z", "family": "public_activity", "field": "registry_update",
             "value": {"event": "x"}, "availability": "available", "evidence_ids": ["ev-3"]},
            {"id": "na-926818317-hiring", "key": "hiring:availability", "family": "hiring", "field": "hiring",
             "value": None, "availability": "not_available", "evidence_ids": []},
        ],
        "evidence": [
            {"id": "ev-1", "source_url": "https://spedify.no/", "source_class": "company_owned_website", "content_sha256": "a" * 64, "retrieved_at": "t"},
            {"id": "ev-2", "source_url": "https://spedify.no/blogg/a", "source_class": "company_owned_website", "content_sha256": "b" * 64, "retrieved_at": "t"},
        ],
    }


class ContractTest(unittest.TestCase):
    def test_scalar_values_and_field_names(self):
        out = publish(_envelope())
        by = {(c["field"], c["availability"]): c for c in out["claims"]}
        self.assertEqual(by[("official_website", "available")]["value"], "https://spedify.no/")
        self.assertEqual(by[("social_profile", "available")]["value"], "https://facebook.com/spedify.no")
        self.assertEqual(by[("dated_news", "available")]["value"], "Spedify lanserer reparasjon (2026-09-22T20:00:00+02:00)")
        self.assertEqual(by[("hiring_signal", "not_available")]["value"], None)
        self.assertNotIn("hiring", {c["field"] for c in out["claims"]})
        site = out["profile"]["evidence"]["website"]
        self.assertTrue(site["value"]["identity_assessment"]["publishable"])
        self.assertEqual(site["value"]["social_links"], [{"platform": "facebook", "url": "https://facebook.com/spedify.no"}])
        self.assertIn("https://spedify.no/blogg/a", [p["url"] for p in site["value"]["pages"]])

    def test_read_back_is_lossless_for_available_claims(self):
        env = _envelope()
        back = internal(json.loads(json.dumps(publish(env))))
        before = {c["key"]: (c["field"], c["value"]) for c in env["claims"] if c["availability"] == "available"}
        after = {c["key"]: (c["field"], c["value"]) for c in back["claims"] if c["availability"] == "available"}
        self.assertEqual(before, after)
        self.assertNotIn("profile", back)

    def test_canonical_social(self):
        self.assertEqual(canonical_social("https://no.linkedin.com/company/elopak/about/"), "https://linkedin.com/company/elopak")
        self.assertEqual(canonical_social("https://www.youtube.com/@Elopak/videos"), "https://youtube.com/@Elopak")
        self.assertEqual(canonical_social("https://m.facebook.com/MtmSkogservice?locale=nb"), "https://facebook.com/MtmSkogservice")
        self.assertEqual(canonical_social("https://www.facebook.com/profile.php?id=100063&ref=x"), "https://facebook.com/profile.php?id=100063")

    def test_kit_pages_include_checked_news_pages(self):
        env = _envelope()
        env["crawl"] = {"pages_checked": [
            {"url": "https://spedify.no/nyheter/", "title": "Nyheter", "content_sha256": "c" * 64},
            {"url": "https://other.no/nyheter/", "title": "x", "content_sha256": "d" * 64},
        ]}
        pages = [p["url"] for p in publish(env)["profile"]["evidence"]["website"]["value"]["pages"]]
        self.assertIn("https://spedify.no/nyheter/", pages)
        self.assertNotIn("https://other.no/nyheter/", pages)


if __name__ == "__main__":
    unittest.main()


class SharedProfileTest(unittest.TestCase):
    def test_profile_claimed_by_two_companies_is_withheld(self):
        from kildespor.model import CompanyRecord
        from kildespor.runner import frozen_copy, shared_profiles, withhold_shared_profiles

        recs = {}
        for org, handle in (("111111111", "acmegroup"), ("222222222", "acmegroup"), ("333333333", "solo")):
            rec = CompanyRecord(org)
            rec.add_claim(family="company_profiles", field_name="social_profile",
                          value={"platform": "facebook", "url": f"https://www.facebook.com/{handle}/"},
                          evidence_ids=["ev-1"], method="fixture")
            rec.set_state("company_profiles", "available", "fixture")
            recs[org] = rec
        shared = shared_profiles(recs)
        self.assertEqual(shared, {"https://facebook.com/acmegroup"})
        snap = frozen_copy(recs["111111111"])
        withhold_shared_profiles(snap, shared)
        self.assertEqual(snap.claims_for("company_profiles"), [])
        self.assertEqual(snap.family_state["company_profiles"]["state"], "ambiguous")
        solo = frozen_copy(recs["333333333"])
        withhold_shared_profiles(solo, shared)
        self.assertEqual(len(solo.claims_for("company_profiles")), 1)

    def test_same_profile_from_two_routes_is_one_published_claim(self):
        env = _envelope()
        env["claims"].append({"id": "cl-9", "key": "social_profile:wd", "family": "company_profiles", "field": "social_profile",
                              "value": {"platform": "facebook", "url": "https://facebook.com/Spedify.no"},
                              "availability": "available", "confidence": 0.85, "evidence_ids": ["ev-wd"]})
        out = publish(env)
        socials = [c for c in out["claims"] if c["field"] == "social_profile" and c["availability"] == "available"]
        self.assertEqual(len(socials), 1)
        self.assertEqual(set(socials[0]["evidence_ids"]), {"ev-1", "ev-wd"})
