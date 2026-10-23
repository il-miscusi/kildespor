"""Mine Common Crawl page text for labelled Norwegian organisation numbers (orgnr -> candidate domains).

    python3 tools/cc_orgnr.py --out OUT_DIR [--releases CC-MAIN-2026-43,...] [--lang-releases ...]
                              [--live-pairs pairs_all.jsonl.gz] [--universe universe.jsonl.gz]

Built to run on a Kaggle CPU or TPU-host session (the campaign kernel ``sp-disc-cc*`` embeds this file);
it reads a few GB of Common Crawl's public index and WARC files over HTTPS and is not meant for a laptop.
Nothing here is used at run time: the agent reads only the small TSV that ``tools/build_noindex.py`` trims
from this script's output, and verifies every row live before it publishes anything.

Stages
------
select   Read the columnar URL index (``cc-index/table/cc-main/warc``) of each release, row groups chosen
         by their ``url_host_tld`` statistics. Keep HTTP 200 HTML captures on ``.no`` hosts, and captures whose
         first detected language is Norwegian (``nob``/``nno``/``nor``) on ``.com .net .org .as .io .eu .biz
         .info`` hosts. Per host keep the newest homepage capture, up to three legal pages (paths matching
         kontakt / om oss / personvern / vilkår / salgsbetingelser / about / contact / privacy / impressum /
         terms, newest capture of each, best class first) and, for a host with neither, its shortest page.
fetch    Range-GET only those WARC records from ``data.commoncrawl.org`` (``Range: bytes=offset-end``).
         Stage A: one page per host (homepage, else best legal page, else the fallback). Stage B: the other
         legal pages, only for hosts whose stage-A page printed no owner number.
extract  The rules of our own live ``.no`` crawl (17 Oct 2026): a mod-11-valid number within 40 characters
         after an ownership label (org.nr, organisasjonsnummer, foretaksregisteret, MVA, VAT, company/business
         registration number) or ``NO 999 999 999 MVA``; the page's owner is the single labelled number, else
         the one inside ``<footer>``, else the one beside ©, else a single JSON-LD vatID/taxID, else a single
         labelled number found only in markup (``raw``). The 100 characters before the label are kept so the
         index can tell when the number is printed under another register entity's name.
index    Merge with the live crawl's pairs (``pairs_all.jsonl.gz``), drop domains printed as their own by more
         than five organisations, rank each organisation's domains and write ``noindex.tsv.gz``.

Common Crawl data is used under its terms of use (https://commoncrawl.org/terms-of-use). Outputs keep facts
(a number, the domain and page that printed it, a <=160 character snippet), never page text.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import concurrent.futures
import glob
import gzip
import html as htmlmod
import io
import json
import os
import random
import re
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone

T0 = time.time()
UA = "KildesporBot/0.3 (+https://github.com/il-miscusi/kildespor) cc-orgnr"
CC = "https://data.commoncrawl.org/"
UNIVERSE_URL = "https://builderr.ai/signalpost-company-universe-2025.jsonl.gz"
LANG_TLDS = ("com", "net", "org", "as", "io", "eu", "biz", "info")
NOR_LANG = re.compile(r"^(?:nob|nno|nor)\b")
MAX_BYTES = 2_000_000


def mp_context():
    """Worker processes never inherit threads: pyarrow's and aiohttp's pools make a plain fork unsafe."""
    import multiprocessing

    return multiprocessing.get_context("forkserver" if sys.platform.startswith("linux") else "spawn")


def log(*a):
    print(f"[{time.time() - T0:7.0f}s]", *a, flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


try:
    import tldextract  # noqa: E402

    _TLD = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)
except Exception:  # pragma: no cover - local smoke without tldextract
    _TLD = None


def regdom(host):
    host = (host or "").lower().strip().strip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    if _TLD is None:
        return ".".join(host.split(".")[-2:])
    e = _TLD(host)
    if not e.suffix or not e.domain:
        return host
    return f"{e.domain}.{e.suffix}"


# ---------------------------------------------------------------- extraction (the live crawl's rules)
NUM = r"(?:NO\s?)?([89]\d{2})[\s.  -]?(\d{3})[\s.  -]?(\d{3})"
NUM_RE = re.compile(r"(?<!\d)" + NUM + r"(?!\d)")
MVA_RE = re.compile(r"(?<!\d)" + NUM + r"\s?MVA\b", re.I)
LABEL_RE = re.compile(
    r"(?i)(?<![a-zæøå])(?:"
    r"org(?:anisasjons|anization|anisation)?\s*\.?\s*-?\s*(?:nr|nummer|number|no)\b\.?"
    r"|orgnr|foretaksregisteret|foretaksnr|f[oø]retaksnummer"
    r"|mva[\s.-]*(?:nr|nummer|no)\b|vat(?:\s*(?:no|nr|number|id|reg))?\b"
    r"|business\s+reg(?:istration)?\.?\s*(?:no|nr|number)\b"
    r"|company\s+(?:reg(?:istration)?\.?\s*)?(?:no|nr|number)\b)")
LD_RE = re.compile(r'(?i)"(?:vatID|taxID|organizationNumber|organisationNumber|orgNumber|orgnr)"\s*:\s*"([^"]{9,30})"')
COPY_RE = re.compile(r"(?i)©|\bcopyright\b")
FOOTER_RE = re.compile(r"(?is)<footer\b.*?</footer\s*>")
DROP_RE = re.compile(r"(?is)<(script|style|template|svg|noscript)\b.*?</\1\s*>")
COMMENT_RE = re.compile(r"(?s)<!--.*?-->")
TAG_RE = re.compile(r"(?s)<[^>]*>")
TITLE_RE = re.compile(r"(?is)<title\b[^>]*>(.*?)</title\s*>")
WS_RE = re.compile(r"[\s   ​]+")
ZW_RE = re.compile("[​-‍⁠﻿­]")
W11 = (3, 2, 7, 6, 5, 4, 3, 2)


def mod11(s):
    r = 11 - sum(int(a) * b for a, b in zip(s[:8], W11)) % 11
    if r == 11:
        r = 0
    return r != 10 and r == int(s[8])


def visible(h):
    h = COMMENT_RE.sub(" ", h)
    h = DROP_RE.sub(" ", h)
    t = htmlmod.unescape(TAG_RE.sub(" ", h))
    return WS_RE.sub(" ", ZW_RE.sub("", t))


def labelled(text):
    """(number, start, end, label) for every mod-11-valid number printed right after an ownership label."""
    out = []
    for m in LABEL_RE.finditer(text):
        seg = text[m.end(): m.end() + 40]
        for n in NUM_RE.finditer(seg):
            c = "".join(n.groups())
            if mod11(c):
                out.append((c, m.start(), m.end() + n.end(), m.group(0)))
                break
    for n in MVA_RE.finditer(text):
        c = "".join(n.groups())
        if mod11(c):
            out.append((c, n.start(), n.end(), "MVA"))
    return out


def _snip(text, a, b):
    return text[max(0, a - 30): b + 50].strip()[:160]


def extract(body, url):
    """Owner decision for one page: a small picklable dict, no page text beyond a snippet."""
    if len(body) > MAX_BYTES:
        body = body[:MAX_BYTES]
    vis = visible(body)
    lab = labelled(vis)
    L = sorted({c for c, _, _, _ in lab})
    owner = rule = snip = label = None
    pre = ""
    if len(L) == 1:
        owner, rule = L[0], "single"
    elif len(L) > 1:
        fm = None
        for fm in FOOTER_RE.finditer(body):
            pass
        if fm is not None:
            F = {c for c, _, _, _ in labelled(visible(fm.group(0)))}
            if len(F) == 1:
                owner, rule = next(iter(F)), "footer"
        if owner is None:
            C = set()
            for cm in COPY_RE.finditer(vis):
                for c, a, b, _ in lab:
                    if abs(a - cm.start()) < 150 or abs(b - cm.start()) < 150:
                        C.add(c)
            if len(C) == 1:
                owner, rule = next(iter(C)), "copyright"
    if owner is not None:
        for c, a, b, lb in lab:
            if c == owner:
                snip, label, pre = _snip(vis, a, b), lb, vis[max(0, a - 100): a]
                break
    if not L:
        ld = set()
        for m in LD_RE.finditer(body):
            d = re.sub(r"\D", "", m.group(1))
            if len(d) == 9 and d[0] in "89" and mod11(d):
                ld.add(d)
                snip = snip or m.group(0)[:160]
        if len(ld) == 1:
            owner, rule, L, label = next(iter(ld)), "ld", sorted(ld), "json-ld"
        else:
            snip = None
            raw = WS_RE.sub(" ", ZW_RE.sub("", htmlmod.unescape(TAG_RE.sub(" ", COMMENT_RE.sub(" ", body)))))
            rl = labelled(raw)
            R = sorted({c for c, _, _, _ in rl})
            if len(R) == 1:
                owner, rule, L, label = R[0], "raw", R, rl[0][3]
                snip, pre = _snip(raw, rl[0][1], rl[0][2]), raw[max(0, rl[0][1] - 100): rl[0][1]]
            elif R:
                L = R
    nv = len({"".join(n.groups()) for n in NUM_RE.finditer(vis) if mod11("".join(n.groups()))})
    tm = TITLE_RE.search(body[:200_000])
    title = WS_RE.sub(" ", htmlmod.unescape(TAG_RE.sub(" ", tm.group(1)))).strip()[:120] if tm else ""
    return {"u": url, "L": L[:8], "o": owner, "r": rule, "sn": snip, "lab": (label or "")[:30], "pre": pre[-100:],
            "nv": nv, "ti": title}


# ---------------------------------------------------------------- WARC records
CHARSET_RE = re.compile(rb"""(?i)<meta[^>]+charset\s*=\s*["']?([\w-]+)""")
HDR_CHARSET = re.compile(r"(?i)charset\s*=\s*[\"']?([\w-]+)")


