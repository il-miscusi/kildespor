"""Deterministic, evidence-bounded company briefs.

The brief opens with two sentences a buyer, applicant or investor can act on:
what the company does, how big it is and which way its filed figures moved
(revenue and result with their years, computed from the cited accounts), then
whether it is hiring now and its latest dated activity. Two more say what
changed (run to run, or on the record: the latest register change and the
latest filing against the year before) and what this run could not establish.
The sections follow, in the order of Builderr's own sample profile: Company
overview, Latest financials, Who runs it?, Working here, Recent activity, What
changed, What we could not establish.

Every sentence carries the claim ids, evidence ids and source links it rests
on, and passes a support check before it is kept (``sentence_support``): each
number, quoted passage and name it states must be in the claims it cites, or
be plain arithmetic on two of them. Nothing is inferred beyond that
arithmetic (year-on-year change, equity ratio), which is labelled as
computed; anything the run could not establish is listed with its state and
reason, never filled in. No language model and no network access: the same
claims always give the same text.

The builder reads claims in both shapes: the internal one (dict values, fields
``careers_page``, ``job_posting``, ``company_news``) and the published one
written to disk (scalar ``value`` with the dict in ``detail``, fields
``hiring_signal`` and ``dated_news``), so carried-forward claims read back from
an earlier envelope are handled like fresh ones. Only a single job ad says a
company is hiring; a careers page (still found in older envelopes) is read as
``careers_page`` and never cited as a hiring fact.
"""
from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import urlsplit

from .model import CompanyRecord

# -- claim access (internal and published shapes) ----------------------------------------------


def claim_value(claim: dict[str, Any]) -> Any:
    """The claim's full value: ``detail`` (published shape) when present, else ``value``."""
    detail = claim.get("detail")
    return detail if detail is not None else claim.get("value")


def internal_field(claim: dict[str, Any]) -> str:
    """The internal field name of a claim in either shape."""
    field = claim.get("internal_field") or claim.get("field") or ""
    if field == "hiring_signal":
        # Older envelopes also published careers pages here; only a titled ad is a job posting.
        value = claim_value(claim)
        if claim.get("signal_type") == "careers_page" or not (isinstance(value, dict) and value.get("title")):
            return "careers_page"
        return "job_posting"
    if field == "dated_news":
        return "company_news"
    return field


def news_date(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("published") or value.get("date") or value.get("published_at") or "")[:10]


def _nav_ad(claim: dict[str, Any]) -> bool:
    value = claim_value(claim)
    return str(claim.get("extraction_method") or "").startswith("nav") or (isinstance(value, dict) and value.get("board") == "arbeidsplassen.nav.no")


def job_date(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("published") or value.get("date_posted") or "")[:10]


# -- wording -----------------------------------------------------------------------------------

LEGAL_FORMS_EN = {
    "AS": "private limited company", "ASA": "public limited company", "ENK": "sole proprietorship",
    "ANS": "general partnership", "DA": "partnership with shared liability", "NUF": "Norwegian branch of a foreign company",
    "SA": "cooperative", "BRL": "housing cooperative", "ESEK": "condominium owners' association", "STI": "foundation",
    "FLI": "association", "KS": "limited partnership", "SAM": "co-ownership", "IKS": "inter-municipal company",
    "KF": "municipal enterprise", "BBL": "housing cooperative association", "SF": "state enterprise", "KOMM": "municipality",
    "FYLK": "county authority", "STAT": "state body", "ORGL": "organisational unit", "PRE": "shipping partnership",
    "BA": "company with limited liability", "GFS": "mutual insurance company", "SPA": "savings bank", "KBO": "estate in bankruptcy",
    "OPMV": "VAT-registered unit", "AAFY": "unit of an unregistered foreign company", "ADOS": "administrative unit",
    "KIRK": "church body", "PK": "pension fund", "TVAM": "compulsory-registered unit", "VPFO": "securities fund",
    "UTLA": "foreign entity", "EOFG": "European economic interest grouping", "BO": "other bankruptcy estate",
}
# Legal forms that are generally not obliged to file annual accounts with the Register of Company Accounts.
NOT_OBLIGED_TO_FILE = {
    "ENK": "sole proprietorships are generally not required to file annual accounts",
    "ANS": "general partnerships whose partners are all individuals are generally not required to file annual accounts",
    "DA": "partnerships whose partners are all individuals are generally not required to file annual accounts",
    "FLI": "associations file annual accounts only above certain size thresholds",
    "SAM": "co-ownerships are not required to file annual accounts",
    "NUF": "a branch of a foreign company normally files its foreign parent's accounts, which the Norwegian accounts API does not normalise",
}
# Plain phrases for industries where "working in <label>" reads badly.
INDUSTRY_PHRASES = {
    "64.21": "registered as a holding company",
    "64.22": "registered as a financing conduit",
    "64.32": "registered as an investment company",
    "68.20": "renting out and operating its own or leased real estate",
    "68.12": "developing building projects",
    "68.11": "buying and selling its own real estate",
    "70.10": "registered as a head office",
    "97.00": "registered as a housing cooperative or condominium",
    "00.00": "",
}
FAMILY_LABELS = {
    "legal_identity": "legal identity",
    "public_brand": "public brand and self-description",
    "annual_accounts": "filed annual accounts",
    "leadership": "leadership and board",
    "workplaces": "registered workplaces",
    "official_website": "official website",
    "company_profiles": "social media profiles",
    "contact_channels": "contact channels",
    "hiring": "single job ads",
    "public_activity": "dated news",
}
STATE_SHORT = {
    "not_available": "not available",
    "ambiguous": "ambiguous, withheld",
    "failed": "failed, could not be checked",
    "blocked": "blocked by the source",
    "not_applicable": "not applicable",
}
STATE_LONG = {
    "ambiguous": "ambiguous: a candidate was found but not proven to be this company, so it is withheld",
    "failed": "failed: it could not be checked in this run",
    "blocked": "blocked by the source",
    "not_applicable": "not applicable",
}
GENERIC_REASONS = {"checked, nothing found", "no evidence-backed value found", "checked, none found"}
STATE_WORDS = {
    "not_available": "checked, none found",
    "blocked": "blocked by the source",
    "failed": "could not be checked in this run",
    "ambiguous": "a candidate was found but not proven to be this company, so it is withheld",
    "not_applicable": "does not apply",
}
# The five data areas of Builderr's own sample viewer.
COVERAGE_AREAS = [
    ("company", "Company record"),
    ("financials", "Financials"),
    ("people_places", "People & locations"),
    ("website", "Company website"),
    ("footprint", "Public footprint"),
]
ROLE_EN = {
    "DAGL": "general manager", "LEDE": "board chair", "NEST": "deputy chair", "MEDL": "board member", "VARA": "deputy board member",
    "REVI": "auditor", "REGN": "accountant", "INNH": "owner", "DTPR": "partner (shared liability)", "DTSO": "partner (joint liability)",
    "KOMP": "general partner", "FFØR": "business manager", "KONT": "contact person", "HFOR": "head office abroad",
    "BEST": "managing shipowner", "KDEB": "estate administrator", "SAM": "co-owner", "OBS": "observer", "REPR": "representative",
}

_NACE: dict[str, str] | None = None


def nace_en(code: str | None) -> str | None:
    """Statistics Norway's English label for an SN2025 industry code (5-digit), or its division (2-digit)."""
    global _NACE
    if _NACE is None:
        _NACE = dict(line.split("\t", 1) for line in NACE_EN_TABLE.strip().splitlines())
    if not code:
        return None
    return _NACE.get(code) or _NACE.get(code[:2])


def _lc(text: str) -> str:
    return text[:1].lower() + text[1:] if text and not text[:2].isupper() else text


def money(amount: float | None, currency: str | None = "NOK") -> str:
    if amount is None:
        return "not reported"
    currency = currency or "NOK"
    value = float(amount)
    sign = "-" if value < 0 else ""
    a = abs(value)
    if a >= 1e9:
        text = f"{a / 1e9:.2f} billion"
    elif a >= 1e6:
        text = f"{a / 1e6:.1f} million"
    elif a >= 1e4:
        text = f"{a / 1e3:,.0f} thousand"
    else:
        text = f"{a:,.0f}"
    return f"{currency} {sign}{text}"


def _signed_money(amount: float, currency: str | None) -> str:
    return ("up " if amount >= 0 else "down ") + money(abs(amount), currency)


def _pct(change: float) -> str:
    text = f"{change * 100:+.0f}%"
    return "0%" if text in ("+0%", "-0%") else text


def _result_words(amount: float, currency: str | None, kind: str) -> str:
    """'net profit of NOK 1.2 million' / 'operating loss of NOK 300 thousand'."""
    if amount == 0:
        return f"{kind} result of {money(0, currency)}"
    return f"{kind} {'loss' if amount < 0 else 'profit'} of {money(abs(amount), currency)}"


def _quote(text: str, limit: int = 180) -> str:
    text = " ".join(str(text).split()).rstrip(" .")
    if len(text) <= limit:
        return text
    cut = text[:limit]
    clause = max(cut.rfind(". "), cut.rfind("; "), cut.rfind(", "))
    head = cut[:clause] if clause >= 60 else cut.rsplit(" ", 1)[0]
    return head.rstrip(",;. ") + " …"


def _host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or url).lower()
    except ValueError:
        return url
    return host[4:] if host.startswith("www.") else host


def _place(municipality: str | None) -> str:
    if not municipality:
        return ""
    return " ".join(part.capitalize() if part.lower() not in ("i", "og") else part.lower() for part in str(municipality).replace("-", "- ").split()).replace("- ", "-")


def _list(items: list[str], limit: int = 4) -> str:
    items = [i for i in items if i]
    if len(items) > limit:
        items = items[:limit] + [f"{len(items) - limit} more"]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# -- builder -----------------------------------------------------------------------------------


class Builder:
    def __init__(self, rec: CompanyRecord, extra_evidence: dict[str, dict[str, Any]] | None = None) -> None:
        self.rec = rec
        self.evidence = dict(extra_evidence or {})
        self.evidence.update(rec.evidence)
        self.by_field: dict[str, list[dict[str, Any]]] = {}
        for claim in rec.claims:
            if claim.get("availability", "available") != "available":
                continue
            field = internal_field(claim)
            norm = dict(claim)
            norm["field"] = field
            norm["value"] = claim_value(claim)
            self.by_field.setdefault(field, []).append(norm)
        self.sections: list[dict[str, Any]] = []
        self.signals: list[dict[str, Any]] = []

    # claims
    def claims(self, field: str, include_historical: bool = True) -> list[dict[str, Any]]:
        items = self.by_field.get(field, [])
        return items if include_historical else [c for c in items if not c.get("historical")]

    def first(self, field: str) -> dict[str, Any] | None:
        items = [c for c in self.claims(field) if not c.get("historical")]
        fresh = [c for c in items if not c.get("stale")]
        return (fresh or items or [None])[0]

    # citations
    def sources(self, claims: Iterable[dict[str, Any]], extra_ids: Iterable[str] = (), limit: int = 2) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        ids = [e for c in claims for e in c.get("evidence_ids", [])] + list(extra_ids)
        for ev_id in ids:
            ev = self.evidence.get(ev_id)
            if not ev:
                continue
            url = ev.get("final_url") or ev.get("source_url")
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({"url": url, "retrieved_at": ev.get("retrieved_at")})
            if len(out) >= limit:
                break
        return out

    def sentence(self, text: str, claims: list[dict[str, Any]], basis: str = "fact", extra_evidence: Iterable[str] = (), checked: Iterable[str] = ()) -> dict[str, Any]:
        claims = [c for c in claims if c]
        extra = [e for e in extra_evidence if e]
        evidence_ids = sorted({e for c in claims for e in c.get("evidence_ids", [])} | set(extra))
        out: dict[str, Any] = {
            "text": text,
            "claim_ids": sorted({c["id"] for c in claims if c.get("id")}),
            "evidence_ids": evidence_ids,
            "sources": self.sources(claims, extra),
            "basis": basis,
        }
        if any(c.get("stale") for c in claims):
            out["basis"] = "carried_forward"
        elif not claims and not extra and basis == "fact":
            out["basis"] = "absence"  # a statement about what the checked sources did not contain
        checked = [u for u in checked if u]
        if checked and (not out["sources"] or out["basis"] in ("absence", "unknown")):
            out["checked_sources"] = sorted(set(checked))[:3]
        if out["basis"] == "fact":
            del out["basis"]  # the default: a statement of what the cited claims say
        return out

    def add(self, key: str, heading: str, question: str, sentences: list[dict[str, Any] | None]) -> None:
        sentences = [s for s in sentences if s and s.get("text")]
        if not sentences:
            return
        self.sections.append({
            "key": key,
            "heading": heading,
            "question": question,
            "text": " ".join(s["text"] for s in sentences),
            "sentences": sentences,
        })

    def signal(self, key: str, text: str, claims: list[dict[str, Any]], tone: str = "neutral") -> None:
        self.signals.append({
            "key": key,
            "text": text,
            "tone": tone,
            "claim_ids": sorted({c["id"] for c in claims if c.get("id")}),
            "evidence_ids": sorted({e for c in claims for e in c.get("evidence_ids", [])}),
        })

    def state(self, family: str) -> dict[str, Any]:
        return self.rec.family_state.get(family) or {}


