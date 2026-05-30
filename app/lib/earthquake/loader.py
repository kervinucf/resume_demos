# app/lib/earthquakes/loader.py
"""
Pull USGS earthquake GeoJSON feeds into root="earthquakes".

    earthquakes.events.<period>.<yyyy>.<mm>.<dd>.<usgs-id>   the event (+ indexes)
    earthquakes.latest.<period>                              newest event per feed
    geo.<location>.refs.earthquakes.*                        backrefs on nearby places
    earthquakes.<event>.refs.nearby_location.*               forward links to places

Each event is one dict whose search is inferred from its data and `kind`.
Browsable indexes cover period, event day, magnitude band, status, and alert.
"""
from __future__ import annotations

import json
import re
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import certifi

from HyperCoreSDK.python.helpers.loader import Loader, projection
from HyperCoreSDK.python.helpers.indexes import ScopeSpec, ValueIndexSpec


EARTHQUAKE_ROOT = "earthquakes"
GEO_ROOT = "geo"

DEFAULT_PERIODS = ["hour", "day"]
DEFAULT_NEARBY_RADIUS = "100km"
DEFAULT_NEARBY_LIMIT = 5

USGS_FEEDS = {
    "hour": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson",
    "day": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson",
}


_EQ_PROJECT = projection("title", "place", "magnitude", "depth_km", "time", "lat", "lon")

EARTHQUAKE_INDEXES: list[ValueIndexSpec] = [
    ValueIndexSpec("period", "period", normalize="slug", link_projections=_EQ_PROJECT),
    ValueIndexSpec(
        "event_day", "event_day", normalize="slug",
        scopes=[ScopeSpec("period", normalize="slug")], link_projections=_EQ_PROJECT,
    ),
    ValueIndexSpec(
        "magnitude_band", "magnitude_band", normalize="slug",
        scopes=[ScopeSpec("period", normalize="slug")], link_projections=_EQ_PROJECT,
    ),
    ValueIndexSpec(
        "status", "status", normalize="slug",
        scopes=[ScopeSpec("period", normalize="slug")], link_projections=_EQ_PROJECT,
    ),
    ValueIndexSpec(
        "alert", "alert", normalize="slug",
        scopes=[ScopeSpec("period", normalize="slug")], link_projections=_EQ_PROJECT,
    ),
]


def slug(value: Any, fallback: str = "unknown") -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "").strip().lower())
    return re.sub(r"-{2,}", "-", text).strip("-") or fallback


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_from_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        out = float(value)
        return default if out != out else out
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int | None = None) -> int | None:
    try:
        return default if value is None else int(value)
    except (TypeError, ValueError):
        return default


def magnitude_band(m: float) -> str:
    bands = [(1, "lt-1"), (2, "1-1_9"), (3, "2-2_9"), (4, "3-3_9"),
             (5, "4-4_9"), (6, "5-5_9"), (7, "6-6_9")]
    for ceiling, label in bands:
        if m < ceiling:
            return label
    return "7-plus"


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

    @property
    def event_dt(self) -> datetime:
        return datetime.fromtimestamp(self.time_ms / 1000, tz=timezone.utc)

    @property
    def event_day(self) -> str:
        return self.event_dt.strftime("%Y-%m-%d")

    @property
    def record_rel(self) -> str:
        dt = self.event_dt
        return f"events/{self.period}/{dt.year:04d}/{dt.month:02d}/{dt.day:02d}/{slug(self.event_id, 'event')}"

    @property
    def record_path(self) -> str:
        return f"{EARTHQUAKE_ROOT}.{self.record_rel.replace('/', '.')}"

    @property
    def latest_path(self) -> str:
        return f"{EARTHQUAKE_ROOT}.latest.{slug(self.period)}"


def event_from_feature(feature: dict[str, Any], *, period: str) -> EarthquakeEvent | None:
    if not isinstance(feature, dict):
        return None

    props = feature.get("properties") or {}
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates") if isinstance(geom, dict) else None
    if not isinstance(props, dict) or not isinstance(coords, list) or len(coords) < 3:
        return None

    event_id = str(feature.get("id") or props.get("code") or "").strip()
    mag = _f(props.get("mag"))
    place = str(props.get("place") or "").strip()
    time_ms = _i(props.get("time"))
    lon, lat, depth = _f(coords[0]), _f(coords[1]), _f(coords[2])

    if not event_id or mag is None or not place or time_ms is None:
        return None
    if lon is None or lat is None or depth is None:
        return None

    return EarthquakeEvent(
        event_id=event_id, period=period,
        title=str(props.get("title") or f"M{mag:g} earthquake - {place}"),
        place=place, magnitude=float(mag), time_ms=int(time_ms),
        updated_ms=_i(props.get("updated")), lon=float(lon), lat=float(lat), depth_km=float(depth),
        url=props.get("url"), detail_url=props.get("detail"),
        status=props.get("status"), alert=props.get("alert"),
        tsunami=_i(props.get("tsunami")), significance=_i(props.get("sig")),
        felt=_i(props.get("felt")), cdi=_f(props.get("cdi")), mmi=_f(props.get("mmi")),
        mag_type=props.get("magType"), event_type=props.get("type"), source=props.get("net"),
    )


def event_data(event: EarthquakeEvent, *, fetched_at: str) -> dict[str, Any]:
    fetched_ms = int(datetime.fromisoformat(fetched_at).timestamp() * 1000)
    return {
        "kind": "earthquake_event",
        "event_id": event.event_id,
        "period": event.period,
        "title": event.title,
        "place": event.place,
        "magnitude": event.magnitude,
        "magnitude_band": magnitude_band(event.magnitude),
        "time": iso_from_ms(event.time_ms),
        "time_ms": event.time_ms,
        "updated": iso_from_ms(event.updated_ms),
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
        "activity_latest_at": max(event.time_ms, fetched_ms),
    }