WARC_DATE = re.compile(rb"(?im)^WARC-Date:\s*(\S+)")


def warc_body(raw):
    """(http_status, content_type, body bytes, WARC-Date) of one gzipped WARC response record."""
    data = zlib.decompressobj(31).decompress(raw)
    i = data.find(b"\r\n\r\n")
    if i < 0:
        return None, "", b"", ""
    wd = WARC_DATE.search(data[:i])
    wdate = wd.group(1).decode("ascii", "ignore")[:20] if wd else ""
    j = data.find(b"\r\n\r\n", i + 4)
    if j < 0:
        return None, "", b"", wdate
    head = data[i + 4: j].decode("latin-1", "replace").split("\r\n")
    status = None
    m = re.match(r"HTTP/\S+\s+(\d{3})", head[0] if head else "")
    if m:
        status = int(m.group(1))
    ctype = ""
    for h in head[1:]:
        if h.lower().startswith("content-type:"):
            ctype = h.split(":", 1)[1].strip()
    return status, ctype, data[j + 4:], wdate


def decode(body, ctype, hint):
    enc = None
    m = HDR_CHARSET.search(ctype or "")
    if m:
        enc = m.group(1)
    if not enc:
        m = CHARSET_RE.search(body[:4096])
        enc = m.group(1).decode("ascii", "ignore") if m else (hint or "utf-8")
    try:
        return body.decode(enc, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def work(raw, url, hint):
    """Process-pool task: one WARC record -> extraction dict (or an error marker)."""
    try:
        status, ctype, body, wdate = warc_body(raw)
    except Exception as e:  # noqa: BLE001
        return {"u": url, "err": "gunzip:" + type(e).__name__}
    if status != 200 or not body:
        return {"u": url, "err": f"http{status}"}
    try:
        out = extract(decode(body[:MAX_BYTES], ctype, hint), url)
    except Exception as e:  # noqa: BLE001
        return {"u": url, "err": "extract:" + type(e).__name__}
    out["wd"] = wdate
    return out


# ---------------------------------------------------------------- HTTP helpers (index reads)
_local = threading.local()


def http_get(url, rng=None, tries=7, timeout=120):
    """GET with a Range header; backs off on 503/429/5xx and network errors (Common Crawl asks for it)."""
    err = None
    for i in range(tries):
        try:
            hdr = {"User-Agent": UA}
            if rng:
                hdr["Range"] = rng
            r = urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=timeout)
            return r.read(), r.headers.get("Content-Range")
        except urllib.error.HTTPError as e:
            err = e
            if e.code not in (429, 500, 502, 503, 504):
                raise
        except Exception as e:  # noqa: BLE001
            err = e
        time.sleep(min(60, 2 ** i) + random.random())
    raise IOError(f"{url}: {err}")


