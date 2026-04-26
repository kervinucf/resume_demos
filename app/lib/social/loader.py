#!/usr/bin/env python3
"""
no_args_bluesky_firehose_loader.py

No-args Bluesky / AT Protocol stream ingester for HyperCore.

Modes controlled by constants below:

1. RAW_FIREHOSE
   - Connects to com.atproto.sync.subscribeRepos.
   - Ingests raw websocket frames as base64.
   - Stores raw firehose payloads without trying to decode CBOR/CAR.
   - Use this when you want the actual low-level firehose capture.

2. JETSTREAM_ALL
   - Connects to Jetstream with no wantedCollections.
   - Receives JSON events for all collections.

3. JETSTREAM_TOPICS
   - Connects to Jetstream with wantedCollections.
   - Use this for specific ATProto collection “topics”:
       app.bsky.feed.post
       app.bsky.feed.like
       app.bsky.graph.follow
       app.bsky.actor.profile
       app.bsky.graph.*
       app.bsky.*

4. JETSTREAM_TOPICS_PLUS_KEYWORDS
   - Same as JETSTREAM_TOPICS, plus client-side keyword/hashtag filtering.
   - Useful for “semantic topics” like hypergraph, chicago, sports, Trump, etc.

Important:
- Raw firehose is not JSON. This file stores raw frames safely.
- Jetstream is JSON and should be preferred for app-level ingestion.
- This file uses certifi for aiohttp TLS verification to avoid local CA issues.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import ssl
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import aiohttp
import certifi

from HyperCoreSDK.python.client import HyperClient
from HyperCoreSDK.python.helpers.server import create_hyper_server
from HyperCoreSDK.python.helpers.storage import create_default_storage_directory
from HyperCoreSDK.python.helpers.indexes import ScopeSpec, ValueIndexSpec


# =============================================================================
# NO-ARGS CONFIG
# =============================================================================

ATPROTO_ROOT = "atproto"

# Choose exactly one:
# MODE = "RAW_FIREHOSE"
# MODE = "JETSTREAM_ALL"
# MODE = "JETSTREAM_TOPICS"
MODE = "JETSTREAM_TOPICS_PLUS_KEYWORDS"

MAX_EVENTS = 500

RAW_FIREHOSE_URL = "wss://bsky.network/xrpc/com.atproto.sync.subscribeRepos"
JETSTREAM_URL = "wss://jetstream2.us-west.bsky.network/subscribe"

# “Topics” at the Jetstream server-filter level are collection NSIDs.
# Examples:
#   ["app.bsky.feed.post"]
#   ["app.bsky.feed.post", "app.bsky.feed.like", "app.bsky.graph.follow"]
#   ["app.bsky.graph.*"]
#   ["app.bsky.*"]
JETSTREAM_WANTED_COLLECTIONS = [
    "app.bsky.feed.post",
]

# Optional DID filter. Empty means all repos.
JETSTREAM_WANTED_DIDS: list[str] = []

# Client-side semantic filters. Only used in JETSTREAM_TOPICS_PLUS_KEYWORDS.
KEYWORDS = [
    "Chicago",
]

HASHTAGS: list[str] = []

# Linkback behavior.
WRITE_TOPICS = True
WRITE_URLS = True
WRITE_AUTHOR_LINKS = True
WRITE_RECORDS_FROM_JETSTREAM_COMMITS = True

# If true, write raw Jetstream event object too, not only extracted records.
WRITE_JETSTREAM_EVENT_NODES = True

# Safe default. Keep True. Only set False for local debugging if your CA bundle is broken.
VERIFY_SSL = True


# =============================================================================
# Indexes
# =============================================================================

RAW_FRAME_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec(
        name="source",
        path="source",
        normalize="slug",
        link_projections={
            "received_at": "received_at",
            "frame_kind": "frame_kind",
            "byte_length": "byte_length",
        },
    ),
    ValueIndexSpec(
        name="frame_kind",
        path="frame_kind",
        normalize="slug",
        scopes=[ScopeSpec(path="source", normalize="slug")],
        link_projections={
            "received_at": "received_at",
            "byte_length": "byte_length",
        },
    ),
]

JETSTREAM_EVENT_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec(
        name="kind",
        path="kind",
        normalize="slug",
        link_projections={
            "did": "did",
            "collection": "collection",
            "operation": "operation",
            "rkey": "rkey",
            "received_at": "received_at",
        },
    ),
    ValueIndexSpec(
        name="collection",
        path="collection",
        normalize="slug",
        link_projections={
            "did": "did",
            "operation": "operation",
            "rkey": "rkey",
            "received_at": "received_at",
        },
    ),
    ValueIndexSpec(
        name="collection",
        path="collection",
        normalize="slug",
        scopes=[ScopeSpec(path="did", normalize="slug")],
        link_projections={
            "did": "did",
            "operation": "operation",
            "rkey": "rkey",
            "received_at": "received_at",
        },
    ),
]

RECORD_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec(
        name="collection",
        path="collection",
        normalize="slug",
        link_projections={
            "did": "did",
            "rkey": "rkey",
            "record_type": "record_type",
            "created_at": "created_at",
        },
    ),
    ValueIndexSpec(
        name="record_type",
        path="record_type",
        normalize="slug",
        link_projections={
            "did": "did",
            "collection": "collection",
            "rkey": "rkey",
            "created_at": "created_at",
        },
    ),
    ValueIndexSpec(
        name="collection",
        path="collection",
        normalize="slug",
        scopes=[ScopeSpec(path="did", normalize="slug")],
        link_projections={
            "did": "did",
            "rkey": "rkey",
            "record_type": "record_type",
            "created_at": "created_at",
        },
    ),
]

TOPIC_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec(
        name="tag",
        path="tag",
        normalize="slug",
        link_projections={
            "display": "display",
            "observed_at": "observed_at",
        },
    ),
]

URL_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec(
        name="host",
        path="host",
        normalize="slug",
        link_projections={
            "url": "url",
            "observed_at": "observed_at",
        },
    ),
]


# =============================================================================
# Helpers
# =============================================================================

def make_aiohttp_session() -> aiohttp.ClientSession:
    """
    Create an aiohttp session with a certifi CA bundle.

    This fixes local Python installs that cannot verify websocket TLS certs,
    especially framework Python installs on macOS.
    """
    if VERIFY_SSL:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_context)
    else:
        connector = aiohttp.TCPConnector(ssl=False)

    return aiohttp.ClientSession(connector=connector)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ms(value: Any = None) -> int:
    if value is None:
        return int(time.time() * 1000)

    if isinstance(value, (int, float)):
        n = float(value)
        if abs(n) > 100000000000:
            return int(n)
        if abs(n) > 1000000000:
            return int(n * 1000)
        return int(time.time() * 1000)

    text = str(value or "").strip()
    if not text:
        return int(time.time() * 1000)

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return int(time.time() * 1000)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return int(dt.timestamp() * 1000)


def slug(value: Any, fallback: str = "unknown") -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "").strip().lower())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or fallback


def stable_hash(value: Any, length: int = 16) -> str:
    if isinstance(value, bytes):
        raw = value
    else:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:length]


def did_key(did: str) -> str:
    return slug(did, "did")


def collection_key(collection: str | None) -> str:
    return slug(collection or "collection", "collection")


def record_type(record: dict[str, Any]) -> str:
    return str(record.get("$type") or record.get("type") or record.get("kind") or "record")


def record_created_at(record: dict[str, Any]) -> str | None:
    for key in ("createdAt", "created_at", "indexedAt", "updatedAt", "updated_at"):
        value = record.get(key)
        if value:
            return str(value)
    return None


def extract_hashtags(text: str) -> list[str]:
    return sorted(set(re.findall(r"(?<!\w)#([A-Za-z0-9_]{2,64})", text or "")))


def extract_urls(text: str) -> list[str]:
    return sorted(set(re.findall(r"https?://[^\s<>\"]+", text or "")))


def url_host(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def stream_link(path: str) -> str:
    return f"{path}?stream=true"


def changes_link(path: str) -> str:
    return f"{path}/api/changes-since"


def jetstream_url(
    *,
    wanted_collections: list[str] | None = None,
    wanted_dids: list[str] | None = None,
) -> str:
    params: list[tuple[str, str]] = []

    for collection in wanted_collections or []:
        params.append(("wantedCollections", collection))

    for did in wanted_dids or []:
        params.append(("wantedDids", did))

    qs = urlencode(params)
    return f"{JETSTREAM_URL}?{qs}" if qs else JETSTREAM_URL


# =============================================================================
# Paths
# =============================================================================

def raw_frame_path(frame_bytes: bytes, received_at: str, count: int) -> str:
    dt = datetime.fromtimestamp(ms(received_at) / 1000, tz=timezone.utc)
    digest = stable_hash(frame_bytes, 16)

    return (
        f"{ATPROTO_ROOT}.raw_firehose.frames."
        f"{dt.year:04d}.{dt.month:02d}.{dt.day:02d}."
        f"{count:08d}-{digest}"
    )


def did_path(did: str) -> str:
    return f"{ATPROTO_ROOT}.dids.{did_key(did)}"


def repo_path(did: str) -> str:
    return f"{ATPROTO_ROOT}.repos.{did_key(did)}"


def collection_path(did: str, collection: str) -> str:
    return f"{ATPROTO_ROOT}.collections.{did_key(did)}.{collection_key(collection)}"


def record_path_abs(did: str, collection: str, rkey: str) -> str:
    return f"{ATPROTO_ROOT}.records.{did_key(did)}.{collection_key(collection)}.{slug(rkey, 'record')}"


def jetstream_event_path(event: dict[str, Any], received_at: str, count: int) -> str:
    dt = datetime.fromtimestamp(ms(received_at) / 1000, tz=timezone.utc)
    event_id = (
        event.get("cid")
        or event.get("seq")
        or event.get("time_us")
        or stable_hash(event, 16)
    )

    return (
        f"{ATPROTO_ROOT}.jetstream.events."
        f"{dt.year:04d}.{dt.month:02d}.{dt.day:02d}."
        f"{count:08d}-{slug(event_id, 'event')}"
    )


def topic_path(tag: str) -> str:
    return f"{ATPROTO_ROOT}.topics.{slug(tag, 'topic')}"


def url_path(url: str) -> str:
    host = url_host(url) or "url"
    return f"{ATPROTO_ROOT}.urls.{slug(host, 'host')}.{stable_hash(url, 12)}"


# =============================================================================
# Graph writers
# =============================================================================

def write_raw_firehose_frame(
    client: HyperClient,
    *,
    frame_bytes: bytes,
    received_at: str,
    count: int,
    message_type: str,
) -> str:
    path = raw_frame_path(frame_bytes, received_at, count)
    digest = stable_hash(frame_bytes, 40)

    body = {
        "model": "atproto-raw-firehose-frame",
        "source": "com.atproto.sync.subscribeRepos",
        "frame_kind": message_type,
        "sha1": digest,
        "byte_length": len(frame_bytes),
        "encoding": "base64",
        "payload_b64": base64.b64encode(frame_bytes).decode("ascii"),
        "received_at": received_at,
        "activity_latest_at": ms(received_at),
        "note": "Raw subscribeRepos frame. Decode separately as CBOR/CAR if needed.",
    }

    rel = path.replace(f"{ATPROTO_ROOT}.", "").replace(".", "/")

    client.write_record_with_indexes(
        root=ATPROTO_ROOT,
        record_path=rel,
        record_data=body,
        index_specs=RAW_FRAME_INDEXES,
        ref_key=path.rsplit(".", 1)[-1],
        ref_payload={
            "source": body["source"],
            "frame_kind": message_type,
            "byte_length": len(frame_bytes),
            "received_at": received_at,
        },
    )

    client.put(
        path=path,
        kind="atproto_raw_firehose_frame",
        name=f"Raw firehose frame {count}",
        body=body,
        links={
            "stream": stream_link(path),
            "changes_since": changes_link(path),
        },
    )

    return path


def ensure_repo_shell(client: HyperClient, did: str, observed_at: str) -> None:
    dpath = did_path(did)
    rpath = repo_path(did)

    client.put(
        path=dpath,
        kind="atproto_did",
        name=did,
        target=rpath,
        body={
            "model": "atproto-did",
            "did": did,
            "did_method": did.split(":")[1] if did.startswith("did:") and len(did.split(":")) > 1 else "unknown",
            "observed_at": observed_at,
            "activity_latest_at": ms(observed_at),
        },
        links={
            "repo": rpath,
            "stream": stream_link(dpath),
            "changes_since": changes_link(dpath),
        },
    )

    client.put(
        path=rpath,
        kind="atproto_repo",
        name=did,
        target=dpath,
        body={
            "model": "atproto-repo",
            "did": did,
            "repo_did": did,
            "observed_at": observed_at,
            "activity_latest_at": ms(observed_at),
        },
        links={
            "did": dpath,
            "collections": f"{ATPROTO_ROOT}.collections.{did_key(did)}",
            "stream": stream_link(rpath),
            "changes_since": changes_link(rpath),
        },
    )


def write_collection_shell(
    client: HyperClient,
    *,
    did: str,
    collection: str,
    observed_at: str,
) -> str:
    path = collection_path(did, collection)

    client.put(
        path=path,
        kind="atproto_collection",
        name=collection,
        target=repo_path(did),
        body={
            "model": "atproto-collection",
            "did": did,
            "repo_did": did,
            "collection": collection,
            "collection_key": collection_key(collection),
            "observed_at": observed_at,
            "activity_latest_at": ms(observed_at),
        },
        links={
            "repo": repo_path(did),
            "did": did_path(did),
            "records": f"{ATPROTO_ROOT}.records.{did_key(did)}.{collection_key(collection)}",
        },
    )

    client.link(
        source=repo_path(did),
        rel=f"collections.{collection_key(collection)}",
        target=path,
        kind="atproto_collection",
        name=collection,
        body={
            "did": did,
            "collection": collection,
        },
        links={
            "repo": repo_path(did),
            "collection": path,
        },
    )

    return path


def ensure_topic(client: HyperClient, tag: str, observed_at: str) -> str:
    path = topic_path(tag)
    body = {
        "model": "topic",
        "tag": tag,
        "display": f"#{tag}",
        "observed_at": observed_at,
        "activity_latest_at": ms(observed_at),
    }

    client.write_record_with_indexes(
        root=ATPROTO_ROOT,
        record_path=f"topics/{slug(tag, 'topic')}",
        record_data=body,
        index_specs=TOPIC_INDEXES,
        ref_key=slug(tag, "topic"),
        ref_payload={
            "tag": tag,
            "display": f"#{tag}",
            "observed_at": observed_at,
        },
    )

    client.put(
        path=path,
        kind="topic",
        name=f"#{tag}",
        body=body,
        links={
            "stream": stream_link(path),
            "changes_since": changes_link(path),
        },
    )

    return path


def ensure_url(client: HyperClient, url: str, observed_at: str) -> str:
    path = url_path(url)
    host = url_host(url)

    body = {
        "model": "external-url",
        "url": url,
        "host": host,
        "observed_at": observed_at,
        "activity_latest_at": ms(observed_at),
    }

    client.write_record_with_indexes(
        root=ATPROTO_ROOT,
        record_path=f"urls/{slug(host, 'host')}/{stable_hash(url, 12)}",
        record_data=body,
        index_specs=URL_INDEXES,
        ref_key=stable_hash(url, 12),
        ref_payload={
            "url": url,
            "host": host,
            "observed_at": observed_at,
        },
    )

    client.put(
        path=path,
        kind="external_url",
        name=host or url,
        body=body,
        links={},
    )

    return path


def write_topic_links(
    client: HyperClient,
    *,
    record_path: str,
    tags: list[str],
    observed_at: str,
) -> int:
    count = 0
    record_tail = record_path.rsplit(".", 1)[-1]

    for tag in tags:
        topic = ensure_topic(client, tag, observed_at)

        client.link(
            source=record_path,
            rel=f"topics.{slug(tag)}",
            target=topic,
            kind="topic_ref",
            name=f"#{tag}",
            body={
                "tag": tag,
                "record": record_path,
            },
            links={
                "record": record_path,
                "topic": topic,
            },
        )
        count += 1

        client.link(
            source=topic,
            rel=f"records.{record_tail}",
            target=record_path,
            kind="topic_record_ref",
            name=f"Post tagged #{tag}",
            body={
                "tag": tag,
                "record": record_path,
            },
            links={
                "topic": topic,
                "record": record_path,
            },
        )
        count += 1

    return count


def write_url_links(
    client: HyperClient,
    *,
    record_path: str,
    urls: list[str],
    observed_at: str,
) -> int:
    count = 0
    record_tail = record_path.rsplit(".", 1)[-1]

    for url in urls:
        target = ensure_url(client, url, observed_at)

        client.link(
            source=record_path,
            rel=f"urls.{stable_hash(url, 8)}",
            target=target,
            kind="external_url_ref",
            name=url_host(url) or url,
            body={
                "url": url,
                "host": url_host(url),
                "record": record_path,
            },
            links={
                "record": record_path,
                "url": target,
            },
        )
        count += 1

        client.link(
            source=target,
            rel=f"records.{record_tail}",
            target=record_path,
            kind="url_record_ref",
            name=f"Record mentioning {url_host(url) or url}",
            body={
                "url": url,
                "host": url_host(url),
                "record": record_path,
            },
            links={
                "url": target,
                "record": record_path,
            },
        )
        count += 1

    return count


def write_jetstream_event(
    client: HyperClient,
    *,
    event: dict[str, Any],
    received_at: str,
    count: int,
) -> str:
    commit = event.get("commit") if isinstance(event.get("commit"), dict) else {}
    did = str(event.get("did") or commit.get("repo") or "")
    collection = str(commit.get("collection") or event.get("collection") or "")
    rkey = str(commit.get("rkey") or event.get("rkey") or "")
    operation = str(commit.get("operation") or "")
    record = commit.get("record") if isinstance(commit.get("record"), dict) else {}

    path = jetstream_event_path(event, received_at, count)

    body = {
        "model": "atproto-jetstream-event",
        "kind": event.get("kind"),
        "did": did or None,
        "repo_did": did or None,
        "collection": collection or None,
        "collection_key": collection_key(collection) if collection else None,
        "operation": operation or None,
        "rkey": rkey or None,
        "cid": commit.get("cid") or event.get("cid"),
        "time_us": event.get("time_us"),
        "received_at": received_at,
        "activity_latest_at": ms(received_at),
        "record_type": record_type(record) if record else None,
        "text": record.get("text") if isinstance(record.get("text"), str) else None,
        "raw": event,
    }

    links: dict[str, Any] = {}

    if did:
        ensure_repo_shell(client, did, received_at)
        links["did"] = did_path(did)
        links["repo"] = repo_path(did)

    if did and collection:
        write_collection_shell(client, did=did, collection=collection, observed_at=received_at)
        links["collection"] = collection_path(did, collection)

    if did and collection and rkey:
        links["record"] = record_path_abs(did, collection, rkey)

    rel = path.replace(f"{ATPROTO_ROOT}.", "").replace(".", "/")

    client.write_record_with_indexes(
        root=ATPROTO_ROOT,
        record_path=rel,
        record_data=body,
        index_specs=JETSTREAM_EVENT_INDEXES,
        ref_key=path.rsplit(".", 1)[-1],
        ref_payload={
            "kind": body.get("kind"),
            "did": did,
            "collection": collection,
            "operation": operation,
            "rkey": rkey,
            "received_at": received_at,
        },
    )

    client.put(
        path=path,
        kind="atproto_jetstream_event",
        name=f"{body.get('kind') or 'event'} {collection} {rkey}".strip(),
        target=links.get("record") or links.get("repo") or path,
        body=body,
        links=links,
    )

    if did:
        client.link(
            source=repo_path(did),
            rel=f"jetstream_events.{path.rsplit('.', 1)[-1]}",
            target=path,
            kind="atproto_jetstream_event",
            name=f"{collection} {operation}".strip(),
            body={
                "kind": body.get("kind"),
                "collection": collection,
                "operation": operation,
                "rkey": rkey,
                "received_at": received_at,
            },
            links={
                "repo": repo_path(did),
                "event": path,
                **({"record": links["record"]} if "record" in links else {}),
            },
        )

    return path


def write_record_from_jetstream_commit(
    client: HyperClient,
    *,
    event: dict[str, Any],
    received_at: str,
) -> str | None:
    commit = event.get("commit") if isinstance(event.get("commit"), dict) else {}
    operation = str(commit.get("operation") or "").lower()

    if operation not in {"create", "update"}:
        return None

    did = str(event.get("did") or commit.get("repo") or "")
    collection = str(commit.get("collection") or "")
    rkey = str(commit.get("rkey") or "")
    record = commit.get("record") if isinstance(commit.get("record"), dict) else {}

    if not did or not collection or not rkey or not record:
        return None

    ensure_repo_shell(client, did, received_at)
    write_collection_shell(client, did=did, collection=collection, observed_at=received_at)

    path = record_path_abs(did, collection, rkey)
    created_at = record_created_at(record)
    text = record.get("text") if isinstance(record.get("text"), str) else ""

    body = {
        "model": "atproto-record",
        "did": did,
        "repo_did": did,
        "collection": collection,
        "collection_key": collection_key(collection),
        "rkey": rkey,
        "uri": f"at://{did}/{collection}/{rkey}",
        "cid": commit.get("cid"),
        "operation": operation,
        "record_type": record_type(record),
        "created_at": created_at,
        "received_at": received_at,
        "activity_latest_at": max(ms(created_at), ms(received_at)) if created_at else ms(received_at),
        "text": text,
        "langs": record.get("langs") if isinstance(record.get("langs"), list) else None,
        "has_text": bool(text),
        "has_embed": bool(record.get("embed")),
        "has_reply": bool(record.get("reply")),
        "hashtags": extract_hashtags(text),
        "urls": extract_urls(text),
        "value": record,
    }

    client.write_record_with_indexes(
        root=ATPROTO_ROOT,
        record_path=f"records/{did_key(did)}/{collection_key(collection)}/{slug(rkey, 'record')}",
        record_data=body,
        index_specs=RECORD_INDEXES,
        ref_key=f"{did_key(did)}-{collection_key(collection)}-{slug(rkey, 'record')}",
        ref_payload={
            "did": did,
            "collection": collection,
            "rkey": rkey,
            "record_type": body["record_type"],
            "created_at": created_at,
        },
    )

    client.put(
        path=path,
        kind="atproto_record",
        name=f"{collection}/{rkey}",
        target=collection_path(did, collection),
        body=body,
        links={
            "did": did_path(did),
            "repo": repo_path(did),
            "collection": collection_path(did, collection),
            "author": did_path(did),
            "stream": stream_link(path),
            "changes_since": changes_link(path),
        },
    )

    client.link(
        source=collection_path(did, collection),
        rel=f"records.{slug(rkey, 'record')}",
        target=path,
        kind="atproto_record",
        name=f"{collection}/{rkey}",
        body={
            "did": did,
            "collection": collection,
            "rkey": rkey,
            "record_type": body["record_type"],
            "created_at": created_at,
        },
        links={
            "collection": collection_path(did, collection),
            "record": path,
            "repo": repo_path(did),
        },
    )

    if WRITE_AUTHOR_LINKS:
        client.link(
            source=did_path(did),
            rel=f"records.{collection_key(collection)}.{slug(rkey, 'record')}",
            target=path,
            kind="atproto_author_record",
            name=f"{collection}/{rkey}",
            body={
                "did": did,
                "collection": collection,
                "rkey": rkey,
                "record_type": body["record_type"],
                "created_at": created_at,
            },
            links={
                "did": did_path(did),
                "record": path,
                "collection": collection_path(did, collection),
            },
        )

    if collection == "app.bsky.feed.post":
        if WRITE_TOPICS:
            write_topic_links(
                client,
                record_path=path,
                tags=extract_hashtags(text),
                observed_at=received_at,
            )

        if WRITE_URLS:
            write_url_links(
                client,
                record_path=path,
                urls=extract_urls(text),
                observed_at=received_at,
            )

    return path


# =============================================================================
# Filtering
# =============================================================================

def event_text(event: dict[str, Any]) -> str:
    commit = event.get("commit") if isinstance(event.get("commit"), dict) else {}
    record = commit.get("record") if isinstance(commit.get("record"), dict) else {}
    return str(record.get("text") or "")


def passes_semantic_topic_filter(event: dict[str, Any]) -> bool:
    if MODE != "JETSTREAM_TOPICS_PLUS_KEYWORDS":
        return True

    text = event_text(event).lower()
    tags = {tag.lower() for tag in extract_hashtags(text)}

    for keyword in KEYWORDS:
        if keyword.lower() in text:
            return True

    for tag in HASHTAGS:
        if tag.lower().removeprefix("#") in tags:
            return True

    return False


# =============================================================================
# Stream loops
# =============================================================================

async def ingest_raw_firehose(client: HyperClient) -> int:
    print(f"raw firehose: {RAW_FIREHOSE_URL}")
    print("storing raw websocket frames as base64; no CBOR/CAR decoding in this file")

    count = 0

    async with make_aiohttp_session() as session:
        async with session.ws_connect(RAW_FIREHOSE_URL) as ws:
            async for msg in ws:
                if count >= MAX_EVENTS:
                    break

                received_at = now_iso()

                if msg.type == aiohttp.WSMsgType.BINARY:
                    frame = bytes(msg.data)
                    count += 1
                    path = write_raw_firehose_frame(
                        client,
                        frame_bytes=frame,
                        received_at=received_at,
                        count=count,
                        message_type="binary",
                    )

                elif msg.type == aiohttp.WSMsgType.TEXT:
                    frame = msg.data.encode("utf-8")
                    count += 1
                    path = write_raw_firehose_frame(
                        client,
                        frame_bytes=frame,
                        received_at=received_at,
                        count=count,
                        message_type="text",
                    )

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    print(f"raw firehose websocket error: {msg.data}")
                    break

                else:
                    continue

                if count % 25 == 0:
                    print(f"  raw frames: {count} latest={path}")

    print(f"raw firehose done — wrote {count} frame(s)")
    return count


async def ingest_jetstream(
    client: HyperClient,
    *,
    wanted_collections: list[str] | None,
    wanted_dids: list[str] | None,
) -> int:
    url = jetstream_url(
        wanted_collections=wanted_collections,
        wanted_dids=wanted_dids,
    )

    print(f"jetstream: {url}")

    count = 0

    async with make_aiohttp_session() as session:
        async with session.ws_connect(url) as ws:
            async for msg in ws:
                if count >= MAX_EVENTS:
                    break

                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type == aiohttp.WSMsgType.ERROR:
                        print(f"jetstream websocket error: {msg.data}")
                        break
                    continue

                try:
                    event = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                if not isinstance(event, dict):
                    continue

                if not passes_semantic_topic_filter(event):
                    continue

                count += 1
                received_at = now_iso()

                if WRITE_JETSTREAM_EVENT_NODES:
                    event_path = write_jetstream_event(
                        client,
                        event=event,
                        received_at=received_at,
                        count=count,
                    )
                else:
                    event_path = ""

                record_path = None
                if WRITE_RECORDS_FROM_JETSTREAM_COMMITS:
                    record_path = write_record_from_jetstream_commit(
                        client,
                        event=event,
                        received_at=received_at,
                    )

                if count % 25 == 0:
                    print(f"  jetstream events: {count} latest_event={event_path} latest_record={record_path}")

    print(f"jetstream done — processed {count} event(s)")
    return count


# =============================================================================
# Entrypoint
# =============================================================================

def run() -> int:
    client = create_hyper_server(
        root=ATPROTO_ROOT,
        data_path=create_default_storage_directory(),
    )

    try:
        if MODE == "RAW_FIREHOSE":
            total = asyncio.run(ingest_raw_firehose(client))

        elif MODE == "JETSTREAM_ALL":
            total = asyncio.run(
                ingest_jetstream(
                    client,
                    wanted_collections=[],
                    wanted_dids=JETSTREAM_WANTED_DIDS,
                )
            )

        elif MODE == "JETSTREAM_TOPICS":
            total = asyncio.run(
                ingest_jetstream(
                    client,
                    wanted_collections=JETSTREAM_WANTED_COLLECTIONS,
                    wanted_dids=JETSTREAM_WANTED_DIDS,
                )
            )

        elif MODE == "JETSTREAM_TOPICS_PLUS_KEYWORDS":
            total = asyncio.run(
                ingest_jetstream(
                    client,
                    wanted_collections=JETSTREAM_WANTED_COLLECTIONS,
                    wanted_dids=JETSTREAM_WANTED_DIDS,
                )
            )

        else:
            raise ValueError(f"Unknown MODE: {MODE}")

        print(f"done — total={total}")
        return 0

    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(run())