# app/lib/weather/loader.py
"""
Fetch current weather for selected populated places in `geo.locations` and
write observations into the hypergraph.

This loader selects Locations from the existing geo indexes / geo collection.
The relay is started with root="geo" so preloaded geo locations are visible,
but weather history indexes are explicitly written under root="weather".
"""

from __future__ import annotations

import sys
import time
from typing import Any

from app.utils.server import create_hyper_server
from app.utils.storage import create_default_storage_directory
from app.utils.dtos.Location import Location
from app.lib.weather.helpers import observation_from_location
from HyperCoreSDK.python.client import HyperClient
from HyperCoreSDK.python.indexes import (
    ScopeSpec,
    ValueIndexSpec,
    upsert_with_indexes,
)


WEATHER_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec(
        name="country_code",
        path="country_code",
        normalize="upper",
        link_projections={
            "name": "name",
            "country_flag_emoji": "country_flag_emoji",
            "temperature": "temperature",
            "condition": "condition",
            "observed_at": "observed_at",
        },
    ),
    ValueIndexSpec(
        name="condition",
        path="condition",
        normalize="slug",
        link_projections={
            "name": "name",
            "country_code": "country_code",
            "country_flag_emoji": "country_flag_emoji",
            "temperature": "temperature",
            "observed_at": "observed_at",
            "lat": "lat",
            "lon": "lon",
        },
    ),
    ValueIndexSpec(
        name="condition",
        path="condition",
        normalize="slug",
        scopes=[ScopeSpec(path="country_code", normalize="upper")],
        link_projections={
            "name": "name",
            "country_code": "country_code",
            "country_flag_emoji": "country_flag_emoji",
            "temperature": "temperature",
            "observed_at": "observed_at",
        },
    ),
]


def _node_data(doc: Any) -> dict[str, Any] | None:
    if not isinstance(doc, dict):
        return None

    data = doc.get("data")

    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]

    if isinstance(data, dict):
        return data

    return None


def _location_from_data(data: dict[str, Any]) -> Location | None:
    try:
        kwargs = {
            "geoname_id": str(data["geoname_id"]),
            "name": str(data["name"]),
            "lat": float(data["lat"]),
            "lon": float(data["lon"]),
            "country_code": str(data["country_code"]),
            "country_flag_emoji": str(data.get("country_flag_emoji") or ""),
            "timezone": str(data.get("timezone") or ""),
            "elevation": data.get("elevation"),
        }

        try:
            kwargs["population"] = int(data.get("population") or 0)
            return Location(**kwargs)
        except TypeError:
            kwargs.pop("population", None)
            return Location(**kwargs)

    except (KeyError, ValueError, TypeError):
        return None


def _hydrate_location(hc: HyperClient, canonical_path: str) -> Location | None:
    doc = hc.read(canonical_path)
    data = _node_data(doc)

    if not isinstance(data, dict):
        return None

    return _location_from_data(data)


def _index_ref_record_dot(entry: dict[str, Any]) -> str:
    payload = entry.get("data")

    if isinstance(payload, dict):
        inner = payload.get("data")

        if isinstance(inner, dict):
            record_dot = inner.get("record_dot")
            if record_dot:
                return str(record_dot)

            record_path = inner.get("record_path")
            if record_path:
                return f"geo.{str(record_path).replace('/', '.')}"

        record_dot = payload.get("record_dot")
        if record_dot:
            return str(record_dot)

    return ""


def _locations_from_index_path(
    hc: HyperClient,
    index_path: str,
    *,
    limit: int | None = None,
) -> list[Location]:
    locations: list[Location] = []
    seen: set[str] = set()
    page = 1

    while True:
        entries = hc.children(index_path, page=page, per_page=200).items()

        if not entries:
            break

        for entry in entries:
            record_dot = _index_ref_record_dot(entry)

            if not record_dot or record_dot in seen:
                continue

            seen.add(record_dot)

            loc = _hydrate_location(hc, record_dot)

            if loc is None:
                continue

            locations.append(loc)

            if limit is not None and len(locations) >= limit:
                return locations

        page += 1

    return locations


def _locations_from_country_code_index(
    hc: HyperClient,
    *,
    country_code: str,
    limit: int | None = None,
) -> list[Location]:
    return _locations_from_index_path(
        hc,
        f"geo.index.by.country_code.{country_code.upper()}",
        limit=limit,
    )


def _locations_from_population_band_indexes(
    hc: HyperClient,
    *,
    population_bands: list[str],
    country_code: str | None = None,
    limit: int | None = None,
) -> list[Location]:
    locations: list[Location] = []
    seen: set[str] = set()

    for band in population_bands:
        batch = _locations_from_index_path(
            hc,
            f"geo.index.by.population_band.{band}",
            limit=None,
        )

        for loc in batch:
            if country_code and loc.country_code.upper() != country_code.upper():
                continue

            key = getattr(loc, "geoname_id", None) or loc.record_key()

            if key in seen:
                continue

            seen.add(key)
            locations.append(loc)

            if limit is not None and len(locations) >= limit:
                return locations

    return locations


