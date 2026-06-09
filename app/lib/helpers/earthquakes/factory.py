"""
Earthquake factory — the typed object + builder for the earthquake database.

Mirrors weather/factory.py and geo/factory.py:
    WeatherEventObject  <->  EarthquakeEventObject  (frozen dataclass, the stored body)
    WeatherFactory      <->  EarthquakeFactory      (builds typed objects from NORMALIZED fields)

Nothing here knows how USGS encodes data — EarthquakeFactory takes plain named
fields. Provider decoding (GeoJSON layout, epoch-ms time) lives in
app/lib/sources/usgs.py. The one piece of domain logic is the magnitude band.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def magnitude_band(m: float) -> str:
    bands = [(1, "lt-1"), (2, "1-1_9"), (3, "2-2_9"), (4, "3-3_9"),
             (5, "4-4_9"), (6, "5-5_9"), (7, "6-6_9")]
    for ceiling, label in bands:
        if m < ceiling:
            return label
    return "7-plus"


# ---------------------------------------------------------------------------
# Earthquake object  (analog of WeatherEventObject)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EarthquakeEventObject:
    event_id: str
    period: str
    title: str
    place: str
    magnitude: float
    magnitude_band: str
    time: str | None
    time_ms: int
    updated: str | None
    event_day: str
    lat: float
    lon: float
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
    fetched_at: str
    activity_latest_at: int

    _eq_root: str = "earthquakes"
    _geo_root: str = "geo"

    @property
    def event_dt(self) -> datetime:
        return datetime.fromtimestamp(self.time_ms / 1000, tz=timezone.utc)

    def record_key(self) -> str:
        """Flat leaf like geo's locations/<key> — no deep shared ancestors."""
        return f"{self.period}-{self.event_day}-{self.event_id}"

    def latest_key(self) -> str:
        return self.period

    def ref_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "title": self.title, "place": self.place,
            "magnitude": self.magnitude, "magnitude_band": self.magnitude_band,
            "time": self.time, "event_day": self.event_day,
            "lat": self.lat, "lon": self.lon, "depth_km": self.depth_km,
            "status": self.status, "alert": self.alert, "period": self.period,
        }


# ---------------------------------------------------------------------------
# Earthquake builder  (analog of WeatherFactory / LocationFactory)
# ---------------------------------------------------------------------------
class EarthquakeFactory:
    """Builds typed EarthquakeEventObjects from already-normalized provider fields."""

    @classmethod
    def create_event_object(cls, *, event_record: dict[str, Any]) -> EarthquakeEventObject:
        mag = float(event_record.get("magnitude") or 0.0)
        return EarthquakeEventObject(
            event_id=str(event_record.get("event_id") or "").strip(),
            period=str(event_record.get("period") or "").strip(),
            title=str(event_record.get("title") or ""),
            place=str(event_record.get("place") or ""),
            magnitude=mag,
            magnitude_band=magnitude_band(mag),
            time=event_record.get("time"),
            time_ms=int(event_record.get("time_ms") or 0),
            updated=event_record.get("updated"),
            event_day=str(event_record.get("event_day") or "").strip(),
            lat=float(event_record.get("lat") or 0.0),
            lon=float(event_record.get("lon") or 0.0),
            depth_km=float(event_record.get("depth_km") or 0.0),
            url=event_record.get("url"),
            detail_url=event_record.get("detail_url"),
            status=event_record.get("status"),
            alert=event_record.get("alert"),
            tsunami=event_record.get("tsunami"),
            significance=event_record.get("significance"),
            felt=event_record.get("felt"),
            cdi=event_record.get("cdi"),
            mmi=event_record.get("mmi"),
            mag_type=event_record.get("mag_type"),
            event_type=event_record.get("event_type"),
            source=event_record.get("source"),
            fetched_at=str(event_record.get("fetched_at") or ""),
            activity_latest_at=int(event_record.get("activity_latest_at") or 0),
        )