class _Tail(io.RawIOBase):
    def __init__(self, size, start, data):
        self.size, self.start, self.data, self.pos = size, start, data, 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def seek(self, o, w=0):
        self.pos = o if w == 0 else self.pos + o if w == 1 else self.size + o
        return self.pos

    def tell(self):
        return self.pos

    def readinto(self, b):
        if self.pos < self.start:
            raise IOError("read before the footer")
        d = self.data[self.pos - self.start: self.pos - self.start + len(b)]
        b[:len(d)] = d
        self.pos += len(d)
        return len(d)


class _Http(io.RawIOBase):
    """A seekable read-only file over HTTP range requests (each read is one GET)."""

    def __init__(self, url, size):
        self.url, self.size, self.pos = url, size, 0
        self.bytes = 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def seek(self, o, w=0):
        self.pos = o if w == 0 else self.pos + o if w == 1 else self.size + o
        return self.pos

    def tell(self):
        return self.pos

    def readinto(self, b):
        n = min(len(b), self.size - self.pos)
        if n <= 0:
            return 0
        d, _ = http_get(self.url, f"bytes={self.pos}-{self.pos + n - 1}")
        b[:len(d)] = d
        self.pos += len(d)
        self.bytes += len(d)
        return len(d)


def footer(path):
    import pyarrow.parquet as pq

    url = CC + path
    data, cr = http_get(url, "bytes=-262144")
    size = int(cr.rsplit("/", 1)[1])
    flen = struct.unpack("<I", data[-8:-4])[0]
    if flen + 8 > len(data):
        data, _ = http_get(url, f"bytes=-{flen + 8}")
    md = pq.read_metadata(_Tail(size, size - len(data), data))
    groups = []
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        cols = {rg.column(i).path_in_schema: rg.column(i) for i in range(rg.num_columns)}
        st = cols["url_host_tld"].statistics
        lo, hi = (st.min, st.max) if st is not None and st.has_min_max else ("", "￿")
        groups.append((g, rg.num_rows, lo, hi))
    return {"path": path, "size": size, "groups": groups}


# ---------------------------------------------------------------- selection
HOME_RE = r"^/(?:(?:index|default|home|forside|hjem|start)\.(?:html?|php|aspx?|jsp))?$"
LANGROOT_RE = r"^/(?:no|nb|nn|nb-no|no-nb|nn-no|nb_no|no_no|norsk|norge|norway)/?$"
LEGAL_CLASSES = [
    r"(?i)kontakt|contact",
    r"(?i)om-?oss|om_oss|omoss|om-os\b|about|hvem-er-vi|selskapet",
    r"(?i)personvern|privacy|gdpr|cookie|informasjonskapsler",
    r"(?i)vilk[aå]r|vilkar|betingelser|salgsbet|kj[oø]psbet|kjopsbet|terms|impressum|juridisk|legal",
]
# A legal page is a short path (at most four segments) whose last segment is the keyword, optionally with a
# prefix word and up to four more words ("/kontakt-oss/", "/om/personvern", "/salgsbetingelser.html"); never a
# blog tag, category or article that only mentions the word.
LEGAL_ANY = (r"(?i)^/(?:[^/]+/){0,3}(?:[a-z0-9æøå]+[-_])?(?:kontakt|contact|om-?oss|om_oss|omoss|about|hvem-er-vi|selskapet|"
             r"personvern|privacy|gdpr|cookies?|informasjonskapsler|vilk[aå]r|vilkar|[a-zæøå]*betingelser|terms|impressum|"
             r"juridisk|legal)(?:[-_][a-z0-9æøå]+){0,4}(?:\.[a-z0-9]{2,5})?/?$")
LEGAL_NOT = r"(?i)/(?:tag|tags|category|categories|kategori|blog|blogg|author|forfatter|nyheter|news|artikler|produkt|product|products|shop|butikk)/"
SKIP_EXT_RE = r"(?i)\.(?:pdf|jpe?g|png|gif|webp|svg|zip|docx?|xlsx?|mp4|mp3|xml|json|txt|css|js)$"
BASE_COLS = ["url_host_tld", "url_host_name", "url_protocol", "url_port", "url_path", "url_query", "fetch_status",
             "content_mime_detected", "content_charset", "content_languages", "warc_filename", "warc_record_offset",
             "warc_record_length", "fetch_time"]
