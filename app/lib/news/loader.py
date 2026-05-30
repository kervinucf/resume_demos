# app/lib/news/loader.py
"""
Pull RSS/Atom feeds into root="news" and cross-link to geo locations.

Each article is one record (with indexes). A "latest" pointer tracks the newest
article per region/source, and every matched location gets a browsable backref.
Search is inferred from each record's data and `kind`; no query is hand-built.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from HyperCoreSDK.python.helpers.loader import Loader

from HyperCoreSDK.python.helpers.indexes import ScopeSpec, ValueIndexSpec
from app.utils.clients.rss import RssApiClient
from app.utils.dtos.NewsEvent import NewsArticle
from app.lib.news.helpers import (
    article_from_raw,
    build_location_matcher,
    now_utc,
    parse_feed_bytes,
)


GEO_ROOT = "geo"
NEWS_ROOT = "news"


@dataclass(frozen=True)
class FeedSpec:
    region: str
    source: str
    url: str
    match_country: str | None = None


DEFAULT_FEEDS: list[FeedSpec] = [
    FeedSpec("world", "bbc", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    FeedSpec("us", "nyt", "https://rss.nytimes.com/services/xml/rss/nyt/US.xml", match_country="US"),
    FeedSpec("us", "reuters", "https://feeds.reuters.com/Reuters/domesticNews", match_country="US"),
]


_ARTICLE_PROJECT = {
    "title": "title", "link": "link", "source": "source", "region": "region",
    "published_at": "published_at", "published_day": "published_day",
    "location_count": "location_count",
}

NEWS_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec("source", "source", normalize="slug", link_projections=_ARTICLE_PROJECT),
    ValueIndexSpec("region", "region", normalize="slug", link_projections=_ARTICLE_PROJECT),
    ValueIndexSpec("published_day", "published_day", normalize="slug", link_projections=_ARTICLE_PROJECT),
    ValueIndexSpec("has_locations", "has_locations", normalize="lower", link_projections=_ARTICLE_PROJECT),
    ValueIndexSpec(
        "source", "source", normalize="slug",
        scopes=[ScopeSpec("region", normalize="slug")], link_projections=_ARTICLE_PROJECT,
    ),
    ValueIndexSpec(
        "published_day", "published_day", normalize="slug",
        scopes=[ScopeSpec("region", normalize="slug")], link_projections=_ARTICLE_PROJECT,
    ),
]


def parse_dt(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return now_utc()
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def published_day(value: str) -> str:
    return parse_dt(value).strftime("%Y-%m-%d")


def article_data(article: NewsArticle) -> dict[str, Any]:
    """The stored record. `kind` drives entity_type; the rest is inferred."""
    data = {"kind": "news_article", **article.to_dict()}
    data["published_day"] = published_day(article.published_at)
    data["location_count"] = len(article.location_keys)
    data["has_locations"] = bool(article.location_keys)
    return data


def article_links(article: NewsArticle) -> dict[str, Any]:
    if not article.location_keys:
        return {}
    return {"location": [f"{GEO_ROOT}.locations.{key}" for key in article.location_keys]}


def write_article(news: Loader, article: NewsArticle) -> str:
    item_rel = f"items/{article.record_key()}"
    item_abs = f"{NEWS_ROOT}.{item_rel.replace('/', '.')}"
    latest_abs = f"{NEWS_ROOT}.latest.{article.latest_key().replace('/', '.')}"
    data = article_data(article)

    # 1. The article record + indexes + (optional) location refs, in one write.
    news.record(
        item_rel,
        data,
        indexes=NEWS_INDEXES,
        ref_key=article.article_id,
        ref_payload={
            **article.ref_payload(),
            "published_day": data["published_day"],
            "location_count": data["location_count"],
            "has_locations": data["has_locations"],
        },
        links=article_links(article),
    )

    # 2. Latest pointer for this region/source.
    latest_links: dict[str, Any] = {}
    if len(article.location_keys) == 1:
        latest_links["location"] = f"{GEO_ROOT}.locations.{article.location_keys[0]}"

    news.thing(
        path=latest_abs,
        kind="news_latest",
        name=f"{article.region}/{article.source}",
        target=item_abs,
        body=article.latest_dict(item_abs),
        links=latest_links,
    )

    # 3. Browsable backref on each matched location.
    pub = parse_dt(article.published_at)
    ts_tail = f"{pub.year:04d}.{pub.month:02d}.{pub.day:02d}.{article.article_id}"
    for key in article.location_keys:
        loc_path = f"{GEO_ROOT}.locations.{key}"
        news.link(
            source=loc_path,
            rel=f"news.{ts_tail}",
            target=item_abs,
            kind="news-article",
            name=article.title,
            body={
                **article.ref_payload(),
                "published_day": data["published_day"],
            },
            links={"location": loc_path},
        )

    return item_abs


def sync_feed(news: Loader, rss: RssApiClient, feed: FeedSpec, *, limit: int | None = None) -> int:
    print(f"feed: {feed.region}/{feed.source} — {feed.url}")
    try:
        raw_items = parse_feed_bytes(rss.fetch_bytes(feed.url))
    except Exception as exc:
        print(f"  failed: {type(exc).__name__}: {exc}")
        return 0

    matchers = build_location_matcher(news.client, feed.match_country) if feed.match_country else []
    if feed.match_country:
        print(f"  matchers built for {feed.match_country}: {len(matchers)} names")

    fetched_at = now_utc().isoformat()
    written = 0

    for raw in raw_items:
        if limit is not None and written >= limit:
            break

        article = article_from_raw(
            raw, source=feed.source, region=feed.region,
            matchers=matchers, fetched_at=fetched_at,
        )
        if article is None:
            continue

        try:
            item_abs = write_article(news, article)
            written += 1
            loc_hint = f" [{', '.join(article.location_keys)}]" if article.location_keys else ""
            print(f"  ok: {article.title[:80]}{loc_hint} -> {item_abs}")
        except Exception as exc:
            print(f"  write failed for {article.article_id}: {type(exc).__name__}: {exc}")

    return written


def run(
    news: Loader,
    *,
    feeds: list[FeedSpec] | None = None,
    limit_per_feed: int | None = None,
    keep_alive: bool = True,
) -> int:
    rss = RssApiClient()
    total = sum(sync_feed(news, rss, feed, limit=limit_per_feed) for feed in (feeds or DEFAULT_FEEDS))
    print(f"done — {total} article(s)")

    if keep_alive:
        news.serve()
    else:
        news.close()
    return 0


def main() -> int:
    # Relay boots on root="geo" so preloaded location indexes are available for matching.
    return run(Loader(GEO_ROOT), feeds=DEFAULT_FEEDS, limit_per_feed=25)


if __name__ == "__main__":
    sys.exit(main())