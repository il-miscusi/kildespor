"""Official Norwegian records from Brønnøysundregistrene (NLOD 2.0 open data).

The organisation number is the identity anchor. Everything here comes from
data.brreg.no: the entity register, public roles, registered subunits, the
group structure, the annual-accounts register and the register's own update
feed. Person data is used only in the company-role context and birth dates
are never stored.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
from typing import Any, Iterable

from .http import Fetcher, Snapshot, utc_now
from .model import CompanyRecord

BASE = "https://data.brreg.no/enhetsregisteret/api"
ACCOUNTS = "https://data.brreg.no/regnskapsregisteret/regnskap"
SOURCE_ENTITY = "official_registry"
POLICY = "NLOD 2.0 open data, data.brreg.no"


def _span(path: str, value: Any) -> str:
    return f"{path} = {json.dumps(value, ensure_ascii=False)}"


def format_address(address: dict[str, Any] | None) -> str | None:
    if not address:
        return None
    lines = [line for line in address.get("adresse") or [] if line]
    place = " ".join(part for part in (address.get("postnummer"), address.get("poststed")) if part)
    parts = lines + ([place] if place else [])
    country = address.get("land")
    if country and country != "Norge":
        parts.append(country)
    return ", ".join(parts) or None


def person_name(person: dict[str, Any]) -> str | None:
    name = person.get("navn") or {}
    parts = [name.get("fornavn"), name.get("mellomnavn"), name.get("etternavn")]
    text = " ".join(part for part in parts if part)
    return text or None


# -- registry snapshot fallback ------------------------------------------------------
def load_registry_snapshot(path: str, wanted: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Read the rows for the requested organisations from a Brreg bulk CSV (optionally gzipped)."""
    wanted_set = set(wanted)
    opener = gzip.open if path.endswith(".gz") else open
    rows: dict[str, dict[str, Any]] = {}
    with opener(path, "rt", encoding="utf-8", newline="") as handle:  # type: ignore[operator]
        reader = csv.DictReader(handle)
        for row in reader:
            org = (row.get("Organisasjonsnummer") or row.get("organisasjonsnummer") or "").strip()
            if org in wanted_set:
                rows[org] = row
                if len(rows) == len(wanted_set):
                    break
    return rows


def entity_from_snapshot_row(row: dict[str, Any]) -> dict[str, Any]:
    def get(*names: str) -> str | None:
        for name in names:
            value = row.get(name)
            if value not in (None, ""):
                return value
        return None

    address_lines = [line for line in (get("Forretningsadresse.adresse") or "").split(";") if line]
    return {
        "organisasjonsnummer": get("Organisasjonsnummer"),
        "navn": get("Navn"),
        "organisasjonsform": {"kode": get("Organisasjonsform.kode"), "beskrivelse": get("Organisasjonsform.beskrivelse")},
        "naeringskode1": {"kode": get("Næringskode 1"), "beskrivelse": get("Næringskode 1.beskrivelse")},
        "antallAnsatte": int(get("Antall ansatte") or 0) if get("Antall ansatte") else None,
        "harRegistrertAntallAnsatte": bool(get("Antall ansatte")),
        "hjemmeside": get("Hjemmeside"),
        "forretningsadresse": {
            "adresse": address_lines,
            "postnummer": get("Forretningsadresse.postnummer"),
            "poststed": get("Forretningsadresse.poststed"),
            "kommune": get("Forretningsadresse.kommune"),
            "land": get("Forretningsadresse.land") or "Norge",
        },
        "stiftelsesdato": get("Stiftelsesdato"),
        "sisteInnsendteAarsregnskap": get("Siste innsendte årsregnskap"),
    }


