#!/usr/bin/env python3
"""
    python -m examples.weather_feed

Writes weather data to the graph. Runs independently.
Any app can read these paths — the globe, a dashboard, a ticker, anything.
"""

import time
import random
from HyperCoreSDK.client import HyperClient

hc = HyperClient(root="weather12", port=8765)
hc.connect()

# Cities this feed is responsible for
CITIES = {
    "new_york":    {"city": "New York",    "lat": 40.71, "lng": -74.01,  "region": "north_america"},
    "los_angeles": {"city": "Los Angeles", "lat": 34.05, "lng":-118.24,  "region": "north_america"},
    "chicago":     {"city": "Chicago",     "lat": 41.88, "lng": -87.63,  "region": "north_america"},
    "london":      {"city": "London",      "lat": 51.51, "lng":  -0.13,  "region": "europe"},
    "paris":       {"city": "Paris",       "lat": 48.86, "lng":   2.35,  "region": "europe"},
    "moscow":      {"city": "Moscow",      "lat": 55.76, "lng":  37.62,  "region": "europe"},
    "tokyo":       {"city": "Tokyo",       "lat": 35.68, "lng": 139.69,  "region": "asia"},
    "mumbai":      {"city": "Mumbai",      "lat": 19.08, "lng":  72.88,  "region": "asia"},
    "singapore":   {"city": "Singapore",   "lat":  1.35, "lng": 103.82,  "region": "asia"},
    "cairo":       {"city": "Cairo",       "lat": 30.04, "lng":  31.24,  "region": "africa"},
    "nairobi":     {"city": "Nairobi",     "lat": -1.29, "lng":  36.82,  "region": "africa"},
    "sydney":      {"city": "Sydney",      "lat":-33.87, "lng": 151.21,  "region": "oceania"},
}

CONDS = ["☀️ Sunny", "🌧️ Rain", "⛅ Cloudy", "🌤️ Partly", "❄️ Cold", "🌩️ Storms"]


def verify_city(path):
    """Read back a node to confirm it exists in the graph."""
    data = hc.read(path)
    if not data:
        return False, {}
    return True, data


def count_weather_nodes():
    snap = hc.snapshot()
    keys = sorted(k for k in snap.keys() if k.startswith("data/weather/"))
    return keys, snap


print("=" * 60)
print("weather_feed starting")
print("root:", hc.root)
print("relay:", hc.relay_url)
print("browser:", hc.browser_url)
print("=" * 60)

cycle = 0

while True:
    cycle += 1
    print(f"\n--- cycle {cycle} ---")

    ok_writes = 0

    for key, info in CITIES.items():
        path = f"data/weather/{key}"

        temp = random.randint(30, 100)
        cond = random.choice(CONDS)

        wrote = hc.write(
            path,
            city=info["city"],
            lat=info["lat"],
            lng=info["lng"],
            region=info["region"],
            temp=temp,
            cond=cond,
        )

        exists, data = verify_city(path)

        print(
            f"[{key:12}] write={wrote!s:5} "
            f"readback={exists!s:5} "
            f"city={info['city']:<12} "
            f"temp={temp:>3} "
            f"cond={cond}"
        )

        if exists:
            print(
                f"             stored -> "
                f"region={data.get('region')} "
                f"lat={data.get('lat')} "
                f"lng={data.get('lng')} "
                f"temp={data.get('temp')} "
                f"cond={data.get('cond')}"
            )

        if wrote and exists:
            ok_writes += 1

    keys, snap = count_weather_nodes()
    print(f"\ncycle summary: {ok_writes}/{len(CITIES)} writes verified")
    print(f"snapshot weather node count: {len(keys)}")

    if keys:
        sample = keys[:5]
        print("sample keys:")
        for k in sample:
            print("  ", k, "->", snap[k])

    time.sleep(30)