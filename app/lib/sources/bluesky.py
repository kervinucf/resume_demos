from __future__ import annotations

import json
import re
import ssl
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from urllib.parse import urlencode, urlparse

import aiohttp
import certifi


JETSTREAM_URL = "wss://jetstream2.us-west.bsky.network/subscribe"
DEFAULT_WANTED_COLLECTIONS = ["app.bsky.feed.post"]
VERIFY_SSL = True

_WORD_RE = re.compile(r"[a-zA-Z0-9]+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ms(value: Any = None) -> int:
    if value is None:
        return int(time.time() * 1000)

    if isinstance(value, (int, float)):
        n = float(value)

        if abs(n) > 100_000_000_000:
            return int(n)

        if abs(n) > 1_000_000_000:
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


def _tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in _WORD_RE.findall(text or "")
        if token
    }


def _contains_phrase_or_token(text_lc: str, tokens: set[str], value: str) -> bool:
    value_lc = str(value or "").strip().lower()

    if not value_lc:
        return False

    if " " in value_lc:
        return value_lc in text_lc

    return value_lc in tokens


def _record_type(record: dict[str, Any]) -> str:
    return str(record.get("$type") or record.get("type") or record.get("tag") or "record")


def _record_created_at(record: dict[str, Any]) -> str | None:
    for key in ("createdAt", "created_at", "indexedAt", "updatedAt", "updated_at"):
        if record.get(key):
            return str(record[key])

    return None


def _extract_hashtags(text: str) -> list[str]:
    return sorted(
        set(
            re.findall(
                r"(?<!\w)#([A-Za-z0-9_]{2,64})",
                text or "",
            )
        )
    )


def _extract_urls(text: str) -> list[str]:
    return sorted(
        set(
            re.findall(
                r"https?://[^\s<>\"]+",
                text or "",
            )
        )
    )


def _url_host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def _make_session() -> aiohttp.ClientSession:
    if VERIFY_SSL:
        ctx = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ctx)
    else:
        connector = aiohttp.TCPConnector(ssl=False)

    return aiohttp.ClientSession(connector=connector)


def _jetstream_url(
    wanted_collections: list[str] | None,
    wanted_dids: list[str] | None,
) -> str:
    params: list[tuple[str, str]] = []

    for collection in wanted_collections or []:
        params.append(("wantedCollections", collection))

    for did in wanted_dids or []:
        params.append(("wantedDids", did))

    qs = urlencode(params)

    return f"{JETSTREAM_URL}?{qs}" if qs else JETSTREAM_URL


def source_available() -> tuple[bool, str]:
    return True, JETSTREAM_URL


def _passes_keywords(text: str, keywords: list[str], hashtags: list[str]) -> bool:
    if not keywords and not hashtags:
        return True

    text_lc = str(text or "").lower()
    tokens = _tokens(text_lc)
    tags = {tag.lower() for tag in _extract_hashtags(text)}

    if hashtags and any(tag.lower().removeprefix("#") in tags for tag in hashtags):
        return True

    for keyword in keywords:
        keyword_lc = str(keyword or "").strip().lower()

        if not keyword_lc:
            continue

        if " " in keyword_lc:
            if keyword_lc in text_lc:
                return True
        elif keyword_lc in tokens:
            return True

    return False

def _record_from_event(
    event: dict[str, Any],
    received_at: str,
) -> dict[str, Any] | None:
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

    text = record.get("text") if isinstance(record.get("text"), str) else ""
    created_at = _record_created_at(record)

    return {
        "did": did,
        "collection": collection,
        "rkey": rkey,
        "operation": operation,
        "cid": commit.get("cid"),
        "uri": f"at://{did}/{collection}/{rkey}",
        "record_type": _record_type(record),
        "created_at": created_at,
        "received_at": received_at,
        "activity_latest_at": max(_ms(created_at), _ms(received_at)) if created_at else _ms(received_at),
        "text": text,
        "langs": record.get("langs") if isinstance(record.get("langs"), list) else None,
        "has_text": bool(text),
        "has_embed": bool(record.get("embed")),
        "has_reply": bool(record.get("reply")),
        "hashtags": _extract_hashtags(text),
        "urls": _extract_urls(text),
        "time_us": event.get("time_us"),
        "value": record,
    }


async def iter_post_events(
    *,
    wanted_collections: list[str] | None = None,
    wanted_dids: list[str] | None = None,
    keywords: list[str] | None = None,
    hashtags: list[str] | None = None,
    max_events: int = 500,
) -> AsyncIterator[dict[str, Any]]:
    url = _jetstream_url(
        wanted_collections or DEFAULT_WANTED_COLLECTIONS,
        wanted_dids,
    )

    keywords = keywords or []
    hashtags = hashtags or []

    print(f"  jetstream: {url}", flush=True)
    print(f"  filter: keywords={keywords} hashtags={hashtags}", flush=True)

    count = 0

    async with _make_session() as session:
        async with session.ws_connect(url) as ws:
            async for msg in ws:
                if count >= max_events:
                    break

                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type == aiohttp.WSMsgType.ERROR:
                        print(f"  jetstream websocket error: {msg.data}", flush=True)
                        break

                    continue

                try:
                    event = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                if not isinstance(event, dict):
                    continue

                commit = event.get("commit") if isinstance(event.get("commit"), dict) else {}
                record = commit.get("record") if isinstance(commit.get("record"), dict) else {}

                text = record.get("text") if isinstance(record.get("text"), str) else ""

                if not _passes_keywords(
                    text=text,
                    keywords=keywords,
                    hashtags=hashtags,
                ):
                    continue

                record_event = _record_from_event(
                    event=event,
                    received_at=_now_iso(),
                )

                if record_event is None:
                    continue

                count += 1
                yield record_event