# -- entity ----------------------------------------------------------------------------
def fetch_entity(fetcher: Fetcher, rec: CompanyRecord, snapshot_row: dict[str, Any] | None = None) -> dict[str, Any] | None:
    org = rec.organisation_number
    url = f"{BASE}/enheter/{org}"
    try:
        snap = fetcher.get(url, retries=2, check_robots=False, headers={"Accept": "application/json"})
    except Exception as exc:  # network failure: fall back to the frozen snapshot if we have one
        rec.error("registry", f"live entity lookup failed: {exc}", url)
        snap = None
    if snap is not None and snap.status == 410:
        rec.set_state("legal_identity", "not_applicable", "the entity has been deleted from the register (HTTP 410)", checked_sources=[url])
        return None
    if snap is not None and snap.ok:
        data = snap.json()
        rec.context["entity"] = data
        rec.context["entity_snapshot"] = snap
        _entity_claims(rec, data, snap=snap, url=url)
        return data
    if snapshot_row:
        data = entity_from_snapshot_row(snapshot_row)
        rec.context["entity"] = data
        _entity_claims(rec, data, snap=None, url="https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv")
        return data
    status = snap.status if snap is not None else "no response"
    rec.set_state("legal_identity", "failed", f"entity register lookup failed ({status})", checked_sources=[url])
    return None


def _entity_claims(rec: CompanyRecord, data: dict[str, Any], *, snap: Snapshot | None, url: str) -> None:
    def ev(path: str, value: Any) -> str:
        if snap is not None:
            return rec.evidence_from_snapshot(snap, source_class=SOURCE_ENTITY, span=_span(path, value), method="brreg_entity_api_field", access_policy=POLICY)
        return rec.add_evidence(
            source_url=url,
            source_class="official_registry_bulk",
            retrieved_at=utc_now(),
            content_sha256=None,
            claim_span=_span(path, value),
            extraction_method="brreg_bulk_snapshot_field",
            access_policy=POLICY,
        )

    def claim(field_name: str, path: str, value: Any, *, family: str = "legal_identity", effective: str | None = None, key: Any = None, label: str | None = None) -> None:
        if value in (None, "", []):
            return
        rec.add_claim(
            family=family,
            field_name=field_name,
            value=value,
            key=key,
            evidence_ids=[ev(path, value)],
            confidence=0.99,
            method="official_register",
            effective_date=effective,
            label=label,
        )

    claim("organisation_number", "organisasjonsnummer", data.get("organisasjonsnummer"), label="Organisation number")
    claim("legal_name", "navn", data.get("navn"), label="Legal name")
    form = data.get("organisasjonsform") or {}
    if form.get("kode"):
        claim("legal_form", "organisasjonsform", {"code": form.get("kode"), "description": form.get("beskrivelse")}, label="Legal form")
    claim("registered_business_address", "forretningsadresse", format_address(data.get("forretningsadresse")), label="Business address")
    claim("postal_address", "postadresse", format_address(data.get("postadresse")), label="Postal address")
    municipality = (data.get("forretningsadresse") or {}).get("kommune")
    claim("municipality", "forretningsadresse.kommune", municipality, label="Municipality")
    for index, key in enumerate(("naeringskode1", "naeringskode2", "naeringskode3"), start=1):
        code = data.get(key) or {}
        if code.get("kode"):
            claim("industry_code", key, {"code": code.get("kode"), "description": code.get("beskrivelse"), "rank": index}, key=code.get("kode"), label="Industry (NACE)")
    if data.get("harRegistrertAntallAnsatte") and data.get("antallAnsatte") is not None:
        claim(
            "registered_employees",
            "antallAnsatte",
            data.get("antallAnsatte"),
            effective=data.get("registreringsdatoAntallAnsatteEnhetsregisteret"),
            label="Registered employees",
        )
    claim("founded_date", "stiftelsesdato", data.get("stiftelsesdato"), label="Founded")
    claim("registered_date", "registreringsdatoEnhetsregisteret", data.get("registreringsdatoEnhetsregisteret"), label="Registered in the Entity Register")
    capital = data.get("kapital") or {}
    if capital.get("belop") is not None:
        claim(
            "share_capital",
            "kapital",
            {"amount": capital.get("belop"), "currency": capital.get("valuta") or "NOK", "type": capital.get("type")},
            effective=capital.get("innfortDato"),
            label="Share capital",
        )
    purpose = " ".join(data.get("vedtektsfestetFormaal") or []).strip()
    claim("statutory_purpose", "vedtektsfestetFormaal", purpose, label="Statutory purpose")
    activity = " ".join(data.get("aktivitet") or []).strip()
    claim("registered_activity", "aktivitet", activity, label="Registered activity")
    for flag, path, label in (
        ("vat_registered", "registrertIMvaregisteret", "VAT registered"),
        ("in_business_register", "registrertIForetaksregisteret", "In the Register of Business Enterprises"),
        ("part_of_group", "erIKonsern", "Part of a corporate group"),
    ):
        if isinstance(data.get(path), bool):
            claim(flag, path, data.get(path), label=label)
    status = []
    for path, label in (("konkurs", "bankrupt"), ("underAvvikling", "under liquidation"), ("underTvangsavviklingEllerTvangsopplosning", "under compulsory dissolution")):
        if data.get(path) is True:
            status.append(label)
    if any(isinstance(data.get(path), bool) for path in ("konkurs", "underAvvikling")):
        claim("registry_status", "konkurs/underAvvikling", ", ".join(status) if status else "active", label="Registry status")
    claim("latest_filed_accounts_year", "sisteInnsendteAarsregnskap", data.get("sisteInnsendteAarsregnskap"), family="annual_accounts", label="Latest filed annual accounts")
    website = data.get("hjemmeside")
    claim("registry_website", "hjemmeside", website, family="legal_identity", label="Website listed in the Entity Register (as registered)")
    for path in ("telefon", "mobil"):
        if data.get(path):
            claim("registry_phone", path, data.get(path), family="contact_channels", key=data.get(path).replace(" ", ""), label="Phone (register)")
    for item in data.get("historiskeNavn") or []:
        name = item.get("navn")
        if name and name != data.get("navn"):
            claim("former_name", "historiskeNavn", {"name": name, "from": (item.get("fraDato") or "")[:10], "to": (item.get("tilDato") or "")[:10]}, key=name, label="Former name")
    rec.set_state("legal_identity", "available", "entity register record found", checked_sources=[url])


