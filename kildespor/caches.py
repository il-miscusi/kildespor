"""Declared discovery caches: candidate URLs and screening counts, never published facts.

Every file lives in ``kildespor/data/`` and is declared in ``CACHE.md`` (source,
release or query date, licence, row count and the build command under
``tools/``). The agent treats a cache row as a *candidate*: it fetches the URL
live and publishes it only after its own identity checks. Deleting any or all of
these files leaves the agent fully functional; every lookup then returns an
empty result.

Files (gzipped TSV, first line is the header):

* ``candidates-<source>.tsv.gz``: ``orgnr kind platform url match_rule source_record release_date licence``
* ``candidates-noindex.tsv.gz``: ``orgnr rank domain page_url rule domain_orgnrs retrieved_at`` (our own
  crawl: the page at ``page_url`` printed the organisation number as its owner's)
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
from urllib.parse import urlsplit

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


NOINDEX_COLUMNS = ("orgnr", "rank", "domain", "page_url", "rule", "domain_orgnrs", "retrieved_at")
REPO_LICENCE = "MIT"  # facts we extracted ourselves ship under the repository's licence


def _header(path: Path) -> list[str]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.readline().rstrip("\n").split("\t")
    except (OSError, EOFError, UnicodeDecodeError):
        return []


def site_root(page_url: str) -> str | None:
    """``scheme://host/`` of a page URL: lower-case scheme and host, default port dropped, IDN host kept as given."""
    try:
        parts = urlsplit(str(page_url or "").strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").rstrip(".")
    if scheme not in ("http", "https") or not host or "." not in host:
        return None
    if port and port != {"http": 80, "https": 443}[scheme]:
        host = f"{host}:{port}"
    return f"{scheme}://{host}/"


def _clean_url(url: str) -> str:
    """A page URL with lower-case scheme and host and no default port (``HTTPS://www.X.no:443`` -> ``https://www.x.no/``)."""
    root = site_root(url)
    if not root:
        return url
    parts = urlsplit(url.strip())
    rest = parts.path or "/"
    return root[:-1] + rest + (f"?{parts.query}" if parts.query else "")


def _noindex_rows(path: Path, header: list[str]) -> Iterator[tuple[str, tuple[int, int], tuple[Any, ...]]]:
    """Rows of the reverse organisation-number index as website candidates, with their sort key."""
    col = {name: header.index(name) for name in NOINDEX_COLUMNS if name in header}
    if len(col) < len(NOINDEX_COLUMNS):
        log.warning("cache file %s lacks columns %s", path, sorted(set(NOINDEX_COLUMNS) - set(col)))
        return
    width = max(col.values()) + 1
    for fields in _rows(path):
        if len(fields) < width:
            continue
        get = lambda name: fields[col[name]].strip()  # noqa: E731
        url = site_root(get("page_url"))
        if not url:
            continue
        rank = int(get("rank")) if get("rank").isdigit() else 9
        owners = int(get("domain_orgnrs")) if get("domain_orgnrs").isdigit() else 1
        rule = get("rule") or "unknown"
        retrieved = get("retrieved_at")
        extra = {"rank": rank, "domain": get("domain"), "domain_orgnrs": owners, "retrieved_at": retrieved or None}
        # A domain several companies print as theirs (a shared platform, a group site) and a number found only
        # in script or attribute text are the weakest leads: they go after every other candidate.
        demoted = 1 if owners > 1 or rule == "raw" else 0
        row = (url, "website", "", "noindex", rule, _clean_url(get("page_url")), retrieved[:10], REPO_LICENCE, extra)
        yield get("orgnr"), (demoted, rank), row


def _build_candidates() -> dict[str, list[tuple[Any, ...]]]:
    """orgnr -> candidate tuples ``(url, kind, platform, source, rule, record, date, licence, extra)``.

    Dataset files first (in file-name order), then the reverse organisation-number index, whose
    rows for a company follow its rank with shared-domain and ``raw`` rows last.
    """
    index: dict[str, list[tuple[Any, ...]]] = {}
    late: dict[str, list[tuple[tuple[int, int], tuple[Any, ...]]]] = {}
    paths = sorted(DATA_DIR.glob("candidates-*.tsv.gz"), key=lambda p: (p.name == "candidates-noindex.tsv.gz", p.name))
    for path in paths:
        source = path.name[len("candidates-"):-len(".tsv.gz")]
        header = _header(path)
        n = 0
        if "page_url" in header:
            for orgnr, key, row in _noindex_rows(path, header):
                late.setdefault(orgnr, []).append((key, row))
                n += 1
            _rows_loaded[path.name] = n
            continue
        for fields in _rows(path):
            if len(fields) < 6:
                continue
            orgnr, kind, platform, url, rule, record = fields[:6]
            date = fields[6] if len(fields) > 6 else ""
            licence = fields[7] if len(fields) > 7 else ""
            index.setdefault(orgnr, []).append((url, kind, platform, source, rule, record, date, licence, None))
            n += 1
        _rows_loaded[path.name] = n
    for orgnr, rows in late.items():
        index.setdefault(orgnr, []).extend(row for _, row in sorted(rows, key=lambda r: r[0]))
    return index


GENERIC = 10**6  # count recorded for a generic mailbox provider


def _build_counts(name: str) -> dict[str, int]:
    path = DATA_DIR / name
    values: dict[str, int] = {}
    for fields in _rows(path):
        if fields and fields[0]:
            count = fields[1] if len(fields) > 1 else ""
            values[fields[0]] = int(count) if count.isdigit() else GENERIC
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
    "source_record", "release_date", "licence"}``. ``platform`` is None for websites. ``source_record``
    is a citable record: ``https://www.wikidata.org/wiki/<QID>#<property>`` for Wikidata,
    ``overture:<release>:<place id>`` for Overture, the page that printed the organisation
    number for ``noindex``. ``release_date`` is the dataset's release or query date
    (YYYY-MM-DD; the crawl date for ``noindex``); ``licence`` the SPDX id(s) of the record's
    source dataset (``+``-joined when several). ``noindex`` rows are websites (``url`` is the
    printing page's ``scheme://host/``) and also carry ``rank``, ``domain``, ``domain_orgnrs``
    and ``retrieved_at``; they come after the other sources, shared-domain and ``raw`` rows
    last. Candidates only: verify each one live before publishing it as found on the web.
    """
    rows = _load("candidates", _build_candidates).get(_orgnr(orgnr), ())
    out = []
    for url, kind, platform, source, rule, record, date, licence, extra in rows:
        item = {"url": url, "kind": kind, "platform": platform or None, "source": source, "match_rule": rule,
                "source_record": record, "release_date": date or None, "licence": licence or None}
        if extra:
            item.update(extra)
        out.append(item)
    return out


def phone8(number: str) -> str | None:
    """The last eight digits of a phone number, the key the shared-phone screen uses."""
    digits = re.sub(r"\D", "", str(number or ""))
    return digits[-8:] if len(digits) >= 8 else None


def phone_holders(number: str) -> int:
    """How many distinct companies list this number (sub-units counted under their parent); 0 below five or unknown."""
    key = phone8(number)
    return _load("shared-phones", lambda: _build_counts("shared-phones.tsv.gz")).get(key, 0) if key else 0


def shared_phone(number: str) -> bool:
    """True when five or more register entities list this number (accountants, managers, group switchboards)."""
    return phone_holders(number) > 0


def _email_domain(value: str) -> str:
    value = str(value or "").strip().lower()
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    value = re.sub(r"^[a-z]+://", "", value).split("/", 1)[0].split(":", 1)[0]
    return value.strip(".").removeprefix("www.")


def email_domain_holders(domain: str) -> int:
    """How many distinct companies use this e-mail domain (``GENERIC`` for a mailbox provider); 0 below five or unknown.

    Accepts a domain or a whole address; a sub-domain inherits its parent domain's count.
    Note that a group's own domain counts its subsidiaries too (afgruppen.no is "shared").
    """
    host = _email_domain(domain)
    if not host or "." not in host:
        return 0
    shared = _load("shared-email-domains", lambda: _build_counts("shared-email-domains.tsv.gz"))
    if not shared:
        return 0
    labels = host.split(".")
    return max((shared.get(".".join(labels[i:]), 0) for i in range(len(labels) - 1)), default=0)


def shared_email_domain(domain: str) -> bool:
    """True for a generic mailbox provider or a domain five or more register entities use for e-mail.

    Accepts a domain or a whole address. A sub-domain of a shared domain is shared too.
    """
    return email_domain_holders(domain) > 0


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
