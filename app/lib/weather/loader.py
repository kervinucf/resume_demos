"""
Fetch current weather for every populated place in `geo.locations` and
write observations into the hypergraph.

After a run, the `weather` namespace looks like:

    weather/
      history/
        new-york-city-5128581/
          2026/04/21/143015        (observation record)
          ...
      latest/
        new-york-city-5128581      (pointer → most recent history record)
      index/
        by/
          country_code/
            US/
              new-york-city-5128581-2026-04-21-143015
          condition/
            partly-cloudy/
              new-york-city-5128581-2026-04-21-143015
              paris-2988507-2026-04-21-143102
            rain/
              ...
        scoped/
          country_code/
            US/
              condition/
                rain/
                  ...
      _meta/
        memberships/
          <sha1 of each record path>

Each Location also gains back-refs under its own record:

    geo.locations.<key>.refs.weather_latest   → weather.latest.<key>
    geo.locations.<key>.refs.weather.<ts>     → weather.history.<key>.<ts>

Usage
-----
    # Auto-spawn a relay if one isn't already running
    python loader.py

    # Use an existing relay
    HYPER_URL=http://127.0.0.1:8765 python loader.py
"""
from __future__ import annotations

import sys

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

# ---------------------------------------------------------------------------
# Index specs
#
#   "Index by country_code, uppercase. Project name, flag, temperature,
#    condition, observed_at onto each entry."
#
#   "Index by condition globally — `index/by/condition/rain/` holds every
#    rainy observation everywhere."
#
#   "Also index by condition scoped under country_code, so
#    `index/scoped/country_code/US/condition/rain/` holds only US rain."
# ---------------------------------------------------------------------------

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
    # Global condition index
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
    # Scoped condition index (per country)
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


# ---------------------------------------------------------------------------
# Location iteration
# ---------------------------------------------------------------------------

def construct_location_list(
    hc: HyperClient,
    PRESET_LOCATION_LIST: list[Location] | None = None,
) -> list[Location]:
    """
    Page through geo.locations and return a list of hydrated Location
    DTOs. If PRESET_LOCATION_LIST is provided, return it unchanged.
    """
    if PRESET_LOCATION_LIST is not None:
        return PRESET_LOCATION_LIST

    location_list: list[Location] = []
    page_count = 1

    while True:
        results = hc.children("geo.locations", page=page_count, per_page=200).items()
        if len(results) == 0:
            break

        for entry in results:
            data = entry.get("data") if isinstance(entry, dict) else None
            if not isinstance(data, dict):
                continue
            try:
                location_list.append(Location(
                    geoname_id=str(data["geoname_id"]),
                    name=str(data["name"]),
                    lat=float(data["lat"]),
                    lon=float(data["lon"]),
                    country_code=str(data["country_code"]),
                    country_flag_emoji=str(data.get("country_flag_emoji") or ""),
                    timezone=str(data.get("timezone") or ""),
                    elevation=data.get("elevation"),
                ))
            except (KeyError, ValueError, TypeError):
                continue

        page_count += 1

    return location_list


# ---------------------------------------------------------------------------
# Per-record write
# ---------------------------------------------------------------------------

def write_observation(hc: HyperClient, loc: Location) -> str:
    obs = observation_from_location(loc)
    if obs is None:
        return f"skip: {loc.name} ({loc.country_code})"

    history_rel = f"history/{obs.record_key()}"
    history_abs = f"weather.{history_rel.replace('/', '.')}"
    latest_abs = f"weather.latest.{obs.latest_key()}"

    # Canonical history record + indexes
    upsert_with_indexes(
        hc,
        record_path=history_rel,
        record_data=obs.to_dict(),
        index_specs=WEATHER_INDEXES,
        ref_key=obs.record_key().replace("/", "-"),
        ref_payload=obs.ref_payload(),
    )

    # "Latest" pointer — plain write, not indexed (it's a redirect)
    hc.write(latest_abs, {
        "data": obs.latest_dict(history_abs),
        "links": {"target": history_abs, "location": obs.location_path},
    })

    # Back-refs on the Location itself
    preview = {
        "temperature": obs.temperature,
        "condition": obs.condition,
        "observed_at": obs.observed_at,
    }
    hc.write(f"{obs.location_path}.refs.weather_latest", {
        "data": {"kind": "weather-latest", "target": latest_abs, **preview},
        "links": {"target": latest_abs},
    })
    ts_tail = obs.record_key().split("/", 1)[1].replace("/", ".")
    hc.write(f"{obs.location_path}.refs.weather.{ts_tail}", {
        "data": {"kind": "weather-observation", "target": history_abs, **preview},
        "links": {"target": history_abs},
    })

    return f"ok: {obs.name} ({obs.country_code}) — {obs.temperature:.1f}°C, {obs.condition}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(client_instance: HyperClient) -> int:
    count = 0
    try:
        locations = construct_location_list(client_instance)
        print(f"loaded {len(locations):,} locations; fetching weather…")

        for loc in locations:
            msg = write_observation(client_instance, loc)
            count += 1
            if count % 100 == 0:
                print(f"  {count:,} observations…  [{msg}]")

    finally:
        # A spawned relay stays up after loading so you can poke at the data
        # or start consumers. Ctrl-C to stop.
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
                root="weather",
                data_path=create_default_storage_directory(),
            )
        )
    )