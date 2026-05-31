# app/lib/weather/loader.py
"""
Read geo locations, observe weather for each, add it to the graph (root="weather").

Each observation is one node, with a `latest` pointer node and two edges back to
the source geo location. Search is inferred by the relay from the node's data and
`kind` — there is no hand-written query.

    weather.history.<key>          the observation node (+ value indexes)
    weather.latest.<key>           a pointer node to the newest observation
    geo.<location>.refs.weather_*  edges linking the location to its weather

Edges are their own sidecar nodes; we never rewrite the location node from here,
so the geo node stays the single source of truth.
"""
from __future__ import annotations

import sys
from typing import Any

from HyperCoreSDK.python.helpers.loader import Graph, projection
from HyperCoreSDK.python.helpers.indexes import ScopeSpec, ValueIndexSpec
from app.utils.dtos.Location import Location
from app.lib.weather.helpers import observation_from_location


GEO_ROOT = "geo"
WEATHER_ROOT = "weather"

# Which geo locations to observe.
SELECT_BY = {"population_band": ["2_5M-4_9M"]}
SELECT_COUNTRY_CODE: str | None = None
SELECT_LIMIT = 500


# Fields each index entry carries forward for cheap rendering without rereading the node.
PROJECT = projection(
    "name", "country_code", "country_flag_emoji",
    "temperature", "condition", "observed_at", "lat", "lon",
)

WEATHER_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec("country_code", "country_code", normalize="upper", link_projections=PROJECT),
    ValueIndexSpec("condition", "condition", normalize="slug", link_projections=PROJECT),
    ValueIndexSpec(
        "condition", "condition", normalize="slug",
        scopes=[ScopeSpec("country_code", normalize="upper")],
        link_projections=PROJECT,
    ),
]


def location_from_data(data: dict[str, Any]) -> Location | None:
    """Rebuild a Location from a stored geo node so we can fetch weather for it."""
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
    """Optional country gate applied on top of the index selector."""
    if SELECT_COUNTRY_CODE is None:
        return True
    return str(data.get("country_code") or "").upper() == SELECT_COUNTRY_CODE.upper()


def observation_data(obs) -> dict[str, Any]:
    """One dict that is both the stored node and the thing search is inferred from."""
    return {"kind": "weather_observation", **obs.to_dict()}


def main() -> int:
    print("observing weather for geo locations…", flush=True)

    written = 0
    with Graph(WEATHER_ROOT) as graph:
        for loc in graph.walk(
            root_key=GEO_ROOT,
            path="locations",
            _return=location_from_data,
            index=SELECT_BY,
            where=location_filter,
            limit=SELECT_LIMIT,
        ):
            obs = observation_from_location(loc)
            if obs is None:
                print(f"  skip: {loc.name} ({loc.country_code})", flush=True)
                continue

            history_rel = f"history/{obs.record_key()}"
            history_abs = f"{WEATHER_ROOT}.{history_rel.replace('/', '.')}"
            latest_abs = f"{WEATHER_ROOT}.latest.{obs.latest_key()}"
            preview = {
                "temperature": obs.temperature,
                "condition": obs.condition,
                "observed_at": obs.observed_at,
            }

            # The observation node + its indexes.
            graph.add(
                path=history_rel,
                content=observation_data(obs),
                indexes=WEATHER_INDEXES,
            )

            # The latest observation, as a pointer node (add with a target).
            graph.add(
                latest_abs,
                {**obs.latest_dict(history_abs), **preview},
                kind="weather_latest",
                name=obs.name,
                target=history_abs,
                links={"location": obs.location_path},
            )

            # Edges from the geo location to its weather (the geo node is untouched).
            graph.link(
                source=obs.location_path,
                rel="weather_latest",
                target=latest_abs,
                kind="weather-latest",
                name=obs.name,
                content=preview,
                links={"location": obs.location_path},
            )
            graph.link(
                source=obs.location_path,
                rel=f"weather.{obs.record_key().split('/', 1)[1].replace('/', '.')}",
                target=history_abs,
                kind="weather-observation",
                name=obs.name,
                content=preview,
            )

            written += 1
            print(
                f"  ok {written}: {obs.name} ({obs.country_code}) — "
                f"{obs.temperature:.1f}°C, {obs.condition}",
                flush=True,
            )

        print(f"done: added {written:,} observations", flush=True)
        graph.serve()

    return 0


if __name__ == "__main__":
    sys.exit(main())