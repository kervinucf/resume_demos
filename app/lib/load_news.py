"""
Build the news database from an RSS/Atom provider (root="news") and cross-link
to geo locations.

Every article is one node at news.items.<region>-<source>-<day>-<article_id>,
written densely and indexed (browsable) by source, region, published_day, and
has_locations (each also scoped by region). A latest pointer tracks the newest
article per feed, and every matched location gets a flat backref leaf.

Orchestration only. The provider is read inside the create_* verbs and named in
exactly one place (app/lib/helpers/news/__init__.py); this file never touches
RSS/Atom shapes, URLs, or the source module — switching providers doesn't touch it.
"""
from __future__ import annotations

import sys

from app.lib.helpers.news import (
    HyperClient,
    apply_graph_operations,
    list_article_candidates,
    create_article_object,
    get_location_matchers,
    NewsArticleObject,
)

__all__ = ["load_news", "NewsArticleObject"]

NEWS_ROOT = "news"

# Neutral feed specs (plain dicts — the provider boundary takes these as-is).
DEFAULT_FEEDS: list[dict] = [
    {"region": "world", "source": "bbc", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"region": "us", "source": "nyt", "url": "https://rss.nytimes.com/services/xml/rss/nyt/US.xml",
     "match_country": "US"},
    {"region": "us", "source": "reuters", "url": "https://feeds.reuters.com/Reuters/domesticNews",
     "match_country": "US"},
]


def load_news(
        feeds: list[dict] | None = None,
        DATA_DIR: str = None
) -> int:
    feeds = feeds or DEFAULT_FEEDS
    print(f"data dir: {DATA_DIR}", flush=True)
    print(f"feeds: {', '.join(f'{f['region']}/{f['source']}' for f in feeds)}", flush=True)

    # Boot LIVE on root="geo" (not a cold builder) so the preloaded geo indexes
    # are queryable for location matching; news.* paths are written through the
    # same live client via per-call root. Mirrors the original Loader(GEO_ROOT).
    data_store = HyperClient(root_key="geo", data_dir=DATA_DIR)
    try:
        # Build per-country matchers once, keyed by country code (best-effort).
        matcher_cache: dict[str, list] = {}
        for feed in feeds:
            cc = feed.get("match_country")
            if cc and cc not in matcher_cache:
                matcher_cache[cc] = get_location_matchers(data_store, cc)

        written = 0
        latest_done: set[str] = set()
        for article_record, candidate_proof in list_article_candidates(feeds):
            if not candidate_proof:
                continue

            matchers = matcher_cache.get(article_record.get("match_country") or "", [])
            article_object, object_proof = create_article_object(article_record, matchers)
            if not article_object:
                print("skipping", article_record.get("article_id"))
                continue

            feed_key = article_object.latest_key()
            write_latest = feed_key not in latest_done
            latest_done.add(feed_key)

            apply_graph_operations(
                article_object=article_object,
                client_instance=data_store,
                namespace=NEWS_ROOT,
                write_latest=write_latest,
            )

            written += 1
            loc_hint = f" [{', '.join(article_object.location_keys)}]" if article_object.location_keys else ""
            print(f"  ok: {article_object.title[:80]}{loc_hint}", flush=True)

        print(f"done: built {data_store.count:,} articles ({data_store.written:,} writes)", flush=True)
    finally:
        data_store.close()

    return 0


if __name__ == "__main__":
    sys.exit(load_news())
