"""
News operations — the verbs the main loader orchestrates.

Mirrors app/lib/helpers/geo/__init__.py and weather/__init__.py: the provider is
read inside the create_* functions, so the loader only ever imports:

    from app.lib.helpers.news import (apply_graph_operations,
                                      list_article_candidates,
                                      create_article_object,
                                      get_location_matchers,
                                      NewsArticleObject)

This module is the ONE place that names the concrete provider. To switch providers,
change the `app.lib.sources.rss` import below to another module exposing the same
source callables — nothing else changes.
"""
from __future__ import annotations

import re
from typing import Any, Iterator

from HyperCoreSDK.python.client import HyperClient, projection, ScopeSpec, ValueIndexSpec
from HyperCoreSDK.python.helpers.records import iter_children
# --- provider boundary: swap this single import to change source ------------
from app.lib.sources.rss import iter_article_candidates, source_available
# ---------------------------------------------------------------------------
from app.lib.helpers.news.factory import NewsFactory, NewsArticleObject

news_factory = NewsFactory()

__all__ = [
    "HyperClient",
    "list_article_candidates",
    "create_article_object",
    "get_location_matchers",
    "apply_graph_operations",
    "source_available",
    "NewsArticleObject",
]

GEO_ROOT = "geo"

# Fields each index entry carries forward for cheap rendering (information density).
PROJECT = projection(
    "title", "link", "source", "region",
    "published_at", "published_day", "location_count",
)
NEWS_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec("source", "source", normalize="slug", link_projections=PROJECT),
    ValueIndexSpec("region", "region", normalize="slug", link_projections=PROJECT),
    ValueIndexSpec("published_day", "published_day", normalize="slug", link_projections=PROJECT),
    ValueIndexSpec("has_locations", "has_locations", normalize="lower", link_projections=PROJECT),
    ValueIndexSpec(
        "source", "source", normalize="slug",
        scopes=[ScopeSpec("region", normalize="slug")], link_projections=PROJECT,
    ),
    ValueIndexSpec(
        "published_day", "published_day", normalize="slug",
        scopes=[ScopeSpec("region", normalize="slug")], link_projections=PROJECT,
    ),
]


# ---------------------------------------------------------------------------
# Location enrichment  (the news analog of geo's currency enrichment)
# Best-effort: reads the geo index via the relay client. If that API isn't
# available under this HyperClient, we warn and continue with no matchers —
# exactly like geo tolerating a missing countryInfo.txt.
# ---------------------------------------------------------------------------
def _name_from_key(key: str) -> str:
    # keys look like "new-york-city-5128581" -> "new york city"
    return key.rsplit("-", 1)[0].replace("-", " ").strip()


def _iter_country_locations(client: Any, country_code: str) -> Iterator[tuple[str, str]]:
    # Children of geo.index.by.country_code.<CC> are named by the geo ref_key
    # (<name>-<geoname_id>); iter_children handles the relay's pagination.
    path = f"{GEO_ROOT}.index.by.country_code.{country_code.upper()}"
    for key, _child in iter_children(client, path):
        key = str(key)
        yield key, _name_from_key(key)


def get_location_matchers(
        client_instance: HyperClient,
        country_code: str | None,
        *,
        min_len: int = 4,
) -> list[tuple[re.Pattern, str]]:
    if not country_code:
        return []
    # Offline builders have no relay to read the geo index from.
    if getattr(client_instance, "building", False):
        print("  warning: client_instance opened offline; skipping location matching", flush=True)
        return []

    try:
        client = client_instance._ensure_client()
    except Exception as exc:
        print(f"  warning: no relay client ({exc}); skipping location matching", flush=True)
        return []

    matchers: list[tuple[re.Pattern, str]] = []
    try:
        for key, name in _iter_country_locations(client, country_code):
            if len(name) < min_len:
                continue
            try:
                matchers.append((re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE), key))
            except re.error:
                continue
    except Exception as exc:
        print(f"  warning: location matcher build failed for {country_code} ({exc}); "
              "continuing without matching", flush=True)
        return []

    print(f"  matchers built for {country_code}: {len(matchers)} names", flush=True)
    return matchers


