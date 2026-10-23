"""Offline tests for the brief (synthesis), the viewer (site), the app export and the Q&A layer.

Run with ``python -m unittest discover -s tests``. No network.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kildespor.envelope import build_envelope
from kildespor.model import FAMILIES, CompanyRecord
from kildespor.synthesis import build_synthesis, claim_value, internal_field

REG = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
ACC = "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"


def _ev(rec: CompanyRecord, url: str, span: str, cls: str = "official_registry") -> str:
    return rec.add_evidence(source_url=url, source_class=cls, retrieved_at="2026-10-17T06:00:00Z", content_sha256="b" * 64, claim_span=span, extraction_method="test")


def rich_record(org: str = "938702675") -> CompanyRecord:
    rec = CompanyRecord(org)
    reg = REG.format(org=org)
    e = _ev(rec, reg, 'navn = "FJORD DATA AS"')
    rec.add_claim(family="legal_identity", field_name="legal_name", value="FJORD DATA AS", evidence_ids=[e], method="t")
    rec.add_claim(family="legal_identity", field_name="legal_form", value={"code": "AS", "description": "Aksjeselskap"}, evidence_ids=[e], method="t")
    rec.add_claim(family="legal_identity", field_name="municipality", value="OSLO", evidence_ids=[e], method="t")
    rec.add_claim(family="legal_identity", field_name="founded_date", value="2015-03-01", evidence_ids=[e], method="t")
    rec.add_claim(family="legal_identity", field_name="industry_code", value={"code": "62.200", "description": "Konsulentvirksomhet tilknyttet informasjonsteknologi", "rank": 1}, evidence_ids=[e], method="t")
    rec.add_claim(family="legal_identity", field_name="registered_employees", value=42, evidence_ids=[e], method="t", effective_date="2026-09-01")
    rec.add_claim(family="legal_identity", field_name="registered_activity", value="Utvikling av programvare og IT-konsulenttjenester.", evidence_ids=[e], method="t")
    roles = _ev(rec, reg + "/roller", "DAGL", "official_roles")
    rec.add_claim(family="leadership", field_name="role", value={"role": "Daglig leder", "role_code": "DAGL", "group_code": "DAGL", "name": "Kari Nordmann"}, key=["DAGL", "Kari"], evidence_ids=[roles], method="t")
    rec.add_claim(family="leadership", field_name="role", value={"role": "Styrets leder", "role_code": "LEDE", "group_code": "STYR", "name": "Ola Hansen"}, key=["LEDE", "Ola"], evidence_ids=[roles], method="t")
    rec.add_claim(family="leadership", field_name="role", value={"role": "Styremedlem", "role_code": "MEDL", "group_code": "STYR", "name": "Per Berg"}, key=["MEDL", "Per"], evidence_ids=[roles], method="t")
    rec.add_claim(family="leadership", field_name="group_relationship", value={"relationship": "parent", "organisation_number": "999999999", "name": "FJORD GRUPPEN AS", "ownership": "100%", "since": "2024-05-02"}, evidence_ids=[e], method="t", relationship="parent")
    acc = _ev(rec, ACC.format(org=org), "SELSKAP 2025", "official_annual_accounts")
    for year, revenue, result, equity, assets in (("2024", 40e6, 2e6, 10e6, 30e6), ("2025", 50e6, -1e6, 9e6, 32e6)):
        for metric, amount in (("revenue", revenue), ("operating_result", result), ("annual_result", result), ("equity", equity), ("total_assets", assets)):
            rec.add_claim(family="annual_accounts", field_name=f"financial.{metric}", value={"amount": amount, "currency": "NOK", "account_type": "SELSKAP", "fiscal_year": year}, key=[metric, year], evidence_ids=[acc], method="t", reporting_period=f"{year}-01-01/{year}-12-31")
    site = _ev(rec, "https://fjorddata.no/", "Org.nr 938 702 675", "company_website")
    rec.add_claim(family="official_website", field_name="official_website", value={"url": "https://fjorddata.no/", "domain": "fjorddata.no", "identity_method": "organisation_number_on_site"}, evidence_ids=[site], method="t")
    rec.add_claim(family="public_brand", field_name="self_description", value="Fjord Data builds booking software for Norwegian ferry operators.", evidence_ids=[site], method="t")
    rec.add_claim(family="company_profiles", field_name="social_profile", value={"platform": "linkedin", "url": "https://www.linkedin.com/company/fjorddata", "handle": "fjorddata"}, evidence_ids=[site], method="t")
    job = _ev(rec, "https://fjorddata.no/karriere", "Senior utvikler", "company_website")
    rec.add_claim(family="hiring", field_name="job_posting", value={"title": "Senior utvikler", "url": "https://fjorddata.no/karriere/senior", "published": "2026-10-01"}, key=["job", "senior"], evidence_ids=[job], method="t")
    rec.set_state("hiring", "available", "1 single job ad(s) read on the company's own careers channel", checked_sources=["https://fjorddata.no/karriere", "https://pam-stilling-feed.nav.no/api/v1/feed (last 45 days)"])
    news = _ev(rec, "https://fjorddata.no/nyheter/ny-avtale", "Ny avtale med Fjord1", "company_website")
    rec.add_claim(family="public_activity", field_name="company_news", value={"title": "Ny avtale med Fjord1", "url": "https://fjorddata.no/nyheter/ny-avtale", "published": "2026-09-20"}, key=["news", "ny-avtale"], evidence_ids=[news], method="t", effective_date="2026-09-20")
    for family in FAMILIES:
        if not rec.claims_for(family):
            rec.set_state(family, "not_available", "checked, nothing found", checked_sources=[reg])
    rec.finalize_states()
    return rec


def bare_holding(org: str = "919263601") -> CompanyRecord:
    rec = CompanyRecord(org)
    reg = REG.format(org=org)
    e = _ev(rec, reg, 'navn = "NORDMANN HOLDING AS"')
    rec.add_claim(family="legal_identity", field_name="legal_name", value="NORDMANN HOLDING AS", evidence_ids=[e], method="t")
    rec.add_claim(family="legal_identity", field_name="legal_form", value={"code": "AS", "description": "Aksjeselskap"}, evidence_ids=[e], method="t")
    rec.add_claim(family="legal_identity", field_name="municipality", value="BÆRUM", evidence_ids=[e], method="t")
    rec.add_claim(family="legal_identity", field_name="industry_code", value={"code": "64.210", "description": "Holdingselskaper", "rank": 1}, evidence_ids=[e], method="t")
    acc = _ev(rec, ACC.format(org=org), "SELSKAP 2025", "official_annual_accounts")
    for metric, amount in (("revenue", 0.0), ("annual_result", 250000.0), ("equity", 900000.0), ("total_assets", 1000000.0)):
        rec.add_claim(family="annual_accounts", field_name=f"financial.{metric}", value={"amount": amount, "currency": "NOK", "account_type": "SELSKAP", "fiscal_year": "2025"}, key=[metric], evidence_ids=[acc], method="t")
    rec.set_state("official_website", "not_available", "no register-listed website and no name-derived domain shows this organisation number", checked_sources=[reg])
    rec.set_state("hiring", "not_available", "no active ad in NAV's vacancy feed names this organisation number", checked_sources=["https://pam-stilling-feed.nav.no/api/v1/feed"])
    rec.set_state("company_profiles", "not_available", "no verified company website to read company-owned profiles from")
    rec.set_state("public_activity", "not_available", "no dated activity found")
    rec.set_state("leadership", "not_available", "the register lists no active public roles", checked_sources=[reg + "/roller"])
    rec.set_state("workplaces", "not_available", "the register lists no active subunits")
    rec.set_state("public_brand", "not_available", "no verified website")
    rec.set_state("contact_channels", "not_available", "no phone in the register and no verified website")
    rec.finalize_states()
    return rec


def sole_proprietor(org: str = "912345678") -> CompanyRecord:
    rec = CompanyRecord(org)
    e = _ev(rec, REG.format(org=org), 'navn = "KARI FRISØR"')
    rec.add_claim(family="legal_identity", field_name="legal_name", value="KARI FRISØR", evidence_ids=[e], method="t")
    rec.add_claim(family="legal_identity", field_name="legal_form", value={"code": "ENK", "description": "Enkeltpersonforetak"}, evidence_ids=[e], method="t")
    rec.set_state("annual_accounts", "not_available", "the Register of Company Accounts returned no normalised accounts (HTTP 404)", checked_sources=[ACC.format(org=org)])
    for family in FAMILIES:
        if not rec.claims_for(family) and family not in rec.family_state:
            rec.set_state(family, "not_available", "checked, nothing found")
    rec.finalize_states()
    return rec


def published(env: dict) -> dict:
    """The on-disk shape (coordinator's contract.publish) when available, else a local equivalent."""
    try:
        from kildespor.contract import publish

        return publish(env)
    except Exception:  # pragma: no cover - contract module absent
        out = dict(env)
        claims = []
        for c in env["claims"]:
            c = dict(c)
            if c["field"] in ("careers_page", "job_posting") and c.get("availability") == "available":
                c["internal_field"], c["field"] = c["field"], "hiring_signal"
            if c["field"] == "company_news" and c.get("availability") == "available":
                c["internal_field"], c["field"] = "company_news", "dated_news"
            if isinstance(c.get("value"), dict) and c.get("availability") == "available":
                c["detail"] = c["value"]
                c["value"] = c["value"].get("url") or c["value"].get("title")
            claims.append(c)
        out["claims"] = claims
        return out


def envelope_of(rec: CompanyRecord, run_id: str = "r1") -> dict:
    syn = build_synthesis(rec, None, baseline=True)
    return build_envelope(rec, run={"run_id": run_id, "completed_at": "2026-10-17T07:00:00Z"}, synthesis=syn, changes=[], refresh_meta={"baseline": True, "history": [run_id]})


def all_sentences(syn: dict) -> list[dict]:
    return [s for sec in syn["sections"] for s in sec["sentences"]] + syn["summary_sentences"]


class SynthesisTests(unittest.TestCase):
    def test_rich_company_answers_the_six_questions(self) -> None:
        rec = rich_record()
        syn = build_synthesis(rec)
        keys = [sec["key"] for sec in syn["sections"]]
        self.assertEqual(keys, ["what", "who", "money", "changed", "signals", "unknown"])
        self.assertTrue(syn["summary"].startswith("FJORD DATA AS is a private limited company in Oslo, founded in 2015"))
        self.assertIn("computer consultancy", syn["summary"])
        text = " ".join(sec["text"] for sec in syn["sections"])
        self.assertIn("The general manager (daglig leder) is Kari Nordmann", text)
        self.assertIn("FJORD GRUPPEN AS", text)
        self.assertIn("revenue of NOK 50.0 million (up NOK 10.0 million, +25% on 2024)", text)
        self.assertIn("net loss of NOK 1.0 million", text)
        self.assertIn("Hiring now: 1 open job ad", text)
        self.assertIn("2026-10-01", text)
        self.assertIn("Ny avtale med Fjord1", text)
        self.assertEqual(syn["coverage"]["score"], 5)
        self.assertTrue(any(s["key"] == "hiring" for s in syn["signals"]))
        self.assertTrue(any(s["key"] == "loss" for s in syn["signals"]))
        # the timeline is newest first
        dates = [t["date"] for t in syn["timeline"]]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_every_fact_sentence_is_cited_with_real_ids(self) -> None:
        for rec in (rich_record(), bare_holding(), sole_proprietor()):
            syn = build_synthesis(rec)
            claim_ids = {c["id"] for c in rec.claims}
            for s in all_sentences(syn):
                if s["basis"] in ("fact", "computed"):
                    self.assertTrue(s["claim_ids"], s["text"])
                    self.assertTrue(s["sources"] or s.get("checked_sources"), s["text"])
                self.assertTrue(set(s["claim_ids"]) <= claim_ids, s["text"])
                self.assertTrue(set(s["evidence_ids"]) <= set(rec.evidence), s["text"])
                for src in s["sources"]:
                    self.assertTrue(src["url"].startswith("http"))
                    self.assertTrue(src["retrieved_at"])

    def test_bare_holding_company(self) -> None:
        syn = build_synthesis(bare_holding())
        self.assertIn("registered as a holding company", syn["summary"])
        self.assertIn("in Bærum", syn["summary"])
        self.assertNotIn("Hiring now", syn["summary"])
        self.assertFalse(any(s["key"] in ("hiring", "careers_page") for s in syn["signals"]))
        unknown = {u["family"]: u for u in syn["unknowns"]}
        self.assertEqual(unknown["official_website"]["state"], "not_available")
        self.assertIn("hiring", unknown)
        self.assertIn("No public roles are recorded", " ".join(sec["text"] for sec in syn["sections"]))
        self.assertEqual(syn["coverage"]["score"], 2)

    def test_sole_proprietor_has_an_accounts_reason(self) -> None:
        syn = build_synthesis(sole_proprietor())
        money = next(sec for sec in syn["sections"] if sec["key"] == "money")
        self.assertIn("sole proprietorships are generally not required to file annual accounts", money["text"])
        self.assertNotIn("NOK 0", money["text"])

    def test_states_are_distinguished(self) -> None:
        rec = bare_holding()
        rec.family_state["official_website"]["state"] = "blocked"
        rec.family_state["official_website"]["reason"] = "robots.txt disallows https://x.no/"
        rec.family_state["company_profiles"]["state"] = "ambiguous"
        syn = build_synthesis(rec)
        unknown = {u["family"]: u for u in syn["unknowns"]}
        self.assertEqual(unknown["official_website"]["state"], "blocked")
        self.assertEqual(unknown["company_profiles"]["state"], "ambiguous")
        text = next(sec for sec in syn["sections"] if sec["key"] == "unknown")["text"]
        self.assertIn("blocked by the source", text)
        self.assertIn("not proven to be this company", text)

    def test_published_shape_reads_the_same(self) -> None:
        rec = rich_record()
        env = published(envelope_of(rec))
        rec2 = CompanyRecord(rec.organisation_number)
        rec2.claims = [c for c in env["claims"] if c.get("availability") == "available"]
        rec2.evidence = {e["id"]: e for e in env["evidence"]}
        rec2.family_state = dict(rec.family_state)
        self.assertEqual(build_synthesis(rec)["summary"], build_synthesis(rec2)["summary"])
        fields = {internal_field(c) for c in rec2.claims}
        self.assertTrue({"job_posting", "company_news", "official_website"} <= fields)
        self.assertNotIn("careers_page", fields)
        news = next(c for c in rec2.claims if internal_field(c) == "company_news")
        self.assertEqual(claim_value(news)["title"], "Ny avtale med Fjord1")

    def test_run_to_run_changes_and_stale_markers(self) -> None:
        rec = rich_record()
        job = next(c for c in rec.claims if c["field"] == "job_posting")
        stale = dict(next(c for c in rec.claims if c["field"] == "social_profile"))
        stale["stale"] = True
        rec.claims = [c for c in rec.claims if c["field"] != "social_profile"] + [stale]
        changes = [{"change_type": "new_job", "field": "job_posting", "previous": None, "current": job["value"], "current_claim_id": job["id"], "current_evidence_ids": job["evidence_ids"], "previous_evidence_ids": [], "materiality": "medium", "summary": "new job"}]
        syn = build_synthesis(rec, changes, baseline=False)
        changed = next(sec for sec in syn["sections"] if sec["key"] == "changed")
        self.assertIn("Since the previous run, 1 change was detected: new job ad: Senior utvikler", changed["text"])
        self.assertIn("carried forward", changed["text"])
        self.assertTrue(any(s["basis"] == "carried_forward" for s in changed["sentences"]))

    def test_deterministic(self) -> None:
        self.assertEqual(json.dumps(build_synthesis(rich_record()), sort_keys=True), json.dumps(build_synthesis(rich_record()), sort_keys=True))


class SiteTests(unittest.TestCase):
    def build(self, shape: str) -> tuple[Path, list[dict]]:
        from kildespor.site import build_site

        envs = [envelope_of(rich_record()), envelope_of(bare_holding()), envelope_of(sole_proprietor())]
        if shape == "published":
            envs = [published(e) for e in envs]
        out = Path(tempfile.mkdtemp()) / "site"
        build_site(envs, out, {"run": {"run_id": "r1", "completed_at": "2026-10-17T07:00:00Z"}})
        return out, envs

    def test_split_files_and_routes(self) -> None:
        for shape in ("internal", "published"):
            out, envs = self.build(shape)
            html = (out / "index.html").read_text()
            for route in ("#/research", "#/companies/", "#/compare", "#/refresh"):
                self.assertIn(route, html)
            self.assertNotIn("https://cdn", html)
            self.assertIn('src="data/index.js"', html)
            index = json.loads((out / "data" / "index.json").read_text())
            self.assertEqual([r["o"] for r in index["rows"]], [e["organisation_number"] for e in envs])
            rich = index["rows"][0]
            self.assertEqual((rich["j"], rich["nw"], rich["cv"]), (1, 1, 5), shape)
            self.assertNotIn("cp", rich)
            self.assertEqual(rich["w"], "https://fjorddata.no/")
            self.assertEqual(rich["so"], ["linkedin"])
            bare = index["rows"][1]
            self.assertEqual((bare["j"], bare["w"]), (0, None))
            for env in envs:
                org = env["organisation_number"]
                data = json.loads((out / "data" / "companies" / f"{org}.json").read_text())
                js = (out / "data" / "companies" / f"{org}.js").read_text()
                self.assertTrue(js.startswith('kildesporLoad("company",'))
                self.assertEqual(data["org"], org)
                # every evidence id a claim cites resolves to a source with URL, time and hash
                for claim in data["cl"]:
                    for ev_id in claim["e"]:
                        source = data["src"][data["ev"][ev_id]["r"]]
                        self.assertTrue(source["u"] and source["t"] and source["h"])
                self.assertTrue(data["syn"]["summary"])

    def test_placeholder_and_bad_input_envelopes(self) -> None:
        from kildespor.envelope import placeholder_envelope
        from kildespor.site import build_site

        envs = [placeholder_envelope("12345", {"run_id": "r1"}, "input is not a nine-digit Norwegian organisation number"), envelope_of(bare_holding())]
        out = Path(tempfile.mkdtemp()) / "site"
        build_site(envs, out, None)
        rows = json.loads((out / "data" / "index.json").read_text())["rows"]
        self.assertEqual([r["o"] for r in rows], ["12345", "919263601"])
        data = json.loads((out / "data" / "companies" / "12345.json").read_text())
        self.assertEqual(data["org"], "12345")

    def test_script_parses(self) -> None:
        import re
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        out, _ = self.build("internal")
        html = (out / "index.html").read_text()
        for i, script in enumerate(re.findall(r"<script>(.*?)</script>", html, flags=re.S)):
            path = out / f"check{i}.js"
            path.write_text(script)
            result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr[:500])


