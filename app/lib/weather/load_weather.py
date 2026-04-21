from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import certifi
from HyperCoreSDK.python.client import HyperClient

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

WMO = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Violent showers",
    85: "Light snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ heavy hail",
}

def now_utc():
    return datetime.now(timezone.utc)

def segs(dt):
    return f"{dt.year:04d}", f"{dt.month:02d}", f"{dt.day:02d}", f"{dt.hour:02d}{dt.minute:02d}{dt.second:02d}"

def fetch_weather(lat, lon):
    q = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,weather_code,wind_speed_10m,precipitation",
        "timezone": "auto",
    })
    req = urllib.request.Request(f"{OPEN_METEO_URL}?{q}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context(cafile=certifi.where())) as r:
        return json.loads(r.read().decode("utf-8"))

def read_location(hc, key):
    doc = hc.read(f"geo.locations.{key}")
    data = doc.get("data") if isinstance(doc, dict) else None
    return data if isinstance(data, dict) else None

def ref(hc, at_path, rel, target, preview=None, kind=None):
    hc.write(f"{at_path}.refs.{rel}", {
        "data": {
            **({"kind": kind} if kind else {}),
            "target": target,
            **(preview or {}),
        },
        "links": {"target": target},
    })

def load_weather(hc: HyperClient, key: str):
    loc = read_location(hc, key)
    if not loc:
        return f"miss: no location at geo.locations.{key}"

    try:
        payload = fetch_weather(float(loc["lat"]), float(loc["lon"]))
    except Exception as e:
        return f"fail: {key}: {type(e).__name__}: {e}"

    dt = now_utc()
    yyyy, mm, dd, hhmmss = segs(dt)
    location = f"geo.locations.{key}"
    history = f"weather.history.{key}.{yyyy}.{mm}.{dd}.{hhmmss}"
    latest = f"weather.latest.{key}"

    cur = payload.get("current") or {}
    code = cur.get("weather_code")

    obs = {
        "model": "weather-observation",
        "location_key": key,
        "location_path": location,
        "name": str(loc.get("name") or key),
        "lat": float(loc["lat"]),
        "lon": float(loc["lon"]),
        "country_code": str(loc.get("country_code") or ""),
        "temperature": float(cur["temperature_2m"]),
        "wind_speed": float(cur["wind_speed_10m"]) if cur.get("wind_speed_10m") is not None else None,
        "precipitation": float(cur["precipitation"]) if cur.get("precipitation") is not None else None,
        "condition": WMO.get(int(code), f"Unknown ({code})") if code is not None else "Unknown",
        "weather_code": int(code) if code is not None else None,
        "local_time": str(cur.get("time") or ""),
        "observed_at": dt.isoformat(),
    }

    hc.write(history, {
        "data": obs,
        "links": {"location": location},
    })

    hc.write(latest, {
        "data": {
            "model": "weather-latest",
            "target": history,
            **{k: obs[k] for k in (
                "temperature", "condition", "weather_code", "wind_speed",
                "precipitation", "local_time", "observed_at",
                "name", "country_code", "lat", "lon"
            )}
        },
        "links": {
            "target": history,
            "location": location,
        },
    })

    preview = {
        "temperature": obs["temperature"],
        "condition": obs["condition"],
        "observed_at": obs["observed_at"],
    }

    ref(hc, location, f"weather.{yyyy}.{mm}.{dd}.{hhmmss}", history, preview, "weather-observation")
    ref(hc, location, "weather_latest", latest, preview, "weather-latest")

    return f"ok: {obs['name']} ({obs['country_code']}) — {obs['temperature']:.1f}°C, {obs['condition']}"