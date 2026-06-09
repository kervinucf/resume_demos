from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

from HyperCoreSDK.python.client import create_default_storage_directory

from app.lib.helpers.social import (
    HyperClient,
    apply_graph_operations,
    iter_record_candidates,
    create_record_object,
)


ATPROTO_ROOT = "atproto"

DATA_DIR = str(
    Path(os.getenv("HYPER_DATA_DIR", create_default_storage_directory()))
    .expanduser()
    .resolve()
)

WANTED_COLLECTIONS = ["app.bsky.feed.post"]

# Broad social topics. This worker is not responsible for precise matching.
# It just keeps a useful social pool warm.
SOCIAL_STREAM_KEYWORDS = [
    # weather
    "weather",
    "rain",
    "storm",
    "thunderstorm",
    "heat",
    "hot",
    "snow",
    "flood",
    "forecast",

    # news
    "breaking",
    "news",
    "report",
    "update",

    # sports
    "game",
    "score",
    "goal",
    "win",
    "loss",
    "trade",

    # earthquakes
    "earthquake",
    "quake",
    "magnitude",
    "aftershock",

    # finance
    "stock",
    "market",
    "earnings",
    "fed",
    "inflation",
    "crypto",
    "bitcoin",
]

MAX_EVENTS_PER_SESSION = 10_000
RECONNECT_SLEEP_SECONDS = 5


async def ingest_social_stream_once(
    *,
    data_store: HyperClient,
) -> int:
    written = 0
    skipped = 0

    candidates = iter_record_candidates(
        wanted_collections=WANTED_COLLECTIONS,
        wanted_dids=None,
        keywords=SOCIAL_STREAM_KEYWORDS,
        hashtags=None,
        max_events=MAX_EVENTS_PER_SESSION,
    )

    try:
        async for record_event, candidate_proof in candidates:
            if not candidate_proof:
                skipped += 1
                continue

            record_object, object_proof = create_record_object(record_event)

            if not object_proof or not record_object:
                skipped += 1
                continue

            apply_graph_operations(
                record_object=record_object,
                client_instance=data_store,
                namespace=ATPROTO_ROOT,
            )

            text = str(record_event.get("text") or "").replace("\n", " ")

            print(
                f"[social-stream] saved {record_object.record_key()} "
                f"text={text[:140]}",
                flush=True,
            )

            written += 1

    finally:
        close = getattr(candidates, "aclose", None)

        if close:
            await close()

    print(
        f"[social-stream] session complete written={written:,} skipped={skipped:,}",
        flush=True,
    )

    return written


async def run_forever() -> int:
    print("[social-stream] starting persistent Jetstream worker", flush=True)
    print(f"[social-stream] data dir: {DATA_DIR}", flush=True)

    while True:
        data_store = HyperClient(
            root_key=ATPROTO_ROOT,
            data_dir=DATA_DIR,
        )

        try:
            await ingest_social_stream_once(
                data_store=data_store,
            )
        except KeyboardInterrupt:
            print("[social-stream] stopped", flush=True)
            return 0
        except Exception as exc:
            print(f"[social-stream] error: {type(exc).__name__}: {exc}", flush=True)
        finally:
            data_store.close()

        print(
            f"[social-stream] reconnecting after {RECONNECT_SLEEP_SECONDS}s",
            flush=True,
        )

        await asyncio.sleep(RECONNECT_SLEEP_SECONDS)


def main() -> int:
    return asyncio.run(run_forever())


if __name__ == "__main__":
    raise SystemExit(main())