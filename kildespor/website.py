"""Find, verify and read the company's own website.

Candidates come from the register (the entity's and its subunits' listed
websites) and from deterministic domain guesses built from the legal name.
A register-listed site is the company's own declaration, so it is published
unless it is parked or plainly belongs to a different, unrelated entity. Any
other candidate (name-derived domains, e-mail domains, former and subunit
names, declared caches) is published on one of three proofs, each labelled in
``identity_method``:

* ``organisation_number_on_site``: this organisation number next to an
  ownership label (org.nr, MVA, Foretaksregisteret), not in a customer,
  supplier, partner, auditor or web-agency context;
* ``register_contact_on_site``: the registered phone or e-mail address is on
  the site and is not shared by five or more register entities;
* ``name_and_register_signal``: every word of the legal name stands together in
  the homepage's own identity text (title, site name, h1, JSON-LD name,
  copyright line) and the site shows a register signal (postcode and town,
  street address, phone, e-mail domain or the daglig leder's/chair's name).

The last two are vetoed by any primary organisation number that is not ours,
by a copyright or site name that names a different legal entity, by
directories, parked pages, foreign country domains and listings of many
organisations, and they never apply to housing co-ops (whose contact details
are their property manager's).

Facts read from a verified site: public brand name, self-description,
phone numbers and e-mail addresses, company-owned social profiles,
the careers page, JSON-LD job postings and dated news from the site's own
RSS/Atom feed.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .http import Fetcher, RequestBudgetExhausted, RobotsDisallowed, Snapshot, UnsafeURL, resolve_public, utc_now

try:  # declared discovery caches (optional: every lookup is empty when the files are absent)
    from . import caches
except ImportError:  # pragma: no cover
    caches = None  # type: ignore[assignment]


def utc_today() -> str:
    return utc_now()[:10]
from .model import CompanyRecord

LEGAL_SUFFIXES = {
    "as", "asa", "ans", "da", "sa", "ba", "nuf", "enk", "ks", "hf", "iks", "sf", "bbl", "fli", "stiftelse", "stiftelsen", "aksjeselskap",
}
# Generic words dropped from a name before guessing a shorter domain ("Vetnes Invest" -> vetnes.no);
# the full name is always guessed first.
STRIP_GUESS_WORDS = {
    "holding", "holdings", "invest", "investment", "investments", "investering", "kapital", "capital", "eiendom", "eiendommer",
    "property", "properties", "gruppen", "group", "drift", "norge", "norway", "as",
}
# Holding, investment and property companies share their family name and address with an
# operating sister ("VETNES INVEST AS" / "Vetnes Bygg AS"): contact details alone never prove them.
HOLDING_WORDS = {
    "holding", "holdings", "invest", "investment", "investments", "investering", "kapital", "capital", "eiendom", "eiendommer",
    "eiendomsselskap", "property", "properties", "finans", "forvaltning", "management",
}
# Housing co-ops and owners' associations register their property manager's phone and e-mail.
HOUSING_WORDS = {"borettslag", "boligsameie", "sameie", "sameiet", "brl", "boligbyggelag", "eierseksjonssameie", "velforening", "huseierforening"}
HOUSING_FORMS = {"BRL", "ESEK", "BBL"}
NOT_FOREIGN_CCTLDS = {"no", "as", "io", "ai", "co", "me", "tv", "cc", "eu", "nu", "to", "gg", "fm", "ly"}
DIRECTORY_HOSTS = (
    "proff.no", "purehelp.no", "gulesider.no", "1881.no", "finn.no", "brreg.no", "regnskapstall.no", "firmasok.no", "bedriftsdatabasen.no",
    "facebook.com", "linkedin.com", "instagram.com", "google.com", "wix.com", "wixsite.com", "squarespace.com", "enirokart.no", "kompass.com",
    "allabolag.se", "dun.com", "dnb.com", "forvalt.no", "norske-bedrifter.no", "bedrift.no", "180.no", "opencorporates.com",
    "companywall.no", "firmalisten.no", "northdata.com", "northdata.de", "nor47business.com", "yra.no", "sokfirma.no", "firmadatabasen.no",
    "nyeselskaper.no", "1850.no", "vexter.no", "firmview.no", "pappers.no", "bedriftsregisteret.no", "infobel.com", "cylex.no",
    "mittanbud.no", "anbudstorget.no", "byggstart.no", "legelisten.no", "tripadvisor.com", "tripadvisor.no", "booking.com",
    "arbeidsplassen.nav.no", "nav.no", "x.com", "twitter.com", "youtube.com", "tiktok.com", "google.no", "goo.gl", "maps.app.goo.gl",
    "wordpress.com", "blogspot.com", "weebly.com", "jimdo.com", "jimdofree.com", "webnode.com", "site123.me", "godaddysites.com",
)
DIRECTORY_PATH_SEGMENTS = {
    "company", "companies", "bedrift", "bedrifter", "selskap", "selskaper", "firma", "foretak", "virksomhet", "enhet", "enheter",
    "owner", "profile", "directory", "katalog",
}
PARKED_MARKERS = (
    "domain is for sale", "domenet er til salgs", "this domain is parked", "buy this domain", "kjøp dette domenet", "domain parking",
    "parkert hos", "domeneshop", "registrert hos one.com", "this domain has been registered", "web hosting by", "default web site page",
    "website is under construction", "siden er under konstruksjon", "denne siden er parkert", "hostinger", "parked free",
    "domain for sale", "hugedomains", "parked at", "miss hosting", "her flytter snart en ny gjest", "is for sale", "kjøp domenet",
    "sedo domain", "dan.com", "afternic", "parkingcrew", "bodis", "this domain may be for sale", "domenet er registrert",
    "domain has been registered", "webhotell", "uniweb", "webhuset", "default page", "it works!", "welcome to nginx",
    "plesk", "cpanel", "index of /", "account suspended", "site not found", "under oppbygging",
)
CONTACT_HINTS = re.compile(r"kontakt|contact|om-oss|om_oss|omoss|about|personvern|privacy|vilk[aå]r|terms|impressum|informasjonskapsler|cookie|firma|selskap", re.I)
CAREER_HINTS = re.compile(r"karriere|ledige[- ]stillinger|stillinger|jobb|career|jobs|work-with-us|bli-med", re.I)
SOCIAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("facebook", re.compile(r"^https?://(?:www\.|m\.|web\.)?facebook\.com/(?!sharer|share|dialog|plugins|tr\b|login|home\.php|groups/?$|events/?$|watch/?$|policy|privacy)([A-Za-z0-9.\-_/%]+?)/?(?:\?.*)?$", re.I)),
    ("instagram", re.compile(r"^https?://(?:www\.)?instagram\.com/(?!p/|reel/|explore/|accounts/|stories/)([A-Za-z0-9._]+)/?(?:\?.*)?$", re.I)),
    ("linkedin", re.compile(r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com/(company|school|showcase)/([A-Za-z0-9\-_%.]+)/?(?:\?.*)?$", re.I)),
    ("youtube", re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/((?:channel|c|user)/[A-Za-z0-9\-_]+|@[A-Za-z0-9\-_.]+)/?(?:\?.*)?$", re.I)),
    ("x", re.compile(r"^https?://(?:www\.|mobile\.)?(?:twitter|x)\.com/(?!intent|share|home|search|hashtag|i/)([A-Za-z0-9_]{1,15})/?(?:\?.*)?$", re.I)),
    ("tiktok", re.compile(r"^https?://(?:www\.)?tiktok\.com/(@[A-Za-z0-9._]+)/?(?:\?.*)?$", re.I)),
    ("pinterest", re.compile(r"^https?://(?:[a-z]{2}\.)?pinterest\.(?:com|no)/([A-Za-z0-9_]+)/?(?:\?.*)?$", re.I)),
    ("vimeo", re.compile(r"^https?://(?:www\.)?vimeo\.com/([A-Za-z][A-Za-z0-9_]+)/?(?:\?.*)?$", re.I)),
]
FREE_MAIL = {
    "gmail.com", "hotmail.com", "hotmail.no", "outlook.com", "live.com", "live.no", "yahoo.com", "yahoo.no", "icloud.com", "me.com",
    "online.no", "frisurf.no", "start.no", "broadpark.no", "getmail.no", "c2i.net", "chello.no", "altibox.no", "lyse.net", "norpost.net",
    "msn.com", "aol.com", "protonmail.com", "proton.me", "mail.com", "gmx.com", "gmx.net", "email.com", "telia.no", "hotmail.co.uk", "ymail.com",
    "tele2.no", "kpnmail.no", "bluezone.no", "loqal.no", "netcom.no", "tdcadsl.no", "sensewave.com", "eunet.no", "nextgentel.com", "post.com",
}
WEIGHTS = (3, 2, 7, 6, 5, 4, 3, 2)
ORG_CANDIDATE = re.compile(r"(?<![\d])(\d{3})[  .\-]?(\d{3})[  .\-]?(\d{3})(?![\d])")


GENERIC_NAME_WORDS = {
    "norge", "norway", "norsk", "nordic", "holding", "invest", "eiendom", "gruppen", "group", "service", "services", "consult",
    "consulting", "bygg", "drift", "utvikling", "handel", "transport", "regnskap", "solutions", "systems", "media", "design",
    "partner", "partners", "energi", "teknikk", "elektro", "maskin", "hotell", "restaurant", "senter", "borettslag", "sameie",
    "stiftelsen", "stiftelse", "forening", "selskap", "company", "management", "capital", "the", "and", "og", "for", "til",
}


def _fold(text: str) -> str:
    return strip_accents(text.lower().replace("æ", "ae").replace("ø", "o").replace("å", "a"))


def name_corroboration(legal_name: str, subunits: list[dict[str, Any]], page_text: str, final_url: str) -> str | None:
    """A short span proving the company name appears on the page or forms the domain, else None."""
    folded_page = _fold(page_text)
    host = registered_domain(urlsplit(final_url).hostname or "").split(".")[0].replace("-", "")
    names = [legal_name] + [s.get("name") or "" for s in subunits[:5]]
    for name in names:
        tokens = [t for t in (_fold(t) for t in name_tokens(name)) if t]
        if not tokens:
            continue
        joined = "".join(tokens)
        if len(joined) >= 4 and joined == host:
            return f"domain {host} spells the name {name}"
        phrase = " ".join(tokens)
        if len(phrase) >= 5 and phrase in folded_page:
            at = folded_page.find(phrase)
            return f"name on page: …{page_text[max(0, at - 40): at + len(phrase) + 40]}…"
        distinctive = [t for t in tokens if len(t) >= 5 and t not in GENERIC_NAME_WORDS]
        if distinctive and all(re.search(rf"\b{re.escape(t)}\b", folded_page) for t in distinctive) and len(distinctive) >= max(1, len(tokens) - 1):
            at = folded_page.find(distinctive[0])
            return f"name words {', '.join(distinctive)} on page: …{page_text[max(0, at - 40): at + 60]}…"
    return None


OWNERSHIP_CONTEXT = re.compile(r"org\.?\s*-?\s*n|organisasjons|foretak|mva|vat|reg\.?\s*nr|registrering|company\s+(reg|no|number)", re.I)
CREDIT_WORDS = re.compile(r"levert av|laget av|designet av|design av|utviklet av|utvikla av|nettside av|webside av|hjemmeside av|webdesign|web design|powered by|made by|website by|site by|developed by|designed by|built by|produsert av|\bbyrå\b", re.I)


def _is_credit_link(anchor: Any) -> bool:
    """A social link that belongs to the web agency credited in the footer, not to the company."""
    node = anchor
    for _ in range(2):
        node = node.parent
        if node is None:
            return False
        text = node.get_text(" ", strip=True)
        if len(text) > 160:
            return False
        if CREDIT_WORDS.search(text):
            return True
    return False


def _names_of(rec: CompanyRecord) -> list[str]:
    entity = rec.context.get("entity") or {}
    names = [entity.get("navn") or ""] + [s.get("name") or "" for s in rec.context.get("subunits") or []]
    names += [c["value"] for c in rec.claims if c["field"] == "public_brand_name" and isinstance(c["value"], str)]
    return [n for n in names if n]


def _name_matches(candidate: str, rec: CompanyRecord) -> bool:
    key = " ".join(_fold(t) for t in name_tokens(candidate))
    if not key:
        return False
    for name in _names_of(rec):
        other = " ".join(_fold(t) for t in name_tokens(name))
        if other and (key == other or key.startswith(other + " ") or other.startswith(key + " ")):
            return True
    return False


def _ld_numbers(item: dict[str, Any]) -> set[str]:
    raw = json.dumps({k: item.get(k) for k in ("taxID", "vatID", "identifier", "leiCode", "duns", "globalLocationNumber")}, ensure_ascii=False)
    return set(org_numbers_in(raw))


def _ld_org_is_ours(item: dict[str, Any], rec: CompanyRecord) -> bool:
    """A JSON-LD organisation is ours only when it identifies itself as us: our (or a subunit's)
    number, our name, or the verified site's own URL. An organisation that says nothing about
    who it is is not ours: a verified site can embed a customer's or a partner's card."""
    numbers = _ld_numbers(item)
    if numbers:
        own = {rec.organisation_number} | {s.get("organisation_number") for s in rec.context.get("subunits") or []}
        return bool(numbers & own)
    name = item.get("legalName") or item.get("name")
    if name:
        return _name_matches(str(name), rec)
    site = (rec.context.get("website") or {}).get("url")
    for key in ("url", "@id"):
        value = item.get(key)
        if site and isinstance(value, str) and value.startswith("http") and registered_domain(urlsplit(value).hostname or "") == registered_domain(urlsplit(site).hostname or ""):
            return True
    return False


def _ld_employer_is_ours(item: dict[str, Any], rec: CompanyRecord) -> bool:
    """A JSON-LD job is ours only when its hiringOrganization identifies us (see _ld_org_is_ours)."""
    employer = item.get("hiringOrganization")
    if not employer:
        return False
    if isinstance(employer, str):
        return _name_matches(employer, rec)
    if isinstance(employer, dict):
        return _ld_org_is_ours(employer, rec)
    return False


# -- identity signals ---------------------------------------------------------------------
# Words that, shortly before an organisation number, say the number is someone else's relation to
# the site (a customer, supplier, partner, auditor, parent or subsidiary), not the site's own registration.
MENTION_CONTEXT = re.compile(
    r"kunde|customer|client|klient|leverand|supplier|vendor|partner|referanse|reference|testimonial|anbefal|recommend|"
    r"oppdragsgiver|datterselskap|subsidiar|søsterselskap|sister|morselskap|parent\s+compan|revisor|revisjon|auditor|"
    r"regnskapsf|accountant|medlem\s+av|member\s+of|på\s+vegne\s+av|on\s+behalf\s+of|levert\s+til|delivered\s+to|prosjekt\s+for|"
    r"eksempel|example",
    re.I,
)
ORG_LD_TYPES = {
    "Organization", "Corporation", "LocalBusiness", "ProfessionalService", "Store", "Restaurant", "AutomotiveBusiness",
    "HomeAndConstructionBusiness", "MedicalBusiness", "LegalService", "FinancialService", "NGO", "EducationalOrganization",
}
LEGAL_FORM_WORDS = ("as", "asa", "ans", "da", "sa", "nuf", "enk", "ks", "ba")
ROLE_CODES_FOR_IDENTITY = {"DAGL", "LEDE", "INNH", "DTPR", "DTSO"}
PHONE_ON_PAGE = re.compile(r"(?<![\d+])(?:(?:\+|00)\s?47[\s.\-]*)?(\d(?:[\s.\-]?\d){7})(?![\d])")
EMAIL_ON_PAGE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
COPYRIGHT = re.compile(r"(?:©|\(c\)|copyright|opphavsrett)\s*(?:\d{4}(?:\s*[-–]\s*\d{2,4})?\s*)?[,:\-–]?\s*([^|•·©]{2,90})", re.I)


def number_contexts(text: str) -> dict[str, tuple[str, str]]:
    """Every valid organisation number in ``text`` mapped to (kind, span).

    kind is ``owner`` (an ownership label such as org.nr/MVA/Foretaksregisteret right before or
    after it), ``mention`` (a customer, supplier, partner, auditor, parent or subsidiary context),
    ``credit`` (a web-agency credit) or ``bare``. A number seen several times keeps its strongest
    ownership reading.
    """
    rank = {"owner": 3, "bare": 2, "mention": 1, "credit": 0}
    out: dict[str, tuple[str, str]] = {}
    for match in ORG_CANDIDATE.finditer(text):
        number = "".join(match.groups())
        if not valid_orgnr(number):
            continue
        before = re.split(r"[|•·©•]", text[max(0, match.start() - 70): match.start()])[-1][-60:]
        after = text[match.end(): match.end() + 14]
        span = re.sub(r"\s+", " ", text[max(0, match.start() - 60): match.end() + 30]).strip()
        if CREDIT_WORDS.search(before):
            kind = "credit"
        elif MENTION_CONTEXT.search(before):
            kind = "mention"
        elif OWNERSHIP_CONTEXT.search(before[-40:] + " " + after):
            kind = "owner"
        else:
            kind = "bare"
        if number not in out or rank[kind] > rank[out[number][0]]:
            out[number] = (kind, span)
    return out


def _markup_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", " ", html, flags=re.I)))


def _cf_emails(html: str) -> list[str]:
    """Addresses hidden by Cloudflare's e-mail obfuscation (data-cfemail / email-protection#...)."""
    found = []
    for hexstr in re.findall(r'data-cfemail="([0-9a-fA-F]{6,})"|email-protection#([0-9a-fA-F]{6,})', html):
        value = hexstr[0] or hexstr[1]
        try:
            key = int(value[:2], 16)
            found.append("".join(chr(int(value[i:i + 2], 16) ^ key) for i in range(2, len(value) - 1, 2)).lower())
        except ValueError:
            continue
    return [f for f in found if EMAIL.match(f)]


def _norm_part(text: str) -> str:
    """Fold a piece of identity text to lowercase ASCII words, with `|` marking separators."""
    t = _fold(text or "").replace("&", " og ")
    t = re.sub(r"[|\-–—:·•,/()\[\]!?«»\"“”]+|\s\.\s", " | ", t)
    t = re.sub(r"[^a-z0-9| ]+", " ", t)
    return " ".join(t.split())


def core_tokens(name: str) -> list[str]:
    """The legal name folded to ASCII words, legal form removed (``og`` kept for word order)."""
    return [_fold(t) for t in name_tokens(name) if _fold(t)]


def _name_phrases(name: str) -> list[str]:
    tokens = core_tokens(name)
    no_og = [t for t in tokens if t not in ("og", "and")]
    phrases = [" ".join(tokens), " ".join(no_og)]
    if len(no_og) > 1:
        phrases.append("".join(no_og))
    return [p for p in dict.fromkeys(phrases) if len(p.replace(" ", "")) >= 3]


def name_in_part(name: str, part: str) -> bool:
    """True when the whole legal name stands in ``part`` as a name, not as the start of a longer one.

    The words must be contiguous and in order, and be followed by the end of the text, a separator
    or a legal form ("Berg Bygg AS | Hjem"), never by more words ("Berg Bygg og Anlegg" is another
    company). A one-word name must also open its segment ("Bolt | ..."), not sit inside a sentence.
    """
    norm = _norm_part(part)
    tokens = [t for t in core_tokens(name) if t not in ("og", "and")]
    if not tokens:
        return False
    tail = r"(?=$| \||" + "|".join(rf" {w}(?:$| \|)" for w in LEGAL_FORM_WORDS) + ")"
    for phrase in _name_phrases(name):
        head = r"(?:^|\| )" if len(tokens) == 1 else r"(?:^| )"
        if re.search(head + re.escape(phrase) + tail, norm):
            return True
    return False


def identity_parts(soup: BeautifulSoup, text: str) -> list[tuple[str, str]]:
    """The homepage's statements about who runs it: title, site name, h1, JSON-LD names, copyright."""
    parts: list[tuple[str, str]] = []
    if soup.title and soup.title.get_text(strip=True):
        parts.append(("title", soup.title.get_text(" ", strip=True)[:200]))
    for attrs, label in (({"property": "og:site_name"}, "og:site_name"), ({"property": "og:title"}, "og:title"), ({"name": "application-name"}, "application-name")):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            parts.append((label, str(meta["content"])[:200]))
    for h1 in soup.find_all("h1")[:3]:
        value = h1.get_text(" ", strip=True)
        if value:
            parts.append(("h1", value[:200]))
    for item in json_ld_items(soup):
        if _types(item) & ORG_LD_TYPES:
            for key in ("legalName", "name", "alternateName"):
                value = item.get(key)
                for v in value if isinstance(value, list) else [value]:
                    if isinstance(v, str) and v.strip():
                        parts.append((f"JSON-LD {key}", v.strip()[:200]))
    for match in COPYRIGHT.finditer(text):
        line = re.split(r"(?i)alle rettigheter|all rights|org\.?\s*-?\s*n|\btlf\b|\btelefon\b|\bphone\b|e-?post|e-?mail|\d{4}\s+[A-ZÆØÅ]", match.group(1))[0]
        line = re.sub(r"^\s*\d{4}(?:\s*[-–]\s*\d{2,4})?\s*", "", line).strip(" .,-")
        if 2 <= len(line) <= 90:
            parts.append(("copyright", line))
    return parts[:30]


def entity_mentions(parts: list[tuple[str, str]]) -> list[str]:
    """Legal entities the identity text names ("Berg Bygg AS"), as folded core words."""
    found: list[str] = []
    for label, part in parts:
        if label not in ("title", "og:site_name", "copyright", "JSON-LD legalName", "JSON-LD name"):
            continue
        for segment in _norm_part(part).split(" | "):
            words = segment.split()
            if len(words) >= 2 and words[-1] in LEGAL_FORM_WORDS and len(words) <= 7:
                core = " ".join(w for w in words[:-1] if w not in ("og", "and"))
                if core and core not in found:
                    found.append(core)
    return found


class RegisterSignals:
    """What the register says about how to reach the company, to be looked for on a candidate site."""

    def __init__(self, rec: CompanyRecord) -> None:
        entity = rec.context.get("entity") or {}
        subunits = rec.context.get("subunits") or []
        phones = [entity.get("telefon"), entity.get("mobil")] + [p for s in subunits for p in s.get("phones") or []]
        emails = [entity.get("epostadresse")] + [s.get("email") for s in subunits]
        bulk = caches.register_contacts(rec.organisation_number) if caches is not None else {"phones": [], "emails": []}
        phones += bulk.get("phones") or []
        emails += bulk.get("emails") or []
        self.phones = {d[-8:] for d in (re.sub(r"\D", "", str(p or "")) for p in phones) if len(d) >= 8}
        self.emails = {str(e).strip().lower() for e in emails if e and EMAIL.match(str(e).strip())}
        self.email_domains = {registered_domain(e.rsplit("@", 1)[1]) for e in self.emails} - FREE_MAIL
        address = entity.get("forretningsadresse") or entity.get("beliggenhetsadresse") or {}
        postal = entity.get("postadresse") or {}
        self.streets = []
        for addr in (address, postal):
            lines = [line for line in addr.get("adresse") or [] if line and not re.match(r"(?i)\s*(postboks|pb\.?|boks|c/o)\b", line)]
            if lines and addr.get("postnummer"):
                self.streets.append((lines[0], str(addr["postnummer"])))
        self.places = [(str(a["postnummer"]), str(a["poststed"])) for a in (address, postal) if a.get("postnummer") and a.get("poststed")]
        self.people = sorted({r["name"] for r in rec.context.get("roles") or [] if r.get("holder_type") == "person" and r.get("role_code") in ROLE_CODES_FOR_IDENTITY and r.get("name")})
        # Current legal name and subunit names only. A former name is a candidate source, never
        # identity: after a restructuring the operating business often moves to a new company that
        # takes the old name ("ABC AS" -> "ABC HOLDING AS" + a new "ABC AS" at the same address).
        self.names = [n for n in _names_of(rec) if n]
        form = ((entity.get("organisasjonsform") or {}).get("kode") or "").upper()
        words = set(core_tokens(entity.get("navn") or ""))
        self.housing = form in HOUSING_FORMS or bool(words & HOUSING_WORDS)
        self.holding = bool(words & HOLDING_WORDS)
        self.in_group = bool(entity.get("erIKonsern")) or bool(rec.context.get("group_orgs"))

    # -- screens --------------------------------------------------------------------------
    @staticmethod
    def screen_ready(kind: str) -> bool:
        if caches is None:
            return False
        name = "shared-phones.tsv.gz" if kind == "phone" else "shared-email-domains.tsv.gz"
        try:
            return name in caches.available()
        except Exception:
            return False

    @staticmethod
    def phone_shared(phone8: str) -> bool:
        try:
            return bool(caches is not None and caches.shared_phone(phone8))
        except Exception:
            return True

    @staticmethod
    def domain_shared(domain: str) -> bool:
        if domain in FREE_MAIL:
            return True
        try:
            return bool(caches is not None and caches.shared_email_domain(domain))
        except Exception:
            return True


def contact_signals(sig: RegisterSignals, text: str, html: str, soup: BeautifulSoup | None) -> dict[str, str]:
    """Register contact details found on one page, each mapped to a short quoted span."""
    found: dict[str, str] = {}
    flat = text
    hrefs = [a.get("href") or "" for a in soup.find_all("a", href=True)] if soup is not None else []
    if sig.phones:
        for match in PHONE_ON_PAGE.finditer(flat):
            digits = re.sub(r"\D", "", match.group(1))
            if digits in sig.phones:
                found["phone"] = flat[max(0, match.start() - 30): match.end() + 10].strip()
                found["phone8"] = digits
                break
        if "phone" not in found:
            for href in hrefs:
                if href.lower().startswith("tel:"):
                    digits = re.sub(r"\D", "", href)[-8:]
                    if digits in sig.phones:
                        found["phone"], found["phone8"] = f"tel link: {href[:40]}", digits
                        break
    page_emails = {e.lower().strip(".") for e in EMAIL_ON_PAGE.findall(flat)}
    page_emails |= {h[7:].split("?")[0].strip().lower() for h in hrefs if h.lower().startswith("mailto:")}
    page_emails |= set(_cf_emails(html))
    for email in sorted(page_emails & sig.emails):
        found["email"] = f"e-mail on page: {email}"
        found["email_address"] = email
        break
    for email in sorted(page_emails):
        if "@" in email and registered_domain(email.rsplit("@", 1)[1]) in sig.email_domains:
            found["email_domain"] = f"e-mail on the registered domain: {email}"
            break
    folded = _fold(flat)
    for street, postcode in sig.streets:
        key = " ".join(_fold(street).split())
        if len(key) >= 6 and re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", folded) and postcode in flat:
            at = folded.find(key)
            found["street"] = flat[max(0, at - 10): at + len(key) + 30].strip()
            break
    for postcode, town in sig.places:
        match = re.search(rf"(?<!\d){postcode}\s*,?\s+{re.escape(_fold(town))}\b", folded)
        if match:
            found["postcode_town"] = flat[max(0, match.start() - 20): match.end() + 10].strip()
            break
    for person in sig.people:
        words = core_tokens(person)
        if len(words) >= 2 and len("".join(words)) >= 7:
            variants = {" ".join(words), f"{words[0]} {words[-1]}"}
            norm = " " + " ".join(re.sub(r"[^a-z0-9]+", " ", folded).split()) + " "
            if any(f" {v} " in norm for v in variants):
                found["person"] = f"registered role holder named on the site: {person}"
                break
    return found


def safe_link(url: Any, site_host: str | None = None) -> str | None:
    """An http(s) URL on a public-looking host (and on the company's domain when given), else None."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith(".local") or re.fullmatch(r"[\d.]+|\[?[0-9a-f:]+\]?", host):
        return None
    if site_host and registered_domain(host) != site_host:
        return None
    return url


def valid_orgnr(number: str) -> bool:
    if len(number) != 9 or not number.isdigit() or number[0] not in "89":
        return False
    total = sum(int(d) * w for d, w in zip(number[:8], WEIGHTS))
    check = 11 - total % 11
    if check == 11:
        check = 0
    return check != 10 and check == int(number[8])


def org_numbers_in(text: str) -> dict[str, str]:
    """Valid organisation numbers found in text, mapped to a short context span."""
    found: dict[str, str] = {}
    for match in ORG_CANDIDATE.finditer(text):
        number = "".join(match.groups())
        if number in found or not valid_orgnr(number):
            continue
        start, end = max(0, match.start() - 60), min(len(text), match.end() + 30)
        found[number] = re.sub(r"\s+", " ", text[start:end]).strip()
    return found


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def name_tokens(legal_name: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9æøåäöüé]+", legal_name.lower().replace("&", " og "))
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    while tokens and tokens[0] in ("stiftelsen",):
        tokens.pop(0)
    return tokens


def _ascii_variants(token: str) -> list[str]:
    a = token.replace("æ", "ae").replace("ø", "o").replace("å", "a").replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("é", "e")
    b = token.replace("æ", "ae").replace("ø", "oe").replace("å", "aa").replace("ä", "ae").replace("ö", "oe").replace("ü", "u").replace("é", "e")
    return list(dict.fromkeys([a, b]))


GEO_SUFFIXES = {"norge", "norway", "nordic", "norden", "scandinavia", "skandinavia"}


def guess_domains(legal_name: str, limit: int = 8) -> list[str]:
    """Candidate domains derived from a legal name, most likely first.

    The whole name comes first (joined, hyphenated, both æøå folds, IDN, .com, -as.no); then
    the name without generic words (holding, invest, eiendom, gruppen, drift, norge ...) when at
    least one distinctive word is left. Every guess is only a candidate: it is DNS-checked and
    published only on the identity rules in this module.
    """
    tokens = name_tokens(legal_name)
    if not tokens:
        return []
    if len("".join(tokens)) < 3:
        return []
    without_og = [t for t in tokens if t not in ("og", "and")]
    stripped = [t for t in without_og if t not in STRIP_GUESS_WORDS and t not in GEO_SUFFIXES]
    if stripped and len("".join(stripped)) < 3:
        stripped = []

    def fold(token_set: list[str], transform: int) -> list[str]:
        return [(_ascii_variants(t) + _ascii_variants(t))[transform] for t in token_set]

    out: list[str] = []

    def add(domain: str) -> None:
        label = domain.rsplit(".", 1)[0]
        if label.endswith("-as"):
            label = label[:-3]
        if 3 <= len(label) <= 40 and re.fullmatch(r"[a-z0-9\-]+|xn--[a-z0-9\-]+", domain.rsplit(".", 1)[0]) and domain not in out:
            out.append(domain)

    full = fold(tokens, 0)
    add("".join(full) + ".no")
    if len(full) > 1:
        add("-".join(full) + ".no")
    alt = fold(tokens, 1)
    if alt != full:
        add("".join(alt) + ".no")
    unicode_joined = "".join(tokens)
    if re.search(r"[æøå]", unicode_joined):
        try:
            add(unicode_joined.encode("idna").decode("ascii") + ".no")
        except UnicodeError:
            pass
    if without_og != tokens and len(without_og) >= 2:
        add("".join(fold(without_og, 0)) + ".no")
    add("".join(full) + ".com")
    add("".join(fold(without_og, 0)) + "-as.no")
    if stripped and stripped != without_og:
        short = fold(stripped, 0)
        add("".join(short) + ".no")
        if len(short) > 1:
            add("-".join(short) + ".no")
        add("".join(short) + ".com")
        alt_short = fold(stripped, 1)
        if alt_short != short:
            add("".join(alt_short) + ".no")
    if len(without_og) >= 3:
        add("".join(fold(without_og[:2], 0)) + ".no")
    return out[:limit]


def normalise_site_url(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().split()[0]
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value.lstrip("/")
    parts = urlsplit(value)
    if not parts.hostname or "." not in parts.hostname:
        return None
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def registered_domain(host: str) -> str:
    host = host.lower().removeprefix("www.")
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_directory(host: str) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in DIRECTORY_HOSTS)


def visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def parse(snap: Snapshot) -> tuple[BeautifulSoup, str, str]:
    html = snap.text()
    soup = BeautifulSoup(html, "lxml")
    text_soup = BeautifulSoup(html, "lxml")
    for tag in text_soup(["script", "style", "noscript", "template", "svg"]):
        if tag.name == "script" and "ld+json" in (tag.get("type") or "").lower():
            continue
        tag.decompose()
    return soup, html, re.sub(r"\s+", " ", text_soup.get_text(" ", strip=True))


def json_ld_items(soup: BeautifulSoup) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if "@graph" in item and isinstance(item["@graph"], list):
                    stack.extend(item["@graph"])
                items.append(item)
            elif isinstance(item, list):
                stack.extend(item)
    return items


def _types(item: dict[str, Any]) -> set[str]:
    value = item.get("@type")
    if isinstance(value, list):
        return {str(v) for v in value}
    return {str(value)} if value else set()


def social_profile(href: str) -> tuple[str, str] | None:
    href = href.strip()
    for platform, pattern in SOCIAL_PATTERNS:
        match = pattern.match(href)
        if match:
            parts = urlsplit(href)
            path = parts.path.rstrip("/")
            if platform == "facebook" and (path.count("/") > 3 or path.lower() in ("", "/")):
                return None
            query = parts.query if path.lower().endswith("profile.php") else ""
            clean = urlunsplit(("https", parts.netloc.lower().replace("m.facebook", "www.facebook").replace("web.facebook", "www.facebook"), path, query, ""))
            return platform, clean
    return None


def normalise_phone(raw: str) -> str | None:
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("+47"):
        rest = digits[3:]
    elif digits.startswith("+"):
        return digits if 8 <= len(digits) <= 16 else None
    else:
        rest = digits
    if len(rest) == 8 and rest[0] in "23456789":
        return "+47 " + " ".join([rest[0:3], rest[3:5], rest[5:8]]) if rest[0] in "489" else "+47 " + " ".join([rest[0:2], rest[2:4], rest[4:6], rest[6:8]])
    return None


EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class SiteVisit:
    def __init__(self, fetcher: Fetcher, rec: CompanyRecord, deadline: float) -> None:
        self.fetcher = fetcher
        self.rec = rec
        self.deadline = deadline
        self.pages: list[tuple[Snapshot, BeautifulSoup, str]] = []

    def time_left(self) -> float:
        return self.deadline - time.monotonic()

    def fetch_page(self, url: str) -> tuple[Snapshot, BeautifulSoup, str] | None:
        if self.time_left() < 3:
            return None
        snap = self.fetcher.get(url, retries=0, timeout=(5.0, min(12.0, max(3.0, self.time_left()))))
        if not snap.ok or "html" not in snap.content_type.lower() and not snap.body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
            return None
        soup, html, text = parse(snap)
        page = (snap, soup, text)
        self.pages.append(page)
        return page


def _org_on_page(text: str, html: str, org: str) -> tuple[bool, str | None, dict[str, str]]:
    numbers = org_numbers_in(text)
    if org not in numbers:
        # Some footers keep the number in an attribute; only our own number is taken from markup.
        extra = org_numbers_in(re.sub(r"<[^>]+>", " ", re.sub(r'<(script|style)[^>]*>[\s\S]*?</\1>', " ", html, flags=re.I)))
        if org in extra:
            numbers[org] = extra[org]
    return org in numbers, numbers.get(org), numbers


DISCOVERY_REQUESTS = 11   # requests the search for a site may spend (DNS lookups are free)
HARVEST_REQUESTS = 16     # further requests a verified site may draw for contact, sitemap, careers and news pages
MAX_GUESSES = 12          # name-derived domains DNS-checked per company
MAX_GUESS_FETCHES = 5     # of those, live candidates actually fetched
DECLARED_ORIGINS = ("registry_listed", "subunit_registry_listed", "nav_employer_homepage")
EMAIL_ORIGINS = ("registry_email_domain", "subunit_email_domain")


def _host_key(url: str) -> str:
    return registered_domain(urlsplit(url).hostname or "")


def _directory_url(url: str) -> bool:
    parts = urlsplit(url)
    if is_directory(parts.hostname or ""):
        return True
    return any(seg in DIRECTORY_PATH_SEGMENTS for seg in parts.path.lower().split("/") if seg)


def website_candidates(rec: CompanyRecord, only_nav: bool = False) -> list[tuple[str, str]]:
    """Every site to check for this company, in the order they should be tried: declared sites
    (register, subunits, NAV employer record), e-mail domains, declared caches, then name guesses."""
    entity = rec.context.get("entity") or {}
    legal_name = entity.get("navn") or ""
    subunits = rec.context.get("subunits") or []
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(url: str | None, origin: str) -> None:
        if not url:
            return
        key = _host_key(url)
        if key and key not in seen:
            seen.add(key)
            candidates.append((url, origin))

    registry_site = normalise_site_url(entity.get("hjemmeside"))
    if registry_site and not social_profile(registry_site):
        add(registry_site, "registry_listed")
    for unit in subunits:
        site = normalise_site_url(unit.get("website"))
        if site and not social_profile(site):
            add(site, "subunit_registry_listed")
    nav = rec.context.get("nav_employer") or {}
    if nav.get("homepage"):
        site = normalise_site_url(nav["homepage"][0])
        if site and not social_profile(site):
            add(site, "nav_employer_homepage")
    if only_nav:
        return [c for c in candidates if c[1] == "nav_employer_homepage"]
    housing = RegisterSignals(rec).housing
    emails = [(entity.get("epostadresse"), "registry_email_domain")] + [(u.get("email"), "subunit_email_domain") for u in subunits]
    for email, origin in emails:
        email = (email or "").strip().lower()
        if "@" not in email or housing:
            continue  # a housing co-op's e-mail is its property manager's
        domain = registered_domain(email.rsplit("@", 1)[1])
        if not domain or "." not in domain or domain in FREE_MAIL or is_directory(domain):
            continue
        if RegisterSignals.domain_shared(domain):
            continue  # an accountant's or property manager's domain, held by five or more entities
        add(f"https://{domain}/", origin)
    if caches is not None:
        try:
            cached = caches.candidates(rec.organisation_number)
        except Exception:
            cached = []
        for item in cached:
            if item.get("kind") == "website":
                site = normalise_site_url(item.get("url"))
                if site and not social_profile(site):
                    add(site, f"cache_{item.get('source') or 'unknown'}")
    guesses: list[str] = []

    def guess(name: str, limit: int) -> None:
        for domain in guess_domains(name, limit=limit):
            if domain not in guesses:
                guesses.append(domain)

    guess(legal_name, 9)
    names_by_origin: list[tuple[str, str]] = [(d, "name_derived_domain") for d in guesses]
    former = sorted(entity.get("historiskeNavn") or [], key=lambda h: str(h.get("tilDato") or ""), reverse=True)
    for item in former[:2]:
        if item.get("navn") and name_tokens(item["navn"]) != name_tokens(legal_name):
            before = len(guesses)
            guess(item["navn"], 3)
            names_by_origin += [(d, "former_name_domain") for d in guesses[before:]]
    for unit in subunits[:4]:
        unit_name = unit.get("name") or ""
        if unit_name and name_tokens(unit_name) != name_tokens(legal_name):
            before = len(guesses)
            guess(unit_name, 2)
            names_by_origin += [(d, "subunit_name_domain") for d in guesses[before:]]
    for domain, origin in names_by_origin[:MAX_GUESSES]:
        add(f"https://{domain}/", origin)
    return candidates


def _prefetch_dns(hosts: list[str], timeout: float = 7.0) -> None:
    """Resolve the candidate hosts in parallel, so dead guesses cost neither a request nor a serial wait."""
    import threading

    threads = [threading.Thread(target=resolve_public, args=(h,), daemon=True) for h in dict.fromkeys(hosts)]
    for t in threads:
        t.start()
    stop = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, stop - time.monotonic()))


def _contact_links(snap: Snapshot, soup: BeautifulSoup) -> list[str]:
    host = urlsplit(snap.final_url).hostname
    scored: list[tuple[int, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(snap.final_url, anchor["href"]).split("#")[0]
        label = f'{anchor.get_text(" ", strip=True)} {anchor["href"]}'
        if urlsplit(href).hostname != host or href == snap.final_url.split("#")[0] or not CONTACT_HINTS.search(label):
            continue
        rank = 0 if re.search(r"kontakt|contact", label, re.I) else 1 if re.search(r"om-oss|om_oss|omoss|om oss|about", label, re.I) else 2
        if all(href != h for _, h in scored):
            scored.append((rank, href))
    return [h for _, h in sorted(scored, key=lambda x: x[0])]


def _stem_matches_name(site_domain: str, names: list[str], min_len: int = 4) -> bool:
    """The domain spells one of the names in full ("vetnesinvest.no" for VETNES INVEST AS)."""
    stem = site_domain.split(".")[0].replace("-", "")
    for name in names:
        tokens = [t for t in name_tokens(name) if t not in ("og", "and")]
        for variant in (0, 1):
            joined = "".join((_ascii_variants(t) + _ascii_variants(t))[variant] for t in tokens)
            if len(joined) >= min_len and joined == stem:
                return True
    return False


def _foreign_cctld(host: str) -> bool:
    tld = host.rsplit(".", 1)[-1].lower() if "." in host else ""
    return len(tld) == 2 and tld not in NOT_FOREIGN_CCTLDS


class _Evidence:
    """Signals gathered over the pages read for one candidate site."""

    def __init__(self) -> None:
        self.numbers: dict[str, tuple[str, str, Snapshot]] = {}
        self.contacts: dict[str, tuple[str, Snapshot]] = {}
        self.texts: list[str] = []

    def add(self, sig: RegisterSignals, org: str, snap: Snapshot, soup: BeautifulSoup, text: str) -> None:
        rank = {"owner": 3, "bare": 2, "mention": 1, "credit": 0}
        html = snap.text()
        contexts = number_contexts(text)
        if org not in contexts:
            markup = number_contexts(_markup_text(html))
            if org in markup:
                contexts[org] = markup[org]
        for number, (kind, span) in contexts.items():
            if number not in self.numbers or rank[kind] > rank[self.numbers[number][0]]:
                self.numbers[number] = (kind, span, snap)
        for key, span in contact_signals(sig, text, html, soup).items():
            self.contacts.setdefault(key, (span, snap))
        self.texts.append(text)

    def own(self, org: str) -> tuple[str, str, Snapshot] | None:
        return self.numbers.get(org)


def _register_rules(sig: RegisterSignals, ev: _Evidence, origin: str, site_domain: str, final_host: str, parts: list[tuple[str, str]],
                    blocked: bool) -> tuple[str, str, Snapshot] | None:
    """``register_contact_on_site`` or ``name_and_register_signal`` for a site without our number, else None."""
    if blocked or sig.housing or _foreign_cctld(final_host):
        return None
    our_cores = {" ".join(t for t in core_tokens(n) if t not in ("og", "and")) for n in sig.names}
    mentions = entity_mentions(parts)
    if mentions and not set(mentions) & our_cores:
        return None  # the site's copyright or name line names a different legal entity
    name_hit = next(((label, part) for label, part in parts for n in sig.names if name_in_part(n, part)), None)
    stem_full = _stem_matches_name(site_domain, sig.names, min_len=3)
    contacts = ev.contacts
    # Rule 1: the registered phone or e-mail address, not shared with other register entities.
    screen = {"phone": RegisterSignals.screen_ready("phone"), "email": RegisterSignals.screen_ready("email")}
    own_domain = site_domain in sig.email_domains
    contact_hit = None
    if "phone8" in contacts:
        unshared = (not RegisterSignals.phone_shared(contacts["phone8"][0])) if screen["phone"] else own_domain
        if unshared:
            contact_hit = contacts["phone"]
    if contact_hit is None and "email_address" in contacts:
        domain = registered_domain(contacts["email_address"][0].rsplit("@", 1)[1])
        unshared = (not RegisterSignals.domain_shared(domain)) if screen["email"] else (own_domain and domain == site_domain and domain not in FREE_MAIL)
        if unshared:
            contact_hit = contacts["email"]
    if contact_hit is not None:
        # A holding or group company shares contact details with its operating sister or parent, and an
        # e-mail domain is no independent proof of the site it was derived from: these need the name too.
        need_name = sig.holding or sig.in_group or (origin in EMAIL_ORIGINS and not screen["email"])
        if not need_name or name_hit or stem_full:
            return "register_contact_on_site", contact_hit[0], contact_hit[1]
    # Rule 2: the whole legal name in the homepage's identity text, plus one register signal on the site.
    if name_hit:
        legal_core = [t for t in core_tokens(sig.names[0] if sig.names else "") if t not in ("og", "and")]
        weak_name = len(legal_core) == 1 and (len(legal_core[0]) < 5 or legal_core[0] in GENERIC_NAME_WORDS)
        order = ["street", "phone", "email_domain", "person"] + ([] if weak_name else ["postcode_town"])
        for key in order:
            if key in contacts:
                span, snap = contacts[key]
                return "name_and_register_signal", f'{name_hit[0]} = "{name_hit[1][:100]}"; {span}', snap
    return None


def _assess(fetcher: Fetcher | None, rec: CompanyRecord, visit: SiteVisit, sig: RegisterSignals, origin: str,
            page: tuple[Snapshot, BeautifulSoup, str]) -> tuple[str, Snapshot, str | None, set[str], set[str]] | None:
    """Decide whether a fetched candidate homepage is this entity's site.

    Returns (identity_method, identity_snapshot, span, foreign_numbers, group_numbers) or None, and
    records why an ambiguous candidate was refused.
    """
    org = rec.organisation_number
    snap, soup, text = page
    group_orgs: set[str] = set(rec.context.get("group_orgs") or set())
    own_numbers = {org} | {s.get("organisation_number") for s in rec.context.get("subunits") or [] if s.get("organisation_number")}
    declared = origin in DECLARED_ORIGINS
    final_host = urlsplit(snap.final_url).hostname or ""
    site_domain = registered_domain(final_host)
    ev = _Evidence()
    ev.add(sig, org, snap, soup, text)
    parts = identity_parts(soup, text)

    def proven() -> bool:
        own = ev.own(org)
        return bool(own and own[0] == "owner") or _register_rules(sig, ev, origin, site_domain, final_host, parts, False) is not None

    folded_words = set(re.findall(r"[a-z0-9]+", _fold(text)))
    legal_core = [t for t in core_tokens(sig.names[0] if sig.names else "") if t not in ("og", "and")]
    plausible = declared or bool(ev.numbers.get(org)) or bool(ev.contacts) or (legal_core and set(legal_core) <= folded_words)
    if plausible and not proven():
        for link in _contact_links(snap, soup)[: 3 if declared else 2]:
            if visit.time_left() < 4:
                break
            try:
                extra = visit.fetch_page(link)
            except Exception:
                continue
            if extra:
                ev.add(sig, org, *extra)
                if proven():
                    break

    own = ev.own(org)
    related = own_numbers | group_orgs
    foreign_primary = {n for n, (kind, _, _) in ev.numbers.items() if n not in related and kind == "owner"}
    foreign_any = {n for n, (kind, _, _) in ev.numbers.items() if n not in related and kind in ("owner", "bare")}
    group_on_page = {n for n, (kind, _, _) in ev.numbers.items() if n in group_orgs and n not in own_numbers and kind in ("owner", "bare")}
    if rec.context.get("group_root") == org:
        group_on_page = set()  # the group's parent: its own site naturally lists its subsidiaries
    own_ok = own is not None and own[0] in ("owner", "bare") if declared else own is not None and own[0] == "owner"

    if not declared:
        if own_ok:
            if len(foreign_any) >= 3:
                rec.error("website", "guessed site shows this organisation number among several others; treated as ambiguous", snap.final_url)
                rec.context["website_ambiguous"] = snap.final_url
                return None
            return "organisation_number_on_site", own[2], own[1], foreign_any, group_on_page
        if own is not None and own[0] == "bare":
            rec.error("website", "organisation number found without an ownership label; not treated as this entity's site", snap.final_url)
            rec.context["website_ambiguous"] = snap.final_url
            rec.context["website_ambiguous_reason"] = f"{snap.final_url} mentions this organisation number but not as its own registration"
            return None
        blocked = bool(foreign_primary or group_on_page or len(foreign_any) >= 3)
        rule = _register_rules(sig, ev, origin, site_domain, final_host, parts, blocked)
        if rule:
            return rule[0], rule[2], rule[1], foreign_any, group_on_page
        return None

    base = "nav_listed" if origin == "nav_employer_homepage" else "registry_listed"
    if own_ok:
        return f"{base}_and_organisation_number_on_site", own[2], own[1], foreign_any, group_on_page
    if foreign_primary or (foreign_any and not group_on_page) or group_on_page:
        other = sorted(foreign_primary or foreign_any or group_on_page)[:3]
        reason = f"the listed site {snap.final_url} shows organisation number(s) {', '.join(other)} and not this one" + (" (a member of the same corporate group)" if group_on_page and not foreign_primary else "")
        rec.context["website_ambiguous"] = snap.final_url
        rec.context["website_ambiguous_reason"] = reason
        return None
    legal_name = sig.names[0] if sig.names else ""
    corroboration = name_corroboration(legal_name, rec.context.get("subunits") or [], text, snap.final_url)
    if corroboration:
        return f"{base}_name_on_site", snap, corroboration, foreign_any, group_on_page
    # A JavaScript shell or a short title: the name in the page's metadata, or the domain spelling it.
    meta_text = " ".join(part for _, part in parts)
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            meta_text += " " + str(meta["content"])
    meta_words = set(re.findall(r"[a-z0-9]+", _fold(meta_text)))
    distinctive = [t for t in legal_core if len(t) >= 2]
    if distinctive and set(distinctive) <= meta_words:
        return f"{base}_name_in_metadata", snap, f"name words {', '.join(distinctive)} in the page title/metadata: {meta_text[:160]}", foreign_any, group_on_page
    if _stem_matches_name(site_domain, sig.names, min_len=3):
        return f"{base}_domain_spells_name", snap, f"the register-listed domain {site_domain} spells the name {legal_name}", foreign_any, group_on_page
    rule = _register_rules(sig, ev, origin, site_domain, final_host, parts, False)
    if rule:
        return f"{base}_{rule[0]}", rule[2], rule[1], foreign_any, group_on_page
    if len(text) < 600 and fetcher is not None:
        sitemap = _sitemap_size(fetcher, visit, snap.final_url)
        if sitemap >= 5:
            return f"{base}_live_site", snap, f"register-listed site is live: its sitemap lists {sitemap} pages on {site_domain}", foreign_any, group_on_page
    rec.context["website_ambiguous"] = snap.final_url
    rec.context["website_ambiguous_reason"] = f"the listed site {snap.final_url} shows neither this organisation number nor the company name"
    return None


def _sitemap_size(fetcher: Fetcher, visit: SiteVisit, url: str) -> int:
    """Pages on the site's own domain listed in /sitemap.xml (a sitemap index counts its sitemaps)."""
    parts = urlsplit(url)
    domain = registered_domain(parts.hostname or "")
    try:
        snap = fetcher.get(f"{parts.scheme}://{parts.netloc}/sitemap.xml", retries=0, timeout=(5.0, min(10.0, max(3.0, visit.time_left()))))
    except Exception:
        return 0
    if not snap.ok:
        return 0
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", snap.text())
    return sum(1 for loc in locs if registered_domain(urlsplit(loc).hostname or "") == domain)


def _fetch_candidate(visit: SiteVisit, rec: CompanyRecord, url: str) -> tuple[tuple[Snapshot, BeautifulSoup, str] | None, str | None, bool, bool]:
    """Fetch a candidate homepage, trying www. and http:// when the bare https host does not answer.

    Returns (page, robots_reason, budget_hit, answered)."""
    parts = urlsplit(url)
    bare = (parts.hostname or "").removeprefix("www.")
    variants = [url]
    if not (parts.hostname or "").startswith("www."):
        variants.append(urlunsplit(("https", "www." + bare, parts.path or "/", parts.query, "")))
    variants.append(urlunsplit(("http", parts.hostname or bare, parts.path or "/", parts.query, "")))
    timeouts = 0
    for attempt_url in variants:
        if visit.time_left() < 5:
            break
        if timeouts >= 2:
            break  # two hosts that accept no connection: plain http on the same machine will not either
        try:
            page = visit.fetch_page(attempt_url)
        except RobotsDisallowed:
            return None, f"robots.txt disallows {attempt_url}", False, True
        except RequestBudgetExhausted as exc:
            rec.error("website", f"request budget: {exc}", attempt_url)
            return None, None, True, False
        except UnsafeURL as exc:
            rec.error("website", f"unsafe or unresolvable URL skipped: {exc}", attempt_url)
            continue
        except Exception as exc:
            rec.error("website", f"fetch failed: {type(exc).__name__}: {str(exc)[:160]}", attempt_url)
            if "Timeout" in type(exc).__name__:
                timeouts += 1
            continue
        # A reachable host that answered (even with an error page) needs no other variant.
        return page, None, False, True
    return None, None, False, False


def research_website(fetcher: Fetcher, rec: CompanyRecord, deadline: float, only_nav: bool = False) -> None:
    entity = rec.context.get("entity") or {}
    registry_site = normalise_site_url(entity.get("hjemmeside"))
    if registry_site and social_profile(registry_site) and not only_nav:
        _publish_registry_social(rec, registry_site)
        registry_site = None
    _publish_nav_profiles(rec, rec.context.get("nav_employer") or {})
    if not only_nav:
        _publish_cache_socials(rec)
    candidates = website_candidates(rec, only_nav=only_nav)
    if only_nav and not candidates:
        return
    sig = RegisterSignals(rec)
    _prefetch_dns([urlsplit(u).hostname or "" for u, o in candidates if o not in DECLARED_ORIGINS])

    checked: list[str] = []
    visit = SiteVisit(fetcher, rec, deadline)
    blocked_reason = None
    budget_hit = False
    out_of_time = False
    declared_unread = False
    guess_fetches = 0
    for url, origin in candidates:
        if visit.time_left() < 5:
            out_of_time = True
            break
        declared = origin in DECLARED_ORIGINS
        if not declared:
            if fetcher is not None and fetcher.company_used() >= DISCOVERY_REQUESTS:
                break
            if guess_fetches >= MAX_GUESS_FETCHES:
                break
        host = urlsplit(url).hostname or ""
        if _directory_url(url):
            continue
        if not declared and not resolve_public(host):
            continue
        checked.append(url)
        page, robots_reason, budget_hit, answered = _fetch_candidate(visit, rec, url)
        if budget_hit:
            break
        if robots_reason:
            if declared:
                blocked_reason = robots_reason
            continue
        if page is None:
            if declared:
                declared_unread = True
            continue
        if not declared:
            guess_fetches += 1
        snap, soup, text = page
        if _directory_url(snap.final_url):
            continue
        lowered = text.lower()
        if len(text) < 1500 and any(marker in lowered for marker in PARKED_MARKERS):
            rec.error("website", "candidate domain is parked or a hosting placeholder", url)
            continue
        decision = _assess(fetcher, rec, visit, sig, origin, page)
        if decision is None:
            continue
        identity, identity_snap, span, foreign, group_on_page = decision
        _publish_site(rec, visit, snap, identity_snap, identity, origin, span, foreign, group_on_page)
        return

    if only_nav:
        return
    if not (out_of_time or budget_hit or blocked_reason or declared_unread):
        # Every declared candidate was read and none is this entity's site: anything read from a
        # previously verified site no longer stands. An unreachable site proves nothing.
        rec.checked_fields.update({"official_website", "social_profile", "website_phone", "website_email", "public_brand_name", "self_description", "careers_page", "company_news"})
    if out_of_time and not rec.claims_for("official_website"):
        rec.set_state("official_website", "failed", "time budget ran out before every website candidate was checked", checked_sources=checked)
    elif budget_hit and not rec.claims_for("official_website"):
        rec.set_state("official_website", "failed", "request budget reached before the website could be verified", checked_sources=checked)
    elif blocked_reason and not rec.claims_for("official_website"):
        rec.set_state("official_website", "blocked", blocked_reason, checked_sources=checked)
    elif rec.context.get("website_ambiguous"):
        rec.set_state("official_website", "ambiguous", rec.context.get("website_ambiguous_reason") or "a candidate site shows this organisation number among several unrelated ones", checked_sources=checked)
    elif registry_site:
        rec.set_state("official_website", "not_available", "the register lists a website but it could not be reached or is parked", checked_sources=checked)
    else:
        rec.set_state("official_website", "not_available", "no register-listed website, and no candidate domain shows this organisation number or the register's contact details", checked_sources=checked)
    unverified = "ambiguous" if rec.context.get("website_ambiguous") else "not_available"
    if not rec.claims_for("company_profiles"):
        rec.set_state("company_profiles", unverified, "no verified company website to read company-owned profiles from", checked_sources=checked)
    rec.set_state("public_brand", unverified, "no verified company website to read a public brand or self-description from", checked_sources=checked)
    if not rec.claims_for("contact_channels"):
        rec.set_state("contact_channels", "not_available", "no phone in the register and no verified website", checked_sources=checked)


def _publish_registry_social(rec: CompanyRecord, url: str) -> None:
    platform, clean = social_profile(url) or (None, None)
    if not platform:
        return
    evidence = [e for c in rec.claims if c["field"] == "registry_website" for e in c["evidence_ids"]]
    if not evidence:
        return
    rec.add_claim(
        family="company_profiles",
        field_name="social_profile",
        value={"platform": platform, "url": clean, "handle": urlsplit(clean).path.strip("/")},
        key=[platform, clean.lower()],
        evidence_ids=evidence,
        confidence=0.95,
        method="registry_listed_profile",
        label=f"{platform.title()} profile",
    )
    rec.set_state("company_profiles", "available", "profile listed as the website in the Entity Register")


def _publish_nav_profiles(rec: CompanyRecord, nav: dict[str, Any]) -> None:
    for key in ("facebookPage", "linkedinPage", "twitterAddress"):
        if key not in nav:
            continue
        value, snap, span = nav[key]
        url = value if value.startswith("http") else "https://" + value.lstrip("/")
        profile = social_profile(url)
        if not profile:
            continue
        platform, clean = profile
        ev = rec.evidence_from_snapshot(snap, source_class="public_job_board", span=span, method="employer_profile_in_verified_job_ad", access_policy="arbeidsplassen.nav.no public ad page; robots.txt allows all")
        rec.add_claim(
            family="company_profiles",
            field_name="social_profile",
            value={"platform": platform, "url": clean, "handle": urlsplit(clean).path.strip("/")},
            key=[platform, clean.lower()],
            evidence_ids=[ev],
            confidence=0.93,
            method="employer_profile_in_verified_job_ad",
            label=f"{platform.title()} profile",
        )
        rec.set_state("company_profiles", "available", "profile declared by the employer in an org-number-verified NAV job ad")


def _publish_site(
    rec: CompanyRecord,
    visit: SiteVisit,
    home: Snapshot,
    identity_snap: Snapshot,
    identity: str,
    origin: str,
    span: str | None,
    foreign: set[str],
    group_on_page: set[str],
) -> None:
    parts = urlsplit(home.final_url)
    site_url = urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
    evidence = []
    if span:
        evidence.append(rec.evidence_from_snapshot(identity_snap, source_class="company_owned_website", span=span, method=identity, url=identity_snap.final_url))
    if not span or identity_snap is not home:
        evidence.append(rec.evidence_from_snapshot(home, source_class="company_owned_website", span=f"homepage reached: {home.final_url}", method="registry_listed_site_fetch" if origin in DECLARED_ORIGINS else "candidate_site_fetch", url=home.final_url))
    registry_claims = [c for c in rec.claims if c["field"] == "registry_website"] if origin in ("registry_listed",) else []
    for claim in registry_claims:
        evidence.extend(claim["evidence_ids"])
    exact = identity.endswith("organisation_number_on_site")
    relationship = None
    if identity.endswith("_group_site"):
        relationship = "group_site"
    elif identity.endswith("_other_entity_on_site"):
        relationship = "registry_listed_site_names_another_entity" if identity.startswith("registry") else "listed_site_names_another_entity"
    confidence = 0.99 if exact else 0.93 if "register_contact_on_site" in identity else 0.92 if "name_and_register_signal" in identity else 0.9 if identity.endswith("name_on_site") else 0.88 if identity.endswith(("name_in_metadata", "domain_spells_name")) else 0.8
    rec.add_claim(
        family="official_website",
        field_name="official_website",
        value={"url": site_url, "domain": registered_domain(parts.hostname or ""), "identity_method": identity, "discovered_via": origin},
        key=registered_domain(parts.hostname or ""),
        evidence_ids=evidence,
        confidence=confidence,
        method=identity,
        relationship=relationship,
        label="Official website",
    )
    rec.context["website"] = {"url": site_url, "identity": identity, "exact": exact, "relationship": relationship}
    rec.set_state("official_website", "available", f"website verified ({identity})")
    rec.checked_fields.add("official_website")
    if relationship in ("registry_listed_site_names_another_entity", "listed_site_names_another_entity"):
        rec.set_state("company_profiles", "ambiguous", "the register-listed site names a different organisation number; its profiles are not attributed to this entity")
        rec.set_state("public_brand", "ambiguous", "the register-listed site names a different organisation number; its brand is not attributed to this entity")
        rec.set_state("contact_channels", "available" if rec.claims_for("contact_channels") else "ambiguous", "site contact details not attributed: the site names another entity")
        return
    if visit.fetcher is not None:
        # A verified site is worth more of the run's request pool than a company with none.
        visit.fetcher.raise_company_cap(HARVEST_REQUESTS)
    from .harvest import _extract_site_facts

    _extract_site_facts(rec, visit, home, True, relationship)


# -- declared cache social profiles ---------------------------------------------------------
CACHE_SOCIAL_RULES = {
    ("wikidata", None): "wikidata_p2333_exact_orgnr",
    ("overture", "overture_email_exact"): "overture_email_exact",
    ("overture", "overture_website_domain"): "overture_website_domain",
}
_cache_social_owners: dict[str, set[str]] | None = None


def _social_owners() -> dict[str, set[str]]:
    """Canonical social URL -> organisation numbers the caches attach it to (built once)."""
    global _cache_social_owners
    if _cache_social_owners is None:
        owners: dict[str, set[str]] = {}
        try:
            index = caches._load("candidates", caches._build_candidates)  # type: ignore[union-attr]
            for orgnr, rows in index.items():
                for row in rows:
                    if row[1] == "social":
                        profile = social_profile(row[0])
                        if profile:
                            owners.setdefault(profile[1].lower().rstrip("/"), set()).add(orgnr)
        except Exception:
            owners = {}
        _cache_social_owners = owners
    return _cache_social_owners


def _publish_cache_socials(rec: CompanyRecord) -> None:
    """Social profiles a declared open dataset ties to this organisation number by an exact join.

    Wikidata P2333 (organisation number) is an exact join; Overture places count only when matched
    by the registered e-mail or website domain. Name-and-postcode matches are never published from
    the cache. A profile the cache also gives another organisation is skipped.
    """
    if caches is None:
        return
    try:
        items = caches.candidates(rec.organisation_number)
    except Exception:
        return
    for item in items:
        if item.get("kind") != "social":
            continue
        source = item.get("source") or ""
        rule = item.get("match_rule") or None
        method = CACHE_SOCIAL_RULES.get((source, None)) if source == "wikidata" else CACHE_SOCIAL_RULES.get((source, rule))
        if not method:
            continue
        profile = social_profile(str(item.get("url") or ""))
        if not profile:
            continue
        platform, clean = profile
        if _social_owners().get(clean.lower().rstrip("/"), {rec.organisation_number}) - {rec.organisation_number}:
            continue
        record = item.get("source_record") or f"{source}:unknown"
        ev = rec.add_evidence(
            source_url=record,
            source_class="licensed_open_dataset",
            retrieved_at=(item.get("release_date") or utc_today()),
            content_sha256=None,
            claim_span=str(item.get("url")),
            extraction_method=method,
            access_policy="declared open dataset cache (see CACHE.md); Wikidata CC0, Overture CDLA-Permissive-2.0",
        )
        rec.add_claim(
            family="company_profiles",
            field_name="social_profile",
            value={"platform": platform, "url": clean, "handle": urlsplit(clean).path.strip("/")},
            key=[platform, clean.lower()],
            evidence_ids=[ev],
            confidence=0.85,
            method=f"open_dataset_{method}",
            label=f"{platform.title()} profile",
        )
        rec.set_state("company_profiles", "available", f"profile tied to this organisation number by {source} ({method})")
