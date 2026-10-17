"""Declared discovery caches: candidate URLs and screening counts, never published facts.

Every file lives in ``kildespor/data/`` and is declared in ``CACHE.md`` (source,
release or query date, licence, row count and the build command under
``tools/``). The agent treats a cache row as a *candidate*: it fetches the URL
live and publishes it only after its own identity checks. Deleting any or all of
these files leaves the agent fully functional; every lookup then returns an
empty result.

Files (gzipped TSV, first line is the header):

* ``candidates-<source>.tsv.gz``: ``orgnr kind platform url match_rule source_record release_date``
* ``shared-phones.tsv.gz``: ``phone8 count`` (last eight digits, held by >= 5 register entities)
* ``shared-email-domains.tsv.gz``: ``domain count`` (held by >= 5 entities, or a generic mailbox provider)
* ``register-contacts.tsv.gz``: ``orgnr phones emails websites`` (``|``-separated; main entity plus its sub-units)
"""
from __future__ import annotations

import gzip
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("KILDESPOR_CACHE_DIR") or Path(__file__).with_name("data"))

_lock = threading.Lock()
_cache: dict[str, Any] = {}
_rows_loaded: dict[str, int] = {}


def reset(data_dir: str | os.PathLike[str] | None = None) -> None:
    """Forget everything loaded so far (tests, or switching to another data directory)."""
    global DATA_DIR
    with _lock:
        _cache.clear()
        _rows_loaded.clear()
        if data_dir is not None:
            DATA_DIR = Path(data_dir)


def _rows(path: Path) -> Iterator[list[str]]:
    """Yield the TSV fields of every data line; nothing when the file is missing or unreadable."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            fh.readline()  # header
            for line in fh:
                line = line.rstrip("\n")
                if line:
                    yield line.split("\t")
    except FileNotFoundError:
        return
    except (OSError, EOFError, UnicodeDecodeError) as exc:  # a damaged cache must never stop a run
        log.warning("cache file %s unreadable: %s", path, exc)
        return


def _load(key: str, builder: Any) -> Any:
    value = _cache.get(key)
    if value is None:
        with _lock:
            value = _cache.get(key)
            if value is None:
                value = builder()
                _cache[key] = value
    return value


def _build_candidates() -> dict[str, list[tuple[str, ...]]]:
    index: dict[str, list[tuple[str, ...]]] = {}
    for path in sorted(DATA_DIR.glob("candidates-*.tsv.gz")):
        source = path.name[len("candidates-"):-len(".tsv.gz")]
        n = 0
        for fields in _rows(path):
            if len(fields) < 6:
                continue
            orgnr, kind, platform, url, rule, record = fields[:6]
            date = fields[6] if len(fields) > 6 else ""
            index.setdefault(orgnr, []).append((url, kind, platform, source, rule, record, date))
            n += 1
        _rows_loaded[path.name] = n
    return index


def _build_set(name: str) -> frozenset[str]:
    path = DATA_DIR / name
    values = frozenset(fields[0] for fields in _rows(path) if fields and fields[0])
    if path.exists():
        _rows_loaded[name] = len(values)
    return values


def _build_contacts() -> dict[str, tuple[str, str, str]]:
    name = "register-contacts.tsv.gz"
    index: dict[str, tuple[str, str, str]] = {}
    for fields in _rows(DATA_DIR / name):
        fields += [""] * (4 - len(fields))
        index[fields[0]] = (fields[1], fields[2], fields[3])
    if index:
        _rows_loaded[name] = len(index)
    return index


def _orgnr(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def candidates(orgnr: str) -> list[dict[str, Any]]:
    """Candidate website and social URLs for one company, from every cache present.

    Each item: ``{"url", "kind": "website"|"social", "platform", "source", "match_rule",
    "source_record", "release_date"}``. ``platform`` is None for websites. ``source_record``
    is a citable record: ``https://www.wikidata.org/wiki/<QID>#<property>`` for Wikidata,
    ``overture:<release>:<place id>`` for Overture. ``release_date`` is the dataset's
    release or query date (YYYY-MM-DD). Candidates only: verify each one live before
    publishing it as found on the web.
    """
    rows = _load("candidates", _build_candidates).get(_orgnr(orgnr), ())
    return [
        {"url": url, "kind": kind, "platform": platform or None, "source": source, "match_rule": rule,
         "source_record": record, "release_date": date or None}
        for url, kind, platform, source, rule, record, date in rows
    ]


def phone8(number: str) -> str | None:
    """The last eight digits of a phone number, the key the shared-phone screen uses."""
    digits = re.sub(r"\D", "", str(number or ""))
    return digits[-8:] if len(digits) >= 8 else None


def shared_phone(number: str) -> bool:
    """True when five or more register entities list this number (accountants, managers, group switchboards)."""
    key = phone8(number)
    return bool(key) and key in _load("shared-phones", lambda: _build_set("shared-phones.tsv.gz"))


def _email_domain(value: str) -> str:
    value = str(value or "").strip().lower()
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    value = re.sub(r"^[a-z]+://", "", value).split("/", 1)[0].split(":", 1)[0]
    return value.strip(".").removeprefix("www.")


def shared_email_domain(domain: str) -> bool:
    """True for a generic mailbox provider or a domain five or more register entities use for e-mail.

    Accepts a domain or a whole address. A sub-domain of a shared domain is shared too.
    """
    host = _email_domain(domain)
    if not host or "." not in host:
        return False
    shared = _load("shared-email-domains", lambda: _build_set("shared-email-domains.tsv.gz"))
    if not shared:
        return False
    labels = host.split(".")
    return any(".".join(labels[i:]) in shared for i in range(len(labels) - 1))


def register_contacts(orgnr: str) -> dict[str, list[str]]:
    """Phones, e-mails and sub-unit websites the bulk register lists for a company and its sub-units."""
    row = _load("register-contacts", _build_contacts).get(_orgnr(orgnr))
    if not row:
        return {"phones": [], "emails": [], "websites": []}
    phones, emails, websites = row
    split = lambda s: [x for x in s.split("|") if x]  # noqa: E731
    return {"phones": split(phones), "emails": split(emails), "websites": split(websites)}


def describe() -> dict[str, int]:
    """Row counts of every cache file loaded so far (for run reports)."""
    return dict(_rows_loaded)


def available() -> list[str]:
    """Names of the cache files present on disk."""
    return sorted(p.name for p in DATA_DIR.glob("*.tsv.gz")) if DATA_DIR.is_dir() else []