CHEAP_COLS = ["url_host_tld", "content_languages", "fetch_status", "content_mime_detected"]
KIND_HOME, KIND_LEGAL, KIND_OTHER = 0, 1, 2


def _read_group(job):
    """Rows worth fetching from one index row group, already reduced per host.

    Returns {host: {"home": row, "legal": {path: row}, "fb": row}} where a row is the tuple
    (crawl, fetch_time_iso, url, warc_filename, offset, length, charset, kind, prio, lang)."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    crawl, path, size, g, mode = job
    f = _Http(CC + path, size)
    pf = pq.ParquetFile(f)
    if mode == "no":
        t = pf.read_row_group(g, columns=BASE_COLS)
        m = pc.equal(t["url_host_tld"], "no")
    else:
        cheap = pf.read_row_group(g, columns=CHEAP_COLS)
        m = pc.and_(pc.is_in(cheap["url_host_tld"], pa.array(LANG_TLDS)),
                    pc.fill_null(pc.match_substring_regex(cheap["content_languages"], NOR_LANG.pattern), False))
        if not pc.any(m).as_py():
            return crawl, path, g, mode, {}, f.bytes, 0
        t = pf.read_row_group(g, columns=[c for c in BASE_COLS if c != "fetch_time"])
        t = t.append_column("fetch_time", pa.nulls(t.num_rows, pa.timestamp("ms", tz="UTC")))
    m = pc.and_(m, pc.equal(t["fetch_status"], 200))
    m = pc.and_(m, pc.is_in(t["content_mime_detected"], pa.array(["text/html", "application/xhtml+xml"])))
    m = pc.and_(m, pc.invert(pc.fill_null(pc.match_substring_regex(t["url_path"], SKIP_EXT_RE), False)))
    t = t.filter(m)
    n = t.num_rows
    if not n:
        return crawl, path, g, mode, {}, f.bytes, 0
    p = t["url_path"]
    q = pc.fill_null(t["url_query"], "")
    noq = pc.equal(q, "")
    home = pc.and_(noq, pc.match_substring_regex(p, HOME_RE))
    if mode != "no":
        home = pc.or_(home, pc.and_(noq, pc.match_substring_regex(p, LANGROOT_RE)))
    legal = pc.and_(pc.less_equal(pc.utf8_length(q), 40),
                    pc.and_(pc.match_substring_regex(p, LEGAL_ANY),
                            pc.invert(pc.match_substring_regex(p, LEGAL_NOT))))
    cols = {c: t[c].to_pylist() for c in ("url_host_name", "url_protocol", "url_port", "url_path", "url_query",
                                          "content_charset", "warc_filename", "warc_record_offset",
                                          "warc_record_length", "content_languages")}
    ft = [x.strftime("%Y-%m-%dT%H:%M:%SZ") if x is not None else crawl for x in t["fetch_time"].to_pylist()]
    home = home.to_pylist()
    legal = legal.to_pylist()
    hosts = {}
    for i in range(n):
        h = (cols["url_host_name"][i] or "").lower()
        if not h:
            continue
        key = h[4:] if h.startswith("www.") else h
        pth = cols["url_path"][i] or "/"
        if home[i]:
            kind, prio = KIND_HOME, 0 if not re.match(LANGROOT_RE, pth) else 1
        elif legal[i]:
            kind = KIND_LEGAL
            prio = next((k for k, c in enumerate(LEGAL_CLASSES) if re.search(c, pth)), len(LEGAL_CLASSES))
        else:
            kind, prio = KIND_OTHER, len(pth) + (50 if cols["url_query"][i] else 0)
        port = cols["url_port"][i]
        qs = cols["url_query"][i]
        url = f"{cols['url_protocol'][i]}://{h}{':' + str(port) if port else ''}{pth}{'?' + qs if qs else ''}"
        row = (crawl, ft[i], url, cols["warc_filename"][i], cols["warc_record_offset"][i], cols["warc_record_length"][i],
               cols["content_charset"][i] or "", kind, prio, (cols["content_languages"][i] or "")[:12])
        hs = hosts.setdefault(key, {"home": None, "legal": {}, "fb": None, "n": 0})
        hs["n"] += 1
        if kind == KIND_HOME:
            if _better(row, hs["home"]):
                hs["home"] = row
        elif kind == KIND_LEGAL:
            cur = hs["legal"].get(pth)
            if cur is None or row[1] > cur[1]:
                hs["legal"][pth] = row
        elif _better(row, hs["fb"]):
            hs["fb"] = row
    return crawl, path, g, mode, hosts, f.bytes, n


def _better(b, a):
    """Capture b beats a: a better page class (lower prio), else a newer capture."""
    return a is None or b[8] < a[8] or (b[8] == a[8] and b[1] > a[1])


def merge_host(dst, src):
    """Combine one host's captures across row groups and releases: best homepage, newest copy of each legal path."""
    dst["n"] = dst.get("n", 0) + src["n"]
    for key in ("home", "fb"):
        if src[key] is not None and _better(src[key], dst.get(key)):
            dst[key] = src[key]
    leg = dst.setdefault("legal", {})
    for pth, row in src["legal"].items():
        cur = leg.get(pth)
        if cur is None or row[1] > cur[1]:
            leg[pth] = row


