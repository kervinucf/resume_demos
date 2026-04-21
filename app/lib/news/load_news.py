# HyperCoreSDK/python/load_news.py
"""
Pull RSS feeds into the hypergraph.

Per feed, per article:
  news.items.{region}.{source}.{YYYY}.{MM}.{DD}.{article_id}   — canonical
  news.latest.{region}.{source}                                — pointer
  geo.locations.{key}.refs.news.{YYYY}.{MM}.{DD}.{article_id}  — per matched location

Location matching is deliberately simple: substring-match article title +
summary against location names in a country's index. Swap in better NLP by
replacing match_locations().

Usage:
    python load_news.py --feed bbc https://feeds.bbci.co.uk/news/rss.xml
    python load_news.py --feed nyt https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml --region us --match-country US
    python load_news.py --all   # uses the bundled DEFAULT_FEEDS
"""
from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from HyperCoreSDK.python.client import (
    DEFAULT_URL,
    HyperClient,
    HyperNotFound,
)
from HyperCoreSDK.python.node import HyperCoreNode, announce_ref


DEFAULT_FEEDS = [
    # (region, source, url, match_country_or_None)
    ("world", "bbc",      "https://feeds.bbci.co.uk/news/world/rss.xml",          None),
    ("us",    "nyt",      "https://rss.nytimes.com/services/xml/rss/nyt/US.xml",  "US"),
    ("us",    "reuters",  "https://feeds.reuters.com/Reuters/domesticNews",       "US"),
]


# ---------------------------------------------------------------------------
# Time segments (same shape as weather)
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def date_segments(dt: datetime) -> tuple[str, str, str]:
    return f"{dt.year:04d}", f"{dt.month:02d}", f"{dt.day:02d}"


# ---------------------------------------------------------------------------
# Article node
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NewsArticle(HyperCoreNode):
    article_id: str
    source: str
    region: str
    title: str
    link: str
    summary: str
    published_at: str
    fetched_at: str
    location_keys: list[str]

    @classmethod
    def properties(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def to_kv(self) -> dict[str, Any]:
        data = asdict(self)
        data["model"] = "news-article"
        return data

    def snapshot_for_ref(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "link": self.link,
            "source": self.source,
            "published_at": self.published_at,
        }


# ---------------------------------------------------------------------------
# RSS parsing (stdlib)
# ---------------------------------------------------------------------------

def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(s or "")).strip()


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