# -- roles -----------------------------------------------------------------------------
def fetch_roles(fetcher: Fetcher, rec: CompanyRecord) -> None:
    org = rec.organisation_number
    url = f"{BASE}/enheter/{org}/roller"
    try:
        snap = fetcher.get(url, retries=2, check_robots=False, headers={"Accept": "application/json"})
    except Exception as exc:
        rec.error("roles", str(exc), url)
        rec.set_state("leadership", "failed", f"roles lookup failed: {exc}", checked_sources=[url])
        return
    if snap.status == 404:
        rec.set_state("leadership", "not_available", "the register returned no public roles", checked_sources=[url])
        return
    if not snap.ok:
        rec.set_state("leadership", "failed", f"roles lookup returned HTTP {snap.status}", checked_sources=[url])
        return
    body = snap.json()
    people: list[dict[str, Any]] = []
    for group in body.get("rollegrupper") or []:
        group_type = group.get("type") or {}
        changed = group.get("sistEndret")
        for role in group.get("roller") or []:
            if role.get("fratraadt") or role.get("avregistrert"):
                continue
            role_type = role.get("type") or {}
            holder: dict[str, Any] = {}
            if role.get("person"):
                if role["person"].get("erDoed"):
                    continue
                holder["name"] = person_name(role["person"])
                holder["holder_type"] = "person"
            elif role.get("enhet"):
                unit = role["enhet"]
                holder["name"] = " ".join(unit.get("navn") or []) if isinstance(unit.get("navn"), list) else unit.get("navn")
                holder["organisation_number"] = unit.get("organisasjonsnummer")
                holder["holder_type"] = "organisation"
            if not holder.get("name"):
                continue
            value = {
                "role": role_type.get("beskrivelse"),
                "role_code": role_type.get("kode"),
                "group": group_type.get("beskrivelse"),
                "group_code": group_type.get("kode"),
                **holder,
            }
            span = f'{group_type.get("kode")}/{role_type.get("kode")}: {holder["name"]}'
            ev = rec.evidence_from_snapshot(snap, source_class="official_roles", span=span, method="brreg_roles_api", access_policy=POLICY)
            rec.add_claim(
                family="leadership",
                field_name="role",
                value=value,
                key=[role_type.get("kode"), holder["name"], holder.get("organisation_number")],
                evidence_ids=[ev],
                confidence=0.99,
                method="official_register",
                effective_date=changed,
                label=role_type.get("beskrivelse"),
            )
            people.append(value)
        if changed and group_type.get("kode") in ("DAGL", "STYR", "REVI", "REGN", "DTPR", "DTSO", "KOMP"):
            ev = rec.evidence_from_snapshot(snap, source_class="official_roles", span=f'{group_type.get("kode")} sistEndret = {changed}', method="brreg_roles_api", access_policy=POLICY)
            rec.add_claim(
                family="public_activity",
                field_name="registry_role_change",
                value={"event": f"{group_type.get('beskrivelse')} registration last changed", "group_code": group_type.get("kode"), "date": changed},
                key=[group_type.get("kode"), changed],
                evidence_ids=[ev],
                confidence=0.99,
                method="official_register",
                effective_date=changed,
                label="Register change",
            )
    rec.context["roles"] = people
    if people:
        rec.set_state("leadership", "available", "public roles found in the register", checked_sources=[url])
    else:
        rec.set_state("leadership", "not_available", "the register lists no active public roles", checked_sources=[url])