class ExportTests(unittest.TestCase):
    def test_app_dataset_from_either_shape(self) -> None:
        from kildespor.export_profiles import build

        for shape in ("internal", "published"):
            envs = [envelope_of(rich_record()), envelope_of(bare_holding())]
            if shape == "published":
                envs = [published(e) for e in envs]
            data = build(envs)
            rich, bare = data["companies"]
            self.assertEqual(rich["website"], "https://fjorddata.no/", shape)
            self.assertEqual([j["title"] for j in rich["jobs"]], ["Senior utvikler"], shape)
            # an "apply here" link from the hiring verdict's checked sources, never a hiring fact
            self.assertEqual(rich["careersPages"], [{"url": "https://fjorddata.no/karriere", "sources": [], "checked": True}], shape)
            self.assertEqual(rich["profiles"][0]["platform"], "linkedin", shape)
            self.assertEqual(rich["coverage"]["hiring"]["state"], "available")
            self.assertTrue(any(a["kind"].startswith("dated news") for a in rich["activity"]))
            # no hiring badge without a job ad, whatever else the activity family holds
            self.assertNotEqual(bare["coverage"]["hiring"]["state"], "available")
            self.assertEqual(rich["brief"][0]["heading"], "In 20 seconds")
            ids = {e["id"] for e in rich["evidence"]}
            for item in rich["brief"] + rich["facts"] + rich["jobs"]:
                self.assertTrue(set(item["sources"]) <= ids)

    def test_stale_and_change_sides_survive(self) -> None:
        from kildespor.export_profiles import company

        env = envelope_of(rich_record())
        site = next(c for c in env["claims"] if c["field"] == "official_website")
        site["stale"] = True
        env["claims"].append(dict(next(c for c in env["claims"] if c["field"] == "legal_name"), stale=True, id="cl-stale"))
        env["changes"] = [{"change_type": "changed_legal_name", "previous": "OLD AS", "current": "FJORD DATA AS", "previous_evidence_ids": ["ev-old"], "current_evidence_ids": ["ev-new"], "materiality": "high"}]
        out = company(env)
        self.assertTrue(any(f["stale"] and "not re-checked" in f["label"] for f in out["facts"]))
        self.assertEqual(out["changes"][0]["sources"], ["ev-new", "ev-old"])