def select(releases, lang_releases, workers, max_groups=0, part_filter=""):
    """Read the index of every release and return {host: merged captures}, plus stats."""
    stats = {"releases": {}, "index_bytes": 0}
    jobs = []
    with concurrent.futures.ThreadPoolExecutor(32) as ex:
        for crawl in sorted(set(releases) | set(lang_releases), reverse=True):
            paths_gz, _ = http_get(CC + f"crawl-data/{crawl}/cc-index-table.paths.gz")
            paths = [p for p in gzip.decompress(paths_gz).decode().split() if "/subset=warc/" in p]
            if part_filter:
                paths = [p for p in paths if re.search(part_filter, p)]
            feet = list(ex.map(footer, paths))
            nj = {"no": 0, "lang": 0}
            for ft in feet:
                for g, rows, lo, hi in ft["groups"]:
                    if crawl in releases and lo <= "no" <= hi:
                        jobs.append((crawl, ft["path"], ft["size"], g, "no"))
                        nj["no"] += 1
                    if crawl in lang_releases and any(lo <= t <= hi for t in LANG_TLDS):
                        jobs.append((crawl, ft["path"], ft["size"], g, "lang"))
                        nj["lang"] += 1
            stats["releases"][crawl] = {"parts": len(paths), "rowgroups": nj, "rows": sum(r for ft in feet for _, r, _, _ in ft["groups"])}
            log("index", crawl, json.dumps(stats["releases"][crawl]))
    # .no row groups first (small, valuable), then the language scan
    jobs.sort(key=lambda j: (j[4] != "no", j[0], j[1], j[3]))
    if max_groups:
        jobs = [j for j in jobs if j[4] == "no"][:max_groups] + [j for j in jobs if j[4] == "lang"][:max_groups]
    hosts = {}
    done = 0
    sel_rows = collections.Counter()
    with concurrent.futures.ProcessPoolExecutor(workers, mp_context=mp_context()) as ex:
        for crawl, path, g, mode, hs, nbytes, n in ex.map(_read_group, jobs, chunksize=1):
            done += 1
            stats["index_bytes"] += nbytes
            sel_rows[f"{crawl}:{mode}"] += n
            for h, src in hs.items():
                if h in hosts:
                    merge_host(hosts[h], src)
                else:
                    hosts[h] = src
            if done % 50 == 0 or done == len(jobs):
                log(f"select {done}/{len(jobs)} rowgroups, {stats['index_bytes'] / 1e6:.0f} MB read, hosts {len(hosts)}")
    stats["selected_rows"] = dict(sel_rows)
    stats["hosts"] = len(hosts)
    return hosts, stats


def plan(hosts, per_domain=25, legal_max=3):
    """Stage-A and stage-B fetch lists. Hosts of one registered domain are capped (tenant platforms)."""
    by_dom = collections.defaultdict(list)
    for h, hs in hosts.items():
        by_dom[regdom(h)].append(h)
    keep = set()
    for d, hs in by_dom.items():
        hs.sort(key=lambda h: (hosts[h]["home"] is None, -len(hosts[h]["legal"]), -hosts[h]["n"], h))
        keep.update(hs[:per_domain])
    stage_a, stage_b = [], {}
    for h in sorted(keep):
        hs = hosts[h]
        legal = sorted(hs["legal"].values(), key=lambda r: (r[8], len(r[2]), r[2]))
        # one page per class first, then the rest
        seen, ordered = set(), []
        for r in legal:
            if r[8] not in seen:
                seen.add(r[8])
                ordered.append(r)
        ordered += [r for r in legal if r not in ordered]
        ordered = ordered[:legal_max]
        first = hs["home"] or (ordered[0] if ordered else None) or hs["fb"]
        if first is None:
            continue
        stage_a.append((h, first))
        rest = [r for r in ordered if r is not first]
        if rest:
            stage_b[h] = rest
    return stage_a, stage_b, {"hosts_kept": len(keep), "domains": len(by_dom), "stage_a": len(stage_a),
                              "stage_b_hosts": len(stage_b), "stage_b_pages": sum(len(v) for v in stage_b.values())}


# ---------------------------------------------------------------- fetching
async def fetch_all(items, pool, conc, out, deadline):
    """items: [(host, row)]. Range-GET each WARC record, extract in the process pool, append JSON lines to out."""
    import aiohttp

    loop = asyncio.get_running_loop()
    conn = aiohttp.TCPConnector(limit=conc, limit_per_host=conc, ttl_dns_cache=3600)
    timeout = aiohttp.ClientTimeout(total=120, sock_connect=20, sock_read=60)
    st = collections.Counter()
    it = iter(items)
    found = {}
    slow = {"until": 0.0}

    async with aiohttp.ClientSession(connector=conn, timeout=timeout, headers={"User-Agent": UA}) as s:
        async def one(host, row):
            crawl, ft, url, fn, off, ln, cs, kind, prio, lang = row
            rng = f"bytes={off}-{off + ln - 1}"
            for attempt in range(7):
                wait = slow["until"] - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    async with s.get(CC + fn, headers={"Range": rng}) as r:
                        if r.status in (429, 500, 502, 503, 504):
                            st[f"http{r.status}"] += 1
                            back = min(60, 2 ** attempt) + random.random()
                            slow["until"] = max(slow["until"], time.time() + back / 4)
                            await asyncio.sleep(back)
                            continue
                        if r.status not in (200, 206):
                            st[f"http{r.status}"] += 1
                            return {"u": url, "err": f"cc{r.status}"}
                        raw = await r.read()
                        st["bytes"] += len(raw)
                        break
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    st["neterr:" + type(e).__name__] += 1
                    await asyncio.sleep(min(60, 2 ** attempt) + random.random())
            else:
                return {"u": url, "err": "cc_giveup"}
            ex = await loop.run_in_executor(pool, work, raw, url, cs)
            ex.update({"h": host, "c": crawl, "t": ft, "k": kind, "p": prio, "lg": lang})
            return ex

        async def worker():
            while time.time() < deadline:
                try:
                    host, row = next(it)
                except StopIteration:
                    return
                try:
                    rec = await one(host, row)
                except Exception as e:  # noqa: BLE001
                    rec = {"u": row[2], "h": host, "err": "exc:" + type(e).__name__}
                st["records"] += 1
                if rec.get("err"):
                    st["err:" + rec["err"]] += 1
                elif rec.get("o"):
                    st["owner"] += 1
                    found[host] = True
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")

        async def progress():
            while True:
                await asyncio.sleep(120)
                log("fetch", dict(st.most_common(12)))

        prog = asyncio.create_task(progress())
        await asyncio.gather(*[worker() for _ in range(conc)])
        prog.cancel()
    return dict(st), found


