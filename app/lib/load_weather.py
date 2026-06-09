from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.lib.helpers.weather import (
    HyperClient,
    create_weather_candidate,
    create_weather_event_object,
    apply_graph_operations,
)
from app.lib.helpers.geo import geo_factory
from app.lib.helpers.common import (
    get_loader_runs,
    is_due,
    mark_ran,
    utc_now,
)


WEATHER_ROOT = "weather"
WEATHER_WAIT_SECONDS = 6 * 60 * 60

SELECT_BY: dict[str, Any] = {}
SELECT_LIMIT = 10

WEATHER_LOCATION_RUNS_STATE = "weather/location_runs"


@dataclass(frozen=True)
class WeatherLoadResult:
    location_key: str
    location: object
    event: object


def stable_hash(value: Any) -> str:
    text = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def location_key_from_record(record: dict[str, Any]) -> str:
    name = str(record.get("name") or "").strip()
    geoname_id = str(record.get("geoname_id") or "").strip()
    return f"{name}-{geoname_id}"


def _matches_country(
    location: dict[str, Any],
    country_code: str | None = None,
) -> bool:
    if country_code is None:
        return True

    return str(location.get("country_code") or "").upper() == country_code.upper()


def make_weather_selector(
    *,
    location_records: list[dict[str, Any]],
    locations: list[str] | None,
    country_code: str | None,
    limit: int,
) -> dict[str, Any]:
    selected_location_keys = sorted(
        location_key_from_record(record)
        for record in location_records
    )

    return {
        "kind": "weather",
        "country_code": country_code.upper() if country_code else None,
        "requested_locations": sorted(
            str(location).strip().lower()
            for location in locations or []
            if str(location).strip()
        ),
        "limit": limit,
        "selected_locations": selected_location_keys,
    }


def weather_batch_state_name(selector_hash: str) -> str:
    return f"weather/selections/{selector_hash}"


def get_fresh_weather_location_keys(
    *,
    DATA_DIR: str | None,
    wait_seconds: int,
) -> set[str]:
    print("[weather] reading location freshness once", flush=True)

    fresh: set[str] = set()
    now = utc_now()

    runs = get_loader_runs(
        name=WEATHER_LOCATION_RUNS_STATE,
        DATA_DIR=DATA_DIR,
    )

    latest_by_location: dict[str, str] = {}

    for record in runs:
        location_key = record.get("location_key")
        last_run_at = record.get("last_run_at")

        if not location_key or not last_run_at:
            continue

        location_key = str(location_key)
        last_run_at = str(last_run_at)

        previous = latest_by_location.get(location_key)

        if previous is None or last_run_at > previous:
            latest_by_location[location_key] = last_run_at

    for location_key, last_run_at in latest_by_location.items():
        last = datetime.fromisoformat(last_run_at)

        if (now - last).total_seconds() < wait_seconds:
            fresh.add(location_key)

    print(
        f"[weather] fresh location cache: {len(fresh)} locations",
        flush=True,
    )

    return fresh


def fetch_weather_for_location(
    *,
    data_store: HyperClient,
    location_record: dict[str, Any],
    DATA_DIR: str | None,
) -> WeatherLoadResult | None:
    location_key = location_key_from_record(location_record)

    location_object = geo_factory.create_location_object(
        from_record=location_record,
    )

    if not location_object:
        print(
            f"[weather] skip: invalid location object ({location_object})",
            flush=True,
        )
        return None

    observation, observation_proof = create_weather_candidate(
        latitude=location_object.lat,
        longitude=location_object.lon,
    )

    if not observation_proof:
        print(
            f"[weather] skip: no weather for {location_object.name} "
            f"({location_object.country_code})",
            flush=True,
        )
        return None

    weather_event_object, event_proof = create_weather_event_object(
        location_object=location_object,
        observation=observation,
    )

    if not event_proof:
        print(
            f"[weather] skip: weather event object ({weather_event_object})",
            flush=True,
        )
        return None

    apply_graph_operations(
        weather_event_object=weather_event_object,
        client_instance=data_store,
        namespace=WEATHER_ROOT,
    )

    mark_ran(
        name=WEATHER_LOCATION_RUNS_STATE,
        DATA_DIR=DATA_DIR,
        location_key=location_key,
        country_code=location_object.country_code,
        location_name=location_object.name,
        geoname_id=location_object.geoname_id,
    )

    return WeatherLoadResult(
        location_key=location_key,
        location=location_object,
        event=weather_event_object,
    )


def load_weather(
    *,
    force: bool = False,
    DATA_DIR: str | None = None,
    locations: list[str] | None = None,
    country_code: str | None = None,
    limit: int = SELECT_LIMIT,
    wait_seconds: int = WEATHER_WAIT_SECONDS,
) -> list[WeatherLoadResult]:
    print("[weather] selecting target locations", flush=True)

    def matches(location: dict[str, Any]) -> bool:
        return _matches_country(location, country_code)

    with HyperClient(
        root_key=WEATHER_ROOT,
        data_dir=DATA_DIR,
    ) as data_store:

        if locations:
            location_records = list(
                data_store.find(
                    terms=locations,
                    namespace="geo",
                    read_path="locations",
                    where=matches,
                    limit=limit,
                )
            )
        else:
            location_records = list(
                data_store.walk(
                    namespace="geo",
                    read_path="locations",
                    in_index=SELECT_BY,
                    where=matches,
                    limit=limit,
                )
            )

        if not location_records:
            print("[weather] skipped: no matching locations", flush=True)
            return []

        selector = make_weather_selector(
            location_records=location_records,
            locations=locations,
            country_code=country_code,
            limit=limit,
        )

        selector_hash = stable_hash(selector)
        batch_state_name = weather_batch_state_name(selector_hash)

        print(
            f"[weather] selected {len(location_records)} locations "
            f"selector={selector_hash}",
            flush=True,
        )

        batch_fresh = not force and not is_due(
            name=batch_state_name,
            wait_seconds=wait_seconds,
            DATA_DIR=DATA_DIR,
        )

        fresh_location_keys: set[str] = set()

        if not force:
            fresh_location_keys = get_fresh_weather_location_keys(
                DATA_DIR=DATA_DIR,
                wait_seconds=wait_seconds,
            )

        results: list[WeatherLoadResult] = []
        skipped = 0

        if batch_fresh:
            print(
                f"[weather] batch {selector_hash} is fresh; "
                "using per-location cache",
                flush=True,
            )

        print(
            f"[weather] updating stale locations in batch {selector_hash}",
            flush=True,
        )

        for location_record in location_records:
            location_key = location_key_from_record(location_record)

            if not force and location_key in fresh_location_keys:
                print(f"[weather] skip: {location_key} is still fresh", flush=True)
                skipped += 1
                continue

            result = fetch_weather_for_location(
                data_store=data_store,
                location_record=location_record,
                DATA_DIR=DATA_DIR,
            )

            if result is None:
                skipped += 1
                continue

            results.append(result)

        mark_ran(
            name=batch_state_name,
            DATA_DIR=DATA_DIR,
            selector_hash=selector_hash,
            selector=selector,
            records_written=len(results),
            records_skipped=skipped,
            selected_count=len(location_records),
        )

    print(
        f"[weather] done: added {len(results):,} events, skipped {skipped:,} "
        f"for selected batch {selector_hash}",
        flush=True,
    )

    return results


if __name__ == "__main__":
    results = load_weather()
    sys.exit(0 if results is not None else 1)