def _accounts(b: Builder) -> dict[tuple[str, str], dict[str, dict[str, Any]]]:
    stmts: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for field, items in b.by_field.items():
        if not field.startswith("financial."):
            continue
        metric = field.split(".", 1)[1]
        for c in items:
            v = c["value"]
            if not isinstance(v, dict) or not v.get("fiscal_year"):
                continue
            key = (str(v.get("account_type") or "SELSKAP"), str(v["fiscal_year"]))
            stmts.setdefault(key, {}).setdefault(metric, c)
    return stmts


def _amount(c: dict[str, Any] | None) -> float | None:
    if not c or not isinstance(c.get("value"), dict):
        return None
    a = c["value"].get("amount")
    return float(a) if isinstance(a, (int, float)) else None


def _cur(c: dict[str, Any] | None) -> str:
    if c and isinstance(c.get("value"), dict):
        return c["value"].get("currency") or "NOK"
    return "NOK"


# The brief's sections, in the order of Builderr's own sample profile and its "Ask" prompts.
SECTION_SPEC = [
    ("what", "Company overview", "What does the company do?"),
    ("money", "Latest financials", "How is the company doing?"),
    ("who", "Who runs it?", "Who runs and owns the company?"),
    ("hiring", "Working here", "Is it hiring, and where does it work?"),
    ("activity", "Recent activity", "What has it published recently, and where is it online?"),
    ("changed", "What changed", "What changed recently?"),
    ("unknown", "What we could not establish", "What could not be established, and why?"),
]
_GENERIC_BRANDS = {"home", "hjem", "forside", "velkommen", "welcome", "start", "index", "hjemmeside", "homepage", "nettside", "website"}


def _clean_brand(brand: str | None, name: str) -> str | None:
    """A public brand fit for a headline: short, not a page title, not the legal name again."""
    if not brand or not isinstance(brand, str):
        return None
    brand = " ".join(brand.split()).strip(" .,-")
    if len(brand) < 3 or len(brand) > 40 or len(brand.split()) > 5:
        return None
    if any(sep in brand for sep in ("|", "–", "—", " - ", ":", "»", "«", "·")):
        return None
    if brand.lower() in _GENERIC_BRANDS or brand.lower() in name.lower():
        return None
    return brand


def _change_words(a: float, p: float) -> str:
    """'up 25% on 2024' style wording for a change from p to a (p > 0)."""
    change = (a - p) / abs(p)
    pct = round(abs(change) * 100)
    if pct == 0:
        return "about unchanged"
    return f"{'up' if change > 0 else 'down'} {pct:,}%"


def _job_title(c: dict[str, Any], limit: int = 70) -> str:
    v = c["value"] if isinstance(c.get("value"), dict) else {}
    return _quote(v.get("title") or v.get("job_title") or "", limit)


def _hiring_where(state: dict[str, Any]) -> str:
    """Where the run looked for job ads, in a few words, from the hiring family's reason."""
    reason = str(state.get("reason") or "")
    places = []
    if "careers page" in reason:
        places.append("on its careers page")
    if "NAV" in reason or any("nav.no" in str(u) for u in state.get("checked_sources") or []):
        places.append("in NAV's vacancy feed")
    return " or ".join(places) if places else "in the sources checked"


