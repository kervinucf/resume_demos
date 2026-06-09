"""
Geo operations — the verbs the main loader orchestrates.

Mirrors app/lib/helpers/weather/__init__.py, where create_weather_candidate calls the
provider (send_open_meteo_request) internally. Here the provider is read inside the
create_* functions too, so the loader only ever imports:

    from app.lib.helpers.geo import (apply_graph_operations,
                                     create_location_builder,
                                     create_location_candidate,
                                     get_currency_information)

This module is the ONE place that names the concrete provider. To switch providers,
change the `app.lib.sources.geonames` import below to another module exposing the same
source callables — nothing else changes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from HyperCoreSDK.python.client import HyperClient, projection, ScopeSpec, ValueIndexSpec
# --- provider boundary: swap this single import to change source ------------
from app.lib.sources.geonames import iter_country_currency, iter_geonames_list
# ---------------------------------------------------------------------------
from .factory import (
    CountryCurrency,
    LocationFactory,
    LocationObject,
    slugify
)

geo_factory = LocationFactory()

__all__ = [
    "HyperClient",
    "geo_factory",
    "list_location_candidates",
    "get_currency_information",
    "create_location_object",
    "apply_graph_operations",
    "LocationObject",
    "CountryCurrency",
    "LOCATION_INDEXES",
    "slugify"
]
# Fields each index entry carries forward for cheap rendering (information density).
PROJECT = projection(
    "name", "country_code", "country_flag_emoji", "timezone", "lat", "lon",
    "country_name", "continent", "currency_code", "currency_name", "currency_path",
)
LOCATION_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec("country_code", "country_code", normalize="upper", link_projections=PROJECT),
    ValueIndexSpec(
        "timezone", "timezone", normalize="slug",
        scopes=[ScopeSpec("country_code", normalize="upper")],
        link_projections=PROJECT,
    ),
    ValueIndexSpec(
        "population_band", "population_band", normalize="none",
        link_projections={**PROJECT, "population": "population"},
    ),
]


def list_location_candidates() -> Iterator[tuple[dict[str, Any], bool]]:
    for location_candidate in iter_geonames_list():
        try:
            # assert validity: the fields create_location_builder depends on are present
            proof = all(
                location_candidate.get(field) not in (None, "")
                for field in ("geoname_id", "name", "lat", "lon", "country_code", "timezone")
            )
        except (TypeError, ValueError) as exc:
            print(f"  skip: bad location record ({exc})", flush=True)
            continue

        yield location_candidate, proof


def get_currency_information(
        source_dir: str | Path | None = None,
) -> dict[str, CountryCurrency]:
    out: dict[str, CountryCurrency] = {}
    for record in iter_country_currency(source_dir):
        cc = CountryCurrency(**record)
        out[cc.country_code] = cc
    return out


def create_location_object(
        location_candidate: dict[str, Any],
        currencies: dict[str, CountryCurrency] | None = None,
) -> tuple[LocationObject | None, bool]:
    try:
        location_object = geo_factory.create_location_object(**location_candidate)
    except (TypeError, ValueError) as exc:
        print(f"  skip: bad location record ({exc})", flush=True)
        return None, False

    # apply currency to location object
    if currencies:
        location_object = geo_factory.add_currency_information(
            base_location_object=location_object,
            currencies=currencies,
        )

    proof = False

    if location_object is None:
        print(f"  skip: bad location record ({location_object})", flush=True)

    return location_object, proof


def apply_graph_operations(
        client_instance: HyperClient,
        location_object: LocationObject,
        namespace
) -> dict[str, str]:
    # save -> geo.locations.<record_key>
    record_key = f"{location_object.name}-{location_object.geoname_id}"
    record_path = f"locations/{record_key}"

    n = client_instance.save_record(
        path=f"locations/{record_key}",
        data=location_object.__dict__,
        indexes=LOCATION_INDEXES,
        root=namespace,
    )

    geo_dot = f"{namespace}.{record_path.replace('/', '.')}"
    # EV: confirms the one geo write landed
    print(f"[geo] saved {geo_dot} ops={n}", flush=True)
    return {"record": geo_dot}
