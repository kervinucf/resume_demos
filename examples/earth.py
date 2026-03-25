#!/usr/bin/env python3
"""
    python -m examples.weather_globe
    open http://localhost:8765/weather

Reads weather data from the graph. Doesn't fetch weather itself.
Run weather_feed.py (or any other writer) to populate the data.
"""

import time, json
from HyperCoreSDK.client import HyperClient

hc = HyperClient(root="weather888", port=8765)
hc.connect()
hc.clear()

# ── Globe setup (boilerplate hidden in a helper) ───────────────────

def mount_globe(hc, key="root/globe"):
    html = '<div id="g" style="width:100%;height:100%;background:#000"></div>'
    js = (
        "(function(){if(window._g)return;window._g=1;"
        "var s=document.createElement('script');s.src='//cdn.jsdelivr.net/npm/globe.gl';"
        "s.onload=function(){var el=document.getElementById('g');"
        "var w=new Globe(el)"
        ".globeImageUrl('//cdn.jsdelivr.net/npm/three-globe/example/img/earth-dark.jpg')"
        ".backgroundColor('rgba(0,0,0,0)').showAtmosphere(true)"
        ".width(el.offsetWidth).height(el.offsetHeight)"
        ".htmlLat('lat').htmlLng('lng').htmlAltitude(0.01)"
        ".htmlElement(function(d){var e=document.createElement('div');"
        "e.style.cssText='transform:translate(-50%,-100%);text-align:center;font-family:sans-serif';"
        "e.innerHTML=d.html;return e});"
        "window._w=w;"
        "new ResizeObserver(function(){w.width(el.offsetWidth).height(el.offsetHeight)}).observe(el)"
        "};document.head.appendChild(s)})()"
    )
    hc.mount(key, html=html, js=js, fixed=True, layer=10)

def fly(hc, lat, lng, markers):
    js = f"if(window._w){{window._w.pointOfView({{lat:{lat},lng:{lng},altitude:1.8}},1500);window._w.htmlElementsData({json.dumps(markers)})}}"
    hc.mount("root/_cmd", js=js, layer=0)

def clear(hc):
    hc.mount("root/_cmd", js="if(window._w)window._w.htmlElementsData([])", layer=0)

# ── Marker HTML (built in Python, not JS) ──────────────────────────

def marker_html(city, temp, cond):
    return (
        f"<div style='background:#fff2;border:1px solid #fff3;padding:3px 6px;border-radius:6px'>"
        f"<div style='font-size:10px;color:#fff'>{city}</div>"
        f"<div style='font-size:15px;font-weight:bold;color:#facc15'>{temp}°F</div>"
        f"<div style='font-size:9px;color:#94a3b8'>{cond}</div></div>"
    )

# ── Regions (just camera positions — data comes from the graph) ────

REGIONS = [
    ("north_america", 38, -98),
    ("europe",        50,  10),
    ("asia",          28, 105),
    ("africa",         5,  25),
    ("oceania",      -28, 145),
]

# ── Read weather from the graph, build markers ─────────────────────

def read_region(hc, region_name):
    """Read all weather paths, return markers for one region."""
    snap = hc.snapshot()
    markers = []
    for key, val in snap.items():
        if not key.startswith("data/weather/"):
            continue
        if val.get("region") != region_name:
            continue
        markers.append({
            "lat":  val.get("lat", 0),
            "lng":  val.get("lng", 0),
            "html": marker_html(val.get("city","?"), val.get("temp","?"), val.get("cond","?")),
        })
    return markers

# ── Mount & Tour ───────────────────────────────────────────────────

mount_globe(hc)

while True:
    for region, lat, lng in REGIONS:
        markers = read_region(hc, region)
        fly(hc, lat, lng, markers)
        time.sleep(5)
        clear(hc)
        time.sleep(0.8)