class AskTests(unittest.TestCase):
    def run_of(self):
        from kildespor.ask import Run

        return Run([published(envelope_of(rich_record())), published(envelope_of(bare_holding())), published(envelope_of(sole_proprietor()))])

    def test_company_questions_cite_sources(self) -> None:
        run = self.run_of()
        ceo = run.answer("who is the CEO of FJORD DATA AS")
        self.assertTrue(ceo["supported"])
        self.assertIn("Kari Nordmann", ceo["answer"])
        self.assertTrue(ceo["citations"][0]["source_url"].startswith("https://data.brreg.no/"))
        hiring = run.answer("is 938702675 hiring")
        self.assertIn("Senior utvikler", hiring["answer"])
        nope = run.answer("is NORDMANN HOLDING AS hiring")
        self.assertFalse(nope["supported"])
        self.assertIn("not proof", nope["answer"])
        does = run.answer("what does FJORD DATA AS do")
        self.assertIn("computer consultancy", does["answer"])

    def test_screening(self) -> None:
        run = self.run_of()
        oslo = run.answer("hiring companies in Oslo")
        self.assertEqual([r["organisation_number"] for r in oslo["results"]], ["938702675"])
        self.assertTrue(all(c["source_url"] for r in oslo["results"] for c in r["citations"]))
        self.assertEqual(run.answer("revenue over 10 MNOK")["count"], 1)
        self.assertEqual(run.answer("companies with a website but no social profile")["count"], 0)
        self.assertEqual(run.answer("companies without a website")["count"], 2)
        self.assertEqual(run.answer("loss-making companies")["count"], 1)
        self.assertTrue(run.answer("what changed")["notes"])

    def test_unsupported(self) -> None:
        run = self.run_of()
        self.assertFalse(run.answer("what is the sentiment around FJORD DATA AS")["supported"])
        self.assertFalse(run.answer("what is the weather tomorrow")["supported"])


if __name__ == "__main__":
    unittest.main()
