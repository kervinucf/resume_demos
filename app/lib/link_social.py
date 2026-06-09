from __future__ import annotations

import re
from typing import Any

from app.lib.helpers.social import HyperClient
from app.lib.helpers.common import is_due, mark_ran


ATPROTO_ROOT = "atproto"

SOCIAL_LINK_WAIT_SECONDS = 30 * 60
DEFAULT_LOOKBACK_LIMIT = 1_000
DEFAULT_MAX_LINKS_PER_EVENT = 25

_SAFE_KEY_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_WORD_RE = re.compile(r"[a-zA-Z0-9]+")


WEATHER_WORDS = {
    "weather",
    "rain",
    "raining",
    "rainy",
    "storm",
    "storms",
    "storming",
    "thunder",
    "thunderstorm",
    "thunderstorms",
    "lightning",
    "flood",
    "floods",
    "flooding",
    "heat",
    "hot",
    "cold",
    "snow",
    "snowing",
    "wind",
    "windy",
    "humid",
    "humidity",
    "forecast",
    "temperature",
    "temp",
    "overcast",
    "clear",
    "cloudy",
    "clouds",
    "sunny",
    "fog",
    "foggy",
    "hail",
}


NEWS_WORDS = {
    "breaking",
    "news",
    "report",
    "reports",
    "reported",
    "update",
    "updates",
    "developing",
    "confirmed",
}


SPORTS_WORDS = {
    "game",
    "score",
    "scored",
    "goal",
    "win",
    "won",
    "loss",
    "lost",
    "trade",
    "injury",
    "match",
    "final",
    "season",
}


EARTHQUAKE_WORDS = {
    "earthquake",
    "quake",
    "magnitude",
    "aftershock",
    "tremor",
    "epicenter",
    "seismic",
}


FINANCE_WORDS = {
    "stock",
    "stocks",
    "market",
    "markets",
    "earnings",
    "revenue",
    "profit",
    "loss",
    "fed",
    "inflation",
    "crypto",
    "bitcoin",
    "price",
    "shares",
}


DOMAIN_WORDS = {
    "weather": WEATHER_WORDS,
    "news": NEWS_WORDS,
    "sports": SPORTS_WORDS,
    "earthquakes": EARTHQUAKE_WORDS,
    "finance": FINANCE_WORDS,
}


def safe_key(value: Any, fallback: str = "item") -> str:
    text = _SAFE_KEY_RE.sub("-", str(value or "").strip()).strip("-")
    return text or fallback


def text_tokens(value: str) -> set[str]:
    return {
        token.lower()
        for token in _WORD_RE.findall(value or "")
        if token
    }


def contains_phrase_or_token(
    *,
    text_lc: str,
    tokens: set[str],
    value: str,
) -> bool:
    value_lc = str(value or "").strip().lower()

    if not value_lc:
        return False

    if " " in value_lc:
        return value_lc in text_lc

    return value_lc in tokens


def record_key_from_record(record: dict[str, Any]) -> str:
    did = str(record.get("did") or "").strip()
    collection = str(record.get("collection") or "").strip()
    rkey = str(record.get("rkey") or "").strip()

    # Match the current AtprotoRecordObject style as closely as possible.
    did_key = safe_key(did.replace(":", "-"), "did")
    collection_key = safe_key(collection.replace(".", "-"), "collection")
    rkey_key = safe_key(rkey, "rkey")

    return f"{did_key}-{collection_key}-{rkey_key}"


def record_dot_path(record: dict[str, Any]) -> str:
    return f"{ATPROTO_ROOT}.records.{record_key_from_record(record)}"


def social_ref_path(
    *,
    domain: str,
    event_key: str,
    record_key: str,
) -> str:
    return (
        f"{ATPROTO_ROOT}.{safe_key(domain)}_refs."
        f"{safe_key(event_key, 'event')}."
        f"records."
        f"{safe_key(record_key, 'record')}"
    )