class _Fin:
    """The latest filed statement and the year before: the group (consolidated) accounts when the company
    files them for its latest year, since a parent's own accounts say little about the business it runs;
    otherwise the company accounts. The scope is always named in the text."""

    def __init__(self, stmts: dict[tuple[str, str], dict[str, dict[str, Any]]]) -> None:
        self.type = self.year = self.prev_year = None
        self.cur: dict[str, dict[str, Any]] = {}
        self.prev: dict[str, dict[str, Any]] = {}
        latest = {t: max((y for (tt, y) in stmts if tt == t and y.isdigit()), default=None) for t in ("SELSKAP", "KONSERN")}
        order = ("KONSERN", "SELSKAP") if latest["KONSERN"] and (not latest["SELSKAP"] or latest["KONSERN"] >= latest["SELSKAP"]) else ("SELSKAP", "KONSERN")
        for account_type in order:
            if latest[account_type]:
                self.type, self.year = account_type, latest[account_type]
                self.cur = stmts[(account_type, self.year)]
                self.prev_year = str(int(self.year) - 1)
                self.prev = stmts.get((account_type, self.prev_year)) or {}
                break

    @property
    def scope(self) -> str:
        return "group (consolidated) " if self.type == "KONSERN" else ""

    def pair(self, metric: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        cur, prev = self.cur.get(metric), self.prev.get(metric)
        if cur is None or _amount(cur) is None:
            return None, None
        if prev is None or _amount(prev) is None or _cur(prev) != _cur(cur):
            prev = None
        return cur, prev

    def headline_clause(self) -> tuple[str, list[dict[str, Any]]]:
        """'its 2025 accounts show revenue of NOK 50.0 million, up 25% on 2024, and a net loss of NOK 1.0 million'."""
        if not self.year:
            return "", []
        parts, cite = [], []
        rev, prev_rev = self.pair("revenue")
        if rev is not None:
            a = _amount(rev) or 0.0
            if a == 0:
                parts.append("no operating revenue")
            else:
                text = f"revenue of {money(a, _cur(rev))}"
                p = _amount(prev_rev) if prev_rev is not None else None
                if p:
                    text += f", {_change_words(a, p)} on {self.prev_year}"
                    cite.append(prev_rev)
                parts.append(text)
            cite.append(rev)
        net, prev_net = self.pair("annual_result")
        op, _ = self.pair("operating_result")
        self.lead_has_result_change = False
        if net is not None:
            text = f"a {_result_words(_amount(net) or 0.0, _cur(net), 'net')}"
            cite.append(net)
            if rev is None and prev_net is not None:  # no revenue to show direction: the result gives it
                text += f", against {_result_words(_amount(prev_net) or 0.0, _cur(prev_net), 'a')} in {self.prev_year}"
                cite.append(prev_net)
                self.lead_has_result_change = True
            parts.append(text)
        elif op is not None:
            parts.append(f"an {_result_words(_amount(op) or 0.0, _cur(op), 'operating')}")
            cite.append(op)
        if not parts:
            return "", []
        joined = parts[0] if len(parts) == 1 else (", ".join(parts[:-1]) + ("," if "," in parts[0] else "") + " and " + parts[-1])
        return f"its {self.year} {self.scope}accounts show " + joined, cite

    def result_change(self) -> tuple[str, list[dict[str, Any]]]:
        """How the net result moved from the year before, or '' when there is no comparable year."""
        net, prev_net = self.pair("annual_result")
        if net is None or prev_net is None:
            return "", []
        a, p = _amount(net) or 0.0, _amount(prev_net) or 0.0
        return (f"the {self.scope}net result moved from {_result_words(p, _cur(prev_net), 'a')} in {self.prev_year} "
                f"to {_result_words(a, _cur(net), 'a')} in {self.year}"), [prev_net, net]


def build_synthesis(rec: CompanyRecord, changes: list[dict[str, Any]] | None = None, baseline: bool = True) -> dict[str, Any]:
    extra_ev = {e["id"]: e for ch in (changes or []) for e in (ch.get("previous_evidence") or []) if e.get("id")}
    b = Builder(rec, extra_ev)
    org = rec.organisation_number
    name_c = b.first("legal_name")
    name = name_c["value"] if name_c else org
    brand_c = b.first("public_brand_name")
    brand = _clean_brand(brand_c["value"] if brand_c else None, str(name))
    display = f"{name} ({brand})" if brand else name
    name_cite = [name_c] + ([brand_c] if brand else [])
    form_c = b.first("legal_form")
    form_code = (form_c["value"] or {}).get("code") if form_c and isinstance(form_c["value"], dict) else None
    form_en = LEGAL_FORMS_EN.get(str(form_code or "").upper())
    muni_c = b.first("municipality")
    town = _place(muni_c["value"]) if muni_c else ""
    industries = sorted(b.claims("industry_code"), key=lambda c: (c["value"] or {}).get("rank", 9) if isinstance(c["value"], dict) else 9)
    ind_c = industries[0] if industries else None
    ind_code = (ind_c["value"] or {}).get("code") if ind_c else None
    ind_no = (ind_c["value"] or {}).get("description") if ind_c else None
    founded_c = b.first("founded_date") or b.first("registered_date")
    status_c = b.first("registry_status")
    desc_c = b.first("self_description")
    activity_c = b.first("registered_activity") or b.first("statutory_purpose")
    employees_c = b.first("registered_employees")
    site_c = b.first("official_website")
    site_url = (site_c["value"] or {}).get("url") if site_c and isinstance(site_c["value"], dict) else (site_c["value"] if site_c else None)
    inactive = bool(status_c and status_c["value"] not in ("active", None))
    industry_en = nace_en(ind_code) if ind_code and not str(ind_code).startswith("00") else None
    phrase = INDUSTRY_PHRASES.get(str(ind_code or "")[:5])
    if phrase and str(ind_code or "").startswith("97.00") and str(form_code or "").upper() in ("BRL", "ESEK", "BBL"):
        phrase = None  # the legal form already says what kind of entity it is
    housing = str(form_code or "").upper() in ("BRL", "ESEK", "BBL") and str(ind_code or "")[:2] in ("97", "68")
    if housing:
        doing = ""  # "a housing cooperative working in activities of housing cooperatives" says it twice
    elif phrase:
        doing = phrase
    elif industry_en:
        doing = f"working in {_lc(industry_en)}"
    elif ind_c and ind_code and not str(ind_code).startswith("00"):
        doing = f"registered under industry code {ind_code} ({ind_no})"
    else:
        doing = ""
    kind = form_en or "registered Norwegian entity"
    article = "an" if kind[0] in "aeiou" else "a"
    stmts = _accounts(b)
    fin = _Fin(stmts)
    roles = [c for c in b.claims("role") if isinstance(c["value"], dict)]
    units = [c for c in b.claims("registered_workplace") if isinstance(c["value"], dict)]
    groups = [c for c in b.claims("group_relationship") if isinstance(c["value"], dict)]
    jobs = sorted(b.claims("job_posting"), key=lambda c: job_date(c["value"]), reverse=True)
    socials = [c for c in b.claims("social_profile") if isinstance(c["value"], (dict, str))]
    news = sorted(b.claims("company_news"), key=lambda c: news_date(c["value"]), reverse=True)

    # ---- dated events on record (register, filings, own channels) -------------------------------
    events: list[tuple[str, str, list[dict[str, Any]], str]] = []  # (date, text, claims, kind)
    for c in news:
        d = news_date(c["value"])
        if d:
            events.append((d, f'news on its website: "{_quote(c["value"].get("title", ""), 90)}"', [c], "news"))
    for c in jobs:
        d = job_date(c["value"])
        if d:
            events.append((d, f'job ad published: "{_job_title(c)}"', [c], "job"))
    for c in b.claims("registry_role_change"):
        v = c["value"] if isinstance(c["value"], dict) else {}
        d = str(v.get("date") or c.get("effective_date") or "")[:10]
        group = {"DAGL": "general manager", "STYR": "board", "REVI": "auditor", "REGN": "accountant"}.get(v.get("group_code"), (v.get("event") or "role").replace(" registration last changed", "").lower())
        if d:
            events.append((d, f"change to the {group} registration", [c], "role"))
    for c in b.claims("former_name"):
        v = c["value"] if isinstance(c["value"], dict) else {}
        if v.get("to") and v.get("name"):
            events.append((str(v["to"])[:10], f"renaming from {v.get('name')}", [c], "name"))
    for c in units:
        d = c.get("effective_date") or c["value"].get("started")
        if d:
            events.append((str(d)[:10], f"registration of workplace {c['value'].get('name')}" + (f" in {_place(c['value'].get('municipality'))}" if c["value"].get("municipality") else ""), [c], "workplace"))
    for c in groups:
        v = c["value"]
        d = v.get("since") or c.get("effective_date")
        if d and v.get("relationship") in ("parent", "subsidiary", "ultimate_parent"):
            if v.get("relationship") == "subsidiary":
                events.append((str(d)[:10], f"group link to subsidiary {v.get('name')}", [c], "subsidiary"))
            else:
                events.append((str(d)[:10], f"group link to parent {v.get('name')}", [c], "group"))
    cap = b.first("share_capital")
    if cap and cap.get("effective_date") and isinstance(cap["value"], dict):
        events.append((cap["effective_date"][:10], f"share-capital registration of {money(cap['value'].get('amount'), cap['value'].get('currency'))}", [cap], "capital"))
    if employees_c and employees_c.get("effective_date"):
        events.append((employees_c["effective_date"][:10], f"employee-count update to {employees_c['value']}", [employees_c], "employees"))
    updates = sorted((c for c in b.claims("registry_update") if isinstance(c["value"], dict) and c["value"].get("date")), key=lambda c: c["value"]["date"], reverse=True)
    if updates:
        events.append((str(updates[0]["value"]["date"])[:10], "update to the Entity Register record", updates[:1], "register"))
    if fin.year:
        figure = fin.cur.get("revenue") or fin.cur.get("annual_result")
        if figure:
            events.append((f"{fin.year}-12-31", f"financial year {fin.year} ended (latest filed accounts)", [figure], "accounts"))
    # Subsidiary links registered on one day are one event.
    by_day: dict[str, list[tuple[str, str, list[dict[str, Any]], str]]] = {}
    for e in events:
        if e[3] == "subsidiary":
            by_day.setdefault(e[0], []).append(e)
    for day, items in by_day.items():
        if len(items) > 1:
            names = [c["value"].get("name") for _, _, cs, _ in items for c in cs]
            merged = (day, f"group links to {len(items)} subsidiaries, including {_list(sorted(names)[:2], 2)}", [c for _, _, cs, _ in items for c in cs], "subsidiary")
            events = [e for e in events if e not in items] + [merged]
    # Newest first; on the same day a specific change outranks the register's generic "record updated".
    events.sort(key=lambda e: (e[0], e[3] != "register", e[3], e[1]), reverse=True)
    register_events = [e for e in events if e[3] not in ("news", "job", "accounts")]

    sections: dict[str, list[dict[str, Any] | None]] = {}

    # ---- company overview ----------------------------------------------------------------------
    s: list[dict[str, Any] | None] = []
    ident = f"{display} is {article} {kind}"
    if form_c and not form_en:
        ident += f" ({(form_c['value'] or {}).get('description')})"
    if town:
        ident += f" in {town}"
    if founded_c and founded_c.get("value"):
        ident += f", founded in {str(founded_c['value'])[:4]}"
    used = name_cite + [form_c, muni_c, founded_c, ind_c]
    if phrase:
        ident += f", {phrase} (industry code {ind_code})"
    elif industry_en:
        ident += f", working in {_lc(industry_en)} (industry code {ind_code})"
    elif ind_c and ind_code and not str(ind_code).startswith("00"):
        ident += f", registered under industry code {ind_code} ({ind_no})"
    elif ind_c:
        ident += "; the register gives no industry code"
    if inactive:
        ident += f". The register marks it as {status_c['value']}"
        used.append(status_c)
        b.signal("status", f"Registry status: {status_c['value']}", [status_c], "negative")
    s.append(b.sentence(ident + ".", used))
    if desc_c:
        where = f" ({_host(site_url)})" if site_url else ""
        s.append(b.sentence(f'On its own website{where} it describes itself as: "{_quote(desc_c["value"], 200)}".', [desc_c] + ([site_c] if site_url else [])))
    if activity_c:
        label = "activity" if activity_c["field"] == "registered_activity" else "purpose"
        s.append(b.sentence(f'The register states its {label} (in Norwegian) as: "{_quote(activity_c["value"], 220)}".', [activity_c]))
    sections["what"] = s

    # ---- latest financials ---------------------------------------------------------------------
    s = []
    for account_type in ("SELSKAP", "KONSERN"):
        years = sorted(y for (t, y) in stmts if t == account_type)
        if not years:
            continue
        latest = years[-1]
        cur = stmts[(account_type, latest)]
        prev_year = str(int(latest) - 1) if latest.isdigit() else None
        prev = stmts.get((account_type, prev_year)) if prev_year else None
        scope = "" if account_type == "SELSKAP" else "group (consolidated) "
        parts: list[str] = []
        cite: list[dict[str, Any]] = []
        rev, prev_rev = cur.get("revenue"), (prev or {}).get("revenue")
        if rev is not None and _amount(rev) is not None:
            text = f"revenue of {money(_amount(rev), _cur(rev))}" if _amount(rev) else f"no operating revenue ({money(0.0, _cur(rev))})"
            cite.append(rev)
            if prev_rev is not None and _amount(prev_rev) is not None and _cur(prev_rev) == _cur(rev):
                a, p = _amount(rev) or 0.0, _amount(prev_rev) or 0.0
                pct = f", {_pct((a - p) / abs(p))}" if p else ""
                text += f" ({_signed_money(a - p, _cur(rev))}{pct} on {prev_year})"
                cite.append(prev_rev)
            parts.append(text)
        op = cur.get("operating_result")
        if op is not None and _amount(op) is not None:
            parts.append(f"an {_result_words(_amount(op) or 0.0, _cur(op), 'operating')}")
            cite.append(op)
        net, prev_net = cur.get("annual_result"), (prev or {}).get("annual_result")
        if net is not None and _amount(net) is not None:
            text = f"a {_result_words(_amount(net) or 0.0, _cur(net), 'net')}"
            cite.append(net)
            if prev_net is not None and _amount(prev_net) is not None and _cur(prev_net) == _cur(net):
                text += f" (against {_result_words(_amount(prev_net) or 0.0, _cur(prev_net), 'a')} in {prev_year})"
                cite.append(prev_net)
            parts.append(text)
        if parts:
            s.append(b.sentence(f"Its {latest} {scope}accounts show " + _list(parts, 5) + ".", cite))
        eq, assets = cur.get("equity"), cur.get("total_assets")
        if eq is not None and assets is not None and _amount(eq) is not None and _amount(assets):
            ratio = (_amount(eq) or 0.0) / (_amount(assets) or 1.0)
            text = f"Equity was {money(_amount(eq), _cur(eq))} at the end of {latest}"
            prev_eq = (prev or {}).get("equity")
            cite = [eq, assets]
            if prev_eq is not None and _amount(prev_eq) is not None and _cur(prev_eq) == _cur(eq):
                text += f" ({_signed_money((_amount(eq) or 0.0) - (_amount(prev_eq) or 0.0), _cur(eq))} on {prev_year})"
                cite.append(prev_eq)
            text += f", {ratio * 100:.0f}% of total assets of {money(_amount(assets), _cur(assets))}; this equity ratio is computed from the filed figures."
            s.append(b.sentence(text, cite, basis="computed"))
            if (_amount(eq) or 0.0) < 0:
                b.signal("negative_equity", f"Negative equity at the end of {latest}", [eq], "negative")
            elif ratio < 0.1:
                b.signal("thin_equity", f"Equity ratio {ratio * 100:.0f}% in {latest}", [eq, assets], "negative")
        if net is not None and (_amount(net) or 0.0) < 0:
            b.signal("loss", f"Net loss of {money(-(_amount(net) or 0.0), _cur(net))} in {latest}", [net], "negative")
        if rev is not None and prev_rev is not None and (_amount(prev_rev) or 0) > 0:
            growth = (_amount(rev) or 0.0) / (_amount(prev_rev) or 1.0) - 1
            if abs(growth) >= 0.2:
                b.signal("revenue_change", f"Revenue {_pct(growth)} in {latest} vs {prev_year}", [rev, prev_rev], "positive" if growth > 0 else "negative")
        audit = cur.get("audit_opt_out")
        if account_type == "SELSKAP" and audit is not None and isinstance(audit["value"], dict) and audit["value"].get("opted_out_of_audit"):
            s.append(b.sentence(f"It has opted out of statutory audit (accounts for {latest}).", [audit]))
        if account_type == "SELSKAP" and latest.isdigit():
            filed = b.first("latest_filed_accounts_year")
            if filed and str(filed["value"]).isdigit() and int(filed["value"]) > int(latest):
                s.append(b.sentence(f"The register records accounts for {filed['value']} as filed, but their figures were not yet in the accounts API; the figures above are for {latest}.", [filed]))
    no_accounts = None
    if not stmts:
        st = b.state("annual_accounts")
        rule = NOT_OBLIGED_TO_FILE.get(str(form_code or "").upper())
        reason = st.get("reason") or "not checked in this run"
        text = f"No filed accounts figures are available ({reason})"
        cite = []
        if rule and st.get("state") != "failed":
            text += f"; as a {form_en}, it is likely exempt: {rule}"
            cite = [form_c] if form_c else []
        filed = b.first("latest_filed_accounts_year")
        if filed:
            text += f". The register lists {filed['value']} as the latest filed year"
            cite.append(filed)
        no_accounts = b.sentence(text + ".", cite, checked=st.get("checked_sources", []))
        s.append(no_accounts)
    sections["money"] = s

    # ---- who runs it ---------------------------------------------------------------------------
    def holders(code: str) -> list[dict[str, Any]]:
        return [c for c in roles if c["value"].get("role_code") == code]

    ceo, chair = holders("DAGL"), holders("LEDE")
    members = [c for c in roles if c["value"].get("group_code") == "STYR" and c["value"].get("role_code") in ("LEDE", "NEST", "MEDL")]
    owner = holders("INNH") + holders("KOMP")
    partners = holders("DTPR") + holders("DTSO")
    auditor, accountant, manager = holders("REVI"), holders("REGN"), holders("FFØR")
    s = []
    if ceo and chair and ceo[0]["value"].get("name") == chair[0]["value"].get("name"):
        s.append(b.sentence(f"{ceo[0]['value']['name']} is both general manager (daglig leder) and chair of the board.", [ceo[0], chair[0]]))
    else:
        if ceo:
            s.append(b.sentence(f"The general manager (daglig leder) is {ceo[0]['value'].get('name')}.", ceo[:1]))
        if chair:
            s.append(b.sentence(f"The board is chaired by {chair[0]['value'].get('name')}.", chair[:1]))
    if members:
        others = [c["value"].get("name") for c in members if c not in chair]
        if others:
            rest = "the other is" if len(others) == 1 else "the others are"
            s.append(b.sentence(f"The board has {len(members)} member{'s' if len(members) != 1 else ''} including the chair; {rest} {_list(others, 4)}.", members))
        elif len(members) == 1 and chair:
            s.append(b.sentence("The chair is the only board member.", members))
    if owner:
        s.append(b.sentence(f"Owner: {owner[0]['value'].get('name')} ({ROLE_EN.get(owner[0]['value'].get('role_code'), owner[0]['value'].get('role'))}).", owner[:1]))
    if partners:
        s.append(b.sentence(f"Partners: {_list([c['value'].get('name') for c in partners], 4)}.", partners))
    parents = [c for c in groups if c["value"].get("relationship") == "parent"]
    ultimate = [c for c in groups if c["value"].get("relationship") == "ultimate_parent"]
    subs = [c for c in groups if c["value"].get("relationship") == "subsidiary"]
    if parents:
        p = parents[0]["value"]
        share = f" ({p.get('ownership')} owned)" if p.get("ownership") else ""
        text = f"It is a subsidiary{share} of {p.get('name')} ({p.get('organisation_number')})"
        cite = parents[:1]
        if ultimate and ultimate[0]["value"].get("organisation_number") != p.get("organisation_number"):
            text += f", in the group ultimately owned by {ultimate[0]['value'].get('name')} ({ultimate[0]['value'].get('organisation_number')})"
            cite = cite + ultimate[:1]
        s.append(b.sentence(text + ".", cite))
    elif ultimate:
        u = ultimate[0]["value"]
        s.append(b.sentence(f"It belongs to the group ultimately owned by {u.get('name')} ({u.get('organisation_number')}).", ultimate[:1]))
    if subs:
        named = [c["value"].get("name") for c in subs]
        s.append(b.sentence(f"It owns {len(subs)} registered subsidiar{'y' if len(subs) == 1 else 'ies'}: {_list(named, 3)}.", subs))
    if manager:
        s.append(b.sentence(f"Business manager (forretningsfører): {manager[0]['value'].get('name')}.", manager[:1]))
    if auditor:
        s.append(b.sentence(f"Auditor: {auditor[0]['value'].get('name')}.", auditor[:1]))
    if accountant:
        s.append(b.sentence(f"Accountant: {accountant[0]['value'].get('name')}.", accountant[:1]))
    if not roles:
        st = b.state("leadership")
        s.append(b.sentence(f"No public roles are recorded ({st.get('reason') or 'not checked'}).", [], checked=st.get("checked_sources", [])))
    sections["who"] = s

    # ---- working here: job ads, people, places ---------------------------------------------------
    s = []
    hiring_state = b.state("hiring")
    if jobs:
        nav = [c for c in jobs if _nav_ad(c)]
        where = []
        if nav:
            where.append(f"{len(nav)} in NAV's vacancy feed naming this company or one of its registered workplaces as employer")
        if len(jobs) > len(nav):
            where.append(f"{len(jobs) - len(nav)} read on its own careers channel")
        listed = []
        for c in jobs[:3]:
            v = c["value"] if isinstance(c["value"], dict) else {}
            bits = [x for x in (str(v.get("location") or "").title() or None, f"published {job_date(v)}" if job_date(v) else None, f"apply by {str(v.get('application_due') or v.get('expires'))[:10]}" if (v.get("application_due") or v.get("expires")) else None) if x]
            listed.append(f'"{_job_title(c)}"' + (f" ({', '.join(bits)})" if bits else ""))
        s.append(b.sentence(f"Hiring now: {len(jobs)} open job ad{'s' if len(jobs) != 1 else ''} ({'; '.join(where)}): " + _list(listed, 3) + ".", jobs))
        b.signal("hiring", f"Hiring: {len(jobs)} open job ad{'s' if len(jobs) != 1 else ''}", jobs, "positive")
    else:
        reason = hiring_state.get("reason") or "job sources were not checked in this run"
        state = hiring_state.get("state") or "failed"
        if state in ("failed", "blocked"):
            s.append(b.sentence(f"Its job ads could not be fully checked in this run: {reason}.", [], checked=hiring_state.get("checked_sources", [])))
        else:
            s.append(b.sentence(f"No single open job ad was found: {reason}. That is not proof that it is not hiring.", [], checked=hiring_state.get("checked_sources", [])))
    size: list[str] = []
    size_cite: list[dict[str, Any]] = []
    if employees_c:
        when = f" (as of {employees_c['effective_date']})" if employees_c.get("effective_date") else ""
        size.append(f"{employees_c['value']} employee{'s' if employees_c['value'] != 1 else ''} in the Entity Register{when}")
        size_cite.append(employees_c)
    if units:
        towns = sorted({_place(c["value"].get("municipality")) for c in units if c["value"].get("municipality")})
        size.append(f"{len(units)} registered workplace{'s' if len(units) != 1 else ''}" + (f" in {_list(towns, 4)}" if towns else ""))
        size_cite += units
    if size:
        s.append(b.sentence("It has " + _list(size) + ".", size_cite))
    elif not employees_c:
        s.append(b.sentence("The register reports no employee count for it.", [], checked=b.state("legal_identity").get("checked_sources", [])))
    sections["hiring"] = s

    # ---- recent activity: dated news, website, profiles ------------------------------------------
    s = []
    if news:
        latest = news[:2]
        items = [f'"{_quote(c["value"].get("title", ""), 100)}" ({news_date(c["value"])})' for c in latest]
        s.append(b.sentence(f"{len(news)} dated news item{'s' if len(news) != 1 else ''} on its own channels; the latest {'is' if len(latest) == 1 else 'are'} " + _list(items, 2) + ".", news))
        b.signal("news", f"Latest news {news_date(news[0]['value'])}", news[:1], "neutral")
    else:
        st = b.state("public_activity")
        s.append(b.sentence("No dated news item was found on its own channels.", [], checked=st.get("checked_sources", [])))
    if site_c:
        method = str((site_c["value"] or {}).get("identity_method", "")) if isinstance(site_c["value"], dict) else ""
        if "organisation_number" in method:
            proof = "the site shows this organisation number"
        elif method.startswith("nav_listed"):
            proof = "the employer gives it as its homepage in a job ad verified to this organisation number"
        elif method:
            proof = f"verified by {method.replace('_', ' ')}"
        else:
            proof = "verified"
        group_site = " It is a group site rather than one for this entity alone." if site_c.get("relationship") == "group_site" else ""
        s.append(b.sentence(f"Its website is {site_url} ({proof}).{group_site}", [site_c]))
    if socials:
        platforms = sorted({(c["value"].get("platform") if isinstance(c["value"], dict) else _host(str(c["value"])).split(".")[0]).title() for c in socials})
        s.append(b.sentence(f"Social profiles linked from its own channels: {_list(platforms, 6)}.", socials))
    if site_c or socials:
        b.signal("web", "Verified web presence: " + _list(([_host(site_url)] if site_url else []) + [c["value"].get("platform", "").title() for c in socials if isinstance(c["value"], dict)], 4), ([site_c] if site_c else []) + socials[:3], "neutral")
    if parents or ultimate:
        top_parent = (parents or ultimate)[0]
        b.signal("group", f"Part of a group ({top_parent['value'].get('name')})", [top_parent], "neutral")
    elif subs:
        b.signal("group", f"Parent of {len(subs)} subsidiar{'y' if len(subs) == 1 else 'ies'}", subs[:3], "neutral")
    sections["activity"] = s

    # ---- what changed: run to run, and on record --------------------------------------------------
    s = []
    run_line = None
    if changes:
        material = [c for c in changes if c.get("materiality") in ("high", "medium")] or changes
        lines = [_humanize_change(c) for c in material[:5]]
        claims_for_change = [{"id": c.get("current_claim_id") or c.get("previous_claim_id"), "evidence_ids": (c.get("current_evidence_ids") or []) + (c.get("previous_evidence_ids") or [])} for c in changes]
        more = f" (and {len(changes) - len(lines)} more)" if len(changes) > len(lines) else ""
        run_line = b.sentence(f"Since the previous run, {len(changes)} change{'s were' if len(changes) != 1 else ' was'} detected: " + "; ".join(lines) + more + ".", claims_for_change, basis="run_to_run")
    elif baseline:
        run_line = b.sentence("This is the first run for this company, so there is no earlier snapshot to compare with; later runs report changes against it.", [], basis="run_to_run")
    else:
        run_line = b.sentence("Compared with the previous run, no published fact changed in the sources that were re-checked.", [], basis="run_to_run")
    s.append(run_line)
    stale = [c for field, items in b.by_field.items() for c in items if c.get("stale")]
    if stale:
        fields = sorted({FIELD_WORDS.get(c["field"], c["field"].replace("_", " ")) for c in stale})
        s.append(b.sentence(f"{len(stale)} fact{'s' if len(stale) != 1 else ''} could not be re-checked this run and {'are' if len(stale) != 1 else 'is'} carried forward from the previous run, marked as not re-checked: {_list(fields, 5)}.", stale, basis="carried_forward"))
    filing_text, filing_cite = fin.result_change()
    rev, prev_rev = fin.pair("revenue")
    if rev is not None and prev_rev is not None and _amount(prev_rev):
        a, p = _amount(rev) or 0.0, _amount(prev_rev) or 0.0
        rev_text = f"revenue went from {money(p, _cur(prev_rev))} in {fin.prev_year} to {money(a, _cur(rev))} in {fin.year} ({_pct((a - p) / abs(p))})"
        filing_text = rev_text + (" and " + filing_text if filing_text else "")
        filing_cite = [prev_rev, rev] + filing_cite
    filing_line = None
    if filing_text:
        filing_line = b.sentence(f"In the filed {fin.scope}accounts, {filing_text}.", filing_cite)
    elif fin.year:
        figure = fin.cur.get("revenue") or fin.cur.get("annual_result") or next(iter(fin.cur.values()))
        filing_line = b.sentence(f"The latest filed {fin.scope}accounts are for {fin.year}; the accounts register returned no comparable earlier year.", [figure])
    if filing_line:
        s.append(filing_line)
    if register_events:
        top = register_events[:4]
        s.append(b.sentence("Latest changes in the Entity Register: " + "; ".join(f"{t} ({d})" for d, t, _, _ in top) + ".", [c for _, _, cs, _ in top for c in cs]))
    sections["changed"] = s
    timeline = [
        {"date": d, "text": t, "kind": k, "claim_ids": sorted({c["id"] for c in cs if c.get("id")}), "evidence_ids": sorted({e for c in cs for e in c.get("evidence_ids", [])})}
        for d, t, cs, k in events[:20]
    ]

    # ---- what we could not establish -------------------------------------------------------------
    unknowns: list[dict[str, Any]] = []
    present = {
        "official_website": bool(site_c),
        "company_profiles": bool(socials),
        "hiring": bool(jobs),
        "public_activity": bool(news),
        "contact_channels": bool(b.claims("registry_phone") or b.claims("website_phone") or b.claims("website_email") or b.claims("registry_email")),
        "public_brand": bool(brand_c or desc_c),
        "annual_accounts": bool(stmts),
        "leadership": bool(roles),
        "workplaces": bool(units),
    }
    for family in ("official_website", "company_profiles", "hiring", "public_activity", "public_brand", "contact_channels", "annual_accounts", "leadership", "workplaces"):
        if present[family]:
            continue
        st = b.state(family)
        state = st.get("state") or "failed"
        reason = st.get("reason") or "not attempted within the run's time budget"
        if state == "available":  # the family is available for a reason that is not this fact (register updates are not news)
            state, reason = "not_available", "no dated news item found on the company's own channels" if family == "public_activity" else "no evidence-backed value found"
        if state == "not_applicable" and family in ("workplaces",):
            continue
        unknowns.append({"family": family, "label": FAMILY_LABELS.get(family, family), "state": state, "state_words": STATE_WORDS.get(state, state), "reason": reason, "checked_sources": (st.get("checked_sources") or [])[:3]})
    s = []
    for u in unknowns:
        label = u["label"][:1].upper() + u["label"][1:]
        generic = u["reason"] in GENERIC_REASONS
        if u["state"] == "not_available":
            text = f"{label}: not available ({'checked, none found' if generic else u['reason']})."
        else:
            text = f"{label}: {STATE_LONG.get(u['state'], u['state'])}" + ("" if generic else f" ({u['reason']})") + "."
        s.append(b.sentence(text, [], basis="unknown"))  # its checked sources are in ``unknowns``
    if unknowns:
        s.append(b.sentence("Missing data is reported as missing, never as zero or as a negative fact.", [], basis="unknown"))
    else:
        s.append(b.sentence("Every profile area has at least one evidence-backed fact.", [], basis="unknown"))
    sections["unknown"] = s

    # ---- the brief: two sentences to act on, then what changed and what is unknown ----------------
    lead = f"{display} is {article} {kind}"
    if town:
        lead += f" in {town}"
    if doing:
        lead += f" {doing}" if not doing.startswith("registered as") else f", {doing}"
    lead_cite = name_cite + [form_c, muni_c, ind_c]
    if employees_c:
        lead += f", with {employees_c['value']} employee{'s' if employees_c['value'] != 1 else ''} in the register"
        lead_cite.append(employees_c)
    if inactive:
        lead += f"; the register marks it as {status_c['value']}"
        lead_cite.append(status_c)
    clause, fin_cite = fin.headline_clause()
    lead_checked: list[str] = []
    if clause:
        lead += "; " + clause
        lead_cite += fin_cite
    elif not stmts:
        st = b.state("annual_accounts")
        rule = NOT_OBLIGED_TO_FILE.get(str(form_code or "").upper())
        if rule and st.get("state") != "failed":
            lead += f"; it has no filed accounts figures ({rule})"
        else:
            lead += "; no filed accounts figures are available"
        lead_checked = st.get("checked_sources", [])
    lead1 = b.sentence(lead + ".", lead_cite, checked=lead_checked)

    if jobs:
        first = jobs[0]
        when = f", published {job_date(first['value'])}" if job_date(first["value"]) else ""
        hire = f'It is hiring: {len(jobs)} open job ad{"s" if len(jobs) != 1 else ""}, the latest "{_job_title(first, 60)}"{when}'
        hire_cite, hire_checked = jobs, []
    else:
        state = hiring_state.get("state") or "failed"
        if state in ("failed", "blocked"):
            hire = "Its job ads could not be fully checked in this run"
        else:
            hire = f"No open job ad was found {_hiring_where(hiring_state)}"
        hire_cite, hire_checked = [], hiring_state.get("checked_sources", [])
    used_event = None
    if news:
        c = news[0]
        act = f'; its latest dated news is "{_quote(c["value"].get("title", ""), 80)}" ({news_date(c["value"])}) on its own website'
        act_cite = [c]
    elif register_events:
        used_event = register_events[0]
        d, t, cs, k = used_event
        act = f"; its Entity Register record was last updated on {d}" if k == "register" else f"; the latest dated event on record is the {t} ({d})"
        act_cite = cs
    else:
        act, act_cite = "; no dated activity was found in the sources checked", []
    if not jobs and not act_cite and hire.startswith("No open job ad was found "):
        act = ", and no dated activity appears in the sources checked"
    lead2 = b.sentence(hire + act + ".", hire_cite + act_cite, checked=hire_checked)

    leader = None
    lead_bits: list[str] = []
    leader_cite: list[dict[str, Any]] = []
    if ceo and chair and ceo[0]["value"].get("name") == chair[0]["value"].get("name"):
        lead_bits.append(f"{ceo[0]['value'].get('name')} is both general manager and board chair")
        leader_cite += [ceo[0], chair[0]]
    else:
        if ceo:
            lead_bits.append(f"{ceo[0]['value'].get('name')} is general manager")
            leader_cite.append(ceo[0])
        if chair:
            lead_bits.append(f"{chair[0]['value'].get('name')} chairs the board")
            leader_cite.append(chair[0])
    if not lead_bits and owner:
        lead_bits.append(f"{owner[0]['value'].get('name')} is registered as {ROLE_EN.get(owner[0]['value'].get('role_code'), 'owner')}")
        leader_cite.append(owner[0])
    group_bit = None
    if parents:
        p = parents[0]["value"]
        group_bit = f"it is a subsidiary of {p.get('name')} ({p.get('organisation_number')})"
        leader_cite.append(parents[0])
    elif ultimate:
        u = ultimate[0]["value"]
        group_bit = f"it belongs to the group of {u.get('name')} ({u.get('organisation_number')})"
        leader_cite.append(ultimate[0])
    if lead_bits or group_bit:
        text = "; ".join(x for x in (_list(lead_bits, 3), group_bit) if x)
        leader = b.sentence(text[:1].upper() + text[1:] + ".", leader_cite)

    if changes:
        changed = run_line
    else:
        start = "This is the first run for this company, so nothing is compared yet" if baseline else "Compared with the previous run, no published fact changed in the sources re-checked"
        bits, bits_cite = [], []
        result_text, result_cite = fin.result_change() if not getattr(fin, "lead_has_result_change", False) else ("", [])
        if result_text:
            bits.append(result_text)
            bits_cite += result_cite
        elif fin.year and not clause:
            figure = fin.cur.get("revenue") or fin.cur.get("annual_result") or next(iter(fin.cur.values()))
            bits.append(f"its latest filed {fin.scope}accounts are for {fin.year}")
            bits_cite.append(figure)
        later = [e for e in register_events if e is not used_event][:1]
        for d, t, cs, k in later:
            bits.append(f"the latest register change is the {t} ({d})")
            bits_cite += cs
        changed = b.sentence(start + (("; on record, " + ", and ".join(bits)) if bits else "") + ".", bits_cite, basis="fact" if bits else "run_to_run")
    if unknowns:
        names = [f"{u['label']} ({STATE_SHORT.get(u['state'], u['state'])})" for u in unknowns if u["family"] in ("official_website", "company_profiles", "hiring", "public_activity", "annual_accounts", "leadership")]
        names = names or [f"{u['label']} ({STATE_SHORT.get(u['state'], u['state'])})" for u in unknowns]
        gap = b.sentence("Not established in this run: " + "; ".join(names) + ".", [], basis="unknown", checked=[x for u in unknowns for x in u["checked_sources"]])
    else:
        gap = b.sentence("Every profile area has at least one evidence-backed fact.", [], basis="unknown")
    summary_sentences = [lead1, lead2, leader, changed, gap]

    # ---- support check: every sentence's numbers, names and quotes must be in what it cites ------
    by_id = {c["id"]: c for items in b.by_field.values() for c in items if c.get("id")}
    reasons = sorted({str(st.get("reason")) for st in rec.family_state.values() if st.get("reason")})
    change_values = [c.get(k) for c in (changes or []) for k in ("previous", "current", "summary")]
    names_on_record = _names_on_record(b)
    checked_total = dropped = 0

    def keep(sentence: dict[str, Any] | None) -> bool:
        nonlocal checked_total, dropped
        if not sentence or not sentence.get("text"):
            return False
        checked_total += 1
        if sentence_support(sentence, by_id, reasons, change_values, names_on_record):
            dropped += 1
            return False
        return True

    for key, heading, question in SECTION_SPEC:
        b.add(key, heading, question, [x for x in sections.get(key, []) if keep(x)])
    summary_sentences = [x for x in summary_sentences if keep(x)]

    coverage_flags = {
        "company": bool(name_c),
        "financials": bool(stmts),
        "people_places": bool(roles or units),
        "website": bool(site_c),
        "footprint": bool(socials or jobs or news),
    }
    coverage = {
        "score": sum(coverage_flags.values()),
        "total": len(COVERAGE_AREAS),
        "areas": [{"key": k, "label": label, "available": coverage_flags[k]} for k, label in COVERAGE_AREAS],
    }
    if phrase:
        industry_label = phrase.replace("registered as a ", "").replace("registered as an ", "").replace("registered as ", "")
    else:
        industry_label = industry_en or (ind_no if ind_code and not str(ind_code).startswith("00") else "")
    industry_label = industry_label[:1].upper() + industry_label[1:] if industry_label else ""
    headline = " · ".join(x for x in (display, industry_label, town) if x)
    return {
        "headline": headline,
        "summary": " ".join(x["text"] for x in summary_sentences),
        "markdown": _markdown(headline, summary_sentences, b.sections),
        "summary_sentences": summary_sentences,
        "sections": b.sections,
        "timeline": timeline,
        "signals": b.signals,
        "unknowns": unknowns,
        "coverage": coverage,
        "checks": {"sentences": checked_total, "unsupported_dropped": dropped},
        "method": "deterministic template over this run's claims; every sentence cites its claims and sources and passes a support check; no language model",
    }


# -- sentence-support check --------------------------------------------------------------------

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_QUOTED = re.compile(r'"([^"]{3,})"')


def _canon_number(text: str) -> str | None:
    text = text.replace(",", "")
    try:
        value = float(text)
    except ValueError:
        return None
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    if value is None or isinstance(value, bool):
        return []
    return [str(value)]


def _amounts(value: Any) -> list[float]:
    out = []
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "amount" and isinstance(v, (int, float)):
                out.append(float(v))
            else:
                out += _amounts(v)
    return out


def _names_on_record(b: Builder) -> set[str]:
    """Names and titles a sentence could state: people, related companies, workplaces, ads and news."""
    names: set[str] = set()
    for field, key in (("role", "name"), ("group_relationship", "name"), ("registered_workplace", "name"), ("job_posting", "title"), ("company_news", "title"), ("former_name", "name")):
        for c in b.claims(field):
            v = c["value"] if isinstance(c["value"], dict) else {}
            text = " ".join(str(v.get(key) or "").split())
            if len(text) >= 5:
                names.add(text)
    return names


def sentence_support(sentence: dict[str, Any], claims_by_id: dict[str, dict[str, Any]], reasons: Iterable[str] = (), change_values: Iterable[Any] = (), names_on_record: Iterable[str] = ()) -> list[str]:
    """What a sentence states that the claims it cites do not contain (empty when fully supported).

    Checked: every cited claim id exists; every number in the text is a value of a cited claim, a
    formatting of one (``NOK 50.0 million``), arithmetic on two of them (difference, change, ratio),
    a count no larger than the number of cited claims, part of an availability reason the sentence
    quotes verbatim, or (for a run-to-run sentence) part of a change record; every quoted passage is a substring of a cited value; and every person, company,
    ad or news title on record that the sentence names appears in a cited claim."""
    problems: list[str] = []
    text = sentence.get("text") or ""
    basis = sentence.get("basis", "fact")
    ids = sentence.get("claim_ids") or []
    cited = [claims_by_id[i] for i in ids if i in claims_by_id]
    if basis not in ("run_to_run",):
        problems += [f"cites unknown claim {i}" for i in ids if i not in claims_by_id]
    strings: list[str] = []
    amounts: list[float] = []
    for c in cited:
        strings += _strings(c.get("value"))
        strings += [str(c.get(k)) for k in ("effective_date", "reporting_period", "first_observed_at") if c.get(k)]
        amounts += _amounts(c.get("value"))
        if isinstance(c.get("value"), (int, float)) and not isinstance(c.get("value"), bool):
            amounts.append(float(c["value"]))
    strings += [r for r in reasons if r and r in text]
    if basis == "run_to_run":
        strings += [s for x in change_values for s in _strings(x)]
        amounts += [a for x in change_values for a in _amounts(x)]
    allowed: set[str] = set()
    for s in strings:
        allowed |= {n for n in (_canon_number(m) for m in _NUMBER.findall(s)) if n}
    formatted = [money(a) for a in amounts] + [money(abs(a)) for a in amounts]
    for i, a in enumerate(amounts):
        for p in amounts[:i] + amounts[i + 1:]:
            formatted.append(money(abs(a - p)))
            if p:
                allowed.add(str(round(abs(a - p) / abs(p) * 100)))
                allowed.add(f"{abs(a - p) / abs(p) * 100:,.0f}".replace(",", ""))
                allowed.add(str(abs(int(f"{a / p * 100:.0f}"))))
    for f in formatted:
        allowed |= {n for n in (_canon_number(m) for m in _NUMBER.findall(f)) if n}
    allowed |= {str(n) for n in range(0, len(cited) + 1)}
    allowed |= {str(round(a)) for a in amounts}
    for m in _NUMBER.findall(text):
        n = _canon_number(m)
        if n is not None and n not in allowed:
            problems.append(f"number {m} is not in the cited claims")
    flat = [" ".join(s.split()) for s in strings]
    for q in _QUOTED.findall(text):
        q = " ".join(q.split()).removesuffix(" …").rstrip(" .")
        if q and not any(q in s for s in flat):
            problems.append(f'quote "{q[:40]}" is not in the cited claims')
    for name in names_on_record:
        if name in text and not any(name in s for s in flat):
            problems.append(f"names {name[:40]} without citing it")
    return problems


def _markdown(headline: str, summary: list[dict[str, Any]], sections: list[dict[str, Any]]) -> str:
    """The brief as plain text with numbered source references, readable without the viewer."""
    refs: dict[str, tuple[int, str]] = {}

    def mark(sentence: dict[str, Any]) -> str:
        nums = []
        for src in sentence.get("sources", []):
            refs.setdefault(src["url"], (len(refs) + 1, f"retrieved {src.get('retrieved_at') or 'time not recorded'}"))
            nums.append(refs[src["url"]][0])
        for url in sentence.get("checked_sources", []):
            refs.setdefault(url, (len(refs) + 1, "checked"))
            nums.append(refs[url][0])
        tag = "".join(f"[{n}]" for n in sorted(set(nums)))
        carried = " (carried forward from the previous run; not re-checked)" if sentence.get("basis") == "carried_forward" and "carried forward" not in sentence["text"] else ""
        return sentence["text"] + carried + (f" {tag}" if tag else "")

    lines = [f"**{headline}**", "", "**Company brief** " + " ".join(mark(s) for s in summary), ""]
    for sec in sections:
        lines += [f"**{sec.get('heading')}** " + " ".join(mark(s) for s in sec["sentences"]), ""]
    if refs:
        lines.append("Sources:")
        lines += [f"[{n}] {url} ({note})" for url, (n, note) in sorted(refs.items(), key=lambda kv: kv[1][0])]
    return "\n".join(lines)


FIELD_WORDS = {
    "official_website": "website", "social_profile": "social profile", "job_posting": "job ad",
    "company_news": "news item", "public_brand_name": "public brand", "self_description": "self-description",
    "website_phone": "phone (website)", "website_email": "e-mail (website)", "role": "role", "registered_workplace": "workplace",
}


def _humanize_change(change: dict[str, Any]) -> str:
    kind = change.get("change_type") or ""
    field = change.get("field") or ""
    word = FIELD_WORDS.get(field, field.replace("_", " ").replace("financial.", ""))

    def short(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("name", "title", "url", "event"):
                if value.get(key):
                    extra = f" ({value.get('role')})" if key == "name" and value.get("role") else ""
                    return _quote(str(value[key]), 60) + extra
            if "amount" in value:
                return money(value.get("amount"), value.get("currency"))
        return _quote(str(value), 60)

    prev, cur = change.get("previous"), change.get("current")
    if kind.startswith("changed_") or kind.startswith("updated_") or kind == "restated_figure":
        return f"{word} changed from {short(prev)} to {short(cur)}"
    if kind == "new_filing":
        period = cur.get("fiscal_year") if isinstance(cur, dict) else ""
        return f"new filed figure for {period}: {word.replace('_', ' ')} {short(cur)}"
    if cur is not None and prev is None:
        return f"new {word}: {short(cur)}"
    if prev is not None and cur is None:
        return f"{word} no longer listed (was {short(prev)})"
    return change.get("summary") or kind.replace("_", " ")


# Statistics Norway (SSB) KLASS classification 6, Standard for næringsgruppering SN2025, English
# names at division (2-digit) and sub-class (5-digit) level, fetched 2026-10-17 from
# data.ssb.no/api/klass/v1/classifications/6/codes.json?from=2025-06-01&language=en (NLOD 2.0).
NACE_EN_TABLE = """
01	Crop and animal production, hunting and related service activities
01.110	Growing of cereals, other than rice, leguminous crops and oil seeds
01.120	Growing of rice
01.130	Growing of vegetables and melons, roots and tubers
01.140	Growing of sugar cane
01.150	Growing of tobacco
01.160	Growing of fibre crops
01.190	Growing of other non-perennial crops
01.210	Growing of grapes
01.220	Growing of tropical and subtropical fruits
01.230	Growing of citrus fruits
01.240	Growing of pome fruits and stone fruits
01.250	Growing of other tree and bush fruits and nuts
01.260	Growing of oleaginous fruits
01.270	Growing of beverage crops
01.280	Growing of spices, aromatic, drug and pharmaceutical crops
01.290	Growing of other perennial crops
01.300	Plant propagation
01.410	Raising of dairy cattle
01.420	Raising of other cattle and buffaloes
01.430	Raising of horses and other equines
01.440	Raising of camels and camelids
01.451	Raising of sheep
01.452	Raising of goats
01.460	Raising of swine and pigs
01.471	Farming of laying hens for production of eggs for consumption
01.479	Raising of other poultry
01.481	Raising of reindeers
01.489	Raising of animals n.e.c.
01.500	Mixed farming
01.610	Support activities for crop production
01.620	Support activities for animal production
01.630	Post-harvest crop activities and seed processing for propagation
01.700	Hunting, trapping and related service activities
02	Forestry and logging
02.100	Silviculture and other forestry activities
02.200	Logging
02.300	Gathering of wild growing non-wood products
02.400	Support services to forestry
03	Fishing and aquaculture
03.110	Marine fishing
03.120	Freshwater fishing
03.211	Operation of marine fish farms
03.212	Operation of marine hatcheries
03.221	Operation of freshwater fish farms
03.222	Operation of freshwater hatcheries
03.300	Support activities for fishing and aquaculture
05	Mining of coal and lignite
05.100	Mining of hard coal
05.200	Mining of lignite
06	Extraction of crude petroleum and natural gas
06.100	Extraction of crude petroleum
06.200	Extraction of natural gas
07	Mining of metal ores
07.100	Mining of iron ores
07.210	Mining of uranium and thorium ores
07.290	Mining of other non-ferrous metal ores
08	Other mining and quarrying
08.110	Quarrying of ornamental stone, limestone, gypsum, slate and other stone
08.120	Operation of gravel and sand pits and mining of clay and kaolin
08.910	Mining of chemical and fertiliser minerals
08.920	Extraction of peat
08.930	Extraction of salt
08.990	Other mining and quarrying n.e.c.
09	Mining support service activities
09.101	Drilling services for petroleum and natural gas extraction
09.109	Other support activities for petroleum and natural gas extraction
09.900	Support activities for other mining and quarrying
10	Manufacture of food products
10.110	Processing and preserving of meat, except of poultry meat
10.120	Processing and preserving of poultry meat
10.130	Production of meat and poultry meat products
10.201	Drying and salting of fish
10.202	Slaughtering of fish, crustaceans and molluscs
10.203	Freezing and further processing of fish, crustaceans and molluscs
10.310	Processing and preserving of potatoes
10.320	Manufacture of fruit and vegetable juice
10.390	Other processing and preserving of fruit and vegetables
10.410	Manufacture of oils and fats
10.420	Manufacture of margarine and similar edible fats
10.510	Manufacture of dairy products
10.520	Manufacture of ice cream and other edible ice
10.610	Manufacture of grain mill products
10.620	Manufacture of starches and starch products
10.710	Manufacture of bread; manufacture of fresh pastry goods and cakes
10.720	Manufacture of rusks, biscuits, preserved pastries and cakes
10.730	Manufacture of farinaceous products
10.810	Manufacture of sugar
10.820	Manufacture of cocoa, chocolate and sugar confectionery
10.830	Processing of tea and coffee
10.840	Manufacture of condiments and seasonings
10.850	Manufacture of prepared meals and dishes
10.860	Manufacture of homogenised food preparations and dietetic food
10.890	Manufacture of other food products n.e.c.
10.911	Manufacture of prepared feeds for farm animals in agriculture
10.912	Manufacture of prepared feeds for farm animals in aquaculture
10.920	Manufacture of prepared pet foods
11	Manufacture of beverages
11.010	Distilling, rectifying and blending of spirits
11.020	Manufacture of wine from grape
11.030	Manufacture of cider and other fermented fruit beverages
11.040	Manufacture of other non-distilled fermented beverages
11.050	Manufacture of beer
11.060	Manufacture of malt
11.070	Manufacture of soft drinks and bottled waters
12	Manufacture of tobacco products
12.000	Manufacture of tobacco products
13	Manufacture of textiles
13.100	Preparation and spinning of textile fibres
13.200	Weaving of textiles
13.300	Finishing of textiles
13.910	Manufacture of knitted and crocheted fabrics
13.920	Manufacture of household textiles and made-up furnishing articles
13.930	Manufacture of carpets and rugs
13.940	Manufacture of cordage, rope, twine and netting
13.950	Manufacture of non-wovens and non-woven articles
13.960	Manufacture of other technical and industrial textiles
13.990	Manufacture of other textiles n.e.c.
14	Manufacture of wearing apparel
14.100	Manufacture of knitted and crocheted apparel
14.210	Manufacture of outerwear
14.220	Manufacture of underwear
14.230	Manufacture of workwear
14.240	Manufacture of leather clothes and fur apparel
14.290	Manufacture of other wearing apparel and accessories n.e.c.
15	Manufacture of leather and related products of other materials
15.110	Tanning, dressing, dyeing of leather and fur
15.120	Manufacture of luggage, handbags, saddlery and harness of any material
15.200	Manufacture of footwear
16	Manufacture of wood and of products of wood and cork, except furniture; manufacture of articles of straw and plaiting materials
16.110	Sawmilling and planing of wood
16.120	Processing and finishing of wood
16.210	Manufacture of veneer sheets and wood-based panels
16.220	Manufacture of assembled parquet floors
16.230	Manufacture of other builders' carpentry and joinery
16.240	Manufacture of wooden containers
16.250	Manufacture of doors and windows of wood
16.260	Manufacture of solid fuels from vegetable biomass
16.270	Finishing of wooden products
16.280	Manufacture of other products of wood and articles of cork, straw and plaiting materials
17	Manufacture of paper and paper products
17.110	Manufacture of pulp
17.120	Manufacture of paper and paperboard
17.210	Manufacture of corrugated paper, paperboard and containers of paper and paperboard
17.220	Manufacture of household and sanitary goods and of toilet requisites
17.230	Manufacture of paper stationery
17.240	Manufacture of wallpaper
17.250	Manufacture of other articles of paper and paperboard
18	Printing and reproduction of recorded media
18.110	Printing of newspapers
18.120	Other printing
18.130	Pre-press and pre-media services
18.140	Binding and related services
18.200	Reproduction of recorded media
19	Manufacture of coke and refined petroleum products
19.100	Manufacture of coke oven products
19.200	Manufacture of refined petroleum products and fossil fuel products
20	Manufacture of chemicals and chemical products
20.110	Manufacture of industrial gases
20.120	Manufacture of dyes and pigments
20.130	Manufacture of other inorganic basic chemicals
20.140	Manufacture of other organic basic chemicals
20.150	Manufacture of fertilisers and nitrogen compounds
20.160	Manufacture of plastics in primary forms
20.170	Manufacture of synthetic rubber in primary forms
20.200	Manufacture of pesticides, disinfectants and other agrochemical products
20.300	Manufacture of paints, varnishes and similar coatings, printing ink and mastics
20.410	Manufacture of soap and detergents, cleaning and polishing preparations
20.420	Manufacture of perfume and toilet preparations
20.510	Manufacture of liquid biofuels
20.590	Manufacture of other chemical products n.e.c.
20.600	Manufacture of man-made fibres
21	Manufacture of basic pharmaceutical products and pharmaceutical preparations
21.100	Manufacture of basic pharmaceutical products
21.200	Manufacture of pharmaceutical preparations
22	Manufacture of rubber and plastic products
22.110	Manufacture, retreading and rebuilding of rubber tyres and manufacture of tubes
22.120	Manufacture of other rubber products
22.210	Manufacture of plastic plates, sheets, tubes and profiles
22.220	Manufacture of plastic packing goods
22.230	Manufacture of doors and windows of plastic
22.240	Manufacture of builders’ ware of plastic
22.250	Processing and finishing of plastic products
22.260	Manufacture of other plastic products
23	Manufacture of other non-metallic mineral products
23.110	Manufacture of flat glass
23.120	Shaping and processing of flat glass
23.130	Manufacture of hollow glass
23.140	Manufacture of glass fibres
23.150	Manufacture and processing of other glass, including technical glassware
23.200	Manufacture of refractory products
23.310	Manufacture of ceramic tiles and flags
23.320	Manufacture of bricks, tiles and construction products, in baked clay
23.410	Manufacture of ceramic household and ornamental articles
23.420	Manufacture of ceramic sanitary fixtures
23.430	Manufacture of ceramic insulators and insulating fittings
23.440	Manufacture of other technical ceramic products
23.450	Manufacture of other ceramic products
23.510	Manufacture of cement
23.520	Manufacture of lime and plaster
23.610	Manufacture of concrete products for construction purposes
23.620	Manufacture of plaster products for construction purposes
23.630	Manufacture of ready-mixed concrete
23.640	Manufacture of mortars
23.650	Manufacture of fibre cement
23.660	Manufacture of other articles of concrete, cement and plaster
23.700	Cutting, shaping and finishing of stone
23.910	Manufacture of abrasive products
23.990	Manufacture of other non-metallic mineral products n.e.c.
24	Manufacture of basic metals
24.100	Manufacture of basic iron and steel and of ferro-alloys
24.200	Manufacture of tubes, pipes, hollow profiles and related fittings, of steel
24.310	Cold drawing of bars
24.320	Cold rolling of narrow strip
24.330	Cold forming or folding
24.340	Cold drawing of wire
24.410	Precious metals production
24.420	Aluminium production
24.430	Lead, zinc and tin production
24.440	Copper production
24.450	Other non-ferrous metal production
24.460	Processing of nuclear fuel
24.510	Casting of iron
24.520	Casting of steel
24.530	Casting of light metals
24.540	Casting of other non-ferrous metals
25	Manufacture of fabricated metal products, except machinery and equipment
25.110	Manufacture of metal structures and parts of structures
25.120	Manufacture of doors and windows of metal
25.210	Manufacture of central heating radiators, steam generators and boilers
25.220	Manufacture of other tanks, reservoirs and containers of metal
25.300	Manufacture of weapons and ammunition
25.400	Forging and shaping metal and powder metallurgy
25.510	Coating of metals
25.520	Heat treatment of metals
25.530	Machining of metals
25.610	Manufacture of cutlery
25.620	Manufacture of locks and hinges
25.630	Manufacture of tools
25.910	Manufacture of steel drums and similar containers
25.920	Manufacture of light metal packaging
25.930	Manufacture of wire products, chain and springs
25.940	Manufacture of fasteners and screw machine products
25.990	Manufacture of other fabricated metal products n.e.c.
26	Manufacture of computer, electronic and optical products
26.110	Manufacture of electronic components
26.120	Manufacture of loaded electronic boards
26.200	Manufacture of computers and peripheral equipment
26.300	Manufacture of communication equipment
26.400	Manufacture of consumer electronics
26.510	Manufacture of instruments and appliances for measuring, testing and navigation
26.520	Manufacture of watches and clocks
26.600	Manufacture of irradiation, electromedical and electrotherapeutic equipment
26.700	Manufacture of optical instruments, magnetic and optical media and photographic equipment
27	Manufacture of electrical equipment
27.110	Manufacture of electric motors, generators and transformers
27.120	Manufacture of electricity distribution and control apparatus
27.200	Manufacture of batteries and accumulators
27.310	Manufacture of fibre optic cables
27.320	Manufacture of other electronic and electric wires and cables
27.330	Manufacture of wiring devices
27.400	Manufacture of lighting equipment
27.510	Manufacture of electric domestic appliances
27.520	Manufacture of non-electric domestic appliances
27.900	Manufacture of other electrical equipment
28	Manufacture of machinery and equipment n.e.c.
28.110	Manufacture of engines and turbines, except aircraft, vehicle and cycle engines
28.120	Manufacture of fluid power equipment
28.130	Manufacture of other pumps and compressors
28.140	Manufacture of other taps and valves
28.150	Manufacture of bearings, gears, gearing and driving elements
28.210	Manufacture of ovens, furnaces and permanent household heating equipment
28.220	Manufacture of lifting and handling equipment
28.230	Manufacture of office machinery and equipment, except computers and peripheral equipment
28.240	Manufacture of power-driven hand tools
28.250	Manufacture of non-domestic air conditioning equipment
28.290	Manufacture of other general-purpose machinery n.e.c.
28.300	Manufacture of agricultural and forestry machinery
28.410	Manufacture of metal forming machinery and machine tools for metal work
28.420	Manufacture of other machine tools
28.910	Manufacture of machinery for metallurgy
28.920	Manufacture of machinery for mining, quarrying and construction
28.930	Manufacture of machinery for food, beverage and tobacco processing
28.940	Manufacture of machinery for textile, apparel and leather production
28.950	Manufacture of machinery for paper and paperboard production
28.960	Manufacture of plastics and rubber machinery
28.970	Manufacture of additive manufacturing machinery
28.990	Manufacture of other special-purpose machinery n.e.c.
29	Manufacture of motor vehicles, trailers and semi-trailers
29.100	Manufacture of motor vehicles
29.200	Manufacture of bodies and coachwork for motor vehicles; manufacture of trailers and semi-trailers
29.310	Manufacture of electrical and electronic equipment for motor vehicles
29.320	Manufacture of other parts and accessories for motor vehicles
30	Manufacture of other transport equipment
30.110	Building of civilian ships and floating structures
30.120	Building of pleasure and sporting boats
30.130	Building of military ships and vessels
30.200	Manufacture of railway locomotives and rolling stock
30.310	Manufacture of civilian air and spacecraft and related machinery
30.320	Manufacture of military air and spacecraft and related machinery
30.400	Manufacture of military fighting vehicles
30.910	Manufacture of motorcycles
30.920	Manufacture of bicycles and invalid carriages
30.990	Manufacture of other transport equipment n.e.c.
31	Manufacture of furniture
31.000	Manufacture of furniture
32	Other manufacturing
32.110	Striking of coins
32.120	Manufacture of jewellery and related articles
32.130	Manufacture of imitation jewellery and related articles
32.200	Manufacture of musical instruments
32.300	Manufacture of sports goods
32.400	Manufacture of games and toys
32.500	Manufacture of medical and dental instruments and supplies
32.910	Manufacture of brooms and brushes
32.990	Other manufacturing n.e.c.
33	Repair, maintenance and installation of machinery and equipment
33.110	Repair and maintenance of fabricated metal products
33.120	Repair and maintenance of machinery
33.130	Repair and maintenance of electronic and optical equipment
33.140	Repair and maintenance of electrical equipment
33.150	Repair and maintenance of civilian ships and boats
33.160	Repair and maintenance of civilian air and spacecraft
33.170	Repair and maintenance of other civilian transport equipment
33.180	Repair and maintenance of military fighting vehicles, ships, boats, air and spacecraft
33.190	Repair and maintenance of other equipment
33.200	Installation of industrial machinery and equipment
35	Electricity, gas, steam and air conditioning supply
35.110	Production of electricity from non-renewable sources
35.121	Production of electricity from hydropower
35.122	Production of electricity from wind power
35.123	Production of electricity from solar power
35.129	Production of electricity from renewable sources n.e.c.
35.130	Transmission of electricity
35.140	Distribution of electricity
35.150	Trade of electricity
35.160	Storage of electricity
35.210	Manufacture of gas
35.220	Distribution of gaseous fuels through mains
35.230	Trade of gas through mains
35.240	Storage of gas as part of network supply services
35.300	Steam and air conditioning supply
35.400	Activities of brokers and agents for electric power and natural gas
36	Water collection, treatment and supply
36.000	Water collection, treatment and supply
37	Sewerage
37.000	Sewerage
38	Waste collection, recovery and disposal activities
38.110	Collection of non-hazardous waste
38.120	Collection of hazardous waste
38.210	Materials recovery
38.220	Energy recovery
38.230	Other waste recovery
38.310	Incineration without energy recovery
38.320	Landfilling or permanent storage
38.330	Other waste disposal
39	Remediation activities and other waste management service activities
39.000	Remediation activities and other waste management service activities
41	Construction of residential and non-residential buildings
41.000	Construction of residential and non-residential buildings
42	Civil engineering
42.110	Construction of roads and motorways
42.120	Construction of railways and underground railways
42.130	Construction of bridges and tunnels
42.210	Construction of utility projects for fluids
42.221	Construction of utility projects for electricity and telecommunications at sea
42.222	Construction of utility projects for electricity and telecommunications on land
42.910	Construction of water projects
42.990	Construction of other civil engineering projects n.e.c.
43	Specialised construction activities
43.110	Demolition
43.120	Site preparation
43.130	Test drilling and boring
43.210	Electrical installation
43.221	Plumbing installation
43.222	Refrigeration installation, heat pump installation and installation of fireplaces
43.223	Ventilation installation
43.230	Installation of insulation
43.240	Other construction installation
43.310	Plastering
43.320	Joinery installation
43.330	Floor and wall covering
43.340	Painting and glazing
43.350	Other building completion and finishing
43.410	Roofing activities
43.420	Other specialised construction activities in construction of buildings
43.500	Specialised construction activities in civil engineering
43.600	Intermediation service activities for specialised construction services
43.910	Masonry and bricklaying activities
43.990	Other specialised construction activities n.e.c.
46	Wholesale trade
46.110	Activities of agents involved in the wholesale of agricultural raw materials, live animals, textile raw materials and semi-finished goods
46.120	Activities of agents involved in the wholesale of fuels, ores, metals and industrial chemicals
46.130	Activities of agents involved in the wholesale of timber and building materials
46.140	Activities of agents involved in the wholesale of machinery, industrial equipment, ships and aircraft
46.150	Activities of agents involved in the wholesale of furniture, household goods, hardware and ironmongery
46.160	Activities of agents involved in the wholesale of textiles, clothing, fur, footwear and leather goods
46.170	Activities of agents involved in the wholesale of food, beverages and tobacco
46.180	Activities of agents involved in the wholesale of other particular products
46.190	Activities of agents involved in non-specialised wholesale
46.210	Wholesale of grain, unmanufactured tobacco, seeds and animal feeds
46.220	Wholesale of flowers and plants
46.230	Wholesale of live animals
46.240	Wholesale of hides, skins and leather
46.310	Wholesale of fruit and vegetables
46.321	Wholesale of meat and meat products
46.322	Wholesale of fish, crustaceans and molluscs
46.330	Wholesale of dairy products, eggs and edible oils and fats
46.340	Wholesale of beverages
46.350	Wholesale of tobacco products
46.360	Wholesale of sugar and chocolate and sugar confectionery
46.370	Wholesale of coffee, tea, cocoa and spices
46.380	Wholesale of other food
46.390	Non-specialised wholesale of food, beverages and tobacco
46.410	Wholesale of textiles
46.420	Wholesale of clothing and footwear
46.430	Wholesale of electrical household appliances
46.440	Wholesale of china and glassware and cleaning materials
46.450	Wholesale of perfume and cosmetics
46.460	Wholesale of pharmaceutical and medical goods
46.470	Wholesale of household, office and shop furniture, carpets and lighting equipment
46.480	Wholesale of watches and jewellery
46.490	Wholesale of other household goods
46.500	Wholesale of information and communication equipment
46.610	Wholesale of agricultural machinery, equipment and supplies
46.620	Wholesale of machine tools
46.630	Wholesale of mining, construction and civil engineering machinery
46.640	Wholesale of other machinery and equipment
46.710	Wholesale of motor vehicles
46.720	Wholesale of motor vehicle parts and accessories
46.730	Wholesale of motorcycles, motorcycle parts and accessories
46.810	Wholesale of solid, liquid and gaseous fuels and related products
46.820	Wholesale of metals and metal ores
46.830	Wholesale of wood, construction materials and sanitary equipment
46.840	Wholesale of hardware, plumbing and heating equipment and supplies
46.850	Wholesale of chemical products
46.860	Wholesale of other intermediate products
46.870	Wholesale of waste and scrap
46.890	Other specialised wholesale n.e.c.
46.900	Non-specialised wholesale trade
47	Retail trade
47.110	Non-specialised retail sale of predominately food, beverages or tobacco
47.120	Other non-specialised retail sale
47.210	Retail sale of fruit and vegetables
47.220	Retail sale of meat and meat products
47.230	Retail sale of fish, crustaceans and molluscs
47.240	Retail sale of bread, cake and confectionery
47.250	Retail sale of beverages
47.260	Retail sale of tobacco products
47.270	Retail sale of other food
47.300	Retail sale of automotive fuel
47.400	Retail sale of information and communication equipment
47.510	Retail sale of textiles
47.520	Retail sale of hardware, building materials, paints and glass
47.530	Retail sale of carpets, rugs, wall and floor coverings
47.540	Retail sale of electrical household appliances
47.551	Retail sale of furniture, including mattresses and box springs
47.552	Retail sale of articles for lighting
47.553	Retail sale of household utensils, cutlery, crockery, glassware, tableware, china and pottery
47.559	Retail sale of household articles and equipment n.e.c.
47.610	Retail sale of books
47.620	Retail sale of newspapers, and other periodical publications and stationery
47.631	Retail sale of sports equipment
47.632	Retail sale of pleasure boats and equipment
47.640	Retail sale of games and toys
47.690	Retail sale of cultural and recreational goods n.e.c.
47.710	Retail sale of clothing
47.720	Retail sale of footwear and leather goods
47.730	Retail sale of pharmaceutical products
47.740	Retail sale of medical and orthopaedic goods
47.750	Retail sale of cosmetic and toilet articles
47.761	Retail sale of flowers, plants, seeds and fertilisers
47.762	Retail sale of pets and pet food
47.770	Retail sale of watches and jewellery
47.780	Retail sale of other new goods
47.790	Retail sale of second-hand goods
47.810	Retail sale of motor vehicles
47.820	Retail sale of motor vehicle parts and accessories
47.830	Retail sale of motorcycles, motorcycle parts and accessories
47.910	Intermediation service activities for non-specialised retail sale
47.920	Intermediation service activities for specialised retail sale
49	Land transport and transport via pipelines
49.110	Passenger heavy rail transport
49.120	Other passenger rail transport
49.200	Freight rail transport
49.310	Scheduled passenger transport by road
49.320	Non-scheduled passenger transport by road
49.330	On-demand passenger transport service activities by vehicle with driver
49.340	Passenger transport by cableways and ski lifts
49.390	Other passenger land transport n.e.c.
49.410	Freight transport by road
49.420	Removal services
49.500	Transport via pipeline
50	Water transport
50.101	Passenger ocean transport
50.102	Scheduled long distance passenger transport in coastal waters
50.109	Other passenger transport in coastal waters
50.201	Freight ocean transport
50.202	Freight coastal transport
50.203	Supply and other sea transport offshore services
50.300	Inland passenger water transport
50.400	Inland freight water transport
51	Air transport
51.100	Passenger air transport
51.210	Freight air transport
51.220	Space transport
52	Warehousing, storage and support activities for transportation
52.100	Warehousing and storage
52.211	Central agencies for goods and transportation procurement
52.212	Operation of car parks and garages
52.219	Other services incidental to land transport n.e.c.
52.220	Service activities incidental to water transportation
52.230	Service activities incidental to air transportation
52.240	Cargo handling
52.250	Logistics service activities
52.260	Other support activities for transportation
52.311	Intermediation service activities for freight transportation by ship
52.312	Intermediation service activities for freight transportation by railway, aircraft or on roads
52.320	Intermediation service activities for passenger transportation
53	Postal and courier activities
53.100	Postal activities under universal service obligation
53.200	Other postal and courier activities
53.300	Intermediation service activities for postal and courier activities
55	Accommodation
55.100	Hotels and similar accommodation
55.200	Holiday and other short-stay accommodation
55.300	Camping grounds and recreational vehicle parks
55.400	Intermediation service activities for accommodation
55.900	Other accommodation
56	Food and beverage service activities
56.110	Restaurant activities
56.120	Mobile food service activities
56.210	Event catering activities
56.220	Contract catering service activities and other food service activities
56.300	Beverage serving activities
56.400	Intermediation service activities for food and beverage services activities
58	Publishing activities
58.110	Publishing of books
58.120	Publishing of newspapers
58.130	Publishing of journals and periodicals
58.190	Other publishing activities, except software publishing
58.210	Publishing of video games
58.290	Other software publishing
59	Motion picture, video and television programme production, sound recording and music publishing activities
59.110	Motion picture, video and television programme production activities
59.120	Motion picture, video and television programme post-production activities
59.130	Motion picture and video distribution activities
59.140	Motion picture projection activities
59.200	Sound recording and music publishing activities
60	Programming, broadcasting, news agency and other content distribution activities
60.100	Radio broadcasting and audio distribution activities
60.200	Television programming, broadcasting and video distribution activities
60.310	News agency activities
60.390	Other content distribution activities
61	Telecommunication
61.100	Wired, wireless, and satellite telecommunication activities
61.200	Telecommunication reselling activities and intermediation service activities for telecommunication
61.900	Other telecommunication activities
62	Computer programming, consultancy and related activities
62.100	Computer programming activities
62.200	Computer consultancy and computer facilities management activities
62.900	Other information technology and computer service activities
63	Computing infrastructure, data processing, hosting and other information service activities
63.100	Computing infrastructure, data processing, hosting and related activities
63.910	Web search portal activities
63.920	Other information service activities
64	Financial service activities, except insurance and pension funding
64.110	Central banking
64.190	Other monetary intermediation
64.210	Activities of holding companies
64.220	Activities of financing conduits
64.311	Unit trusts
64.312	Other investment companies, except captive companies
64.321	Charities which do not distribute means themselves
64.322	Venture and development capital companies
64.323	Other captive investment companies
64.910	Financial leasing
64.920	Other credit granting
64.990	Other financial service activities, except insurance and pension funding n.e.c.
65	Insurance, reinsurance and pension funding, except compulsory social security
65.110	Life insurance
65.120	Non-life insurance
65.200	Reinsurance
65.300	Pension funding
66	Activities auxiliary to financial services and insurance activities
66.110	Administration of financial markets
66.120	Security and commodity contracts brokerage
66.190	Other activities auxiliary to financial services, except insurance and pension funding
66.210	Risk and damage evaluation
66.220	Activities of insurance agents and brokers
66.290	Activities auxiliary to insurance and pension funding n.e.c.
66.300	Fund management activities
68	Real estate activities
68.110	Buying and selling of own real estate
68.120	Development of building projects
68.200	Rental and operating of own or leased real estate
68.310	Intermediation service activities for real estate activities
68.320	Other real estate activities on a fee or contract basis
69	Legal and accounting activities
69.100	Legal activities
69.201	Auditing
69.202	Accounting and bookkeeping
69.203	Tax consultancy activity
70	Activities of head offices and management consultancy
70.100	Activities of head offices
70.200	Business and other management consultancy activities
71	Architectural and engineering activities; technical testing and analysis
71.110	Architectural activities
71.121	Civil engineering activities
71.122	Geological surveying
71.123	Preparation of maps and surveying
71.129	Other technical consultancy
71.200	Technical testing and analysis
72	Scientific research and development
72.100	Research and experimental development on natural sciences and engineering
72.200	Research and experimental development on social sciences and humanities
73	Activities of advertising, market research and public relations
73.110	Activities of advertising agencies
73.120	Media representation
73.200	Market research and public opinion polling
73.300	Public relations and communication activities
74	Other professional, scientific and technical activities
74.110	Industrial product and fashion design activities
74.120	Graphic design and visual communication activities
74.130	Interior design activities
74.140	Other specialised design activities
74.200	Photographic activities
74.300	Translation and interpretation activities
74.910	Patent brokering and marketing service activities
74.990	All other professional, scientific and technical activities n.e.c.
75	Veterinary activities
75.000	Veterinary activities
77	Rental and leasing activities
77.110	Rental and leasing of cars and light motor vehicles
77.120	Rental and leasing of trucks
77.210	Rental and leasing of recreational and sports goods
77.220	Rental and leasing of other personal and household goods
77.310	Rental and leasing of agricultural machinery and equipment
77.320	Rental and leasing of construction and civil engineering machinery and equipment
77.330	Rental and leasing of office machinery, equipment and computers
77.340	Rental and leasing of water transport equipment
77.350	Rental and leasing of air transport equipment
77.390	Rental and leasing of other machinery, equipment and tangible goods n.e.c.
77.400	Leasing of intellectual property and similar products, except copyrighted works
77.510	Intermediation service activities for rental and leasing of cars, motorhomes and trailers
77.520	Intermediation service activities for rental and leasing of other tangible goods and non-financial intangible assets
78	Employment activities
78.100	Activities of employment placement agencies
78.200	Temporary employment agency activities and other human resource provisions
79	Travel agency, tour operator and other reservation service and related activities
79.110	Travel agency activities
79.121	Activities of excursion and event organisers
79.122	Other tour operator activities
79.901	Activities of tourist offices and destination marketing organisations
79.902	Activities of tour guides
80	Investigation and security activities
80.010	Investigation and private security activities
80.090	Security activities n.e.c.
81	Services to buildings and landscape activities
81.100	Combined facilities support activities
81.210	General cleaning of buildings
81.220	Other building and industrial cleaning activities
81.230	Other cleaning activities
81.300	Landscape service activities
82	Office administrative, office support and other business support activities
82.100	Office administrative and support activities
82.200	Activities of call centres
82.300	Organisation of conventions and trade shows
82.400	Intermediation service activities for business support service activities n.e.c.
82.910	Activities of collection agencies and credit bureaus
82.920	Packaging activities
82.990	Other business support service activities n.e.c.
84	Public administration and defence; compulsory social security
84.110	General public administration activities
84.120	Regulation of health care, education, cultural services and other social services
84.130	Regulation of and contribution to more efficient operation of businesses
84.210	Foreign affairs
84.220	Defence activities
84.230	Justice and judicial activities
84.240	Public order and safety activities
84.250	Fire service activities
84.300	Compulsory social security activities
85	Education
85.100	Pre-primary education
85.201	General primary education
85.202	Special education at primary level
85.310	General secondary education
85.320	Vocational secondary education
85.330	Post-secondary non-tertiary education
85.401	Education at universities
85.402	Education at specialised university institutions
85.403	Education at other colleges
85.404	Tertiary vocational education
85.510	Sports and recreation education
85.521	Cultural education for children and young people
85.529	Other cultural education
85.531	Education at authorised driving schools
85.532	Miscellaneous vehicular training excluding authorised driving schools
85.591	Folk high school education
85.592	Education in the form of labour market training/work-oriented courses
85.593	Activities of adult education centres and adult education associations
85.594	Private tuition
85.595	Religious education
85.596	Activities at camp schools
85.597	Other education activities
85.610	Intermediation service activities for courses and tutors
85.691	Educational and psychological counselling service (EPS)
85.692	Activities of special education resource centres and non-EPS services
85.699	Other educational support services n.e.c.
86	Human health activities
86.101	General hospital activities
86.102	Psychiatric hospital activities for adults
86.103	Specialised substance abuse treatment
86.104	Psychiatric hospital activities for children and adolescents
86.210	General medical practice activities
86.221	Activities of specialist physicians, other than psychiatrists
86.222	Activities of psychiatrists
86.230	Dental practice care activities
86.910	Diagnostic imaging services and medical laboratory activities
86.921	Patient transportation by ambulance, except by air
86.922	Air ambulance patient transportation by aircraft or helicopter
86.930	Activities of psychologists and psychotherapists, except medical doctors
86.941	Nursing and other human health services at patients' homes
86.942	Maternal and child health care and school health activities
86.950	Physiotherapy activities
86.960	Traditional, complementary and alternative medicine activities
86.970	Intermediation service activities for medical, dental and other human health services
86.991	Activities of orthopedists and chiropodists
86.992	Preventive health care activities
86.993	Other health services
87	Residential care activities
87.101	Specialised residential nursing care activities
87.102	Nursing care activities in nursing homes
87.103	Municipal acute bed unit activities
87.104	Nursing care activities in facilities for short-term respite care
87.105	Nursing care activities in children's homes
87.106	Nursing care activities in co-located care homes and similar
87.201	Residential care activities for persons living with or having a diagnosis of a mental illness and/or substance abuse
87.202	Residential care activities for persons living with or having a diagnosis of intellectual disability
87.300	Residential care activities for older persons or persons with physical disabilities
87.910	Intermediation service activities for residential care activities
87.991	Activities in child welfare institutions
87.992	Activities in reception centres for asylum seekers
87.999	Residential care activities in other types of accommodation n.e.c.
88	Social work activities without accommodation
88.101	Practical assistance in the home
88.102	Day care activities adapted to specific target groups
88.103	Activities in centres for the elderly
88.104	User-controlled personal assistance
88.105	Respite care activities without accommodation
88.106	Personal support contact and visiting services
88.910	Child day-care activities
88.991	Child welfare services
88.992	Family counselling services
88.993	Work training and permanently adapted work
88.994	Welfare services for vulnerable groups
88.995	Services provided through social services offices
88.996	Other unspecified social services without accommodation
90	Arts creation and performing arts activities
90.111	Arts creation activities in literature
90.112	Arts creation activities in musical composition
90.120	Visual arts creation activities
90.130	Other arts creation activities
90.201	Performing arts activities in music
90.202	Performing arts activities in dramatic arts
90.209	Other performing arts activities
90.310	Operation of arts facilities and sites
90.390	Other support activities to arts and performing arts
91	Libraries, archives, museums and other cultural activities
91.111	Public library activities
91.112	Special and research library activities
91.120	Archive activities
91.211	Art museum activities
91.212	Cultural history museum activities
91.213	Natural history, technological and science history museum activities
91.220	Historical site and monument activities
91.300	Conservation, restoration and other support activities for cultural heritage
91.410	Botanical and zoological garden activities
91.420	Nature reserve activities
92	Gambling and betting activities
92.000	Gambling and betting activities
93	Sports activities and amusement and recreation activities
93.110	Operation of sports facilities
93.120	Activities of sports clubs
93.130	Activities of fitness centres
93.190	Sports activities n.e.c.
93.210	Activities of amusement parks and theme parks
93.291	Activities of adventure companies
93.292	Other recreational activities
94	Activities of membership organisations
94.110	Activities of business and employers membership organisations
94.120	Activities of professional membership organisations
94.200	Activities of trade unions
94.910	Activities of religious organisations
94.920	Activities of political organisations
94.991	Activities of charities which distribute means themselves
94.992	Other activities of other membership organisations
95	Repair and maintenance of computers, personal and household goods, and motor vehicles and motorcycles
95.100	Repair and maintenance of computers and communication equipment
95.210	Repair and maintenance of consumer electronics
95.220	Repair and maintenance of household appliances and home and garden equipment
95.230	Repair and maintenance of footwear and leather goods
95.240	Repair and maintenance of furniture and home furnishings
95.250	Repair and maintenance of watches, clocks and jewellery
95.290	Repair and maintenance of personal and household goods n.e.c.
95.310	Repair and maintenance of motor vehicles
95.320	Repair and maintenance of motorcycles
95.400	Intermediation service activities for repair and maintenance of computers, personal and household goods, and motor vehicles and motorcycles
96	Personal service activities
96.100	Washing and cleaning of textile and fur products
96.210	Hairdressing and barber activities
96.220	Beauty care and other beauty treatment activities
96.230	Day spa, sauna and steam bath activities
96.300	Funeral and related activities
96.400	Intermediation service activities for personal services
96.910	Provision of domestic personal service activities
96.990	Other personal service activities n.e.c.
97	Activities of households as employers of domestic personnel
97.001	Activities of housing cooperatives
97.002	Other activities of households as employers of domestic personnel
98	Undifferentiated goods- and service-producing activities of private households for own use
98.100	Undifferentiated goods-producing activities of private households for own use
98.200	Undifferentiated service-producing activities of private households for own use
99	Activities of extraterritorial organisations and bodies
99.000	Activities of extraterritorial organisations and bodies
"""