# -- subunits --------------------------------------------------------------------------
def fetch_subunits(fetcher: Fetcher, rec: CompanyRecord) -> None:
    org = rec.organisation_number
    url = f"{BASE}/underenheter?overordnetEnhet={org}&size=200"
    try:
        snap = fetcher.get(url, retries=2, check_robots=False, headers={"Accept": "application/json"})
    except Exception as exc:
        rec.error("workplaces", str(exc), url)
        rec.set_state("workplaces", "failed", f"subunit lookup failed: {exc}", checked_sources=[url])
        return
    if not snap.ok:
        rec.set_state("workplaces", "failed", f"subunit lookup returned HTTP {snap.status}", checked_sources=[url])
        return
    body = snap.json()
    units = (body.get("_embedded") or {}).get("underenheter") or []
    rec.context["subunits"] = []
    for unit in units:
        if unit.get("nedleggelsesdato"):
            continue
        address = format_address(unit.get("beliggenhetsadresse"))
        industry = unit.get("naeringskode1") or {}
        value = {
            "organisation_number": unit.get("organisasjonsnummer"),
            "name": unit.get("navn"),
            "address": address,
            "municipality": (unit.get("beliggenhetsadresse") or {}).get("kommune"),
            "industry": {"code": industry.get("kode"), "description": industry.get("beskrivelse")} if industry.get("kode") else None,
            "employees": unit.get("antallAnsatte") if unit.get("harRegistrertAntallAnsatte") else None,
            "started": unit.get("oppstartsdato"),
            "website": unit.get("hjemmeside"),
        }
        rec.context["subunits"].append(value)
        span = f'underenhet {unit.get("organisasjonsnummer")}: {unit.get("navn")}, {address}'
        ev = rec.evidence_from_snapshot(snap, source_class="official_subunits", span=span, method="brreg_subunits_api", access_policy=POLICY)
        rec.add_claim(
            family="workplaces",
            field_name="registered_workplace",
            value={k: v for k, v in value.items() if v not in (None, "")},
            key=unit.get("organisasjonsnummer"),
            evidence_ids=[ev],
            confidence=0.99,
            method="official_register",
            effective_date=unit.get("oppstartsdato"),
            label="Registered workplace",
        )
    if rec.context["subunits"]:
        rec.set_state("workplaces", "available", "registered subunits found", checked_sources=[url])
    else:
        rec.set_state("workplaces", "not_available", "the register lists no active subunits (workplaces) for this entity", checked_sources=[url])


# -- group -----------------------------------------------------------------------------
def fetch_group(fetcher: Fetcher, rec: CompanyRecord) -> None:
    entity = rec.context.get("entity") or {}
    if not entity.get("erIKonsern"):
        return
    org = rec.organisation_number
    url = f"{BASE}/konsernstruktur/{org}"
    try:
        snap = fetcher.get(url, retries=1, check_robots=False, headers={"Accept": "application/json"})
    except Exception as exc:
        rec.error("group", str(exc), url)
        return
    if not snap.ok:
        return
    tree = snap.json()
    nodes: dict[str, dict[str, Any]] = {}

    def walk(node: dict[str, Any], parent: str | None) -> None:
        number = node.get("organisasjonsnummer")
        if number:
            nodes[number] = {
                "organisation_number": number,
                "name": node.get("navn"),
                "parent": node.get("parentOrganisasjonsnummer") or parent,
                "relation": (node.get("knytningsform") or {}).get("beskrivelse"),
                "ownership": node.get("grunnlag"),
                "since": node.get("dato"),
            }
        for child in node.get("children") or []:
            walk(child, number)

    walk(tree, None)
    me = nodes.get(org)
    if not me:
        return
    root = tree.get("organisasjonsnummer")
    related: list[dict[str, Any]] = []
    if me.get("parent") and me["parent"] in nodes:
        parent = nodes[me["parent"]]
        related.append({"relationship": "parent", "organisation_number": parent["organisation_number"], "name": parent["name"], "ownership": me.get("ownership"), "since": me.get("since")})
    if root and root != org and root != me.get("parent"):
        related.append({"relationship": "ultimate_parent", "organisation_number": root, "name": tree.get("navn")})
    children = [node for node in nodes.values() if node.get("parent") == org]
    for child in children[:40]:
        related.append({"relationship": "subsidiary", "organisation_number": child["organisation_number"], "name": child["name"], "ownership": child.get("ownership"), "since": child.get("since")})
    rec.context["group_orgs"] = set(nodes)
    rec.context["group_root"] = root
    for item in related:
        span = f'{item["relationship"]}: {item["organisation_number"]} {item["name"]}'
        ev = rec.evidence_from_snapshot(snap, source_class="official_group_structure", span=span, method="brreg_group_structure_api", access_policy=POLICY)
        rec.add_claim(
            family="legal_identity",
            field_name="group_relationship",
            value={k: v for k, v in item.items() if v not in (None, "")},
            key=[item["relationship"], item["organisation_number"]],
            evidence_ids=[ev],
            confidence=0.98,
            method="official_register",
            effective_date=item.get("since"),
            relationship=item["relationship"],
            label="Group relationship",
        )


# -- annual accounts ---------------------------------------------------------------------
METRICS: list[tuple[str, tuple[str, ...], str]] = [
    ("revenue", ("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter"), "Operating revenue"),
    ("operating_expenses", ("resultatregnskapResultat", "driftsresultat", "driftskostnad", "sumDriftskostnad"), "Operating expenses"),
    ("operating_result", ("resultatregnskapResultat", "driftsresultat", "driftsresultat"), "Operating result"),
    ("net_financial_items", ("resultatregnskapResultat", "finansresultat", "nettoFinans"), "Net financial items"),
    ("profit_before_tax", ("resultatregnskapResultat", "ordinaertResultatFoerSkattekostnad"), "Profit before tax"),
    ("tax_expense", ("resultatregnskapResultat", "ordinaertResultatSkattekostnad"), "Tax on ordinary result"),
    ("annual_result", ("resultatregnskapResultat", "aarsresultat"), "Annual result"),
    ("total_assets", ("eiendeler", "sumEiendeler"), "Total assets"),
    ("fixed_assets", ("eiendeler", "anleggsmidler", "sumAnleggsmidler"), "Fixed assets"),
    ("current_assets", ("eiendeler", "omloepsmidler", "sumOmloepsmidler"), "Current assets"),
    ("equity", ("egenkapitalGjeld", "egenkapital", "sumEgenkapital"), "Equity"),
    ("total_debt", ("egenkapitalGjeld", "gjeldOversikt", "sumGjeld"), "Total liabilities"),
    ("short_term_debt", ("egenkapitalGjeld", "gjeldOversikt", "kortsiktigGjeld", "sumKortsiktigGjeld"), "Current liabilities"),
    ("long_term_debt", ("egenkapitalGjeld", "gjeldOversikt", "langsiktigGjeld", "sumLangsiktigGjeld"), "Long-term liabilities"),
]


