from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")
_DASHES_RE = re.compile(r"-{2,}")


def slugify(value: str, *, fallback: str = "observation") -> str:
    text = _SLUG_RE.sub("-", str(value or "").strip().lower())
    text = _DASHES_RE.sub("-", text).strip("-")
    return text or fallback


@dataclass(frozen=True)
class WeatherObservation:
    location_key: str          # e.g. "new-york-city-5128581"
    location_path: str         # e.g. "geo.locations.new-york-city-5128581"
    name: str
    lat: float
    lon: float
    country_code: str
    country_flag_emoji: str
    temperature: float
    condition: str
    weather_code: int | None
    wind_speed: float | None
    precipitation: float | None
    local_time: str
    observed_at: str           # ISO-8601 UTC

    def record_key(self) -> str:
        """
        Stable, time-partitioned id:
            <location_key>/<yyyy>/<mm>/<dd>/<hhmmss>
        Slashes become dots when composed into the record path, giving the
        hierarchical `weather.history.<loc>.<yyyy>.<mm>.<dd>.<hhmmss>`
        layout.
        """
        dt = datetime.fromisoformat(self.observed_at)
        return (
            f"{self.location_key}/"
            f"{dt.year:04d}/{dt.month:02d}/{dt.day:02d}/"
            f"{dt.hour:02d}{dt.minute:02d}{dt.second:02d}"
        )

    def latest_key(self) -> str:
        """Per-location pointer to the most recent observation."""
        return self.location_key

    def to_dict(self) -> dict[str, Any]:
        return {"model": "weather-observation", **asdict(self)}

    def latest_dict(self, history_path: str) -> dict[str, Any]:
        d = self.to_dict()
        d["model"] = "weather-latest"
        d["target"] = history_path
        return d

    def ref_payload(self) -> dict[str, Any]:
        """
        Compact snapshot merged into each index entry's `data`. Lets a UI
        render listings (`NYC 12.4°C · Partly cloudy`) without hydrating
        the source record.
        """
        return {
            "name": self.name,
            "country_code": self.country_code,
            "country_flag_emoji": self.country_flag_emoji,
            "temperature": self.temperature,
            "condition": self.condition,
            "weather_code": self.weather_code,
            "observed_at": self.observed_at,
            "lat": self.lat,
            "lon": self.lon,
        }