def event_links(event: EarthquakeEvent) -> dict[str, Any]:
    links: dict[str, Any] = {"latest": event.latest_path}
    if event.url:
        links["usgs"] = event.url
    if event.detail_url:
        links["usgs_detail"] = event.detail_url
    return links


def event_ref_payload(event: EarthquakeEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id, "title": event.title, "place": event.place,
        "magnitude": event.magnitude, "magnitude_band": magnitude_band(event.magnitude),
        "time": iso_from_ms(event.time_ms), "event_day": event.event_day,
        "lat": event.lat, "lon": event.lon, "depth_km": event.depth_km,
        "status": event.status, "alert": event.alert, "period": event.period,
    }


def fetch_geojson(period: str) -> dict[str, Any]:
    url = USGS_FEEDS.get(period)
    if not url:
        raise ValueError(f"invalid period {period!r}; expected one of {sorted(USGS_FEEDS)}")

    req = urllib.request.Request(url, headers={
        "Accept": "application/geo+json, application/json",
        "User-Agent": "HyperCoreSDK earthquake loader",
    })
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(req, timeout=60, context=context) as resp:
        if resp.getcode() != 200:
            raise RuntimeError(f"USGS request failed with status {resp.getcode()}")
        return json.loads(resp.read().decode("utf-8"))


def nearby_location_paths(eq: Loader, event: EarthquakeEvent) -> list[str]:
    """Opportunistic: if geo is loaded, find nearby places; otherwise return nothing."""
    try:
        result = eq.find(
            kind="location",
            near=("lat", "lon", event.lat, event.lon, DEFAULT_NEARBY_RADIUS),
            near_mode="geo",
            limit=DEFAULT_NEARBY_LIMIT,
        )
    except Exception:
        return []

    out = []
    for item in (result.get("items", []) if isinstance(result, dict) else []):
        path = item.get("canonical_path") or item.get("entity_id")
        if path:
            out.append(str(path))
    return list(dict.fromkeys(out))


def write_event(eq: Loader, event: EarthquakeEvent, *, fetched_at: str, link_geo: bool = True) -> str:
    # 1. Canonical, searchable event + indexes + usgs links, in one write.
    eq.record(
        event.record_rel,
        event_data(event, fetched_at=fetched_at),
        indexes=EARTHQUAKE_INDEXES,
        ref_key=event.event_id,
        ref_payload=event_ref_payload(event),
        links=event_links(event),
    )

    # 2. Latest pointer for this feed period.
    eq.thing(
        path=event.latest_path,
        kind="earthquake_latest",
        name=f"Latest earthquake feed: {event.period}",
        target=event.record_path,
        body={"period": event.period, "latest_event_id": event.event_id, **event_ref_payload(event)},
        links={"latest_event": event.record_path},
    )

    # 3. Cross-link to nearby geo locations, both directions.
    if link_geo:
        for loc_path in nearby_location_paths(eq, event):
            loc_key = slug(loc_path.rsplit(".", 1)[-1], "location")
            dt = event.event_dt
            rel_tail = f"{dt.year:04d}.{dt.month:02d}.{dt.day:02d}.{slug(event.event_id)}"

            eq.link(
                source=loc_path, rel=f"earthquakes.{rel_tail}", target=event.record_path,
                kind="location_earthquake_ref", name=event.title,
                body={**event_ref_payload(event), "earthquake_path": event.record_path, "location_path": loc_path},
                links={"earthquake": event.record_path, "location": loc_path},
            )
            eq.link(
                source=event.record_path, rel=f"nearby_location.{loc_key}", target=loc_path,
                kind="earthquake_nearby_location_ref", name=f"Nearby location for {event.title}",
                body={"event_id": event.event_id, "location_path": loc_path, "radius_hint": DEFAULT_NEARBY_RADIUS},
                links={"earthquake": event.record_path, "location": loc_path},
            )

    return event.record_path


def sync_period(eq: Loader, *, period: str, limit: int | None = None, link_geo: bool = True) -> int:
    print(f"feed: USGS all_{period}")
    try:
        payload = fetch_geojson(period)
    except Exception as exc:
        print(f"  fetch failed: {type(exc).__name__}: {exc}")
        return 0

    fetched_at = now_utc().isoformat()
    written = 0

    for feature in payload.get("features") or []:
        if limit is not None and written >= limit:
            break
        event = event_from_feature(feature, period=period)
        if event is None:
            continue
        try:
            path = write_event(eq, event, fetched_at=fetched_at, link_geo=link_geo)
            written += 1
            print(f"  ok: M{event.magnitude:g} {event.place} -> {path}")
        except Exception as exc:
            print(f"  write failed for {event.event_id}: {type(exc).__name__}: {exc}")

    print(f"  wrote {written:,} event(s); feed count={payload.get('metadata', {}).get('count')}")
    return written


def run(
    eq: Loader,
    *,
    periods: list[str] | None = None,
    limit_per_period: int | None = None,
    link_geo: bool = True,
    keep_alive: bool = False,
) -> int:
    total = sum(
        sync_period(eq, period=p, limit=limit_per_period, link_geo=link_geo)
        for p in (periods or DEFAULT_PERIODS)
    )
    print(f"done — {total:,} earthquake event(s)")

    if keep_alive:
        eq.serve()
    else:
        eq.close()
    return total


def main() -> int:
    run(Loader(EARTHQUAKE_ROOT), periods=DEFAULT_PERIODS, link_geo=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())