def social_link_state_name(
    *,
    domain: str,
    event_key: str,
) -> str:
    return f"social_links/{safe_key(domain)}/{safe_key(event_key, 'event')}"


def event_key(event: dict[str, Any]) -> str:
    return str(
        event.get("event_key")
        or event.get("record_key")
        or event.get("id")
        or event.get("key")
        or ""
    )


def event_entities(event: dict[str, Any]) -> list[str]:
    values: list[str] = []

    for key in (
        "entity_names",
        "entities",
        "teams",
        "players",
        "symbols",
        "tickers",
        "companies",
        "locations",
        "places",
    ):
        raw = event.get(key)

        if isinstance(raw, list):
            values.extend(str(item) for item in raw if str(item).strip())
        elif isinstance(raw, str) and raw.strip():
            values.append(raw)

    for key in (
        "location_name",
        "city",
        "country",
        "team",
        "home_team",
        "away_team",
        "ticker",
        "symbol",
        "company",
        "title",
    ):
        raw = event.get(key)

        if isinstance(raw, str) and raw.strip():
            values.append(raw)

    deduped: list[str] = []
    seen: set[str] = set()

    for value in values:
        clean = str(value).strip()
        norm = clean.lower()

        if clean and norm not in seen:
            seen.add(norm)
            deduped.append(clean)

    return deduped


def event_keywords(event: dict[str, Any]) -> list[str]:
    values: list[str] = []

    raw = event.get("keywords")

    if isinstance(raw, list):
        values.extend(str(item) for item in raw if str(item).strip())
    elif isinstance(raw, str) and raw.strip():
        values.append(raw)

    for key in ("condition", "topic", "category", "league", "event_type"):
        raw_value = event.get(key)

        if isinstance(raw_value, str) and raw_value.strip():
            values.append(raw_value)

    deduped: list[str] = []
    seen: set[str] = set()

    for value in values:
        clean = str(value).strip()
        norm = clean.lower()

        if clean and norm not in seen:
            seen.add(norm)
            deduped.append(clean)

    return deduped


def social_record_matches_event(
    *,
    domain: str,
    event: dict[str, Any],
    record: dict[str, Any],
) -> bool:
    text = str(record.get("text") or "")
    text_lc = text.lower()
    tokens = text_tokens(text)

    if not text.strip():
        return False

    entities = event_entities(event)
    keywords = event_keywords(event)
    domain_words = DOMAIN_WORDS.get(domain, set())

    has_entity = any(
        contains_phrase_or_token(
            text_lc=text_lc,
            tokens=tokens,
            value=entity,
        )
        for entity in entities
    )

    if not has_entity:
        return False

    has_domain_word = bool(tokens & domain_words)

    has_keyword = any(
        contains_phrase_or_token(
            text_lc=text_lc,
            tokens=tokens,
            value=keyword,
        )
        for keyword in keywords
        if len(str(keyword).strip()) > 2
    )

    # Domain-specific fallback for finance tickers.
    if domain == "finance":
        for entity in entities:
            symbol = str(entity).strip().upper()

            if symbol and f"${symbol}" in text:
                return True

    return has_domain_word or has_keyword


def get_recent_social_records(
    *,
    data_store: HyperClient,
    limit: int,
) -> list[dict[str, Any]]:
    records = list(
        data_store.walk(
            namespace=ATPROTO_ROOT,
            read_path="records",
            in_index={},
            where=lambda _record: True,
            limit=limit,
        )
    )

    return records


