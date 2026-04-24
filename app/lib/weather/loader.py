from __future__ import annotations

import sys
import time
from typing import Any

from HyperCoreSDK.python.client import HyperClient
from HyperCoreSDK.python.helpers.server import create_hyper_server
from HyperCoreSDK.python.helpers.storage import create_default_storage_directory
from HyperCoreSDK.python.helpers.indexes import ScopeSpec, ValueIndexSpec
from app.utils.dtos.Location import Location
from app.lib.weather.helpers import observation_from_location


GEO_ROOT = "geo"
WEATHER_ROOT = "weather"

LOCATION_QUERY = {
    "by": {
        "population_band": ["2_5M-4_9M"],
    },
    "country_code": None,
    "limit": 500,
}

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


def location_from_data(data: dict[str, Any]) -> Location | None:
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
            return Location(**kwargs, population=int(data.get("population") or 0))
        except TypeError:
            return Location(**kwargs)

    except (KeyError, TypeError, ValueError):
        return None


def location_filter(data: dict[str, Any]) -> bool:
    country_code = LOCATION_QUERY.get("country_code")

    if country_code is None:
        return True

    return str(data.get("country_code") or "").upper() == str(country_code).upper()


def write_observation(client: HyperClient, loc: Location) -> str:
    obs = observation_from_location(loc)

    if obs is None:
        return f"skip: {loc.name} ({loc.country_code})"

    history_rel = f"history/{obs.record_key()}"
    history_abs = f"{WEATHER_ROOT}.{history_rel.replace('/', '.')}"
    latest_abs = f"{WEATHER_ROOT}.latest.{obs.latest_key()}"
    observed_ms = int(time.time() * 1000)

    preview = {
        "temperature": obs.temperature,
        "condition": obs.condition,
        "observed_at": obs.observed_at,
    }

    client.write_record_with_indexes(
        root=WEATHER_ROOT,
        record_path=history_rel,
        record_data=obs.to_dict(),
        index_specs=WEATHER_INDEXES,
        ref_key=obs.record_key().replace("/", "-"),
        ref_payload=obs.ref_payload(),
    )

    client.write_pointer(
        path=latest_abs,
        target=history_abs,
        data=obs.latest_dict(history_abs),
        links={
            "location": obs.location_path,
        },
        query={
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
    )

    client.write_backref(
        source=obs.location_path,
        rel="weather_latest",
        target=latest_abs,
        data={
            "kind": "weather-latest",
            "target": latest_abs,
            **preview,
        },
        query={
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
    )

    client.write_backref(
        source=obs.location_path,
        rel=f"weather.{obs.record_key().split('/', 1)[1].replace('/', '.')}",
        target=history_abs,
        data={
            "kind": "weather-observation",
            "target": history_abs,
            **preview,
        },
    )

    return f"ok: {obs.name} ({obs.country_code}) — {obs.temperature:.1f}°C, {obs.condition}"


def main(client: HyperClient) -> int:
    try:
        count = 0

        for loc in client.select_records(
            root=GEO_ROOT,
            collection="locations",
            from_data=location_from_data,
            by=LOCATION_QUERY["by"],
            where=location_filter,
            limit=LOCATION_QUERY["limit"],
        ):
            msg = write_observation(client, loc)
            count += 1

            if count % 100 == 0:
                print(f"  {count:,} observations… [{msg}]")

        print(f"done: wrote {count:,} observations")

    finally:
        if client.owns_relay():
            print(f"relay still running at {client.url} (Ctrl-C to stop)")
            try:
                client._owned.process.wait()
            except KeyboardInterrupt:
                pass
            finally:
                client.close()
        else:
            client.close()

    return 0


if __name__ == "__main__":
    sys.exit(
        main(
            create_hyper_server(
                root=GEO_ROOT,
                data_path=create_default_storage_directory(),
            )
        )
    )