def _locations_from_geo_collection(
    hc: HyperClient,
    *,
    country_code: str | None = None,
    limit: int | None = None,
) -> list[Location]:
    locations: list[Location] = []
    page = 1

    while True:
        entries = hc.children("geo.locations", page=page, per_page=200).items()

        if not entries:
            break

        for entry in entries:
            data = _node_data(entry)

            if not isinstance(data, dict):
                continue

            loc = _location_from_data(data)

            if loc is None:
                continue

            if country_code and loc.country_code.upper() != country_code.upper():
                continue

            locations.append(loc)

            if limit is not None and len(locations) >= limit:
                return locations

        page += 1

    return locations


def construct_location_list(
    hc: HyperClient,
    preset_location_list: list[Location] | None = None,
    *,
    country_code: str | None = None,
    population_bands: list[str] | None = None,
    limit: int | None = None,
) -> list[Location]:
    if preset_location_list is not None:
        return preset_location_list

    if population_bands:
        return _locations_from_population_band_indexes(
            hc,
            population_bands=population_bands,
            country_code=country_code,
            limit=limit,
        )

    if country_code:
        return _locations_from_country_code_index(
            hc,
            country_code=country_code,
            limit=limit,
        )

    return _locations_from_geo_collection(
        hc,
        limit=limit,
    )


def write_observation(hc: HyperClient, loc: Location) -> str:
    obs = observation_from_location(loc)

    if obs is None:
        return f"skip: {loc.name} ({loc.country_code})"

    history_rel = f"history/{obs.record_key()}"
    history_abs = f"weather.{history_rel.replace('/', '.')}"
    latest_abs = f"weather.latest.{obs.latest_key()}"

    upsert_with_indexes(
        hc,
        root="weather",
        record_path=history_rel,
        record_data=obs.to_dict(),
        index_specs=WEATHER_INDEXES,
        ref_key=obs.record_key().replace("/", "-"),
        ref_payload=obs.ref_payload(),
    )

    observed_ms = int(time.time() * 1000)

    hc.write(
        latest_abs,
        {
            "data": obs.latest_dict(history_abs),
            "links": {
                "target": history_abs,
                "location": obs.location_path,
            },
            "query": {
                "entity_id": latest_abs,
                "entity_type": "weather_latest",
                "canonical_path": latest_abs,
                "display": obs.name,
                "facets": {
                    "country_code": obs.country_code,
                    "condition": obs.condition,
                    "location_path": obs.location_path,
                },
                "times": {
                    "observed_at": observed_ms,
                    "updated_at": observed_ms,
                },
                "refs": {
                    "location": obs.location_path,
                    "target": history_abs,
                },
                "tokens": [
                    obs.name,
                    obs.country_code,
                    obs.condition,
                ],
            },
        },
    )

    preview = {
        "temperature": obs.temperature,
        "condition": obs.condition,
        "observed_at": obs.observed_at,
    }

    hc.write(
        f"{obs.location_path}.refs.weather_latest",
        {
            "data": {
                "kind": "weather-latest",
                "target": latest_abs,
                **preview,
            },
            "links": {
                "target": latest_abs,
            },
            "query": {
                "entity_id": obs.location_path,
                "entity_type": "location",
                "canonical_path": obs.location_path,
                "display": obs.name,
                "facets": {
                    "country_code": obs.country_code,
                    "has_weather": True,
                },
                "times": {
                    "weather_latest_at": observed_ms,
                    "activity_latest_at": observed_ms,
                },
                "refs": {
                    "weather_latest": latest_abs,
                },
                "tokens": [
                    obs.name,
                    obs.country_code,
                ],
            },
        },
    )

    ts_tail = obs.record_key().split("/", 1)[1].replace("/", ".")

    hc.write(
        f"{obs.location_path}.refs.weather.{ts_tail}",
        {
            "data": {
                "kind": "weather-observation",
                "target": history_abs,
                **preview,
            },
            "links": {
                "target": history_abs,
            },
        },
    )

    return f"ok: {obs.name} ({obs.country_code}) — {obs.temperature:.1f}°C, {obs.condition}"


def run(
    client_instance: HyperClient,
    preset_location_list: list[Location] | None = None,
    *,
    country_code: str | None = None,
    population_bands: list[str] | None = None,
    limit: int | None = None,
) -> int:
    count = 0

    try:
        locations = construct_location_list(
            client_instance,
            preset_location_list=preset_location_list,
            country_code=country_code,
            population_bands=population_bands,
            limit=limit,
        )

        print(f"loaded {len(locations):,} locations; fetching weather…")

        for loc in locations:
            msg = write_observation(client_instance, loc)
            count += 1

            if count % 100 == 0:
                print(f"  {count:,} observations…  [{msg}]")

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
    client = create_hyper_server(
        root="geo",
        data_path=create_default_storage_directory(),
    )

    sys.exit(
        run(
            client_instance=client,
            population_bands=[
                "2_5M-4_9M"
            ],
            limit=500,
        )
    )