def _match_locations(text: str, matchers: list[tuple[re.Pattern, str]], *, max_hits: int = 5) -> list[str]:
    seen: list[str] = []
    for pat, key in matchers:
        if pat.search(text) and key not in seen:
            seen.append(key)
            if len(seen) >= max_hits:
                break
    return seen


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------
def list_article_candidates(
        feeds: list[dict[str, Any]],
) -> Iterator[tuple[dict[str, Any], bool]]:
    for record in iter_article_candidates(feeds):
        proof = bool(record.get("article_id")) and bool(record.get("published_at"))
        if not proof:
            print(f"  skip: bad article record ({record.get('article_id')})", flush=True)
            continue
        yield record, proof


def create_article_object(
        article_record: dict[str, Any],
        matchers: list[tuple[re.Pattern, str]] | None = None,
) -> tuple[NewsArticleObject | None, bool]:
    keys: list[str] = []
    if matchers:
        text = f"{article_record.get('title', '')}  {article_record.get('summary', '')}"
        keys = _match_locations(text, matchers)

    try:
        article_object = news_factory.create_article_object(
            article_record=article_record, location_keys=keys,
        )
    except (TypeError, ValueError) as exc:
        print(f"  skip: bad article record ({exc})", flush=True)
        return None, False

    proof = bool(article_object.article_id) and bool(article_object.published_at)
    if not proof:
        print(f"  skip: incomplete article object ({article_record.get('article_id')})", flush=True)
    return article_object, proof


def apply_graph_operations(
        article_object: NewsArticleObject,
        client_instance: HyperClient,
        namespace,
        write_latest: bool = True,
) -> dict[str, str]:
    record_key = article_object.record_key()
    item_path = f"items/{record_key}"
    latest_path = f"latest/{article_object.latest_key()}"

    item_dot = f"{namespace}.{item_path.replace('/', '.')}"
    latest_dot = f"{namespace}.{latest_path.replace('/', '.')}"

    # 1) RECORD — indexed, dense (+ optional location links carried on the record)
    record_data = {"tag": "news_article", "model": "news-article", **article_object.__dict__}
    record_links = (
        {"location": str([f"{GEO_ROOT}.locations.{k}" for k in article_object.location_keys])}
        if article_object.location_keys else None
    )
    n1 = client_instance.save_record(
        path=item_path,
        data=record_data,
        indexes=NEWS_INDEXES,
        links=record_links,
        root=namespace,
    )

    # 2) POINTER news.latest.<region>-<source> -> item. Written ONCE per feed; per-item
    #    writes would insert the same (parent=latest, name=feed) node N times -> collision.
    n2 = 0
    if write_latest:
        n2 = client_instance.write_ops([{
            "path": latest_dot,
            "data": {"data": {"tag": "news_latest", **article_object.latest_payload(item_dot)},
                     "links": {"item": item_dot}},
        }], root=namespace)

    # 3) SIDECARS — flat unique leaves news.loc_refs.<article_id>-<key>, each back-ref the
    #    article and the geo location (no deep shared ancestors under geo.locations.<key>).
    n3 = 0
    if article_object.location_keys:
        ops = [{
            "path": f"{namespace}.loc_refs.{article_object.article_id}-{key}",
            "data": {"data": {"tag": "ref", "rel": "news", "title": article_object.title,
                              "published_day": article_object.published_day},
                     "links": {"item": item_dot, "location": f"{GEO_ROOT}.locations.{key}"}},
        } for key in article_object.location_keys]
        n3 = client_instance.write_ops(ops, root=namespace)

    # EV: three numbers, one line — latest=0 expected for 2nd+ item of a feed;
    # loc_refs=0 expected for articles with no matched location.
    print(f"[news] {item_dot} record={n1} latest={n2} loc_refs={n3}", flush=True)
    return {"item": item_dot, "latest": latest_dot}
