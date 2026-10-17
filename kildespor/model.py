"""Claims, evidence and per-company state.

A claim is one published fact. It always points at one or more evidence
records, and an evidence record always names the source URL, the retrieval
time, the SHA-256 of the fetched body and the span or field the fact came
from. Identifiers are deterministic hashes, so running the same source
snapshot twice produces the same ids and no duplicate records.
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Any

from .http import Snapshot, utc_now

STATES = ("available", "not_available", "blocked", "not_applicable", "ambiguous", "failed")

# When set (by the runner), every snapshot that backs a claim is stored gzipped under
# <SNAPSHOT_DIR>/<sha[:2]>/<sha>.gz and the evidence record names the path.
SNAPSHOT_DIR = None  # type: ignore[var-annotated]
SNAPSHOT_LABEL = "snapshots"


PERSONAL_KEYS = {"fodselsdato"}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items() if k not in PERSONAL_KEYS}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def store_body(snap: Snapshot) -> str | None:
    """Store the body behind a claim; role responses are stored with birth dates removed."""
    if SNAPSHOT_DIR is None or not snap.body:
        return None
    import gzip
    from pathlib import Path

    body = snap.body
    suffix = ".gz"
    if "/roller" in snap.requested_url:
        try:
            body = json.dumps(_redact(json.loads(body.decode("utf-8"))), ensure_ascii=False, sort_keys=True).encode("utf-8")
            suffix = ".redacted.json.gz"
        except (ValueError, UnicodeDecodeError):
            return None  # never store an unredactable roles body
    rel = f"{SNAPSHOT_LABEL}/{snap.sha256[:2]}/{snap.sha256}{suffix}"
    path = Path(SNAPSHOT_DIR) / snap.sha256[:2] / f"{snap.sha256}{suffix}"
    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            with gzip.open(tmp, "wb") as handle:
                handle.write(body)
            tmp.replace(path)
    except OSError:
        return None
    return rel

# Section -> families. The order is the order of the required envelope sections.
SECTIONS: dict[str, list[str]] = {
    "legal_identity_and_brand": ["legal_identity", "public_brand"],
    "annual_accounts": ["annual_accounts"],
    "leadership_and_workplaces": ["leadership", "workplaces"],
    "website_and_profiles": ["official_website", "company_profiles", "contact_channels"],
    "hiring_and_activity": ["hiring", "public_activity"],
}
FAMILIES = [family for families in SECTIONS.values() for family in families]


def _digest(*parts: Any, length: int = 16) -> str:
    raw = "\x1f".join(json.dumps(part, ensure_ascii=False, sort_keys=True, default=str) for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:length]


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class CompanyRecord:
    organisation_number: str
    claims: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    family_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    # Fields whose source was checked completely this run; refresh reports a removal only for these.
    checked_fields: set[str] = field(default_factory=set)
    requests: int = 0
    started_monotonic: float = 0.0
    runtime_ms: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- evidence -------------------------------------------------------------------
    def add_evidence(
        self,
        *,
        source_url: str,
        source_class: str,
        retrieved_at: str,
        content_sha256: str | None,
        claim_span: str | None = None,
        extraction_method: str,
        snapshot: Snapshot | None = None,
        reporting_period: str | None = None,
        access_policy: str | None = None,
    ) -> str:
        span = (claim_span or "").strip()
        if len(span) > 400:
            span = span[:397] + "..."
        ev_id = "ev-" + _digest(self.organisation_number, source_url, content_sha256, span, extraction_method, length=14)
        record: dict[str, Any] = {
            "id": ev_id,
            "source_url": source_url,
            "source_class": source_class,
            "retrieved_at": retrieved_at,
            "content_sha256": content_sha256,
            "claim_span": span or None,
            "extraction_method": extraction_method,
        }
        if reporting_period:
            record["reporting_period"] = reporting_period
        if access_policy:
            record["access_policy"] = access_policy
        if snapshot is not None:
            record["http_status"] = snapshot.status
            stored = store_body(snapshot)
            if stored:
                record["snapshot_path"] = stored
                if stored.endswith(".redacted.json.gz"):
                    record["snapshot_redaction"] = "birth dates (fodselsdato) removed before storage; content_sha256 is of the original response"
            if snapshot.final_url != source_url:
                record["final_url"] = snapshot.final_url
            if snapshot.redirect_chain:
                record["redirect_chain"] = snapshot.redirect_chain
        with self._lock:
            self.evidence.setdefault(ev_id, record)
        return ev_id

    def evidence_from_snapshot(
        self,
        snap: Snapshot,
        *,
        source_class: str,
        span: str | None,
        method: str,
        reporting_period: str | None = None,
        access_policy: str | None = None,
        url: str | None = None,
    ) -> str:
        return self.add_evidence(
            source_url=url or snap.requested_url,
            source_class=source_class,
            retrieved_at=snap.retrieved_at,
            content_sha256=snap.sha256,
            claim_span=span,
            extraction_method=method,
            snapshot=snap,
            reporting_period=reporting_period,
            access_policy=access_policy,
        )

    # -- claims ---------------------------------------------------------------------
    def add_claim(
        self,
        *,
        family: str,
        field_name: str,
        value: Any,
        evidence_ids: list[str],
        key: Any = None,
        confidence: float = 0.95,
        method: str,
        reporting_period: str | None = None,
        effective_date: str | None = None,
        relationship: str | None = None,
        label: str | None = None,
    ) -> str | None:
        if value is None or value == "" or value == [] or not evidence_ids:
            return None
        claim_key = f"{field_name}:{canonical(key if key is not None else value)}"
        claim_id = "cl-" + _digest(self.organisation_number, claim_key, length=14)
        claim: dict[str, Any] = {
            "id": claim_id,
            "key": claim_key,
            "family": family,
            "field": field_name,
            "value": value,
            "availability": "available",
            "confidence": round(float(confidence), 3),
            "evidence_ids": sorted(set(evidence_ids)),
            "extraction_method": method,
        }
        if label:
            claim["label"] = label
        if reporting_period:
            claim["reporting_period"] = reporting_period
        if effective_date:
            claim["effective_date"] = effective_date
        if relationship:
            claim["relationship"] = relationship
        with self._lock:
            for existing in self.claims:
                if existing["id"] == claim_id:
                    existing["evidence_ids"] = sorted(set(existing["evidence_ids"]) | set(claim["evidence_ids"]))
                    return claim_id
            self.claims.append(claim)
        return claim_id

    def claims_for(self, family: str) -> list[dict[str, Any]]:
        return [claim for claim in self.claims if claim["family"] == family]

    # -- availability -----------------------------------------------------------------
    def set_state(self, family: str, state: str, reason: str, *, checked_sources: list[str] | None = None) -> None:
        assert state in STATES, state
        with self._lock:
            current = self.family_state.get(family)
            # An "available" verdict is never downgraded by a later, weaker one.
            if current and current["state"] == "available" and state != "available":
                return
            self.family_state[family] = {
                "state": state,
                "reason": reason,
                "checked_sources": sorted(set(checked_sources or [])),
                "decided_at": utc_now(),
            }

    def error(self, module: str, message: str, url: str | None = None) -> None:
        with self._lock:
            self.errors.append({"module": module, "error": str(message)[:300], "url": url, "at": utc_now()})

    def finalize_states(self) -> None:
        for family in FAMILIES:
            has_claims = any(claim["family"] == family for claim in self.claims)
            if has_claims:
                self.set_state(family, "available", self.family_state.get(family, {}).get("reason") or "evidence-backed claims published")
            elif family not in self.family_state:
                self.set_state(family, "failed", "not attempted within the run's time budget")
