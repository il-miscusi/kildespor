"""Read facts from a verified company website: brand, description, contacts,
company-owned social profiles, careers pages, job postings and dated news.

Split out of ``website.py`` so discovery/identity (website.py) and per-site
harvesting (this module) can evolve independently. ``website._publish_site``
calls ``extract_site_facts`` once a site's identity holds.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .http import Fetcher, RequestBudgetExhausted, RobotsDisallowed, Snapshot, UnsafeURL, utc_now
from .model import CompanyRecord
from .website import *  # noqa: F401,F403  (shared helpers: SiteVisit, safe_link, social_profile, ...)
from .website import _fold, _is_credit_link, _ld_employer_is_ours, _ld_org_is_ours, _name_matches, _names_of, _types  # noqa: F401


def _extract_site_facts(rec: CompanyRecord, visit: SiteVisit, home: Snapshot, exact: bool, relationship: str | None) -> None:
    site_host = registered_domain(urlsplit(home.final_url).hostname or "")
    pages = list(visit.pages)
    # Read the contact/about pages too when the identity was proven on the homepage.
    home_page = next((p for p in pages if p[0] is home), None)
    if home_page is not None:
        _, soup, _ = home_page
        extra_links: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = urljoin(home.final_url, anchor["href"]).split("#")[0]
            label = f'{anchor.get_text(" ", strip=True)} {anchor["href"]}'
            if registered_domain(urlsplit(href).hostname or "") == site_host and re.search(r"kontakt|contact|om-oss|about", label, re.I) and href not in extra_links:
                extra_links.append(href)
        fetched = {p[0].final_url for p in pages} | {p[0].requested_url for p in pages}
        for link in extra_links[:2]:
            if link in fetched:
                continue
            try:
                visit.fetch_page(link)
            except Exception:
                continue
    pages = [p for p in visit.pages if registered_domain(urlsplit(p[0].final_url).hostname or "") == site_host]

    socials: dict[str, tuple[str, Snapshot, str]] = {}
    phones: dict[str, tuple[Snapshot, str]] = {}
    emails: dict[str, tuple[Snapshot, str]] = {}
    career_links: list[tuple[str, Snapshot, str]] = []
    feeds: list[str] = []
    brand_candidates: list[tuple[str, Snapshot, str]] = []
    description: tuple[str, Snapshot] | None = None
    jobs_ld: list[tuple[dict[str, Any], Snapshot]] = []
    news_ld: list[tuple[dict[str, Any], Snapshot]] = []

    for snap, soup, _text in pages:
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            label = anchor.get_text(" ", strip=True)
            if href.lower().startswith("tel:"):
                phone = normalise_phone(href[4:])
                if phone:
                    phones.setdefault(phone, (snap, f"tel link: {href[:40]} {label[:40]}"))
                continue
            if href.lower().startswith("mailto:"):
                address = href[7:].split("?")[0].strip().lower()
                if EMAIL.match(address) and not address.endswith((".png", ".jpg")):
                    emails.setdefault(address, (snap, f"mailto link: {address}"))
                continue
            absolute = urljoin(snap.final_url, href)
            profile = social_profile(absolute)
            if profile:
                platform, url = profile
                if platform not in socials and not _is_credit_link(anchor):
                    socials[platform] = (url, snap, f"link on {snap.final_url}: {url}")
                continue
            if registered_domain(urlsplit(absolute).hostname or "") == site_host and CAREER_HINTS.search(f"{label} {href}") and len(career_links) < 3:
                if all(absolute.split("#")[0] != c[0] for c in career_links):
                    career_links.append((absolute.split("#")[0], snap, f"link '{label[:60]}' -> {absolute[:120]}"))
        for link in soup.find_all("link", href=True):
            rel = " ".join(link.get("rel") or []).lower()
            ltype = (link.get("type") or "").lower()
            if "alternate" in rel and ("rss" in ltype or "atom" in ltype):
                feed = urljoin(snap.final_url, link["href"])
                if registered_domain(urlsplit(feed).hostname or "") == site_host and feed not in feeds and "comments" not in feed:
                    feeds.append(feed)
        if snap is home:
            og_site = soup.find("meta", attrs={"property": "og:site_name"})
            if og_site and og_site.get("content"):
                brand_candidates.append((og_site["content"].strip(), snap, f'og:site_name = "{og_site["content"].strip()}"'))
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            if title:
                brand_candidates.append((title, snap, f'<title> = "{title[:120]}"'))
            for attrs in ({"name": "description"}, {"property": "og:description"}):
                meta = soup.find("meta", attrs=attrs)
                if meta and meta.get("content") and len(meta["content"].strip()) >= 25 and description is None:
                    description = (meta["content"].strip(), snap)
        for item in json_ld_items(soup):
            types = _types(item)
            if types & {"Organization", "Corporation", "LocalBusiness", "ProfessionalService", "Store", "Restaurant", "AutomotiveBusiness", "HomeAndConstructionBusiness", "MedicalBusiness", "LegalService", "FinancialService"} and _ld_org_is_ours(item, rec):
                same_as = item.get("sameAs")
                for url in same_as if isinstance(same_as, list) else [same_as] if same_as else []:
                    profile = social_profile(str(url))
                    if profile and profile[0] not in socials:
                        socials[profile[0]] = (profile[1], snap, f"JSON-LD sameAs: {profile[1]}")
                if item.get("telephone"):
                    phone = normalise_phone(str(item["telephone"]))
                    if phone:
                        phones.setdefault(phone, (snap, f'JSON-LD telephone: {item["telephone"]}'))
                if item.get("email"):
                    address = str(item["email"]).replace("mailto:", "").strip().lower()
                    if EMAIL.match(address):
                        emails.setdefault(address, (snap, f"JSON-LD email: {address}"))
                if item.get("name") and snap is home:
                    brand_candidates.insert(0, (str(item["name"]).strip(), snap, f'JSON-LD {sorted(types)[0]} name = "{item["name"]}"'))
            if "JobPosting" in types and _ld_employer_is_ours(item, rec):
                jobs_ld.append((item, snap))
            if types & {"NewsArticle", "BlogPosting", "Article", "PressRelease"} and item.get("datePublished"):
                news_ld.append((item, snap))

    company_label = "group_site" if relationship == "group_site" else None
    # Brand
    for name, snap, span in brand_candidates[:1]:
        clean = re.split(r"\s[|\-–—:]\s", name)[0].strip()
        if 2 <= len(clean) <= 80:
            ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=span, method="html_metadata", url=snap.final_url)
            rec.add_claim(family="public_brand", field_name="public_brand_name", value=clean, evidence_ids=[ev], confidence=0.85 if exact else 0.7, method="html_metadata", relationship=company_label, label="Public brand")
    if description:
        text, snap = description
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=f'meta description = "{text[:300]}"', method="html_metadata", url=snap.final_url)
        rec.add_claim(family="public_brand", field_name="self_description", value=text[:600], evidence_ids=[ev], confidence=0.9, method="html_metadata", relationship=company_label, label="Self-description (company website)")
    rec.set_state("public_brand", "available" if rec.claims_for("public_brand") else "not_available", "brand and description read from the verified website" if rec.claims_for("public_brand") else "the verified website carries no usable brand name or description")

    for platform, (url, snap, span) in sorted(socials.items()):
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=span, method="outbound_link_from_verified_site", url=snap.final_url)
        handle = urlsplit(url).path.strip("/")
        rec.add_claim(
            family="company_profiles",
            field_name="social_profile",
            value={"platform": platform, "url": url, "handle": handle},
            key=[platform, url.lower()],
            evidence_ids=[ev],
            confidence=0.92 if exact else 0.8,
            method="outbound_link_from_verified_site",
            relationship=company_label,
            label=f"{platform.title()} profile",
        )
    if socials:
        rec.set_state("company_profiles", "available", "company-owned profiles linked from the verified website")
    else:
        rec.set_state("company_profiles", "not_available", "the verified website links to no social or video profiles", checked_sources=[p[0].final_url for p in pages])

    for phone, (snap, span) in list(phones.items())[:4]:
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=span, method="tel_link", url=snap.final_url)
        rec.add_claim(family="contact_channels", field_name="website_phone", value=phone, key=phone.replace(" ", ""), evidence_ids=[ev], confidence=0.9, method="tel_link", relationship=company_label, label="Phone (website)")
    for address, (snap, span) in list(emails.items())[:4]:
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=span, method="mailto_link", url=snap.final_url)
        rec.add_claim(family="contact_channels", field_name="website_email", value=address, evidence_ids=[ev], confidence=0.9, method="mailto_link", relationship=company_label, label="E-mail (website)")
    if rec.claims_for("contact_channels"):
        rec.set_state("contact_channels", "available", "contact channels found")
    else:
        rec.set_state("contact_channels", "not_available", "no phone or e-mail in the register or on the verified website")

    for url, snap, span in career_links[:2]:
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=span, method="careers_link_on_verified_site", url=snap.final_url)
        rec.add_claim(family="hiring", field_name="careers_page", value=url, evidence_ids=[ev], confidence=0.85, method="careers_link_on_verified_site", relationship=company_label, label="Careers page")
        rec.context.setdefault("career_pages", []).append(url)
    today = utc_today()
    for item, snap in jobs_ld[:20]:
        title = item.get("title")
        if not title:
            continue
        valid_through = str(item.get("validThrough") or "")[:10]
        if valid_through and valid_through < today:
            continue
        posted = str(item.get("datePosted") or "")[:10] or None
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=f'JSON-LD JobPosting title = "{title}" datePosted = {posted}', method="json_ld_jobposting", url=snap.final_url)
        rec.add_claim(
            family="hiring",
            field_name="job_posting",
            value={"title": title, "url": safe_link(item.get("url"), site_host) or snap.final_url, "date_posted": posted, "valid_through": valid_through or None, "source": "company_website"},
            key=["site", title, posted],
            evidence_ids=[ev],
            confidence=0.9,
            method="json_ld_jobposting",
            effective_date=posted,
            label="Job posting",
        )
    for item, snap in news_ld[:10]:
        title = item.get("headline") or item.get("name")
        date = str(item.get("datePublished"))[:10]
        if not title:
            continue
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=f'JSON-LD article "{title}" datePublished = {date}', method="json_ld_article", url=snap.final_url)
        rec.add_claim(family="public_activity", field_name="company_news", value={"title": title, "url": safe_link(item.get("url"), site_host) or snap.final_url, "date": date, "source": "company_website"}, key=["news", title, date], evidence_ids=[ev], confidence=0.9, method="json_ld_article", effective_date=date, label="Company news")

    # Careers page job postings (JSON-LD only) and the site's own feed.
    for url, _snap, _span in career_links[:1]:
        if visit.time_left() < 6:
            break
        try:
            page = visit.fetch_page(url)
        except Exception:
            page = None
        if not page:
            continue
        c_snap, c_soup, _ = page
        for item in json_ld_items(c_soup):
            if "JobPosting" in _types(item) and item.get("title") and _ld_employer_is_ours(item, rec):
                expires = str(item.get("validThrough") or "")[:10]
                if expires and expires < today:
                    continue
                posted = str(item.get("datePosted") or "")[:10] or None
                ev = rec.evidence_from_snapshot(c_snap, source_class="company_owned_website", span=f'JSON-LD JobPosting title = "{item["title"]}"', method="json_ld_jobposting", url=c_snap.final_url)
                rec.add_claim(
                    family="hiring",
                    field_name="job_posting",
                    value={"title": item["title"], "url": safe_link(item.get("url"), site_host) or c_snap.final_url, "date_posted": posted, "valid_through": expires or None, "source": "company_website"},
                    key=["site", item["title"], posted],
                    evidence_ids=[ev],
                    confidence=0.9,
                    method="json_ld_jobposting",
                    effective_date=posted,
                    label="Job posting",
                )
    for feed in feeds[:1]:
        if visit.time_left() < 5:
            break
        _read_feed(rec, visit, feed, company_label)
    is_wordpress = any("wp-content" in p[0].text()[:400000] or "wp-json" in p[0].text()[:400000] for p in pages[:1])
    if is_wordpress and not any(c["field"] == "company_news" for c in rec.claims) and visit.time_left() > 5:
        _read_wordpress_posts(rec, visit, home, company_label)
    if not any(c["field"] == "company_news" for c in rec.claims) and visit.time_left() > 5 and home_page is not None:
        _read_news_listing(rec, visit, home, home_page[1], company_label)
    # The pages were read: these fields reflect the site as it is now.
    rec.checked_fields.update({"social_profile", "website_phone", "website_email", "public_brand_name", "self_description", "careers_page"})
    if visit.time_left() > 5:
        rec.checked_fields.add("company_news")


def _read_feed(rec: CompanyRecord, visit: SiteVisit, url: str, relationship: str | None) -> None:
    try:
        snap = visit.fetcher.get(url, retries=0, timeout=(5.0, 10.0))
    except Exception as exc:
        rec.error("public_activity", f"feed fetch failed: {exc}", url)
        return
    if not snap.ok:
        return
    soup = BeautifulSoup(snap.body, "xml")
    entries = soup.find_all(["item", "entry"])[:10]
    from email.utils import parsedate_to_datetime

    for entry in entries:
        title_tag = entry.find("title")
        title = title_tag.get_text(" ", strip=True) if title_tag else None
        link_tag = entry.find("link")
        link = None
        if link_tag is not None:
            link = link_tag.get("href") or link_tag.get_text(strip=True)
        date_tag = entry.find(["pubDate", "published", "updated", "dc:date", "date"])
        date = None
        stamp = None
        if date_tag is not None:
            raw = date_tag.get_text(strip=True)
            try:
                parsed = parsedate_to_datetime(raw)
                date, stamp = parsed.date().isoformat(), parsed.isoformat()
            except (TypeError, ValueError, IndexError):
                match = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
                date = match.group(1) if match else None
        if not title or not date:
            continue
        link = safe_link(link, registered_domain(urlsplit(url).hostname or ""))
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_feed", span=f'feed item "{title[:150]}" {date}', method="rss_atom_feed", url=url)
        rec.add_claim(
            family="public_activity",
            field_name="company_news",
            value={k: v for k, v in {"title": title[:300], "url": link, "date": date, "datetime": stamp or (raw if date_tag is not None else None), "source": "company_feed"}.items() if v},
            key=["news", title[:300], date],
            evidence_ids=[ev],
            confidence=0.92,
            method="rss_atom_feed",
            effective_date=date,
            relationship=relationship,
            label="Company news",
        )


def _read_wordpress_posts(rec: CompanyRecord, visit: SiteVisit, home: Snapshot, relationship: str | None) -> None:
    parts = urlsplit(home.final_url)
    url = urlunsplit((parts.scheme, parts.netloc, "/wp-json/wp/v2/posts", "per_page=10&_fields=date,link,title", ""))
    try:
        snap = visit.fetcher.get(url, retries=0, timeout=(5.0, 10.0), headers={"Accept": "application/json"})
    except Exception as exc:
        rec.error("public_activity", f"WordPress posts fetch failed: {type(exc).__name__}", url)
        return
    if not snap.ok:
        return
    try:
        posts = snap.json()
    except ValueError:
        return
    if not isinstance(posts, list):
        return
    for post in posts[:10]:
        if not isinstance(post, dict):
            continue
        title = re.sub(r"<[^>]+>", "", str((post.get("title") or {}).get("rendered") or "")).strip()
        title = BeautifulSoup(title, "lxml").get_text(" ", strip=True) if "&" in title else title
        date = str(post.get("date") or "")
        link = post.get("link")
        if not title or not re.match(r"\d{4}-\d{2}-\d{2}", date):
            continue
        link = safe_link(link, registered_domain(parts.hostname or ""))
        if not link:
            continue
        ev = rec.evidence_from_snapshot(snap, source_class="company_owned_website", span=f'post "{title[:150]}" date {date}', method="wordpress_posts_api", url=url)
        rec.add_claim(
            family="public_activity",
            field_name="company_news",
            value={"title": title[:300], "url": link, "date": date[:10], "datetime": date, "source": "company_website"},
            key=["news", title[:300], date[:10]],
            evidence_ids=[ev],
            confidence=0.92,
            method="wordpress_posts_api",
            effective_date=date[:10],
            relationship=relationship,
            label="Company news",
        )


NEWS_HINTS = re.compile(r"\b(nyheter|nyhet|aktuelt|news|blogg|blog|artikler|presse|press|siste nytt)\b", re.I)


def _dated_items(soup: BeautifulSoup, base_url: str, site_host: str) -> list[tuple[str, str, str, str]]:
    """(title, url, date, span) for links that sit in the same item as a <time datetime>."""
    items: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for time_tag in soup.find_all("time"):
        stamp = (time_tag.get("datetime") or "").strip()
        match = re.match(r"(\d{4}-\d{2}-\d{2})", stamp)
        if not match:
            continue
        container = time_tag
        for _ in range(4):
            if container.parent is None:
                break
            container = container.parent
            if container.name in ("article", "li") or any(k in " ".join(container.get("class") or []).lower() for k in ("post", "news", "article", "entry", "card", "item")):
                break
        link = None
        for anchor in container.find_all("a", href=True):
            text = anchor.get_text(" ", strip=True)
            if len(text) >= 12:
                link = anchor
                break
        if link is None:
            heading = container.find(["h1", "h2", "h3", "h4"])
            link = heading.find("a", href=True) if heading else None
        if link is None:
            continue
        url = urljoin(base_url, link["href"]).split("#")[0]
        if registered_domain(urlsplit(url).hostname or "") != site_host or url in seen:
            continue
        title = link.get_text(" ", strip=True)
        if len(title) < 12 or len(title) > 300:
            continue
        seen.add(url)
        items.append((title, url, match.group(1), f'"{title[:150]}" <time datetime="{stamp}">'))
    return items


def _read_news_listing(rec: CompanyRecord, visit: SiteVisit, home: Snapshot, home_soup: BeautifulSoup, relationship: str | None) -> None:
    site_host = registered_domain(urlsplit(home.final_url).hostname or "")
    found = _dated_items(home_soup, home.final_url, site_host)
    source = home
    if not found:
        target = None
        for anchor in home_soup.find_all("a", href=True):
            href = urljoin(home.final_url, anchor["href"]).split("#")[0]
            label = f'{anchor.get_text(" ", strip=True)} {urlsplit(href).path}'
            if registered_domain(urlsplit(href).hostname or "") == site_host and NEWS_HINTS.search(label) and href.rstrip("/") != home.final_url.rstrip("/"):
                target = href
                break
        if not target:
            return
        try:
            page = visit.fetch_page(target)
        except Exception:
            return
        if not page:
            return
        source = page[0]
        found = _dated_items(page[1], page[0].final_url, site_host)
    for title, url, date, span in found[:10]:
        ev = rec.evidence_from_snapshot(source, source_class="company_owned_website", span=span, method="news_listing_time_element", url=source.final_url)
        rec.add_claim(
            family="public_activity",
            field_name="company_news",
            value={"title": title, "url": url, "date": date, "source": "company_website"},
            key=["news", title[:300], date],
            evidence_ids=[ev],
            confidence=0.88,
            method="news_listing_time_element",
            effective_date=date,
            relationship=relationship,
            label="Company news",
        )
