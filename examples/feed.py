#!/usr/bin/env python3
"""
    python weather_feed.py --discovery lan --port 8766

Writes weather to gun paths. That's it.
"""

import argparse, time, random
from HyperCoreSDK.client import HyperClient

p = argparse.ArgumentParser()
p.add_argument("--discovery", default="lan")
p.add_argument("--port", type=int, default=8766)
a = p.parse_args()

hc = HyperClient(root="weather", discovery=a.discovery, port=a.port)
hc.connect()
# NO hc.clear() — we don't own the UI, we just write data

CITIES = [
    ("new_york",    "New York",     40.71,  -74.01, "north_america"),
    ("los_angeles", "Los Angeles",  34.05, -118.24, "north_america"),
    ("london",      "London",       51.51,   -0.13, "europe"),
    ("paris",       "Paris",        48.86,    2.35, "europe"),
    ("tokyo",       "Tokyo",        35.68,  139.69, "asia"),
    ("mumbai",      "Mumbai",       19.08,   72.88, "asia"),
    ("cairo",       "Cairo",        30.04,   31.24, "africa"),
    ("nairobi",     "Nairobi",      -1.29,   36.82, "africa"),
    ("sydney",      "Sydney",      -33.87,  151.21, "oceania"),
]

CONDS = ["☀️ Sunny", "🌧️ Rain", "⛅ Cloudy", "🌤️ Partly", "❄️ Cold", "🌩️ Storms"]

while True:
    for key, city, lat, lng, region in CITIES:
        hc.write(f"data/weather/{key}",
            city=city, lat=lat, lng=lng, region=region,
            temp=random.randint(30, 100),
            cond=random.choice(CONDS),
        )
    print(f"Updated {len(CITIES)} cities")
    time.sleep(30)