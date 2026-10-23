"""Is a dated item a real post about this company? The learned news gate.

Builderr, 23 Oct: each news item must be "a real, dated post about the company. Menu labels and
product pages aren't news". The rules in ``harvest.news_drop_reason`` catch the page parts, job
ads, guides, group stories and copy dates they were measured on. What is left is a content
judgement the rules cannot make: a product or treatment page filed as a WordPress post, an
evergreen SEO guide with a real publish date, a customer's story. This module makes it with a
logistic regression over hashed word and character n-grams of the item's title, URL path, lead
text and categories, plus structural signals (Article JSON-LD, ``article:published_time``, a
price or a cart on the page, the company's own name in the title, where the date was read).

The weights were fitted offline (``tools/news_train.py``) on news candidates labelled by an
open-weight LLM teacher and audited by hand, split by host; they ship in
``kildespor/models/newsgate.json`` (declared in CACHE.md). Inference is pure Python, deterministic
and takes well under a millisecond per item. The gate only ever refuses an item; it never adds
one, and it never decides which company an item belongs to: the site's identity is settled before
the harvest runs.

Corpus dump (development only): with ``KILDESPOR_NEWS_DUMP=<dir>`` every news candidate is
written, before any gate, to ``<dir>/news-<pid>.jsonl`` with the rules' and the gate's verdicts.
Off by default; it changes nothing in the output.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import threading
import unicodedata
import zlib
from array import array
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

MODEL_PATH = Path(__file__).resolve().parent / "models" / "newsgate.json"
FEATURE_VERSION = 1
LEAD_CHARS = 600

# ----------------------------------------------------------------------------------------------
# Page signals (computed once per page by the harvest, stored on the item)

_PRICE = re.compile(r"(?:\bkr\.?\s?\d[\d .]*(?:,-|,\d\d)?|\d[\d .]*\s?(?:kr|nok|,-)(?![a-zæøå])|\bnok\s?\d|€\s?\d|\$\s?\d|\binkl\.?\s+mva\b|\beks\.?\s+mva\b)", re.I)
_CART = re.compile(r"legg\s+i\s+(?:handle)?kurv|handlekurv|add\s+to\s+(?:cart|basket)|kjøp\s+nå|buy\s+now|bestill\s+nå|på\s+lager|in\s+stock|woocommerce|add_to_cart", re.I)
_BOOKING = re.compile(r"bestill\s+(?:time|konsultasjon|behandling)|book\s+(?:time|now|a\s+table)|timebestilling|online\s+booking", re.I)


def page_signals(html: str, text: str, ld_types: set[str]) -> tuple[str, ...]:
    """Coarse flags of what kind of page this is: a shop, a booking page, structured data types
    that are not articles. Read from the page's markup and visible text."""
    flags = set()
    head = text[:20000]
    if _PRICE.search(head):
        flags.add("price")
    if _CART.search(html[:400000]):
        flags.add("cart")
    if _BOOKING.search(head):
        flags.add("booking")
    for kind in ("Product", "Offer", "Service", "FAQPage", "Event", "JobPosting", "Recipe", "Course", "HowTo", "LocalBusiness", "CollectionPage", "ItemList"):
        if kind in ld_types:
            flags.add("ld_" + kind.lower())
    return tuple(sorted(flags))


def text_signals(text: str) -> tuple[str, ...]:
    """The same flags read from an item's own short text (a WordPress excerpt, a feed description)."""
    flags = set()
    if _PRICE.search(text or ""):
        flags.add("price")
    if _CART.search(text or ""):
        flags.add("cart")
    if _BOOKING.search(text or ""):
        flags.add("booking")
    return tuple(sorted(flags))


# ----------------------------------------------------------------------------------------------
# Candidate records: what the dump writes and what the model reads


def fold(text: str) -> str:
    text = (text or "").lower().replace("æ", "ae").replace("ø", "o").replace("å", "a")
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def own_words_in(text: str, own: list[str]) -> bool:
    squashed = re.sub(r"[^a-z0-9]", "", fold(text))
    return any(len(w) >= 4 and w in squashed for w in own)


def other_in(text: str, other: list[str]) -> bool:
    plain = " " + " ".join(re.sub(r"[^a-z0-9]+", " ", fold(text)).split()) + " "
    return any(f" {phrase} " in plain for phrase in other)


