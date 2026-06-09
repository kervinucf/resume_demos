"""
ATProto operations — the verbs the main loader orchestrates.

Mirrors app/lib/helpers/geo/__init__.py and weather/__init__.py: the provider is
read inside the create_* functions, so the loader only ever imports:

    from app.lib.helpers.atproto import (apply_graph_operations,
                                         iter_record_candidates,
                                         create_record_object,
                                         AtprotoRecordObject)

This module is the ONE place that names the concrete provider. To switch providers,
change the `app.lib.sources.bluesky` import below to another module exposing the same
source callables — nothing else changes.
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from HyperCoreSDK.python.client import HyperClient, projection, ScopeSpec, ValueIndexSpec
# --- provider boundary: swap this single import to change source ------------
from app.lib.sources.bluesky import iter_post_events, source_available
# ---------------------------------------------------------------------------
from app.lib.helpers.social.factory import AtprotoFactory, AtprotoRecordObject

atproto_factory = AtprotoFactory()

__all__ = [
    "HyperClient",
    "iter_record_candidates",
    "create_record_object",
    "apply_graph_operations",
    "source_available",
    "AtprotoRecordObject",
]

ATPROTO_ROOT = "atproto"

PROJECT = projection("did", "collection", "rkey", "record_type", "created_at")
RECORD_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec("collection", "collection", normalize="slug", link_projections=PROJECT),
    ValueIndexSpec("record_type", "record_type", normalize="slug", link_projections=PROJECT),
    ValueIndexSpec(
        "collection", "collection", normalize="slug",
        scopes=[ScopeSpec("did", normalize="slug")], link_projections=PROJECT,
    ),
]


async def iter_record_candidates(
        *,
        wanted_collections: list[str] | None = None,
        wanted_dids: list[str] | None = None,
        keywords: list[str] | None = None,
        hashtags: list[str] | None = None,
        max_events: int = 500,
) -> AsyncIterator[tuple[dict[str, Any], bool]]:
    async for record_event in iter_post_events(
            wanted_collections=wanted_collections, wanted_dids=wanted_dids,
            keywords=keywords, hashtags=hashtags, max_events=max_events,
    ):
        proof = bool(record_event.get("did")) and bool(record_event.get("collection")) \
            and bool(record_event.get("rkey"))
        if not proof:
            print(f"  skip: bad record event ({record_event.get('uri')})", flush=True)
            continue
        yield record_event, proof


def create_record_object(
        record_event: dict[str, Any],
) -> tuple[AtprotoRecordObject | None, bool]:
    try:
        record_object = atproto_factory.create_record_object(record_event=record_event)
    except (TypeError, ValueError) as exc:
        print(f"  skip: bad record event ({exc})", flush=True)
        return None, False

    proof = bool(record_object.did) and bool(record_object.collection) and bool(record_object.rkey)
    if not proof:
        print(f"  skip: incomplete record object ({record_event.get('uri')})", flush=True)
    return record_object, proof


def apply_graph_operations(
        record_object: AtprotoRecordObject,
        client_instance: HyperClient,
        namespace,
) -> dict[str, str]:
    record_key = record_object.record_key()
    record_path = f"records/{record_key}"
    record_dot = f"{namespace}.{record_path.replace('/', '.')}"

    did_dot = f"{namespace}.dids.{record_object.did_key}"
    coll_dot = f"{namespace}.collections.{record_object.did_key}-{record_object.collection_key}"

    record_data = {"tag": "atproto_record", "model": "atproto-record", **record_object.__dict__}

    # 1) RECORD — indexed, dense. ref_key is the flat per-record key (unique).
    n1 = client_instance.save_record(
        path=record_path,
        data=record_data,
        indexes=RECORD_INDEXES,
        links={"did": did_dot, "collection": coll_dot, "author": did_dot},
        root=namespace,
    )

    # 2) SHELLS — flat did + collection nodes (upsert; live batch dedupes), and the
    #    author backref. Shared shells re-upsert harmlessly across records.
    n2 = client_instance.write_ops([
        {"path": did_dot,
         "data": {"data": {"tag": "atproto_did", "did": record_object.did,
                           "activity_latest_at": record_object.activity_latest_at},
                  "links": {"collections": coll_dot}}},
        {"path": coll_dot,
         "data": {"data": {"tag": "atproto_collection", "did": record_object.did,
                           "collection": record_object.collection,
                           "activity_latest_at": record_object.activity_latest_at},
                  "links": {"did": did_dot}}},
        {"path": f"{did_dot}.refs.records.{record_key}",
         "data": {"data": {"tag": "ref", "rel": "author_record",
                           "collection": record_object.collection, "rkey": record_object.rkey},
                  "links": {"record": record_dot, "collection": coll_dot}}},
    ], root=namespace)

    # EV: two numbers, one line. NOTE: topic/url enrichment and separate jetstream
    # event nodes are deferred — same flat-sidecar pattern once records run green.
    print(f"[atproto] {record_dot} record={n1} shells={n2}", flush=True)
    return {"record": record_dot, "did": did_dot, "collection": coll_dot}