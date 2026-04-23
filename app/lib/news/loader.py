"""
Pull RSS/Atom feeds into the hypergraph.

After a run, the `news` namespace looks like:

    news/
      items/
        world/bbc/
          2026/04/21/bbc-3a7f2c1d9e0b4a56     (canonical article record)
        us/nyt/
          2026/04/21/nyt-81b0e4c7f5d2a913
      latest/
        world/bbc                              (pointer → most recent item)
        us/nyt
      index/
        by/
          source/
            bbc/
              bbc-3a7f2c1d9e0b4a56
          region/
            us/
              nyt-81b0e4c7f5d2a913
        scoped/
          region/
            us/
              source/
                nyt/
                  nyt-81b0e4c7f5d2a913
      _meta/
        memberships/
          <sha1 of each record path>

Each matched Location also gains a back-ref:

    geo.locations.<key>.refs.news.<yyyy>.<mm>.<dd>.<article_id>
      → news.items.<region>.<source>.<yyyy>.<mm>.<dd>.<article_id>

Location matching is deliberately simple: substring-match article title
+ summary against location names in a country's geo index. Swap in
better NLP by replacing `build_location_matcher` / `match_locations`
in app/lib/news/helpers.py.

Usage
-----
    # One ad-hoc feed
    python loader.py --feed bbc https://feeds.bbci.co.uk/news/rss.xml

    # With location matching scoped to a country
    python loader.py --feed nyt https://rss.nytimes.com/services/xml/rss/nyt/US.xml \
        --region us --match-country US

    # All bundled defaults
    python loader.py --all
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from app.utils.server import create_hyper_server
from app.utils.storage import create_default_storage_directory
from app.utils.dtos.NewsEvent import NewsArticle
from app.lib.news.helpers import (
    article_from_raw,
    build_location_matcher,
    now_utc,
    parse_feed_bytes,
)
from app.lib.news.rss_api_client import RssApiClient
from HyperCoreSDK.python.client import HyperClient
from HyperCoreSDK.python.indexes import (
    ScopeSpec,
    ValueIndexSpec,
    upsert_with_indexes,
)


DEFAULT_FEEDS: list[dict[str, str | None]] = [
    # region, source, url, match_country_or_None
    {"region": "world", "source": "bbc",     "url": "https://feeds.bbci.co.uk/news/world/rss.xml",         "match_country": None},
    {"region": "us",    "source": "nyt",     "url": "https://rss.nytimes.com/services/xml/rss/nyt/US.xml", "match_country": "US"},
    {"region": "us",    "source": "reuters", "url": "https://feeds.reuters.com/Reuters/domesticNews",      "match_country": "US"},
]


# ---------------------------------------------------------------------------
# Index specs
#
#   "Index by source, slugified. `index/by/source/bbc/` holds every BBC
#    article regardless of region."
#
#   "Index by region globally — `index/by/region/us/` holds every US
#    article regardless of source."
#
#   "Also index by source scoped under region, so
#    `index/scoped/region/us/source/nyt/` holds only NYT's US coverage."
# ---------------------------------------------------------------------------

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
        },
    ),
]


# ---------------------------------------------------------------------------
# Per-record write
# ---------------------------------------------------------------------------

def write_article(hc: HyperClient, article: NewsArticle) -> str:
    item_rel = f"items/{article.record_key()}"
    item_abs = f"news.{item_rel.replace('/', '.')}"
    latest_abs = f"news.latest.{article.latest_key().replace('/', '.')}"

    # Canonical article + indexes
    upsert_with_indexes(
        hc,
        record_path=item_rel,
        record_data=article.to_dict(),
        index_specs=NEWS_INDEXES,
        ref_key=article.article_id,
        ref_payload=article.ref_payload(),
    )

    # "Latest" pointer for this feed — plain write, not indexed
    latest_links: dict[str, str] = {"target": item_abs}
    if len(article.location_keys) == 1:
        latest_links["location"] = f"geo.locations.{article.location_keys[0]}"

    hc.write(latest_abs, {
        "data": article.latest_dict(item_abs),
        "links": latest_links,
    })

    # Per-location back-refs (one per matched location)
    pub_dt = datetime.fromisoformat(article.published_at)
    ts_tail = f"{pub_dt.year:04d}.{pub_dt.month:02d}.{pub_dt.day:02d}.{article.article_id}"
    for key in article.location_keys:
        loc_path = f"geo.locations.{key}"
        hc.write(f"{loc_path}.refs.news.{ts_tail}", {
            "data": {"kind": "news-article", "target": item_abs, **article.ref_payload()},
            "links": {"target": item_abs},
        })

    return item_abs


# ---------------------------------------------------------------------------
# Per-feed sync
# ---------------------------------------------------------------------------

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
            write_article(hc, article)
            written += 1
            loc_hint = f" [{', '.join(article.location_keys)}]" if article.location_keys else ""
            print(f"  ok: {article.title[:80]}{loc_hint}")
        except Exception as exc:
            print(f"  write failed for {article.article_id}: {type(exc).__name__}: {exc}")

    return written


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(client_instance: HyperClient, argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="loader.py")
    p.add_argument("--all", action="store_true", help="use DEFAULT_FEEDS")
    p.add_argument("--feed", nargs=2, metavar=("SOURCE", "URL"),
                   help="one ad-hoc feed; also pass --region and --match-country")
    p.add_argument("--region", default="world")
    p.add_argument("--match-country", default=None)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args(argv[1:])

    if not args.all and not args.feed:
        p.error("pass --all or --feed SOURCE URL")

    rss = RssApiClient()

    if args.all:
        feeds = DEFAULT_FEEDS
    else:
        source, url = args.feed
        feeds = [{
            "region": args.region,
            "source": source,
            "url": url,
            "match_country": args.match_country,
        }]

    try:
        total = 0
        for feed in feeds:
            total += sync_feed(
                client_instance,
                rss,
                region=str(feed["region"]),
                source=str(feed["source"]),
                url=str(feed["url"]),
                match_country=feed.get("match_country"),
                limit=args.limit,
            )
        print(f"done — {total} article(s)")

    finally:
        if client_instance.owns_relay():
            print(f"relay still running at {client_instance.url} (Ctrl-C to stop)")
            try:
                client_instance._owned.process.wait()
            except KeyboardInterrupt:
                pass
            finally:
                client_instance.close()
        else:
            client_instance.close()

    return 0


if __name__ == "__main__":
    sys.exit(
        main(
            client_instance=create_hyper_server(
                root="news",
                data_path=create_default_storage_directory(),
            ),
            argv=sys.argv,
        )
    )