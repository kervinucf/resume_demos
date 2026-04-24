# app/lib/news/loader.py
"""
Pull RSS/Atom feeds into the hypergraph.

Canonical records and news indexes are written under root="news".
The relay starts with root="geo" so preloaded geo indexes and location records
are available for location matching.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from HyperCoreSDK.python.client import HyperClient
from HyperCoreSDK.python.helpers.server import create_hyper_server
from HyperCoreSDK.python.helpers.storage import create_default_storage_directory
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
    FeedSpec(
        region="world",
        source="bbc",
        url="https://feeds.bbci.co.uk/news/world/rss.xml",
        match_country=None,
    ),
    FeedSpec(
        region="us",
        source="nyt",
        url="https://rss.nytimes.com/services/xml/rss/nyt/US.xml",
        match_country="US",
    ),
    FeedSpec(
        region="us",
        source="reuters",
        url="https://feeds.reuters.com/Reuters/domesticNews",
        match_country="US",
    ),
]


NEWS_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec(
        name="source",
        path="source",
        normalize="slug",
        link_projections={
            "title": "title",
            "link": "link",
            "region": "region",
            "published_at": "published_at",
            "published_day": "published_day",
            "location_count": "location_count",
        },
    ),
    ValueIndexSpec(
        name="region",
        path="region",
        normalize="slug",
        link_projections={
            "title": "title",
            "link": "link",
            "source": "source",
            "published_at": "published_at",
            "published_day": "published_day",
            "location_count": "location_count",
        },
    ),
    ValueIndexSpec(
        name="published_day",
        path="published_day",
        normalize="slug",
        link_projections={
            "title": "title",
            "link": "link",
            "source": "source",
            "region": "region",
            "published_at": "published_at",
            "location_count": "location_count",
        },
    ),
    ValueIndexSpec(
        name="has_locations",
        path="has_locations",
        normalize="lower",
        link_projections={
            "title": "title",
            "link": "link",
            "source": "source",
            "region": "region",
            "published_at": "published_at",
            "location_count": "location_count",
        },
    ),
    ValueIndexSpec(
        name="source",
        path="source",
        normalize="slug",
        scopes=[ScopeSpec(path="region", normalize="slug")],
        link_projections={
            "title": "title",
            "link": "link",
            "region": "region",
            "published_at": "published_at",
            "published_day": "published_day",
            "location_count": "location_count",
        },
    ),
    ValueIndexSpec(
        name="published_day",
        path="published_day",
        normalize="slug",
        scopes=[ScopeSpec(path="region", normalize="slug")],
        link_projections={
            "title": "title",
            "link": "link",
            "source": "source",
            "region": "region",
            "published_at": "published_at",
            "location_count": "location_count",
        },
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

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def dt_ms(value: str) -> int:
    return int(parse_dt(value).timestamp() * 1000)


def published_day(value: str) -> str:
    return parse_dt(value).strftime("%Y-%m-%d")


def article_text(article: NewsArticle) -> str:
    data = article.to_dict()

    return " ".join(
        str(part)
        for part in [
            data.get("title"),
            data.get("summary"),
            data.get("description"),
            data.get("source"),
            data.get("region"),
            data.get("link"),
        ]
        if part
    )


def article_tokens(article: NewsArticle) -> list[str]:
    data = article.to_dict()

    return [
        token
        for token in [
            str(data.get("title") or ""),
            str(data.get("summary") or ""),
            str(data.get("source") or ""),
            str(data.get("region") or ""),
            *[str(key) for key in article.location_keys],
        ]
        if token
    ]


def article_record_data(article: NewsArticle) -> dict[str, Any]:
    data = article.to_dict()
    data["published_day"] = published_day(article.published_at)
    data["location_count"] = len(article.location_keys)
    data["has_locations"] = bool(article.location_keys)
    return data


def article_ref_payload(article: NewsArticle) -> dict[str, Any]:
    payload = article.ref_payload()
    payload["published_day"] = published_day(article.published_at)
    payload["location_count"] = len(article.location_keys)
    payload["has_locations"] = bool(article.location_keys)
    return payload


def article_refs(article: NewsArticle, item_abs: str | None = None) -> dict[str, Any]:
    refs: dict[str, Any] = {}

    if item_abs:
        refs["target"] = item_abs

    if article.location_keys:
        refs["location"] = [
            f"{GEO_ROOT}.locations.{key}"
            for key in article.location_keys
        ]

    return refs


def article_query(
    article: NewsArticle,
    *,
    entity_id: str,
    entity_type: str,
    display: str,
    fetched_at: str,
    target: str | None = None,
) -> dict[str, Any]:
    published_ms = dt_ms(article.published_at)
    fetched_ms = dt_ms(fetched_at)

    return {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "canonical_path": entity_id,
        "display": display,
        "text": article_text(article),
        "facets": {
            "source": article.source,
            "region": article.region,
            "published_day": published_day(article.published_at),
            "has_locations": bool(article.location_keys),
        },
        "numbers": {
            "location_count": len(article.location_keys),
        },
        "times": {
            "published_at": published_ms,
            "fetched_at": fetched_ms,
            "activity_latest_at": max(published_ms, fetched_ms),
        },
        "refs": article_refs(article, target),
        "tokens": article_tokens(article),
    }


def location_news_ref_query(
    *,
    loc_path: str,
    article: NewsArticle,
    item_abs: str,
    ref_path: str,
    fetched_at: str,
) -> dict[str, Any]:
    published_ms = dt_ms(article.published_at)
    fetched_ms = dt_ms(fetched_at)

    return {
        "entity_id": ref_path,
        "entity_type": "location_news_ref",
        "canonical_path": ref_path,
        "display": article.title,
        "text": article_text(article),
        "facets": {
            "source": article.source,
            "region": article.region,
            "published_day": published_day(article.published_at),
            "location_path": loc_path,
        },
        "times": {
            "published_at": published_ms,
            "fetched_at": fetched_ms,
            "activity_latest_at": max(published_ms, fetched_ms),
        },
        "refs": {
            "location": loc_path,
            "news_article": item_abs,
        },
        "tokens": article_tokens(article),
    }


def write_article(
    client: HyperClient,
    article: NewsArticle,
    *,
    fetched_at: str,
) -> str:
    item_rel = f"items/{article.record_key()}"
    item_abs = f"{NEWS_ROOT}.{item_rel.replace('/', '.')}"
    latest_abs = f"{NEWS_ROOT}.latest.{article.latest_key().replace('/', '.')}"
    article_data = article_record_data(article)

    client.write_record_with_indexes(
        root=NEWS_ROOT,
        record_path=item_rel,
        record_data=article_data,
        index_specs=NEWS_INDEXES,
        ref_key=article.article_id,
        ref_payload=article_ref_payload(article),
    )

    client.write(
        item_abs,
        {
            **article_data,
            "query": article_query(
                article,
                entity_id=item_abs,
                entity_type="news_article",
                display=article.title,
                fetched_at=fetched_at,
            ),
        },
    )

    latest_links: dict[str, str] = {}

    if len(article.location_keys) == 1:
        latest_links["location"] = f"{GEO_ROOT}.locations.{article.location_keys[0]}"

    client.write_pointer(
        path=latest_abs,
        target=item_abs,
        data=article.latest_dict(item_abs),
        links=latest_links,
        query=article_query(
            article,
            entity_id=latest_abs,
            entity_type="news_latest",
            display=f"{article.region}/{article.source}",
            fetched_at=fetched_at,
            target=item_abs,
        ),
    )

    pub_dt = parse_dt(article.published_at)
    ts_tail = f"{pub_dt.year:04d}.{pub_dt.month:02d}.{pub_dt.day:02d}.{article.article_id}"

    for key in article.location_keys:
        loc_path = f"{GEO_ROOT}.locations.{key}"
        rel = f"news.{ts_tail}"
        ref_path = f"{loc_path}.refs.{rel}"

        client.write_backref(
            source=loc_path,
            rel=rel,
            target=item_abs,
            data={
                "kind": "news-article",
                "target": item_abs,
                **article_ref_payload(article),
            },
            query=location_news_ref_query(
                loc_path=loc_path,
                article=article,
                item_abs=item_abs,
                ref_path=ref_path,
                fetched_at=fetched_at,
            ),
        )

    return item_abs


def sync_feed(
    client: HyperClient,
    rss: RssApiClient,
    feed: FeedSpec,
    *,
    limit: int | None = None,
) -> int:
    print(f"feed: {feed.region}/{feed.source} — {feed.url}")

    try:
        body = rss.fetch_bytes(feed.url)
        raw_items = parse_feed_bytes(body)
    except Exception as exc:
        print(f"  failed: {type(exc).__name__}: {exc}")
        return 0

    matchers = build_location_matcher(client, feed.match_country) if feed.match_country else []

    if feed.match_country:
        print(f"  matchers built for {feed.match_country}: {len(matchers)} names")

    fetched_at = now_utc().isoformat()
    written = 0

    for raw in raw_items:
        if limit is not None and written >= limit:
            break

        article = article_from_raw(
            raw,
            source=feed.source,
            region=feed.region,
            matchers=matchers,
            fetched_at=fetched_at,
        )

        if article is None:
            continue

        try:
            item_abs = write_article(client, article, fetched_at=fetched_at)
            written += 1

            loc_hint = (
                f" [{', '.join(article.location_keys)}]"
                if article.location_keys
                else ""
            )

            print(f"  ok: {article.title[:80]}{loc_hint} -> {item_abs}")

        except Exception as exc:
            print(f"  write failed for {article.article_id}: {type(exc).__name__}: {exc}")

    return written


def run(
    client: HyperClient,
    *,
    feeds: list[FeedSpec] | None = None,
    limit_per_feed: int | None = None,
    close_client: bool = False,
    keep_alive: bool = True,
) -> int:
    rss = RssApiClient()
    total = 0

    try:
        for feed in feeds or DEFAULT_FEEDS:
            total += sync_feed(
                client,
                rss,
                feed,
                limit=limit_per_feed,
            )

        print(f"done — {total} article(s)")

        if keep_alive:
            print(f"relay still running at {client.url} (Ctrl-C to stop)")
            while True:
                time.sleep(3600)

    except KeyboardInterrupt:
        pass

    finally:
        if close_client:
            client.close()

    return 0


if __name__ == "__main__":
    client = create_hyper_server(
        root=GEO_ROOT,
        data_path=create_default_storage_directory(),
    )

    sys.exit(
        run(
            client,
            feeds=[
                FeedSpec(
                    region="world",
                    source="bbc",
                    url="https://feeds.bbci.co.uk/news/world/rss.xml",
                    match_country=None,
                ),
                FeedSpec(
                    region="us",
                    source="nyt",
                    url="https://rss.nytimes.com/services/xml/rss/nyt/US.xml",
                    match_country="US",
                ),
            ],
            limit_per_feed=25,
            close_client=True,
            keep_alive=True,
        )
    )