import json
import ssl
import urllib.parse
import urllib.request


import certifi


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

WMO: dict[int, str] = {
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

def send_open_meteo_request(*, latitude: float, longitude: float):
    """Fetch current weather. Returns a 5-tuple or None on any failure."""
    query = urllib.parse.urlencode({
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,weather_code,wind_speed_10m,precipitation",
        "timezone": "auto",
    })

    try:
        req = urllib.request.Request(f"{OPEN_METEO_URL}?{query}", headers={"Accept": "application/json"})
        ctx = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    cur = payload.get("current") or {}
    temp = cur.get("temperature_2m")
    if temp is None:
        return None

    code = cur.get("weather_code")
    code_int = int(code) if code is not None else None

    condition = "Unknown" if code_int is None else WMO.get(code_int, f"Unknown ({code_int})")

    return cur.get("time"), temp, cur.get("wind_speed_10m"), cur.get("precipitation"), code_int, condition