def link_social_to_events(
    *,
    DATA_DIR: str | None,
    domain: str,
    events: list[dict[str, Any]],
    force: bool = False,
    lookback_limit: int = DEFAULT_LOOKBACK_LIMIT,
    max_links_per_event: int = DEFAULT_MAX_LINKS_PER_EVENT,
    wait_seconds: int = SOCIAL_LINK_WAIT_SECONDS,
) -> int:
    if not events:
        print(f"[social-link] skipped {domain}: no events", flush=True)
        return 0

    due_events: list[dict[str, Any]] = []

    for event in events:
        key = event_key(event)

        if not key:
            print(f"[social-link] skip {domain}: event missing key", flush=True)
            continue

        state_name = social_link_state_name(
            domain=domain,
            event_key=key,
        )

        if not force and not is_due(
            name=state_name,
            wait_seconds=wait_seconds,
            DATA_DIR=DATA_DIR,
        ):
            print(f"[social-link] skip {domain}: already linked event={key}", flush=True)
            continue

        due_events.append(event)

    if not due_events:
        print(f"[social-link] skipped {domain}: no due events", flush=True)
        return 0

    data_store = HyperClient(
        root_key=ATPROTO_ROOT,
        data_dir=DATA_DIR,
    )

    total_links = 0

    try:
        social_records = get_recent_social_records(
            data_store=data_store,
            limit=lookback_limit,
        )

        print(
            f"[social-link] {domain}: events={len(due_events)} "
            f"social_records={len(social_records)}",
            flush=True,
        )

        for event in due_events:
            key = event_key(event)
            links_for_event = 0

            for record in social_records:
                if links_for_event >= max_links_per_event:
                    break

                if not social_record_matches_event(
                    domain=domain,
                    event=event,
                    record=record,
                ):
                    continue

                record_key = record_key_from_record(record)
                ref_path = social_ref_path(
                    domain=domain,
                    event_key=key,
                    record_key=record_key,
                )

                data_store.write_ops(
                    [
                        {
                            "path": ref_path,
                            "data": {
                                "data": {
                                    "tag": f"{domain}_social_ref",
                                    "domain": domain,
                                    "event_key": key,
                                    "record_key": record_key,
                                    "entities": event_entities(event),
                                    "keywords": event_keywords(event),
                                },
                                "links": {
                                    "record": record_dot_path(record),
                                },
                            },
                        }
                    ],
                    root=ATPROTO_ROOT,
                )

                links_for_event += 1
                total_links += 1

                print(
                    f"[social-link] linked {domain} event={key} record={record_key}",
                    flush=True,
                )

            if links_for_event > 0:
                mark_ran(
                    name=social_link_state_name(
                        domain=domain,
                        event_key=key,
                    ),
                    DATA_DIR=DATA_DIR,
                    domain=domain,
                    event_key=key,
                    links_written=links_for_event,
                )
            else:
                print(
                    f"[social-link] not marking {domain} event={key}: 0 links",
                    flush=True,
                )

    finally:
        data_store.close()

    print(
        f"[social-link] done {domain}: links={total_links:,}",
        flush=True,
    )

    return total_links


def link_social_to_weather(
    *,
    DATA_DIR: str | None,
    events: list[dict[str, Any]],
    force: bool = False,
) -> int:
    return link_social_to_events(
        DATA_DIR=DATA_DIR,
        domain="weather",
        events=events,
        force=force,
    )


def link_social_to_news(
    *,
    DATA_DIR: str | None,
    events: list[dict[str, Any]],
    force: bool = False,
) -> int:
    return link_social_to_events(
        DATA_DIR=DATA_DIR,
        domain="news",
        events=events,
        force=force,
    )


def link_social_to_sports(
    *,
    DATA_DIR: str | None,
    events: list[dict[str, Any]],
    force: bool = False,
) -> int:
    return link_social_to_events(
        DATA_DIR=DATA_DIR,
        domain="sports",
        events=events,
        force=force,
    )


def link_social_to_earthquakes(
    *,
    DATA_DIR: str | None,
    events: list[dict[str, Any]],
    force: bool = False,
) -> int:
    return link_social_to_events(
        DATA_DIR=DATA_DIR,
        domain="earthquakes",
        events=events,
        force=force,
    )


def link_social_to_finance(
    *,
    DATA_DIR: str | None,
    events: list[dict[str, Any]],
    force: bool = False,
) -> int:
    return link_social_to_events(
        DATA_DIR=DATA_DIR,
        domain="finance",
        events=events,
        force=force,
    )