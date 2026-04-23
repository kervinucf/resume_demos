from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterator

from HyperCoreSDK.python.client import HyperClient, HyperNotFound
from app.utils.dtos.NewsEvent import NewsArticle


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_date(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# RSS / Atom parsing
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(s: str) -> str:
    return _TAG_RE.sub("", html.unescape(s or "")).strip()


def parse_feed_bytes(raw: bytes) -> list[dict[str, Any]]:
    """
    Parse an RSS 2.0 or Atom feed payload into a list of raw item dicts:
        {"title", "link", "summary", "published", "guid"}
    """
    root = ET.fromstring(raw)

    items = root.findall(".//item")
    if items:
        out = []
        for it in items:
            title = strip_tags(it.findtext("title") or "")
            link = (it.findtext("link") or "").strip()
            summary = strip_tags(it.findtext("description") or "")
            published = (it.findtext("pubDate") or "").strip()
            guid = (it.findtext("guid") or link or title).strip()
            out.append({
                "title": title, "link": link, "summary": summary,
                "published": published, "guid": guid,
            })
        return out

    # Atom fallback
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in root.findall(".//a:entry", ns):
        title = strip_tags(e.findtext("a:title", default="", namespaces=ns) or "")
        link_el = e.find("a:link", ns)
        link = link_el.get("href") if link_el is not None else ""
        summary = strip_tags(
            e.findtext("a:summary", default="", namespaces=ns)
            or e.findtext("a:content", default="", namespaces=ns)
            or ""
        )
        published = (
            e.findtext("a:published", default="", namespaces=ns)
            or e.findtext("a:updated", default="", namespaces=ns)
        )
        guid = e.findtext("a:id", default="", namespaces=ns) or link
        out.append({
            "title": title, "link": link, "summary": summary,
            "published": published, "guid": guid,
        })
    return out


# ---------------------------------------------------------------------------
# Stable IDs
# ---------------------------------------------------------------------------

def stable_article_id(source: str, guid: str) -> str:
    digest = hashlib.sha1(f"{source}|{guid}".encode("utf-8")).hexdigest()[:16]
    return f"{source}-{digest}"


# ---------------------------------------------------------------------------
# Location matching via the geo index
# ---------------------------------------------------------------------------

def _name_from_key(key: str) -> str:
    # keys look like "new-york-city-5128581" → "new york city"
    stem = key.rsplit("-", 1)[0]
    return stem.replace("-", " ").strip()


def iter_country_locations(
    hc: HyperClient,
    country_code: str,
) -> Iterator[tuple[str, str]]:
    """Yield (key, name) pairs from geo.index.by.country_code.{CC}."""
    path = f"geo.index.by.country_code.{country_code.upper()}"
    page = 1
    while True:
        try:
            doc = hc.children(path, page=page, per_page=200)
        except HyperNotFound:
            return
        if not isinstance(doc, dict):
            return
        names = (doc.get("data") or {}).get("children") or []
        if not names:
            return
        for key in names:
            yield str(key), _name_from_key(str(key))
        state = doc.get("_state") or {}
        if page >= int(state.get("num_pages") or 1):
            return
        page += 1


def build_location_matcher(
    hc: HyperClient,
    country_code: str,
    *,
    min_len: int = 4,
) -> list[tuple[re.Pattern, str]]:
    """
    Build a cheap word-boundary regex matcher over location names in a
    country. Returns [(pattern, location_key), ...]. Names shorter than
    `min_len` are skipped to avoid matching common short words.
    """
    matchers: list[tuple[re.Pattern, str]] = []
    for key, name in iter_country_locations(hc, country_code):
        if len(name) < min_len:
            continue
        try:
            pat = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
        except re.error:
            continue
        matchers.append((pat, key))
    return matchers


def match_locations(
    text: str,
    matchers: list[tuple[re.Pattern, str]],
    *,
    max_hits: int = 5,
) -> list[str]:
    seen: list[str] = []
    for pat, key in matchers:
        if pat.search(text):
            if key not in seen:
                seen.append(key)
                if len(seen) >= max_hits:
                    break
    return seen


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def article_from_raw(
    raw: dict[str, Any],
    *,
    source: str,
    region: str,
    matchers: list[tuple[re.Pattern, str]] | None = None,
    fetched_at: str | None = None,
) -> NewsArticle | None:
    """
    Build a NewsArticle from a raw item dict produced by `parse_feed_bytes`.
    Returns None if the item lacks enough identity to be useful.
    """
    guid = (raw.get("guid") or raw.get("link") or raw.get("title") or "").strip()
    if not guid:
        return None

    pub_dt = parse_date(raw.get("published") or "") or now_utc()
    aid = stable_article_id(source, guid)
    fetched = fetched_at or now_utc().isoformat()

    text_for_match = f"{raw.get('title', '')}  {raw.get('summary', '')}"
    location_keys = match_locations(text_for_match, matchers) if matchers else []

    return NewsArticle(
        article_id=aid,
        source=source,
        region=region,
        title=raw.get("title") or "",
        link=raw.get("link") or "",
        summary=raw.get("summary") or "",
        published_at=pub_dt.isoformat(),
        fetched_at=fetched,
        location_keys=location_keys,
    )