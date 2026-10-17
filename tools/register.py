"""Read the Brreg Enhetsregisteret bulk files (NLOD 2.0) into compact per-company records.

Bulk files (JSON arrays, gzipped, refreshed nightly):

* https://data.brreg.no/enhetsregisteret/api/enheter/lastned       (main entities)
* https://data.brreg.no/enhetsregisteret/api/underenheter/lastned  (sub-units, keyed to ``overordnetEnhet``)

Streams with ``ijson`` when it is installed (the main file is ~2 GB uncompressed),
otherwise loads the whole array.
"""
from __future__ import annotations

import gzip
import re
import shutil
import unicodedata
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator

ENHETER_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/lastned"
UNDERENHETER_URL = "https://data.brreg.no/enhetsregisteret/api/underenheter/lastned"
ACCEPT = {
    ENHETER_URL: "application/vnd.brreg.enhetsregisteret.enhet.v2+gzip;charset=UTF-8",
    UNDERENHETER_URL: "application/vnd.brreg.enhetsregisteret.underenhet.v2+gzip;charset=UTF-8",
}
UA = "kildespor-cache-builder/1.0 (+https://github.com/il-miscusi/kildespor)"

# Generic mailbox providers: an address there says nothing about the company's own domain.
GENERIC_MAIL = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.no", "hotmail.co.uk", "outlook.com", "outlook.no", "live.com", "live.no",
    "msn.com", "yahoo.com", "yahoo.no", "yahoo.co.uk", "ymail.com", "icloud.com", "me.com", "mac.com", "aol.com",
    "protonmail.com", "proton.me", "pm.me", "mail.com", "gmx.com", "gmx.net", "gmx.de", "email.com", "web.de", "zoho.com",
    "online.no", "frisurf.no", "start.no", "broadpark.no", "getmail.no", "c2i.net", "chello.no", "altibox.no", "lyse.net",
    "norpost.net", "telia.no", "netcom.no", "tele2.no", "ebnett.no", "enivest.net", "haugnett.no", "tdcadsl.no", "sensewave.com",
    "bluecom.no", "nextgentel.com", "ngt.no", "powertech.no", "kvinnherad.net", "loqal.no", "mail.no", "epost.no", "posten.no",
    "tussa.com", "nordlysnett.no", "eunet.no", "tiscali.no", "privat.no", "yandex.com", "yandex.ru", "mail.ru", "qq.com",
}


def download(url: str, path: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": ACCEPT.get(url, "*/*")})
    with urllib.request.urlopen(req, timeout=600) as resp, open(path, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)
    return path


def iter_entities(path: str) -> Iterator[dict[str, Any]]:
    try:
        import ijson  # type: ignore
    except ImportError:
        import json
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            yield from json.load(fh)
        return
    with gzip.open(path, "rb") as fh:
        yield from ijson.items(fh, "item")


def phone8(raw: Any) -> str | None:
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits[-8:] if len(digits) >= 8 else None


def email_of(raw: Any) -> str | None:
    value = str(raw or "").strip().lower().rstrip(".")
    return value if re.fullmatch(r"[^@\s]+@[a-z0-9.\-]+\.[a-z]{2,}", value) else None


def host_of(url: Any) -> str | None:
    value = str(url or "").strip().lower()
    if not value:
        return None
    value = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", value)
    host = re.split(r"[/?#:\s]", value, 1)[0].strip(".").removeprefix("www.")
    return host if re.fullmatch(r"[a-z0-9\-.æøå]+\.[a-zæøå]{2,}", host) else None


def registered_domain(host: str) -> str:
    parts = host.lower().strip(".").removeprefix("www.").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


LEGAL_WORDS = {"as", "asa", "ans", "da", "enk", "nuf", "sa", "ba", "ks", "sf", "iks", "kf", "fkf", "bbl", "aps", "ab", "ltd", "gmbh", "inc", "llc"}
CONNECTORS = {"og", "and", "i", "pa", "the", "of", "for", "av", "til", "med"}


def fold(text: str) -> str:
    text = str(text or "").lower().replace("æ", "ae").replace("ø", "o").replace("å", "a").replace("&", " og ")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def norm_name(name: Any) -> str:
    """Legal name or place name -> comparison key: folded, legal suffixes and connector words removed."""
    text = re.sub(r"\ba\s*/\s*s\b", " as ", fold(name))
    tokens = [t for t in re.split(r"[^a-z0-9]+", text) if t and t not in LEGAL_WORDS and t not in CONNECTORS]
    key = " ".join(tokens)
    return key if len(key.replace(" ", "")) >= 3 and not key.replace(" ", "").isdigit() else ""


@dataclass(slots=True)
class Unit:
    """One register entity (main entity or sub-unit) reduced to the fields the caches use."""
    orgnr: str
    parent: str  # the main entity this unit belongs to (itself for a main entity)
    name: str
    email: str | None
    phones: list[str] = field(default_factory=list)
    website: str | None = None
    postcodes: set[str] = field(default_factory=set)
    subunit: bool = False
    towns: set[str] = field(default_factory=set)  # folded post-town and municipality names of its addresses


def _unit(rec: dict[str, Any], subunit: bool) -> Unit:
    org = str(rec.get("organisasjonsnummer") or "")
    parent = str(rec.get("overordnetEnhet") or org) if subunit else org
    phones = [p for p in (phone8(rec.get("telefon")), phone8(rec.get("mobil"))) if p]
    postcodes = set()
    towns = set()
    for key in ("forretningsadresse", "postadresse", "beliggenhetsadresse"):
        addr = rec.get(key) or {}
        pc = str(addr.get("postnummer") or "").strip()
        if re.fullmatch(r"\d{4}", pc) and (addr.get("landkode") in (None, "", "NO")):
            postcodes.add(pc)
        for k in ("poststed", "kommune"):
            towns.update(t for t in re.split(r"[^a-z0-9]+", fold(addr.get(k) or "")) if t)
    return Unit(org, parent, str(rec.get("navn") or ""), email_of(rec.get("epostadresse")), phones,
                str(rec.get("hjemmeside") or "").strip() or None, postcodes, subunit, towns)


def load_units(enheter: str, underenheter: str | None) -> list[Unit]:
    units = [_unit(r, False) for r in iter_entities(enheter)]
    if underenheter:
        units += [_unit(r, True) for r in iter_entities(underenheter)]
    return units
