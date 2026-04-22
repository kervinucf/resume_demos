from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import certifi

from app.utils.dtos.Location import Location
from app.utils.dtos.Weather import WeatherObservation

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

WMO = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Violent showers",
    85: "Light snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ heavy hail",
}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Open-Meteo
# ---------------------------------------------------------------------------

def _describe(code: int | None) -> str:
    if code is None:
        return "Unknown"
    return WMO.get(int(code), f"Unknown ({code})")


def _fetch_open_meteo(lat: float, lon: float) -> dict:
    q = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,weather_code,wind_speed_10m,precipitation",
        "timezone": "auto",
    })
    req = urllib.request.Request(
        f"{OPEN_METEO_URL}?{q}",
        headers={"Accept": "application/json"},
    )
    ctx = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def _as_float(v) -> float | None:
    return float(v) if v is not None else None


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def observation_from_location(loc: Location) -> WeatherObservation | None:
    """
    Fetch current weather for `loc` and return a WeatherObservation, or
    None on any failure (network error, missing temperature, etc.).
    """
    try:
        payload = _fetch_open_meteo(loc.lat, loc.lon)
    except Exception:
        return None

    cur = payload.get("current") or {}
    temp = cur.get("temperature_2m")
    if temp is None:
        return None

    code = cur.get("weather_code")
    code_int = int(code) if code is not None else None

    return WeatherObservation(
        location_key=loc.record_key(),
        location_path=f"geo.locations.{loc.record_key()}",
        name=loc.name,
        lat=loc.lat,
        lon=loc.lon,
        country_code=loc.country_code,
        country_flag_emoji=loc.country_flag_emoji,
        temperature=float(temp),
        condition=_describe(code_int),
        weather_code=code_int,
        wind_speed=_as_float(cur.get("wind_speed_10m")),
        precipitation=_as_float(cur.get("precipitation")),
        local_time=str(cur.get("time") or ""),
        observed_at=now_utc().isoformat(),
    )