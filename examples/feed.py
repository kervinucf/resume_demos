#!/usr/bin/env python3
"""
    python -m examples.weather_feed

Writes weather data to the graph. Runs independently.
Any app can read these paths — the globe, a dashboard, a ticker, anything.
"""

import time, random
from HyperCoreSDK.client import HyperClient

hc = HyperClient(root="weather", port=8765)
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

while True:
    for key, info in CITIES.items():
        # In production: fetch from a real weather API
        temp = random.randint(30, 100)
        cond = random.choice(CONDS)

        hc.write(f"data/weather/{key}",
            city   = info["city"],
            lat    = info["lat"],
            lng    = info["lng"],
            region = info["region"],
            temp   = temp,
            cond   = cond,
        )

    print(f"Updated {len(CITIES)} cities")
    time.sleep(30)