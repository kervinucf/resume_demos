# app/lib/earthquakes/loader.py
"""
Pull USGS earthquake GeoJSON feeds into the hypergraph.

Readable graph shape:

    earthquakes.events.<period>.<yyyy>.<mm>.<dd>.<usgs-id>
    earthquakes.latest.<period>

Optional cross-links when nearby geo locations are found:

    geo.locations.<location-key>.refs.earthquakes.<yyyy>.<mm>.<dd>.<usgs-id>
    earthquakes.events.<period>.<yyyy>.<mm>.<dd>.<usgs-id>.refs.nearby_location.<location-key>

The loader treats earthquakes as first-class searchable entities with:
    - magnitude
    - depth_km
    - lat/lon
    - event time
    - USGS status/type/source
    - nearby location links, when resolvable

Normal search comes from client.put(...).
Explicit browsable indexes are added for period, magnitude band, event day, and alert/status.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from HyperCoreSDK.python.client import HyperClient
from HyperCoreSDK.python.helpers.server import create_hyper_server
from HyperCoreSDK.python.helpers.storage import create_default_storage_directory
from HyperCoreSDK.python.helpers.indexes import ScopeSpec, ValueIndexSpec
import ssl
import certifi
import urllib.request

EARTHQUAKE_ROOT = "earthquakes"
GEO_ROOT = "geo"

DEFAULT_PERIODS = ["hour", "day"]
DEFAULT_NEARBY_RADIUS = "100km"
DEFAULT_NEARBY_LIMIT = 5


USGS_FEEDS = {
    "hour": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson",
    "day": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson",
}


EARTHQUAKE_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec(
        name="period",
        path="period",
        normalize="slug",
        link_projections={
            "title": "title",
            "place": "place",
            "magnitude": "magnitude",
            "depth_km": "depth_km",
            "time": "time",
            "lat": "lat",
            "lon": "lon",
        },
    ),
    ValueIndexSpec(
        name="event_day",
        path="event_day",
        normalize="slug",
        scopes=[ScopeSpec(path="period", normalize="slug")],
        link_projections={
            "title": "title",
            "place": "place",
            "magnitude": "magnitude",
            "depth_km": "depth_km",
            "time": "time",
            "lat": "lat",
            "lon": "lon",
        },
    ),
    ValueIndexSpec(
        name="magnitude_band",
        path="magnitude_band",
        normalize="slug",
        scopes=[ScopeSpec(path="period", normalize="slug")],
        link_projections={
            "title": "title",
            "place": "place",
            "magnitude": "magnitude",
            "depth_km": "depth_km",
            "time": "time",
            "lat": "lat",
            "lon": "lon",
        },
    ),
    ValueIndexSpec(
        name="status",
        path="status",
        normalize="slug",
        scopes=[ScopeSpec(path="period", normalize="slug")],
        link_projections={
            "title": "title",
            "place": "place",
            "magnitude": "magnitude",
            "depth_km": "depth_km",
            "time": "time",
            "lat": "lat",
            "lon": "lon",
        },
    ),
    ValueIndexSpec(
        name="alert",
        path="alert",
        normalize="slug",
        scopes=[ScopeSpec(path="period", normalize="slug")],
        link_projections={
            "title": "title",
            "place": "place",
            "magnitude": "magnitude",
            "depth_km": "depth_km",
            "time": "time",
            "lat": "lat",
            "lon": "lon",
        },
    ),
]


@dataclass(frozen=True)
class EarthquakeEvent:
    event_id: str
    period: str
    title: str
    place: str
    magnitude: float
    time_ms: int
    updated_ms: int | None
    lon: float
    lat: float
    depth_km: float
    url: str | None
    detail_url: str | None
    status: str | None
    alert: str | None
    tsunami: int | None
    significance: int | None
    felt: int | None
    cdi: float | None
    mmi: float | None
    mag_type: str | None
    event_type: str | None
    source: str | None
    raw: dict[str, Any]

    @property
    def event_dt(self) -> datetime:
        return datetime.fromtimestamp(self.time_ms / 1000, tz=timezone.utc)

    @property
    def event_day(self) -> str:
        return self.event_dt.strftime("%Y-%m-%d")

    @property
    def record_key(self) -> str:
        clean_id = slug(self.event_id, "event")
        return f"{self.period}/{self.event_dt.year:04d}/{self.event_dt.month:02d}/{self.event_dt.day:02d}/{clean_id}"

    @property
    def record_rel(self) -> str:
        return f"events/{self.record_key}"

    @property
    def record_path(self) -> str:
        return f"{EARTHQUAKE_ROOT}.{self.record_rel.replace('/', '.')}"

    @property
    def latest_path(self) -> str:
        return f"{EARTHQUAKE_ROOT}.latest.{slug(self.period)}"


def slug(value: Any, fallback: str = "unknown") -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "").strip().lower())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or fallback


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_from_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        out = float(value)
        if out != out:
            return default
        return out
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def magnitude_band(magnitude: float) -> str:
    if magnitude < 1:
        return "lt-1"
    if magnitude < 2:
        return "1-1_9"
    if magnitude < 3:
        return "2-2_9"
    if magnitude < 4:
        return "3-3_9"
    if magnitude < 5:
        return "4-4_9"
    if magnitude < 6:
        return "5-5_9"
    if magnitude < 7:
        return "6-6_9"
    return "7-plus"


def fetch_geojson(period: str) -> dict[str, Any]:
    url = USGS_FEEDS.get(period)

    if not url:
        raise ValueError(f"invalid period {period!r}; expected one of {sorted(USGS_FEEDS)}")

    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/geo+json, application/json",
            "User-Agent": "HyperCoreSDK earthquake loader",
        },
    )

    # Create a secure context using certifi's root certificates
    context = ssl.create_default_context(cafile=certifi.where())

    with urllib.request.urlopen(req, timeout=60, context=context) as response:
        if response.getcode() != 200:
            raise RuntimeError(f"USGS request failed with status {response.getcode()}")

        return json.loads(response.read().decode("utf-8"))


def event_from_feature(feature: dict[str, Any], *, period: str) -> EarthquakeEvent | None:
    if not isinstance(feature, dict):
        return None

    properties = feature.get("properties") or {}
    geometry = feature.get("geometry") or {}

    if not isinstance(properties, dict) or not isinstance(geometry, dict):
        return None

    coords = geometry.get("coordinates")
    if not isinstance(coords, list) or len(coords) < 3:
        return None

    event_id = str(feature.get("id") or properties.get("code") or "").strip()
    mag = safe_float(properties.get("mag"))
    place = str(properties.get("place") or "").strip()
    time_ms = safe_int(properties.get("time"))
    lon = safe_float(coords[0])
    lat = safe_float(coords[1])
    depth = safe_float(coords[2])

    if not event_id or mag is None or not place or time_ms is None:
        return None

    if lon is None or lat is None or depth is None:
        return None

    title = str(properties.get("title") or f"M{mag:g} earthquake - {place}")

    return EarthquakeEvent(
        event_id=event_id,
        period=period,
        title=title,
        place=place,
        magnitude=float(mag),
        time_ms=int(time_ms),
        updated_ms=safe_int(properties.get("updated")),
        lon=float(lon),
        lat=float(lat),
        depth_km=float(depth),
        url=properties.get("url"),
        detail_url=properties.get("detail"),
        status=properties.get("status"),
        alert=properties.get("alert"),
        tsunami=safe_int(properties.get("tsunami")),
        significance=safe_int(properties.get("sig")),
        felt=safe_int(properties.get("felt")),
        cdi=safe_float(properties.get("cdi")),
        mmi=safe_float(properties.get("mmi")),
        mag_type=properties.get("magType"),
        event_type=properties.get("type"),
        source=properties.get("net"),
        raw=feature,
    )


def earthquake_body(event: EarthquakeEvent, *, fetched_at: str) -> dict[str, Any]:
    return {
        "model": "earthquake-event",
        "event_id": event.event_id,
        "period": event.period,
        "title": event.title,
        "place": event.place,
        "magnitude": event.magnitude,
        "magnitude_band": magnitude_band(event.magnitude),
        "time": iso_from_ms(event.time_ms),
        "time_ms": event.time_ms,
        "updated": iso_from_ms(event.updated_ms),
        "updated_ms": event.updated_ms,
        "event_day": event.event_day,
        "lat": event.lat,
        "lon": event.lon,
        "depth_km": event.depth_km,
        "url": event.url,
        "detail_url": event.detail_url,
        "status": event.status,
        "alert": event.alert,
        "tsunami": event.tsunami,
        "significance": event.significance,
        "felt": event.felt,
        "cdi": event.cdi,
        "mmi": event.mmi,
        "mag_type": event.mag_type,
        "event_type": event.event_type,
        "source": event.source,
        "fetched_at": fetched_at,
        "activity_latest_at": max(event.time_ms, int(datetime.fromisoformat(fetched_at).timestamp() * 1000)),
    }


def earthquake_ref_payload(event: EarthquakeEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "title": event.title,
        "place": event.place,
        "magnitude": event.magnitude,
        "magnitude_band": magnitude_band(event.magnitude),
        "time": iso_from_ms(event.time_ms),
        "event_day": event.event_day,
        "lat": event.lat,
        "lon": event.lon,
        "depth_km": event.depth_km,
        "status": event.status,
        "alert": event.alert,
        "period": event.period,
    }


def earthquake_links(event: EarthquakeEvent) -> dict[str, Any]:
    links: dict[str, Any] = {
        "latest": event.latest_path,
        "stream": f"{event.record_path}?stream=true",
        "changes_since": f"{event.record_path}/api/changes-since",
    }

    if event.url:
        links["usgs"] = event.url

    if event.detail_url:
        links["usgs_detail"] = event.detail_url

    return links


def latest_body(event: EarthquakeEvent, *, fetched_at: str) -> dict[str, Any]:
    return {
        "model": "earthquake-latest",
        "period": event.period,
        "latest_event": event.record_path,
        "latest_event_id": event.event_id,
        "title": event.title,
        "place": event.place,
        "magnitude": event.magnitude,
        "magnitude_band": magnitude_band(event.magnitude),
        "time": iso_from_ms(event.time_ms),
        "time_ms": event.time_ms,
        "event_day": event.event_day,
        "lat": event.lat,
        "lon": event.lon,
        "depth_km": event.depth_km,
        "status": event.status,
        "alert": event.alert,
        "fetched_at": fetched_at,
        "activity_latest_at": event.time_ms,
    }


def nearby_location_paths(
    client: HyperClient,
    event: EarthquakeEvent,
    *,
    radius: str = DEFAULT_NEARBY_RADIUS,
    limit: int = DEFAULT_NEARBY_LIMIT,
) -> list[str]:
    """
    Find nearby existing geo.location records by radius.

    This is intentionally opportunistic. If geo is absent, query fails, or no
    locations match, the earthquake still gets written.
    """
    try:
        result = client.find_things(
            kind="location",
            near=("lat", "lon", event.lat, event.lon, radius),
            near_mode="geo",
            limit=limit,
        )
    except Exception:
        return []

    out: list[str] = []

    for item in result.get("items", []) if isinstance(result, dict) else []:
        path = item.get("canonical_path") or item.get("entity_id")
        if path:
            out.append(str(path))

    return list(dict.fromkeys(out))


def location_key_from_path(path: str) -> str:
    return slug(str(path).rsplit(".", 1)[-1], "location")


def write_nearby_location_links(
    client: HyperClient,
    event: EarthquakeEvent,
    *,
    nearby_locations: list[str],
) -> None:
    for loc_path in nearby_locations:
        loc_key = location_key_from_path(loc_path)
        rel_tail = f"{event.event_dt.year:04d}.{event.event_dt.month:02d}.{event.event_dt.day:02d}.{slug(event.event_id)}"

        # Location -> earthquake.
        client.link(
            source=loc_path,
            rel=f"earthquakes.{rel_tail}",
            target=event.record_path,
            kind="location_earthquake_ref",
            name=event.title,
            body={
                "event_id": event.event_id,
                "title": event.title,
                "place": event.place,
                "magnitude": event.magnitude,
                "magnitude_band": magnitude_band(event.magnitude),
                "time": iso_from_ms(event.time_ms),
                "event_day": event.event_day,
                "depth_km": event.depth_km,
                "earthquake_path": event.record_path,
                "location_path": loc_path,
            },
            links={
                "earthquake": event.record_path,
                "location": loc_path,
            },
        )

        # Earthquake -> nearby location.
        client.link(
            source=event.record_path,
            rel=f"nearby_location.{loc_key}",
            target=loc_path,
            kind="earthquake_nearby_location_ref",
            name=f"Nearby location for {event.title}",
            body={
                "event_id": event.event_id,
                "location_path": loc_path,
                "earthquake_path": event.record_path,
                "radius_hint": DEFAULT_NEARBY_RADIUS,
            },
            links={
                "earthquake": event.record_path,
                "location": loc_path,
            },
        )


def write_event(
    client: HyperClient,
    event: EarthquakeEvent,
    *,
    fetched_at: str,
    link_geo: bool = True,
) -> str:
    body = earthquake_body(event, fetched_at=fetched_at)

    # Canonical record with optional browsable indexes.
    client.write_record_with_indexes(
        root=EARTHQUAKE_ROOT,
        record_path=event.record_rel,
        record_data=body,
        index_specs=EARTHQUAKE_INDEXES,
        ref_key=event.event_id,
        ref_payload=earthquake_ref_payload(event),
    )

    # Searchable hypermedia event.
    client.put(
        path=event.record_path,
        kind="earthquake_event",
        name=event.title,
        target=event.record_path,
        body=body,
        links=earthquake_links(event),
    )

    # Stable latest pointer for the feed period.
    client.put(
        path=event.latest_path,
        kind="earthquake_latest",
        name=f"Latest earthquake feed: {event.period}",
        target=event.record_path,
        body=latest_body(event, fetched_at=fetched_at),
        links={
            "target": event.record_path,
            "latest_event": event.record_path,
            "stream": f"{event.latest_path}?stream=true",
            "changes_since": f"{event.latest_path}/api/changes-since",
        },
    )

    if link_geo:
        nearby = nearby_location_paths(client, event)
        if nearby:
            write_nearby_location_links(client, event, nearby_locations=nearby)

    return event.record_path


def sync_period(
    client: HyperClient,
    *,
    period: str,
    limit: int | None = None,
    link_geo: bool = True,
) -> int:
    print(f"feed: USGS all_{period}")

    try:
        payload = fetch_geojson(period)
    except urllib.error.URLError as exc:
        print(f"  fetch failed: {exc}")
        return 0
    except Exception as exc:
        print(f"  fetch failed: {type(exc).__name__}: {exc}")
        return 0

    fetched_at = now_utc().isoformat()
    features = payload.get("features") or []
    written = 0

    for feature in features:
        if limit is not None and written >= limit:
            break

        event = event_from_feature(feature, period=period)
        if event is None:
            continue

        try:
            path = write_event(
                client,
                event,
                fetched_at=fetched_at,
                link_geo=link_geo,
            )
            written += 1
            print(f"  ok: M{event.magnitude:g} {event.place} -> {path}")

        except Exception as exc:
            print(f"  write failed for {event.event_id}: {type(exc).__name__}: {exc}")

    metadata_count = payload.get("metadata", {}).get("count")
    print(f"  wrote {written:,} event(s); feed metadata count={metadata_count}")

    return written


def run(
    client: HyperClient,
    *,
    periods: list[str] | None = None,
    limit_per_period: int | None = None,
    link_geo: bool = True,
    close_client: bool = False,
    keep_alive: bool = False,
) -> int:
    total = 0

    try:
        for period in periods or DEFAULT_PERIODS:
            total += sync_period(
                client,
                period=period,
                limit=limit_per_period,
                link_geo=link_geo,
            )

        print(f"done — {total:,} earthquake event(s)")

        if keep_alive:
            print(f"relay still running at {client.url} (Ctrl-C to stop)")
            while True:
                time.sleep(3600)

    except KeyboardInterrupt:
        pass

    finally:
        if close_client:
            client.close()

    return total


def main() -> int:
    client = create_hyper_server(
        root=EARTHQUAKE_ROOT,
        data_path=create_default_storage_directory(),
    )

    run(
        client,
        periods=DEFAULT_PERIODS,
        limit_per_period=None,
        link_geo=True,
        close_client=True,
        keep_alive=False,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())