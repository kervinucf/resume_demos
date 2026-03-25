#!/usr/bin/env python3
"""
    python weather_globe.py --discovery lan --port 8765
    open http://localhost:8765/weather

Reads weather from the gun graph (written by weather_feed.py on any peer).
Renders a globe that tours through regions showing markers.
"""

import argparse, time, json
from HyperCoreSDK.client import HyperClient

p = argparse.ArgumentParser()
p.add_argument("--discovery", default="lan")
p.add_argument("--port", type=int, default=8765)
a = p.parse_args()

hc = HyperClient(root="weather8887", discovery=a.discovery, port=a.port)
hc.connect()
hc.clear()

# ── Globe HTML + JS ────────────────────────────────────────────────

GLOBE_HTML = """
<div style="width:100%;height:100%;position:relative;background:#000;font-family:sans-serif">
  <div id="g" style="width:100%;height:100%"></div>
  <div data-bind-text="region" style="position:absolute;top:20px;left:50%;transform:translateX(-50%);font-size:22px;font-weight:bold;color:#fff;text-shadow:0 2px 8px #000"></div>
</div>
"""

GLOBE_JS = r"""
(function(){
  if(window._g) return;
  window._g = 1;

  var s = document.createElement("script");
  s.src = "//cdn.jsdelivr.net/npm/globe.gl";
  s.onload = init;
  document.head.appendChild(s);

  function init() {
    var el = document.getElementById("g");
    if (!el || !el.offsetHeight) { setTimeout(init, 100); return; }

    window._w = new Globe(el)
      .globeImageUrl("//cdn.jsdelivr.net/npm/three-globe/example/img/earth-dark.jpg")
      .backgroundColor("rgba(0,0,0,0)")
      .showAtmosphere(true)
      .width(el.offsetWidth)
      .height(el.offsetHeight)
      .htmlLat("lat").htmlLng("lng").htmlAltitude(0.01)
      .htmlElement(function(d) {
        var e = document.createElement("div");
        e.style.cssText = "transform:translate(-50%,-100%);text-align:center";
        e.innerHTML =
          "<div style='background:#fff2;border:1px solid #fff3;padding:3px 6px;border-radius:6px'>" +
          "<div style='font-size:10px;color:#fff'>" + d.city + "</div>" +
          "<div style='font-size:15px;font-weight:bold;color:#facc15'>" + d.temp + "°F</div>" +
          "<div style='font-size:9px;color:#94a3b8'>" + d.cond + "</div></div>";
        return e;
      });

    new ResizeObserver(function() {
      window._w.width(el.offsetWidth).height(el.offsetHeight);
    }).observe(el);
  }
})();
"""

hc.mount("root/globe", html=GLOBE_HTML, js=GLOBE_JS, fixed=True, layer=10)

# ── Region tour ────────────────────────────────────────────────────

REGIONS = [
    ("North America", "north_america", 38, -98),
    ("Europe",        "europe",        50,  10),
    ("Asia",          "asia",          28, 105),
    ("Africa",        "africa",         5,  25),
    ("Oceania",       "oceania",      -28, 145),
]

def cities_for(region_key):
    """Read weather data from local snapshot (Gun keeps it synced)."""
    markers = []
    for key, val in hc.snapshot().items():
        if key.startswith("data/weather/") and val.get("region") == region_key:
            markers.append({
                "city": val.get("city", "?"),
                "lat":  float(val.get("lat", 0)),
                "lng":  float(val.get("lng", 0)),
                "temp": val.get("temp", "?"),
                "cond": val.get("cond", "?"),
            })
    return markers

while True:
    for name, key, lat, lng in REGIONS:
        markers = cities_for(key)
        hc.write("root/globe", region=name)
        hc.mount("root/_fly", layer=0, js=
            f"if(window._w){{window._w.pointOfView({{lat:{lat},lng:{lng},altitude:1.8}},1500);"
            f"window._w.htmlElementsData({json.dumps(markers)})}}"
        )
        time.sleep(5)

        # Clear before next region
        hc.mount("root/_fly", layer=0, js=
            "if(window._w)window._w.htmlElementsData([])"
        )
        time.sleep(0.8)