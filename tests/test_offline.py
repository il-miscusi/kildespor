"""Offline tests: no network. Run with ``python -m unittest discover -s tests``."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kildespor import http as khttp
from kildespor.envelope import build_envelope
from kildespor.model import FAMILIES, STATES, CompanyRecord
from kildespor.refresh import diff
from kildespor.runner import assemble, read_inputs, validate
from kildespor.synthesis import build_synthesis
from kildespor.website import guess_domains, org_numbers_in, social_profile, valid_orgnr


def sample_record(org: str = "938702675", employees: int = 10, job: bool = True) -> CompanyRecord:
    rec = CompanyRecord(org)
    ev = rec.add_evidence(source_url=f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}", source_class="official_registry", retrieved_at="2026-10-17T00:00:00Z", content_sha256="a" * 64, claim_span='navn = "TEST AS"', extraction_method="brreg_entity_api_field")
    rec.add_claim(family="legal_identity", field_name="legal_name", value="TEST AS", evidence_ids=[ev], method="official_register")
    rec.add_claim(family="legal_identity", field_name="registered_employees", value=employees, evidence_ids=[ev], method="official_register")
    rec.add_claim(family="leadership", field_name="role", value={"role": "Daglig leder", "role_code": "DAGL", "name": "Kari Nordmann"}, key=["DAGL", "Kari Nordmann", None], evidence_ids=[ev], method="official_register")
    if job:
        rec.add_claim(family="hiring", field_name="job_posting", value={"title": "Elektriker", "url": "https://arbeidsplassen.nav.no/stillinger/stilling/x"}, key=["nav", "x"], evidence_ids=[ev], method="nav_ad_page_orgnr_match")
    for family in FAMILIES:
        if not rec.claims_for(family):
            rec.set_state(family, "not_available", "checked, nothing found")
    rec.checked_fields.add("job_posting")  # the job source was consulted completely
    rec.finalize_states()
    return rec


def envelope_for(rec: CompanyRecord, previous: dict | None = None, run_id: str = "r1") -> dict:
    changes, meta = diff(rec, previous, run_id)
    return build_envelope(rec, run={"run_id": run_id, "completed_at": "2026-10-17T00:00:00Z"}, synthesis=build_synthesis(rec, changes, baseline=bool(meta.get("baseline"))), changes=changes, refresh_meta=meta)


class IdentityTests(unittest.TestCase):
    def test_orgnr_checksum(self) -> None:
        self.assertTrue(valid_orgnr("938702675"))
        self.assertTrue(valid_orgnr("971487046"))
        self.assertFalse(valid_orgnr("938702676"))
        self.assertFalse(valid_orgnr("123456789"))

    def test_orgnr_in_text_formats(self) -> None:
        text = "AF Gruppen ASA · Org.nr. 938 702 675 MVA · Tlf 22 89 11 00"
        self.assertIn("938702675", org_numbers_in(text))
        self.assertIn("938702675", org_numbers_in("Foretaksregisteret NO938702675MVA"))
        self.assertNotIn("938702675", org_numbers_in("Ring 9387026750 nå"))

    def test_domain_guesses(self) -> None:
        self.assertIn("mjorud.no", guess_domains("MJØRUD AS"))
        self.assertIn("af-gruppen.no", guess_domains("AF GRUPPEN ASA"))
        self.assertEqual(guess_domains("OLSEN HOLDING AS"), [])
        self.assertEqual(guess_domains("A AS"), [])

    def test_social_profiles(self) -> None:
        self.assertEqual(social_profile("https://www.facebook.com/afgruppen/")[0], "facebook")
        self.assertIsNone(social_profile("https://www.facebook.com/sharer/sharer.php?u=x"))
        self.assertEqual(social_profile("https://no.linkedin.com/company/af-gruppen")[0], "linkedin")
        self.assertIsNone(social_profile("https://www.linkedin.com/in/someone"))
        self.assertIsNone(social_profile("https://twitter.com/intent/tweet?text=x"))
        self.assertEqual(social_profile("https://www.youtube.com/@afgruppen")[0], "youtube")


class SafetyTests(unittest.TestCase):
    def test_unsafe_urls_refused(self) -> None:
        for url in ("file:///etc/passwd", "http://127.0.0.1/", "http://localhost/", "ftp://example.no/", "http://user:pw@example.no/", "http://10.0.0.1/", "http://example.no:8080/"):
            with self.assertRaises(khttp.UnsafeURL, msg=url):
                khttp.check_url(url)


class ContractTests(unittest.TestCase):
    def test_inputs_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.jsonl"
            p.write_text('{"organisation_number":"938702675"}\n{"organisation_number":"971 487 046"}\n{"organisation_number":"938702675"}\n', encoding="utf-8")
            self.assertEqual(read_inputs(str(p)), ["938702675", "971487046"])
            t = Path(tmp) / "b.txt"
            t.write_text("938702675\n971487046\n", encoding="utf-8")
            self.assertEqual(read_inputs(str(t)), ["938702675", "971487046"])
            j = Path(tmp) / "c.json"
            j.write_text('["938702675", {"orgnr": "971487046"}]', encoding="utf-8")
            self.assertEqual(read_inputs(str(j)), ["938702675", "971487046"])

    def test_one_terminal_envelope_per_input(self) -> None:
        orgs = ["938702675", "971487046", "123"]
        recs = {org: CompanyRecord(org) for org in orgs}
        recs["938702675"] = sample_record("938702675")
        envelopes = assemble(orgs, recs, {}, {"run_id": "r1", "completed_at": "x"}, final=True)
        self.assertEqual([e["organisation_number"] for e in envelopes], orgs)
        result = validate(envelopes, orgs)
        self.assertTrue(result["passed"], result)
        for env in envelopes:
            self.assertIn(env["state"], STATES)
            for claim in env["claims"]:
                self.assertIn(claim["availability"], STATES)
                if claim["availability"] == "available":
                    self.assertTrue(claim["evidence_ids"])
                else:
                    self.assertIsNone(claim["value"])  # absence is never turned into zero

    def test_evidence_ids_resolve(self) -> None:
        env = envelope_for(sample_record())
        ids = {e["id"] for e in env["evidence"]}
        for claim in env["claims"]:
            for ev in claim["evidence_ids"]:
                self.assertIn(ev, ids)


class RefreshTests(unittest.TestCase):
    def test_same_snapshot_is_idempotent(self) -> None:
        first = envelope_for(sample_record(), None, "r1")
        again = envelope_for(sample_record(), json.loads(json.dumps(first)), "r2")
        self.assertEqual(again["changes"], [])
        self.assertEqual(sorted(c["id"] for c in first["claims"]), sorted(c["id"] for c in again["claims"]))
        firsts = {c["id"]: c.get("first_observed_at") for c in first["claims"] if c["availability"] == "available"}
        for claim in again["claims"]:
            if claim["availability"] == "available":
                self.assertEqual(claim["first_observed_at"], firsts[claim["id"]])
        self.assertEqual(again["refresh"]["history"], ["r1", "r2"])

    def test_changes_detected_with_both_sides_evidence(self) -> None:
        first = envelope_for(sample_record(employees=10, job=True), None, "r1")
        second = envelope_for(sample_record(employees=12, job=False), json.loads(json.dumps(first)), "r2")
        types = {c["change_type"] for c in second["changes"]}
        self.assertIn("changed_registered_employees", types)
        self.assertIn("closed_job", types)
        for change in second["changes"]:
            if change["previous"] is not None:
                self.assertTrue(change["previous_evidence_ids"])
                self.assertTrue(change.get("previous_evidence"))

    def test_failed_family_keeps_last_value(self) -> None:
        first = envelope_for(sample_record(job=True), None, "r1")
        rec = sample_record(job=False)
        rec.family_state["hiring"] = {"state": "failed", "reason": "job board unreachable", "checked_sources": []}
        rec.checked_fields.discard("job_posting")
        second = envelope_for(rec, json.loads(json.dumps(first)), "r2")
        self.assertNotIn("closed_job", {c["change_type"] for c in second["changes"]})
        stale = [c for c in second["claims"] if c.get("stale")]
        self.assertTrue(stale and stale[0]["field"] == "job_posting")

    def test_other_hiring_source_does_not_close_unchecked_jobs(self) -> None:
        """A careers link keeps the family available; that alone must not close jobs the feed did not re-check."""
        first = envelope_for(sample_record(job=True), None, "r1")
        rec = sample_record(job=False)
        ev = next(iter(rec.evidence))
        rec.add_claim(family="hiring", field_name="careers_page", value="https://example.no/karriere", evidence_ids=[ev], method="careers_link_on_verified_site")
        rec.checked_fields.discard("job_posting")
        rec.finalize_states()
        second = envelope_for(rec, json.loads(json.dumps(first)), "r2")
        self.assertNotIn("closed_job", {c["change_type"] for c in second["changes"]})


class SafetyAndOwnershipTests(unittest.TestCase):
    def test_literal_public_ip_refused(self) -> None:
        with self.assertRaises(khttp.UnsafeURL):
            khttp.check_url("http://93.184.216.34/")

    def test_connect_guard_refuses_loopback(self) -> None:
        with self.assertRaises(Exception):
            khttp._public_create_connection(("127.0.0.1", 9), 1)

    def test_sister_company_job_posting_rejected(self) -> None:
        from kildespor.website import _ld_employer_is_ours
        rec = CompanyRecord("926818317")
        rec.context["entity"] = {"navn": "SPEDIFY AS"}
        self.assertTrue(_ld_employer_is_ours({"hiringOrganization": {"name": "Spedify AS"}}, rec))
        self.assertFalse(_ld_employer_is_ours({"hiringOrganization": {"name": "Other Sister AS"}}, rec))
        self.assertFalse(_ld_employer_is_ours({"hiringOrganization": {"name": "Spedify AS", "taxID": "NO 938 702 675 MVA"}}, rec))

    def test_agency_credit_link_is_not_a_company_profile(self) -> None:
        from bs4 import BeautifulSoup
        from kildespor.website import _is_credit_link
        soup = BeautifulSoup('<div><p>Nettside levert av <a href="https://www.linkedin.com/company/byraa">Byrå</a></p></div>', "lxml")
        self.assertTrue(_is_credit_link(soup.find("a")))

    def test_roles_snapshot_is_stored_without_birth_dates(self) -> None:
        import gzip
        from kildespor import model
        from kildespor.http import Snapshot
        body = json.dumps({"rollegrupper": [{"roller": [{"person": {"fodselsdato": "1970-01-01", "navn": {"fornavn": "Kari"}}}]}]}).encode()
        snap = Snapshot("https://data.brreg.no/enhetsregisteret/api/enheter/938702675/roller", "https://data.brreg.no/enhetsregisteret/api/enheter/938702675/roller", 200, "2026-10-17T00:00:00Z", "b" * 64, "application/json", body)
        with tempfile.TemporaryDirectory() as tmp:
            old_dir, old_label = model.SNAPSHOT_DIR, model.SNAPSHOT_LABEL
            model.SNAPSHOT_DIR, model.SNAPSHOT_LABEL = tmp, "s"
            try:
                rel = model.store_body(snap)
            finally:
                model.SNAPSHOT_DIR, model.SNAPSHOT_LABEL = old_dir, old_label
            stored = gzip.open(Path(tmp) / "bb" / ("b" * 64 + ".redacted.json.gz")).read().decode()
        self.assertTrue(rel and rel.endswith(".redacted.json.gz"))
        self.assertNotIn("1970-01-01", stored)
        self.assertIn("Kari", stored)


if __name__ == "__main__":
    unittest.main()
