# app/lib/news/loader.py
"""
Pull RSS/Atom feeds into the hypergraph.

This loader starts the relay with root="geo" so preloaded geo indexes and
location records are visible, while canonical news records and news indexes
are explicitly written under root="news".

Selection/config is controlled by calling run(...), not CLI args.

Example
-------
    run(
        client_instance=client,
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
    )
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.utils.server import create_hyper_server
from app.utils.storage import create_default_storage_directory
from app.utils.clients.rss import RssApiClient
from app.utils.dtos.NewsEvent import NewsArticle
from app.lib.news.helpers import (
    article_from_raw,
    build_location_matcher,
    now_utc,
    parse_feed_bytes,
)
from HyperCoreSDK.python.client import HyperClient
from HyperCoreSDK.python.indexes import (
    ScopeSpec,
    ValueIndexSpec,
    upsert_with_indexes,
)


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


def _parse_dt(value: str) -> datetime:
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


def _dt_ms(value: str) -> int:
    return int(_parse_dt(value).timestamp() * 1000)


def _published_day(value: str) -> str:
    return _parse_dt(value).strftime("%Y-%m-%d")


def _article_text(article: NewsArticle) -> str:
    data = article.to_dict()

    parts = [
        data.get("title"),
        data.get("summary"),
        data.get("description"),
        data.get("source"),
        data.get("region"),
        data.get("link"),
    ]

    return " ".join(str(p) for p in parts if p)


def _article_tokens(article: NewsArticle) -> list[str]:
    data = article.to_dict()

    tokens: list[str] = [
        str(data.get("title") or ""),
        str(data.get("summary") or ""),
        str(data.get("source") or ""),
        str(data.get("region") or ""),
    ]

    for key in article.location_keys:
        tokens.append(str(key))

    return [t for t in tokens if t]


def _article_record_data(article: NewsArticle) -> dict[str, Any]:
    data = article.to_dict()
    data["published_day"] = _published_day(article.published_at)
    data["location_count"] = len(article.location_keys)
    data["has_locations"] = bool(article.location_keys)
    return data


def _article_ref_payload(article: NewsArticle) -> dict[str, Any]:
    payload = article.ref_payload()
    payload["published_day"] = _published_day(article.published_at)
    payload["location_count"] = len(article.location_keys)
    payload["has_locations"] = bool(article.location_keys)
    return payload


def _article_query(article: NewsArticle, item_abs: str, fetched_at: str) -> dict[str, Any]:
    published_ms = _dt_ms(article.published_at)
    fetched_ms = _dt_ms(fetched_at)

    refs: dict[str, Any] = {}

    if article.location_keys:
        refs["location"] = [
            f"geo.locations.{key}"
            for key in article.location_keys
        ]

    return {
        "entity_id": item_abs,
        "entity_type": "news_article",
        "canonical_path": item_abs,
        "display": article.title,
        "text": _article_text(article),
        "facets": {
            "source": article.source,
            "region": article.region,
            "published_day": _published_day(article.published_at),
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
        "refs": refs,
        "tokens": _article_tokens(article),
    }


def _latest_query(
    article: NewsArticle,
    latest_abs: str,
    item_abs: str,
    fetched_at: str,
) -> dict[str, Any]:
    published_ms = _dt_ms(article.published_at)
    fetched_ms = _dt_ms(fetched_at)

    refs: dict[str, Any] = {
        "target": item_abs,
    }

    if article.location_keys:
        refs["location"] = [
            f"geo.locations.{key}"
            for key in article.location_keys
        ]

    return {
        "entity_id": latest_abs,
        "entity_type": "news_latest",
        "canonical_path": latest_abs,
        "display": f"{article.region}/{article.source}",
        "text": _article_text(article),
        "facets": {
            "source": article.source,
            "region": article.region,
            "published_day": _published_day(article.published_at),
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
        "refs": refs,
        "tokens": _article_tokens(article),
    }


def _location_news_ref_query(
    *,
    loc_path: str,
    article: NewsArticle,
    item_abs: str,
    ref_path: str,
    fetched_at: str,
) -> dict[str, Any]:
    published_ms = _dt_ms(article.published_at)
    fetched_ms = _dt_ms(fetched_at)

    return {
        "entity_id": ref_path,
        "entity_type": "location_news_ref",
        "canonical_path": ref_path,
        "display": article.title,
        "text": _article_text(article),
        "facets": {
            "source": article.source,
            "region": article.region,
            "published_day": _published_day(article.published_at),
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
        "tokens": _article_tokens(article),
    }


def write_article(
    hc: HyperClient,
    article: NewsArticle,
    *,
    fetched_at: str,
) -> str:
    item_rel = f"items/{article.record_key()}"
    item_abs = f"news.{item_rel.replace('/', '.')}"
    latest_abs = f"news.latest.{article.latest_key().replace('/', '.')}"

    article_data = _article_record_data(article)

    upsert_with_indexes(
        hc,
        root="news",
        record_path=item_rel,
        record_data=article_data,
        index_specs=NEWS_INDEXES,
        ref_key=article.article_id,
        ref_payload=_article_ref_payload(article),
    )

    hc.write(
        item_abs,
        {
            **article_data,
            "query": _article_query(article, item_abs, fetched_at),
        },
    )

    latest_links: dict[str, str] = {
        "target": item_abs,
    }

    if len(article.location_keys) == 1:
        latest_links["location"] = f"geo.locations.{article.location_keys[0]}"

    latest_data = article.latest_dict(item_abs)

    hc.write(
        latest_abs,
        {
            **latest_data,
            "_links": latest_links,
            "query": _latest_query(article, latest_abs, item_abs, fetched_at),
        },
    )

    pub_dt = _parse_dt(article.published_at)
    ts_tail = f"{pub_dt.year:04d}.{pub_dt.month:02d}.{pub_dt.day:02d}.{article.article_id}"

    for key in article.location_keys:
        loc_path = f"geo.locations.{key}"
        ref_path = f"{loc_path}.refs.news.{ts_tail}"

        hc.write(
            ref_path,
            {
                "kind": "news-article",
                "target": item_abs,
                **_article_ref_payload(article),
                "_links": {
                    "target": item_abs,
                },
                "query": _location_news_ref_query(
                    loc_path=loc_path,
                    article=article,
                    item_abs=item_abs,
                    ref_path=ref_path,
                    fetched_at=fetched_at,
                ),
            },
        )

    return item_abs


def sync_feed(
    hc: HyperClient,
    rss: RssApiClient,
    *,
    region: str,
    source: str,
    url: str,
    match_country: str | None,
    limit: int | None = None,
) -> int:
    print(f"feed: {region}/{source} — {url}")

    try:
        body = rss.fetch_bytes(url)
    except Exception as exc:
        print(f"  fetch failed: {type(exc).__name__}: {exc}")
        return 0

    try:
        raw_items = parse_feed_bytes(body)
    except Exception as exc:
        print(f"  parse failed: {type(exc).__name__}: {exc}")
        return 0

    matchers = build_location_matcher(hc, match_country) if match_country else []

    if match_country:
        print(f"  matchers built for {match_country}: {len(matchers)} names")

    fetched_at = now_utc().isoformat()
    written = 0

    for raw in raw_items:
        if limit is not None and written >= limit:
            break

        article = article_from_raw(
            raw,
            source=source,
            region=region,
            matchers=matchers,
            fetched_at=fetched_at,
        )

        if article is None:
            continue

        try:
            item_abs = write_article(
                hc,
                article,
                fetched_at=fetched_at,
            )
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
    client_instance: HyperClient,
    *,
    feeds: list[FeedSpec] | None = None,
    limit_per_feed: int | None = None,
    close_client: bool = False,
    keep_alive: bool = True,
) -> int:
    rss = RssApiClient()
    selected_feeds = feeds or DEFAULT_FEEDS

    total = 0

    try:
        for feed in selected_feeds:
            total += sync_feed(
                client_instance,
                rss,
                region=feed.region,
                source=feed.source,
                url=feed.url,
                match_country=feed.match_country,
                limit=limit_per_feed,
            )

        print(f"done — {total} article(s)")

        if keep_alive:
            print(f"relay still running at {client_instance.url} (Ctrl-C to stop)")
            while True:
                time.sleep(3600)

    except KeyboardInterrupt:
        pass

    finally:
        if close_client:
            client_instance.close()

    return 0


if __name__ == "__main__":
    client = create_hyper_server(
        root="geo",
        data_path=create_default_storage_directory(),
    )

    sys.exit(
        run(
            client_instance=client,
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