def record(item: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    """The candidate as the gate sees it (and the dump writes it): the item's own fields plus the
    company context the entity features need."""
    return {
        "v": FEATURE_VERSION,
        "org": ctx.get("org") or "",
        "legal_name": ctx.get("legal_name") or "",
        "brands": list(ctx.get("brands") or [])[:6],
        "host": ctx.get("host") or "",
        "home": ctx.get("home") or "",
        "wordpress": bool(ctx.get("wordpress")),
        "group_site": bool(ctx.get("group_site")),
        "own": sorted(ctx.get("own") or []),
        "other": list(ctx.get("other") or []),
        "title": item.title,
        "url": item.url or "",
        "published": item.published,
        "date_source": getattr(item, "date_source", "") or "",
        "method": item.method,
        "categories": list(item.categories or ()),
        "lead": (getattr(item, "lead", "") or "")[:LEAD_CHARS],
        "og_type": getattr(item, "og_type", "") or "",
        "ld_types": list(getattr(item, "ld_types", ()) or ()),
        "article_time": bool(getattr(item, "article_time", False)),
        "flags": list(getattr(item, "flags", ()) or ()),
        "listing": getattr(item, "listing", "") or "",
    }


# ----------------------------------------------------------------------------------------------
# Features

_YEAR = re.compile(r"^(?:19|20)\d\d$")
_NEWS_SEG = re.compile(r"^(?:nyheter|nyhet|nyheiter|aktuelt|news|newsroom|presse|press|pressemeldinger|pressemelding|press-releases?|presserom|blogg|blog|artikler|artikkel|articles?|siste-nytt|media|innlegg|posts?)$", re.I)
_QUESTION_START = re.compile(r"^(?:hvordan|hva|hvorfor|hvilke?|hvem|hvor|når|kan|bør|skal|er|how|what|why|which|when|can|should|is|are|do|does)\b", re.I)


def _words(text: str) -> list[str]:
    out = []
    for w in re.sub(r"[^a-z0-9]+", " ", fold(text)).split():
        if w.isdigit():
            w = "<y>" if _YEAR.match(w) else "<n>"
        out.append(w)
    return out


def _bucket(n: int, edges: tuple[int, ...]) -> str:
    for edge in edges:
        if n <= edge:
            return str(edge)
    return f">{edges[-1]}"


def features(c: dict[str, Any]) -> list[str]:
    """Feature strings of a candidate record (binary; the order does not matter)."""
    f: list[str] = []
    title = c.get("title") or ""
    tw = _words(title)
    f += ["t:" + w for w in tw]
    f += ["t2:" + a + "_" + b for a, b in zip(tw, tw[1:])]
    if tw:
        f.append("t0:" + tw[0])
    chars = " " + re.sub(r"\d", "0", " ".join(re.sub(r"[^a-z0-9]+", " ", fold(title)).split())) + " "
    for n in (3, 4, 5):
        f += ["tc:" + chars[i:i + n] for i in range(max(0, len(chars) - n + 1))]

    url = c.get("url") or ""
    path = urlsplit(url).path if url else ""
    segs = [unquote(s) for s in path.split("/") if s]
    for s in segs[:-1]:
        f.append("useg:" + "-".join(_words(s))[:40])
        f += ["u:" + w for w in _words(s)]
    if segs:
        last = segs[-1]
        f += ["ul:" + w for w in _words(last)]
        slug = " " + re.sub(r"\d", "0", "-".join(re.sub(r"[^a-z0-9]+", " ", fold(last)).split())) + " "
        f += ["uc:" + slug[i:i + 4] for i in range(max(0, len(slug) - 3))]
    f.append("depth:" + _bucket(len(segs), (0, 1, 2, 3, 4)))
    if any(_NEWS_SEG.match(s) for s in segs[:-1]):
        f.append("s:newspath")
    if re.search(r"/(?:19|20)\d\d/\d\d?/", path):
        f.append("s:datedpath")
    if urlsplit(url).query:
        f.append("s:query")
    if not url:
        f.append("s:nourl")

    lead = (c.get("lead") or "")[:LEAD_CHARS]
    lw = _words(lead)
    f += ["l:" + w for w in lw]
    f += ["l2:" + a + "_" + b for a, b in zip(lw, lw[1:])]
    f.append("leadlen:" + _bucket(len(lead), (0, 80, 200, 400)))

    for cat in c.get("categories") or []:
        slug = "-".join(_words(cat))[:40]
        f.append("c:" + slug)
        f += ["cw:" + w for w in _words(cat)]
    f.append("ncat:" + _bucket(len(c.get("categories") or []), (0, 1, 2, 4)))

    f.append("m:" + (c.get("method") or ""))
    f.append("ds:" + (c.get("date_source") or ""))
    f.append("og:" + (c.get("og_type") or "none").lower()[:20])
    for t in c.get("ld_types") or []:
        f.append("ld:" + str(t)[:30])
    if c.get("article_time"):
        f.append("s:article_time")
    for flag in c.get("flags") or []:
        f.append("flag:" + flag)
    f.append("time:" + ("y" if "T" in (c.get("published") or "") else "n"))
    if c.get("listing"):
        f.append("s:listing")
    if c.get("wordpress"):
        f.append("s:wp")

    f.append("tlen:" + _bucket(len(title), (15, 30, 50, 80, 120)))
    f.append("twords:" + _bucket(len(tw), (1, 2, 3, 5, 8, 12)))
    if "?" in title:
        f.append("s:question")
    if _QUESTION_START.match(title.strip()):
        f.append("s:qstart")
    if re.search(r"\b(?:19|20)\d\d\b", title):
        f.append("s:titleyear")
    if re.search(r"[!]", title):
        f.append("s:exclaim")
    if title.isupper() and len(title) > 6:
        f.append("s:caps")

    own = c.get("own") or []
    other = c.get("other") or []
    if own_words_in(title, own):
        f.append("e:title_own")
    if own_words_in(lead, own):
        f.append("e:lead_own")
    if other_in(title, other):
        f.append("e:title_other")
    if other_in(lead, other):
        f.append("e:lead_other")
    return f


def hashed(feats: list[str], bits: int) -> list[int]:
    """Bucket of every distinct feature (crc32, stable across runs and platforms)."""
    mask = (1 << bits) - 1
    return [zlib.crc32(s.encode("utf-8")) & mask for s in sorted(set(feats))]


# ----------------------------------------------------------------------------------------------
# The model


class Gate:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.bits = int(spec["bits"])
        self.bias = float(spec["bias"])
        self.scale = float(spec["scale"])
        self.threshold = float(spec["threshold"])
        self.version = int(spec.get("feature_version", FEATURE_VERSION))
        weights = array("b")
        weights.frombytes(zlib.decompress(base64.b64decode(spec["weights"])))
        if len(weights) != 1 << self.bits:
            raise ValueError("newsgate weights do not match their bit width")
        self.weights = weights
        self.sha256 = spec.get("sha256") or ""

    def score(self, c: dict[str, Any]) -> float:
        z = self.bias + self.scale * sum(self.weights[i] for i in hashed(features(c), self.bits))
        z = max(-30.0, min(30.0, z))
        return 1.0 / (1.0 + math.exp(-z))


_GATE: Gate | None = None
_GATE_LOADED = False
_LOAD_LOCK = threading.Lock()


def gate() -> Gate | None:
    """The shipped model, or None when its file is missing or unreadable (the rules then decide alone)."""
    global _GATE, _GATE_LOADED
    if _GATE_LOADED:
        return _GATE
    with _LOAD_LOCK:
        if not _GATE_LOADED:
            try:
                if os.environ.get("KILDESPOR_NEWSGATE", "1") != "0" and MODEL_PATH.exists():
                    _GATE = Gate(json.loads(MODEL_PATH.read_text(encoding="utf-8")))
            except Exception:  # a damaged model file must not stop the run
                _GATE = None
            _GATE_LOADED = True
    return _GATE


def verdict(c: dict[str, Any]) -> tuple[bool, float | None]:
    """(keep, score). Without a model every item is kept and the score is None."""
    model = gate()
    if model is None:
        return True, None
    p = model.score(c)
    return p >= model.threshold, round(p, 4)


def encode_weights(values: list[int]) -> str:
    return base64.b64encode(zlib.compress(array("b", values).tobytes(), 9)).decode("ascii")


def spec_sha256(spec: dict[str, Any]) -> str:
    body = {k: v for k, v in spec.items() if k != "sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------------------------
# Corpus dump

_DUMP_LOCK = threading.Lock()


def dump_dir() -> str:
    return os.environ.get("KILDESPOR_NEWS_DUMP") or ""


def dump(rows: list[dict[str, Any]]) -> None:
    folder = dump_dir()
    if not folder or not rows:
        return
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"news-{os.getpid()}.jsonl")
        with _DUMP_LOCK, open(path, "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass
