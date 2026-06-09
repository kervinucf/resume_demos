"""
USGS source — the provider-specific read layer for the earthquake loader.

Analog of app/lib/sources/geonames.py and open_metro.py: everything that knows
*how USGS encodes data* (GeoJSON feature/geometry/properties layout, epoch-ms
timestamps, feed URLs) lives here and nowhere else. To swap providers, write
another module in app/lib/sources/ exposing the same callables and re-point the
import in app/lib/helpers/earthquakes/__init__.py:

    source_available(period)        -> (bool, str)       # feed url known
    iter_event_candidates(periods)  -> Iterator[dict]    # one normalized event per feature

Records are plain dicts with neutral keys — no app typed imports here, so a new
provider only has to match the key names, not our object model.
"""
from __future__ import annotations

import json
import ssl
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterator

import certifi

USGS_FEEDS = {
    "hour": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson",
    "day": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson",
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_from_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        out = float(value)
        return default if out != out else out  # drop NaN
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int | None = None) -> int | None:
    try:
        return default if value is None else int(value)
    except (TypeError, ValueError):
        return default


def source_available(period: str) -> tuple[bool, str]:
    url = USGS_FEEDS.get(period, "")
    return bool(url), url


def _fetch_geojson(period: str) -> dict[str, Any]:
    url = USGS_FEEDS.get(period)
    if not url:
        raise ValueError(f"invalid period {period!r}; expected one of {sorted(USGS_FEEDS)}")

    req = urllib.request.Request(url, headers={
        "Accept": "application/geo+json, application/json",
        "User-Agent": "HyperCoreSDK earthquake loader",
    })
    ctx = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
        if resp.getcode() != 200:
            raise RuntimeError(f"USGS request failed with status {resp.getcode()}")
        return json.loads(resp.read().decode("utf-8"))


def _record_from_feature(feature: dict[str, Any], *, period: str, fetched_at: str) -> dict[str, Any] | None:
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

    event_dt = datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc)
    fetched_ms = int(datetime.fromisoformat(fetched_at).timestamp() * 1000)

    return {
        "event_id": event_id,
        "period": period,
        "title": str(props.get("title") or f"M{mag:g} earthquake - {place}"),
        "place": place,
        "magnitude": float(mag),
        "time": _iso_from_ms(time_ms),
        "time_ms": int(time_ms),
        "updated": _iso_from_ms(_i(props.get("updated"))),
        "updated_ms": _i(props.get("updated")),
        "event_day": event_dt.strftime("%Y-%m-%d"),
        "lat": float(lat),
        "lon": float(lon),
        "depth_km": float(depth),
        "url": props.get("url"),
        "detail_url": props.get("detail"),
        "status": props.get("status"),
        "alert": props.get("alert"),
        "tsunami": _i(props.get("tsunami")),
        "significance": _i(props.get("sig")),
        "felt": _i(props.get("felt")),
        "cdi": _f(props.get("cdi")),
        "mmi": _f(props.get("mmi")),
        "mag_type": props.get("magType"),
        "event_type": props.get("type"),
        "source": props.get("net"),
        "fetched_at": fetched_at,
        "activity_latest_at": max(int(time_ms), fetched_ms),
    }


def iter_event_candidates(periods: list[str]) -> Iterator[dict[str, Any]]:
    """
    Stream normalized earthquake events across USGS feed periods.

    USGS GeoJSON shape knowledge (features[].geometry.coordinates [lon,lat,depth],
    properties.time in epoch ms) lives here only. A feed that fails to fetch warns
    and yields nothing, mirroring the optional-source behavior in the geo provider.
    """
    fetched_at = _now_utc().isoformat()

    for period in periods:
        try:
            payload = _fetch_geojson(period)
        except Exception as exc:
            print(f"  warning: USGS fetch failed for {period} ({exc})", flush=True)
            continue

        count = (payload.get("metadata") or {}).get("count")
        print(f"  feed all_{period}: count={count}", flush=True)

        for feature in payload.get("features") or []:
            record = _record_from_feature(feature, period=period, fetched_at=fetched_at)
            if record is not None:
                yield record