# ---------------------------------------------------------------- index
LEGAL_FORMS = r"(?:AS|ASA|ANS|DA|SA|NUF|ENK|BA|BBL|IKS|KF|HF|KS|SF)"
ENTITY_BEFORE = re.compile(r"((?:[^\W\d_][\w&.'’\-]*\s+){1,6}?)" + LEGAL_FORMS + r"\b[\s,.:;·|\-–()]*$", re.U)
FORM_WORDS = {"as", "asa", "ans", "da", "sa", "nuf", "enk", "ba", "bbl", "iks", "kf", "hf", "ks", "sf"}
STOP = {"og", "and", "the", "©", "copyright", "c"}


def _fold(s):
    s = (s or "").lower().replace("æ", "ae").replace("ø", "o").replace("å", "a").replace("ä", "a").replace("ö", "o")
    return s.replace("ü", "u").replace("é", "e")


def core(name):
    toks = re.findall(r"[a-z0-9]+", _fold(name).replace("&", " og "))
    while toks and toks[-1] in FORM_WORDS:
        toks.pop()
    return [t for t in toks if t not in STOP]


def named_other(pre, legal_name):
    """The legal entity printed right before the label when it is not the number's own register name, else ''."""
    m = ENTITY_BEFORE.search((pre or "")[-90:].rstrip())
    if not m:
        return ""
    words = [w for w in core(m.group(1) + " AS") if not w.isdigit()]
    ours = core(legal_name)
    if not words or not ours:
        return ""
    if words[-len(ours):] == ours or ours[-len(words):] == words:
        return ""
    return " ".join(words[-5:])[:60]


GENERIC = {"as", "asa", "ans", "da", "enk", "sa", "ba", "nuf", "ks", "iks", "sf", "the", "og", "and", "norge",
           "norway", "holding", "invest", "eiendom", "group", "gruppen"}


def name_score(name, domain):
    label = domain.split(".")[0].replace("-", "")
    best = 0.0
    for v in (_fold(name), (name or "").lower().replace("æ", "ae").replace("ø", "oe").replace("å", "aa")):
        toks = [t for t in re.findall(r"[a-z0-9]+", v) if len(t) >= 3 and t not in GENERIC]
        if not toks:
            continue
        hit = sum(1 for t in toks if t in label) / len(toks)
        if label and len(label) >= 4 and label in "".join(toks):
            hit = max(hit, 0.8)
        best = max(best, hit)
    return round(best, 3)


def _names_site(name, p):
    """The domain spells most of the legal name, or the page title holds every word of it."""
    if name_score(name, p["domain"]) >= 0.5:
        return True
    words = [t for t in core(name) if t]
    title = " " + " ".join(re.findall(r"[a-z0-9]+", _fold(p.get("title") or ""))) + " "
    return bool(words) and f" {' '.join(words)} " in title


RULE_W = {"single": 3, "footer": 3, "copyright": 2, "ld": 2, "raw": 1}
BLOCK = {"facebook.com", "instagram.com", "linkedin.com", "proff.no", "gulesider.no", "1881.no", "purehelp.no",
         "brreg.no", "finn.no", "google.com", "youtube.com", "twitter.com", "x.com", "wix.com", "wixsite.com",
         "squarespace.com", "proff.se", "regnskapstall.no", "firmaer.no", "bedriftsdatabasen.no", "forvalt.no",
         "enin.no", "kunngjoringer.no", "norid.no", "domeneshop.no", "one.com", "webhuset.no", "purehelp.no",
         "northdata.com", "northdata.de", "opencorporates.com", "dnb.com", "companywall.no", "firmalisten.no",
         "nor47business.com", "180.no", "sokfirma.no", "firmadatabasen.no", "nyeselskaper.no", "infobel.com",
         "cylex.no", "kompass.com", "allabolag.se", "bizstanding.com", "mittanbud.no", "anbudstorget.no",
         "legelisten.no", "tripadvisor.com", "tripadvisor.no", "booking.com", "nav.no", "arbeidsplassen.nav.no"}


def cc_pairs(rec_files):
    for fp in rec_files:
        op = gzip.open if fp.endswith(".gz") else open
        with op(fp, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    p = json.loads(line)
                except ValueError:
                    continue
                if not p.get("o"):
                    continue
                host = (urllib.parse.urlsplit(p["u"]).hostname or "").lower()
                yield {"orgnr": p["o"], "host": host[4:] if host.startswith("www.") else host, "page_url": p["u"],
                       "snippet": p.get("sn") or "", "pre": p.get("pre") or "", "label": p.get("lab") or "",
                       "n_distinct_orgnrs": len(p.get("L") or []), "rule": p["r"],
                       "where": {KIND_HOME: "home", KIND_LEGAL: "sub"}.get(p.get("k"), "other"),
                       "retrieved_at": p.get("wd") or p.get("t") or "", "source": p.get("c") or "cc", "title": p.get("ti") or ""}


def live_pairs(path):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", encoding="utf-8") as f:
        for line in f:
            try:
                p = json.loads(line)
            except ValueError:
                continue
            sn = p.get("snippet") or ""
            m = LABEL_RE.search(sn)
            yield {"orgnr": p["orgnr"], "host": p.get("host") or p["domain"], "page_url": p["page_url"], "snippet": sn,
                   "pre": sn[: m.start()] if m else "", "label": m.group(0) if m else "",
                   "n_distinct_orgnrs": p.get("n_distinct_orgnrs") or 1, "rule": p["rule"], "where": p.get("where") or "home",
                   "retrieved_at": p.get("retrieved_at") or "", "source": "live-20261017"}


def build_index(pairs, names, out_dir):
    """Rank (orgnr, domain) pairs and write noindex.tsv.gz (top three domains per universe organisation)."""
    pairs = [p for p in pairs if p["host"]]
    for p in pairs:
        p["domain"] = regdom(p["host"])
    pairs = [p for p in pairs if p["domain"] and p["domain"] not in BLOCK]
    with gzip.open(f"{out_dir}/pairs_merged.jsonl.gz", "wt", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    dom_owners = collections.defaultdict(set)
    dom_hosts = collections.defaultdict(set)
    for p in pairs:
        dom_owners[p["domain"]].add(p["orgnr"])
        dom_hosts[p["domain"]].add(p["host"])
    n_host_keyed = 0
    for p in pairs:  # tenant platforms: several owners on different subdomains -> key on the full host
        d = p["domain"]
        if len(dom_owners[d]) > 1 and len(dom_hosts[d]) > 1 and p["host"] != d:
            p["domain"] = p["host"]
            n_host_keyed += 1
    dom_owners = collections.defaultdict(set)
    for p in pairs:
        dom_owners[p["domain"]].add(p["orgnr"])
    agg = {}
    dropped_other = 0
    for p in pairs:
        if len(dom_owners[p["domain"]]) > 5 or p["orgnr"] not in names:
            continue
        if p["where"] == "other" and not _names_site(names[p["orgnr"]], p):
            # A deep page (no homepage or legal page captured for the host) is a store page of a chain, a
            # directory entry or a customer case as often as the company's own: kept only when the domain
            # or the page title carries the company's name.
            dropped_other += 1
            continue
        other = named_other(p["pre"], names[p["orgnr"]])
        sc = RULE_W.get(p["rule"], 1) * 10 + (3 if p["where"] == "home" else 0) \
            + 8 * name_score(names[p["orgnr"]], p["domain"]) - (2 if p["n_distinct_orgnrs"] > 1 else 0) \
            - (8 if other else 0)
        if len(dom_owners[p["domain"]]) > 1:
            sc -= 10 if urllib.parse.urlsplit(p["page_url"]).path.strip("/") else 4
        k = (p["orgnr"], p["domain"])
        cur = agg.get(k)
        if cur is None:
            agg[k] = {**p, "score": sc, "pages": {p["page_url"]}, "sources": {p["source"]}, "named_other": other}
        else:
            cur["pages"].add(p["page_url"])
            cur["sources"].add(p["source"])
            if sc > cur["score"] or (sc == cur["score"] and p["retrieved_at"] > cur["retrieved_at"]):
                keep = {"pages": cur["pages"], "sources": cur["sources"]}
                cur.clear()
                cur.update({**p, "score": sc, "named_other": other, **keep})
    by_org = collections.defaultdict(list)
    for (o, d), p in agg.items():
        p["score"] = round(p["score"] + min(len(p["pages"]), 3) - 1, 2)
        by_org[o].append(p)
    n_rows = 0
    with gzip.open(f"{out_dir}/noindex.tsv.gz", "wt", encoding="utf-8") as f:
        f.write("orgnr\trank\tdomain\tpage_url\trule\twhere\tn_distinct_orgnrs\tn_domains_for_orgnr\tdomain_orgnrs\t"
                "score\tretrieved_at\tsource\tn_pages\tlabel\tnamed_other\tsnippet\n")
        for o in sorted(by_org):
            lst = sorted(by_org[o], key=lambda p: (-p["score"], p["domain"]))
            for i, p in enumerate(lst[:3], 1):
                src = "live" if any(s.startswith("live") for s in p["sources"]) else "cc"
                if src == "live" and len(p["sources"]) > 1:
                    src = "live+cc"
                sn = re.sub(r"[\t\r\n]+", " ", p["snippet"])[:160]
                lab = re.sub(r"[\t\r\n]+", " ", p["label"])[:30]
                f.write(f"{o}\t{i}\t{p['domain']}\t{p['page_url']}\t{p['rule']}\t{p['where']}\t{p['n_distinct_orgnrs']}\t"
                        f"{len(lst)}\t{len(dom_owners[p['domain']])}\t{p['score']}\t{p['retrieved_at']}\t{src}\t"
                        f"{len(p['pages'])}\t{lab}\t{p['named_other']}\t{sn}\n")
                n_rows += 1
    src_orgs = collections.Counter()
    for o, lst in by_org.items():
        srcs = set().union(*(p["sources"] for p in lst))
        live = any(s.startswith("live") for s in srcs)
        cc = any(not s.startswith("live") for s in srcs)
        src_orgs["live_only" if live and not cc else "cc_only" if cc and not live else "both"] += 1
    return {"pairs": len(pairs), "pairs_host_keyed": n_host_keyed, "pair_domains": len(dom_owners),
            "other_page_pairs_dropped": dropped_other,
            "domains_with_gt5_owners": sum(1 for v in dom_owners.values() if len(v) > 5),
            "index_orgnrs": len(by_org), "index_rows": n_rows, "universe_coverage": round(len(by_org) / max(1, len(names)), 4),
            "orgnrs_by_source": dict(src_orgs),
            "named_other_rows": sum(1 for lst in by_org.values() for p in lst[:3] if p["named_other"])}, by_org


def load_universe(path=None):
    if not path:
        path = "/tmp/universe.jsonl.gz"
        if not os.path.exists(path):
            data, _ = http_get(UNIVERSE_URL)
            open(path, "wb").write(data)
    rows = [json.loads(line) for line in gzip.open(path, "rt", encoding="utf-8") if line.strip()]
    return rows


def band(e):
    if not e:
        return "0"
    return "1-9" if e < 10 else "10-49" if e < 50 else "50+"


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--tmp", default="/tmp/ccorg")
    ap.add_argument("--releases", default="CC-MAIN-2026-43,CC-MAIN-2026-39,CC-MAIN-2026-34,CC-MAIN-2026-30")
    ap.add_argument("--lang-releases", default="CC-MAIN-2026-43,CC-MAIN-2026-39")
    ap.add_argument("--live-pairs", default="", help="glob of the live crawl's pairs_all.jsonl.gz")
    ap.add_argument("--universe", default="")
    ap.add_argument("--index-workers", type=int, default=16)
    ap.add_argument("--conc", type=int, default=96)
    ap.add_argument("--procs", type=int, default=0)
    ap.add_argument("--per-domain", type=int, default=25)
    ap.add_argument("--legal-max", type=int, default=3)
    ap.add_argument("--max-groups", type=int, default=0, help="smoke: at most this many row groups per mode")
    ap.add_argument("--part-filter", default="", help="smoke: only index part files matching this regex")
    ap.add_argument("--max-fetch", type=int, default=0, help="smoke: at most this many stage-A records")
    ap.add_argument("--hours", type=float, default=8.0, help="stop fetching after this many hours")
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    os.makedirs(args.tmp, exist_ok=True)
    deadline = T0 + args.hours * 3600
    stats = {"ua": UA, "started_at": now_iso(), "args": vars(args)}
    rels = [r for r in args.releases.split(",") if r]
    lrels = [r for r in args.lang_releases.split(",") if r]

    hosts, sst = select(rels, lrels, args.index_workers, args.max_groups, args.part_filter)
    stats["select"] = sst
    stage_a, stage_b, pst = plan(hosts, args.per_domain, args.legal_max)
    tld = collections.Counter(h.rsplit(".", 1)[-1] for h, _ in stage_a)
    pst["stage_a_tlds"] = dict(tld.most_common(12))
    stats["plan"] = pst
    log("plan", json.dumps(pst))
    del hosts
    if args.max_fetch:
        stage_a = stage_a[: args.max_fetch]
        stage_b = {h: v for h, v in stage_b.items() if h in {x for x, _ in stage_a}}
    # apex hosts (the registered domain itself) first, then subdomains; shuffled within each to spread
    # requests over WARC files
    random.Random(20261023).shuffle(stage_a)
    stage_a.sort(key=lambda x: x[0] != regdom(x[0]))

    pool = concurrent.futures.ProcessPoolExecutor(max_workers=args.procs or os.cpu_count() or 4, mp_context=mp_context())
    rec_path = f"{args.tmp}/cc_records.jsonl"
    with open(rec_path, "w", encoding="utf-8") as out:
        t = time.time()
        sa, found = asyncio.run(fetch_all(stage_a, pool, args.conc, out, deadline))
        sa["seconds"] = round(time.time() - t)
        log("stage A", json.dumps(sa))
        items_b = [(h, r) for h in sorted(stage_b) if h not in found for r in stage_b[h]]
        random.Random(20261024).shuffle(items_b)
        t = time.time()
        sb, found_b = asyncio.run(fetch_all(items_b, pool, args.conc, out, deadline))
        sb["seconds"] = round(time.time() - t)
        log("stage B", json.dumps(sb))
    pool.shutdown(wait=False, cancel_futures=True)
    stats["fetch"] = {"stage_a": sa, "stage_b": sb, "stage_b_items": len(items_b),
                      "owner_hosts": len(set(found) | set(found_b))}
    with open(rec_path, "rb") as fi, gzip.open(f"{args.out}/cc_records.jsonl.gz", "wb") as fo:
        while True:
            b = fi.read(1 << 22)
            if not b:
                break
            fo.write(b)

    rows = load_universe(args.universe or None)
    names = {r["organisation_number"]: r.get("name") or "" for r in rows}
    emp = {r["organisation_number"]: r.get("employees") for r in rows}
    pairs = list(cc_pairs([f"{args.out}/cc_records.jsonl.gz"]))
    n_cc = len(pairs)
    live = sorted(glob.glob(args.live_pairs, recursive=True)) if args.live_pairs else []
    for fp in live:
        pairs.extend(live_pairs(fp))
    stats["pairs_in"] = {"cc": n_cc, "live": len(pairs) - n_cc, "live_files": live}
    ist, by_org = build_index(pairs, names, args.out)
    bands = collections.Counter(band(e) for e in emp.values())
    hit = collections.Counter(band(emp[o]) for o in by_org)
    cc_only = collections.Counter(band(emp[o]) for o, lst in by_org.items()
                                  if not any(s.startswith("live") for p in lst for s in p["sources"]))
    ist["bands"] = {b: {"universe": bands[b], "index": hit[b], "share": round(hit[b] / max(1, bands[b]), 4),
                        "cc_only": cc_only[b]} for b in ("0", "1-9", "10-49", "50+")}
    stats["index"] = ist
    stats["finished_at"] = now_iso()
    json.dump(stats, open(f"{args.out}/stats_cc.json", "w"), indent=1, default=str)
    log("index", json.dumps(ist))
    return stats


if __name__ == "__main__":
    main()