def _dig(obj: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def fetch_accounts(fetcher: Fetcher, rec: CompanyRecord) -> None:
    org = rec.organisation_number
    url = f"{ACCOUNTS}/{org}"
    try:
        snap = fetcher.get(url, retries=2, check_robots=False, headers={"Accept": "application/json"})
    except Exception as exc:
        rec.error("annual_accounts", str(exc), url)
        rec.set_state("annual_accounts", "failed", f"accounts lookup failed: {exc}", checked_sources=[url])
        return
    if snap.status == 404:
        rec.set_state("annual_accounts", "not_available", "the Register of Company Accounts returned no normalised accounts (HTTP 404)", checked_sources=[url])
        return
    if not snap.ok:
        rec.set_state("annual_accounts", "failed", f"accounts lookup returned HTTP {snap.status}", checked_sources=[url])
        return
    try:
        records = snap.json()
    except ValueError:
        rec.set_state("annual_accounts", "failed", "accounts response was not JSON", checked_sources=[url])
        return
    if isinstance(records, dict):
        records = [records]
    statements: list[dict[str, Any]] = []
    for item in records or []:
        period = item.get("regnskapsperiode") or {}
        start, end = period.get("fraDato"), period.get("tilDato")
        if not end:
            continue
        account_type = item.get("regnskapstype") or "SELSKAP"
        currency = item.get("valuta") or "NOK"
        reporting = f"{start}/{end}"
        values: dict[str, Any] = {}
        for metric, path, label in METRICS:
            amount = _dig(item, path)
            if amount is None:
                continue
            values[metric] = amount
            span = f'{account_type} {end[:4]} {"/".join(path[-2:])} = {amount}'
            ev = rec.evidence_from_snapshot(snap, source_class="official_annual_accounts", span=span, method="brreg_accounts_api", reporting_period=reporting, access_policy=POLICY)
            rec.add_claim(
                family="annual_accounts",
                field_name=f"financial.{metric}",
                value={"amount": amount, "currency": currency, "account_type": account_type, "fiscal_year": end[:4]},
                key=[metric, account_type, end],
                evidence_ids=[ev],
                confidence=0.99,
                method="official_register",
                reporting_period=reporting,
                label=label,
            )
        audit = item.get("revisjon") or {}
        if isinstance(audit.get("fravalgRevisjon"), bool):
            ev = rec.evidence_from_snapshot(snap, source_class="official_annual_accounts", span=f"{account_type} {end[:4]} revisjon.fravalgRevisjon = {audit.get('fravalgRevisjon')}", method="brreg_accounts_api", reporting_period=reporting, access_policy=POLICY)
            rec.add_claim(
                family="annual_accounts",
                field_name="financial.audit_opt_out",
                value={"opted_out_of_audit": audit.get("fravalgRevisjon"), "account_type": account_type, "fiscal_year": end[:4]},
                key=[account_type, end],
                evidence_ids=[ev],
                confidence=0.99,
                method="official_register",
                reporting_period=reporting,
                label="Audit opt-out",
            )
        statements.append({"account_type": account_type, "start": start, "end": end, "currency": currency, "values": values, "small_company": (item.get("regnkapsprinsipper") or {}).get("smaaForetak")})
    statements.sort(key=lambda s: (s["account_type"] != "SELSKAP", s["end"]), reverse=False)
    rec.context["statements"] = statements
    if any(s["values"] for s in statements):
        rec.set_state("annual_accounts", "available", "normalised annual accounts found", checked_sources=[url])
    else:
        rec.set_state("annual_accounts", "not_available", "the accounts register returned no reported figures", checked_sources=[url])


# -- register update feed ----------------------------------------------------------------
def fetch_updates(fetcher: Fetcher, rec: CompanyRecord) -> None:
    org = rec.organisation_number
    url = f"{BASE}/oppdateringer/enheter?organisasjonsnummer={org}&dato=2018-01-01T00:00:00.000Z&size=100"
    try:
        snap = fetcher.get(url, retries=1, check_robots=False, headers={"Accept": "application/json"})
    except Exception as exc:
        rec.error("public_activity", f"register update feed failed: {exc}", url)
        return
    if not snap.ok:
        return
    body = snap.json()
    events = (body.get("_embedded") or {}).get("oppdaterteEnheter") or []
    events.sort(key=lambda e: e.get("dato") or "", reverse=True)
    for event in events[:5]:
        date = (event.get("dato") or "")[:10]
        if not date:
            continue
        kind = event.get("endringstype")
        span = f'oppdateringsid {event.get("oppdateringsid")}: {kind} {event.get("dato")}'
        ev = rec.evidence_from_snapshot(snap, source_class="official_registry_updates", span=span, method="brreg_update_feed", access_policy=POLICY)
        rec.add_claim(
            family="public_activity",
            field_name="registry_update",
            value={"event": f"Entity register record updated ({kind})", "date": date, "update_id": event.get("oppdateringsid")},
            key=event.get("oppdateringsid"),
            evidence_ids=[ev],
            confidence=0.99,
            method="official_register",
            effective_date=date,
            label="Register update",
        )