def fetch_feed(url: str) -> list[dict[str, Any]]:
    req = urllib.request.Request(url, headers={
        "User-Agent": "HyperCore/1.0 (+https://example)",
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()

    root = ET.fromstring(raw)
    # RSS 2.0: channel/item. Atom: feed/entry. Handle both minimally.
    items = root.findall(".//item")
    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entries = root.findall(".//a:entry", ns)
        out = []
        for e in entries:
            title = strip_tags((e.findtext("a:title", default="", namespaces=ns) or ""))
            link_el = e.find("a:link", ns)
            link = link_el.get("href") if link_el is not None else ""
            summary = strip_tags(
                e.findtext("a:summary", default="", namespaces=ns)
                or e.findtext("a:content", default="", namespaces=ns)
                or ""
            )
            published = e.findtext("a:published", default="", namespaces=ns) \
                or e.findtext("a:updated", default="", namespaces=ns)
            guid = e.findtext("a:id", default="", namespaces=ns) or link
            out.append({"title": title, "link": link, "summary": summary,
                        "published": published, "guid": guid})
        return out

    out = []
    for it in items:
        title = strip_tags(it.findtext("title") or "")
        link = (it.findtext("link") or "").strip()
        summary = strip_tags(it.findtext("description") or "")
        published = (it.findtext("pubDate") or "").strip()
        guid = (it.findtext("guid") or link or title).strip()
        out.append({"title": title, "link": link, "summary": summary,
                    "published": published, "guid": guid})
    return out


def stable_article_id(source: str, guid: str) -> str:
    digest = hashlib.sha1(f"{source}|{guid}".encode("utf-8")).hexdigest()[:16]
    return f"{source}-{digest}"


# ---------------------------------------------------------------------------
# Location matching via the geo index
# ---------------------------------------------------------------------------

def iter_country_locations(
    hc: HyperClient,
    country_code: str,
) -> Iterator[tuple[str, str]]:
    """Yield (key, name) pairs from geo.index.by_property.country_code.{CC}."""
    path = f"geo.index.by_property.country_code.{country_code.upper()}"
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
            # The index leaf has the location name on it via projections;
            # but cheapest is to treat the key itself as slug-name-geonameid
            # and recover the name from the location record on demand.
            yield str(key), _name_from_key(str(key))
        state = doc.get("_state") or {}
        if page >= int(state.get("num_pages") or 1):
            return
        page += 1


def _name_from_key(key: str) -> str:
    # keys look like "new-york-city-5128581" → "new york city"
    stem = key.rsplit("-", 1)[0]
    return stem.replace("-", " ").strip()


def build_location_matcher(
    hc: HyperClient,
    country_code: str,
    *,
    min_len: int = 4,
) -> list[tuple[re.Pattern, str]]:
    """
    Build a cheap word-boundary regex matcher over location names in a country.
    Returns [(pattern, location_key), ...]. Skip names shorter than min_len to
    avoid matching "as", "oz", etc.
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
# Sync
# ---------------------------------------------------------------------------

def write_article(
    hc: HyperClient,
    article: NewsArticle,
    published_at_dt: datetime,
) -> str:
    yyyy, mm, dd = date_segments(published_at_dt)
    canonical_path = (
        f"news.items.{article.region}.{article.source}."
        f"{yyyy}.{mm}.{dd}.{article.article_id}"
    )
    latest_path = f"news.latest.{article.region}.{article.source}"

    # Build links: include a location link if exactly one match; otherwise
    # keep them out of _links (they're still in data.location_keys) and the
    # back-refs below still fire for every match.
    links: dict[str, str] = {}
    if len(article.location_keys) == 1:
        links["location"] = f"geo.locations.{article.location_keys[0]}"

    # 1) Canonical article
    old_root = hc.root
    try:
        hc.root = "news"
        article.commit(
            hc,
            sub_paths=["items", article.region, article.source, yyyy, mm, dd, article.article_id],
            links=links or None,
        )
    finally:
        hc.root = old_root

    # 2) Latest pointer for this feed
    hc.write(latest_path, {
        "data": {
            "model": "news-latest",
            "target": canonical_path,
            "title": article.title,
            "link": article.link,
            "source": article.source,
            "region": article.region,
            "published_at": article.published_at,
        },
        "links": {"target": canonical_path},
    })

    # 3) Back-ref under each matched location, date-partitioned
    for key in article.location_keys:
        announce_ref(
            hc,
            at_path=f"geo.locations.{key}",
            rel=f"news.{yyyy}.{mm}.{dd}.{article.article_id}",
            target_path=canonical_path,
            kind="news-article",
            extra=article.snapshot_for_ref(),
        )

    return canonical_path


def sync_feed(
    hc: HyperClient,
    *,
    region: str,
    source: str,
    url: str,
    match_country: str | None,
    limit: int | None = None,
) -> int:
    print(f"feed: {region}/{source} — {url}")
    try:
        items = fetch_feed(url)
    except Exception as exc:
        print(f"  fetch failed: {type(exc).__name__}: {exc}")
        return 0

    matchers = build_location_matcher(hc, match_country) if match_country else []
    if match_country:
        print(f"  matchers built for {match_country}: {len(matchers)} names")

    fetched_at = now_utc().isoformat()
    n = 0
    for raw in items:
        if limit is not None and n >= limit:
            break

        pub_dt = parse_date(raw.get("published") or "") or now_utc()
        aid = stable_article_id(source, raw.get("guid") or raw.get("link") or raw.get("title") or "")
        text_for_match = f"{raw.get('title','')}  {raw.get('summary','')}"
        location_keys = match_locations(text_for_match, matchers) if matchers else []

        article = NewsArticle(
            article_id=aid,
            source=source,
            region=region,
            title=raw.get("title") or "",
            link=raw.get("link") or "",
            summary=raw.get("summary") or "",
            published_at=pub_dt.isoformat(),
            fetched_at=fetched_at,
            location_keys=location_keys,
        )
        try:
            write_article(hc, article, pub_dt)
            n += 1
            loc_hint = f" [{', '.join(location_keys)}]" if location_keys else ""
            print(f"  ok: {article.title[:80]}{loc_hint}")
        except Exception as exc:
            print(f"  write failed for {aid}: {type(exc).__name__}: {exc}")

    return n


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="load_news.py")
    p.add_argument("--url", default=os.getenv("HYPER_URL", DEFAULT_URL))
    p.add_argument("--all", action="store_true", help="use DEFAULT_FEEDS")
    p.add_argument("--feed", nargs=2, metavar=("SOURCE", "URL"),
                   help="one ad-hoc feed; also pass --region and --match-country")
    p.add_argument("--region", default="world")
    p.add_argument("--match-country", default=None)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args(argv[1:])

    if not args.all and not args.feed:
        p.error("pass --all or --feed SOURCE URL")

    if HyperClient._health_ok(args.url):
        print(f"attaching to {args.url}")
        hc = HyperClient.attach(args.url, root="news")
    else:
        data_dir = os.getenv("HYPER_DATA_DIR", str(Path.cwd() / ".hyper-data"))
        print(f"no relay at {args.url}; spawning (data: {data_dir})")
        hc = HyperClient.spawn(data_dir=data_dir, root="news")

    try:
        total = 0
        feeds: Iterable[tuple[str, str, str, str | None]]
        if args.all:
            feeds = DEFAULT_FEEDS
        else:
            source, url = args.feed
            feeds = [(args.region, source, url, args.match_country)]

        for region, source, url, match_country in feeds:
            total += sync_feed(
                hc,
                region=region,
                source=source,
                url=url,
                match_country=match_country,
                limit=args.limit,
            )
        print(f"done — {total} article(s)")
        return 0
    finally:
        if hc.owns_relay():
            print(f"relay still running at {hc.url}")
            try: hc._owned.process.wait()
            except KeyboardInterrupt: pass
            finally: hc.close()
        else:
            hc.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))