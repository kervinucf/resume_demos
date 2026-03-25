#!/usr/bin/env python3
"""
    python weather_globe.py --discovery lan --port 8765
    open http://localhost:8765/weather
"""

import argparse, time, json
from HyperCoreSDK.client import HyperClient

p = argparse.ArgumentParser()
p.add_argument("--discovery", default="lan")
p.add_argument("--port", type=int, default=8765)
a = p.parse_args()

hc = HyperClient(root="weather", discovery=a.discovery, port=a.port)
hc.connect()

# Only clear UI, not data — remove old root/* mounts but leave data/* alone
for key in hc.keys():
    if key.startswith("root/"):
        hc.remove(key)

# ── Globe ──────────────────────────────────────────────────────────

GLOBE_HTML = '<div id="g" style="width:100vw;height:100vh;overflow:visible"></div>'

GLOBE_JS = r"""
(function(){
  if(window._g) return;
  window._g = 1;

  var s = document.createElement("script");
  s.src = "https://cdn.jsdelivr.net/npm/globe.gl";
  s.onload = function() {
    var el = document.getElementById("g");

    window._w = new Globe(el)
      .globeImageUrl("https://cdn.jsdelivr.net/npm/three-globe/example/img/earth-dark.jpg")
      .backgroundColor("#000")
      .showAtmosphere(true)
      .width(el.offsetWidth)
      .height(el.offsetHeight)
      .htmlLat("lat").htmlLng("lng").htmlAltitude(0.1)
      .htmlElement(function(d) {
        var div = document.createElement("div");
        div.innerHTML =
          "<div style='color:#fff;background:rgba(255,200,0,0.9);padding:6px 10px;border-radius:6px;font-family:sans-serif;font-size:14px;white-space:nowrap'>" +
          d.city + " " + d.temp + "°F</div>";
        return div;
      });

    // Fix overflow on all ancestors so CSS2DRenderer markers aren't clipped
    var node = el;
    while (node && node !== document.body) {
      node.style.overflow = "visible";
      node = node.parentElement;
    }

    new ResizeObserver(function() {
      window._w.width(el.offsetWidth).height(el.offsetHeight);
    }).observe(el);

    console.log("Globe ready");
  };
  document.head.appendChild(s);
})();
"""

hc.mount("root/globe", html=GLOBE_HTML, js=GLOBE_JS, fixed=True, layer=10)

# ── Regions ────────────────────────────────────────────────────────

REGIONS = [
    ("North America", "north_america", 38, -98),
    ("Europe",        "europe",        50,  10),
    ("Asia",          "asia",          28, 105),
    ("Africa",        "africa",         5,  25),
    ("Oceania",       "oceania",      -28, 145),
]

# ── Tour ───────────────────────────────────────────────────────────

cmd_counter = 0

while True:
    for name, region_key, lat, lng in REGIONS:

        # Read weather data from local snapshot (Gun-synced from feed)
        markers = []
        snap = hc.snapshot()
        for key, val in snap.items():
            if key.startswith("data/weather/") and val.get("region") == region_key:
                markers.append({
                    "city": val.get("city", "?"),
                    "lat":  float(val.get("lat", 0)),
                    "lng":  float(val.get("lng", 0)),
                    "temp": str(val.get("temp", "?")),
                    "cond": val.get("cond", "?"),
                })

        print(f"{name}: {len(markers)} markers — {[m['city'] for m in markers]}")

        # Fly + show markers — unique key each time so JS always re-executes
        cmd_counter += 1
        hc.mount(f"root/_cmd{cmd_counter}", layer=0, js=
            f"if(window._w){{"
            f"window._w.pointOfView({{lat:{lat},lng:{lng},altitude:1.8}},1500);"
            f"window._w.htmlElementsData({json.dumps(markers)})"
            f"}}"
        )
        time.sleep(5)

        # Clear markers
        cmd_counter += 1
        hc.mount(f"root/_cmd{cmd_counter}", layer=0, js=
            "if(window._w)window._w.htmlElementsData([])"
        )
        time.sleep(0.8)

        # Clean up old command nodes
        for k in hc.keys():
            if k.startswith("root/_cmd"):
                hc.remove(k)