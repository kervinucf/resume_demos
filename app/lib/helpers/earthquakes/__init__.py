"""
Earthquake operations — the verbs the main loader orchestrates.

Mirrors app/lib/helpers/geo/__init__.py and weather/__init__.py: the provider is
read inside the create_* functions, so the loader only ever imports:

    from app.lib.helpers.earthquakes import (apply_graph_operations,
                                             list_event_candidates,
                                             create_event_object,
                                             EarthquakeEventObject)

This module is the ONE place that names the concrete provider. To switch providers,
change the `app.lib.sources.usgs` import below to another module exposing the same
source callables — nothing else changes.
"""
from __future__ import annotations

from typing import Any, Iterator

from HyperCoreSDK.python.client import HyperClient, projection, ScopeSpec, ValueIndexSpec
# --- provider boundary: swap this single import to change source ------------
from app.lib.sources.usgs import iter_event_candidates, source_available
# ---------------------------------------------------------------------------
from app.lib.helpers.earthquakes.factory import EarthquakeFactory, EarthquakeEventObject

earthquake_factory = EarthquakeFactory()

__all__ = [
    "HyperClient",
    "list_event_candidates",
    "create_event_object",
    "apply_graph_operations",
    "source_available",
    "EarthquakeEventObject",
]

EARTHQUAKE_ROOT = "earthquakes"
GEO_ROOT = "geo"

# Fields each index entry carries forward for cheap rendering (information density).
PROJECT = projection("title", "place", "magnitude", "depth_km", "time", "lat", "lon")
EARTHQUAKE_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec("period", "period", normalize="slug", link_projections=PROJECT),
    ValueIndexSpec(
        "event_day", "event_day", normalize="slug",
        scopes=[ScopeSpec("period", normalize="slug")], link_projections=PROJECT,
    ),
    ValueIndexSpec(
        "magnitude_band", "magnitude_band", normalize="slug",
        scopes=[ScopeSpec("period", normalize="slug")], link_projections=PROJECT,
    ),
    ValueIndexSpec(
        "status", "status", normalize="slug",
        scopes=[ScopeSpec("period", normalize="slug")], link_projections=PROJECT,
    ),
    ValueIndexSpec(
        "alert", "alert", normalize="slug",
        scopes=[ScopeSpec("period", normalize="slug")], link_projections=PROJECT,
    ),
]


def list_event_candidates(
        periods: list[str],
) -> Iterator[tuple[dict[str, Any], bool]]:
    for record in iter_event_candidates(periods):
        proof = (
            bool(record.get("event_id"))
            and record.get("time_ms") is not None
            and record.get("magnitude") is not None
        )
        if not proof:
            print(f"  skip: bad event record ({record.get('event_id')})", flush=True)
            continue
        yield record, proof


def create_event_object(
        event_record: dict[str, Any],
) -> tuple[EarthquakeEventObject | None, bool]:
    try:
        event_object = earthquake_factory.create_event_object(event_record=event_record)
    except (TypeError, ValueError) as exc:
        print(f"  skip: bad event record ({exc})", flush=True)
        return None, False

    proof = bool(event_object.event_id) and event_object.time_ms > 0
    if not proof:
        print(f"  skip: incomplete event object ({event_record.get('event_id')})", flush=True)
    return event_object, proof


def apply_graph_operations(
        event_object: EarthquakeEventObject,
        client_instance: HyperClient,
        namespace,
        write_latest: bool = True,
) -> dict[str, str]:
    record_key = event_object.record_key()
    event_path = f"events/{record_key}"
    latest_path = f"latest/{event_object.latest_key()}"

    event_dot = f"{namespace}.{event_path.replace('/', '.')}"
    latest_dot = f"{namespace}.{latest_path.replace('/', '.')}"

    # Carried USGS links live on the record.
    record_links: dict[str, str] = {"latest": latest_dot}
    if event_object.url:
        record_links["usgs"] = event_object.url
    if event_object.detail_url:
        record_links["usgs_detail"] = event_object.detail_url

    record_data = {"tag": "earthquake_event", **event_object.__dict__}

    # 1) RECORD — indexed, dense. ref_key MUST be unique per stored record: USGS
    #    reuses the same event_id across the hour and day feeds, so keying the index
    #    on event_id alone makes the unscoped band/day/status/alert buckets emit the
    #    same leaf twice -> UNIQUE collision. Use the period-qualified record_key,
    #    exactly like geo keys on <name>-<geoname_id>.
    n1 = client_instance.save_record(
        path=event_path,
        data=record_data,
        indexes=EARTHQUAKE_INDEXES,
        links=record_links,
        root=namespace,
    )

    # 2) POINTER earthquakes.latest.<period> -> event. Written ONCE per feed period;
    #    per-event writes would insert the same (parent=latest, name=period) node N
    #    times -> UNIQUE collision at bulk flush.
    n2 = 0
    if write_latest:
        n2 = client_instance.write_ops([{
            "path": latest_dot,
            "data": {"data": {"tag": "earthquake_latest", "period": event_object.period,
                              "latest_event_id": event_object.event_id, **event_object.ref_payload()},
                     "links": {"latest_event": event_dot}},
        }], root=namespace)

    # EV: two numbers, one line — latest=0 expected for the 2nd+ event of a period.
    # NOTE: geo cross-linking (nearby_location refs) deferred — needs a relay spatial
    # query (the old eq.find(near=...)) that isn't on the HyperClient API yet.
    print(f"[earthquake] {event_dot} record={n1} latest={n2}", flush=True)
    return {"event": event_dot, "latest": latest_dot}