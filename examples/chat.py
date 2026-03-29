#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from HyperCoreSDK.client import HyperClient

PANEL_HTML = """
<div style="font-family:monospace;padding:16px">
  <div>city: <span id="top-city" data-bind-text="city"></span></div>
  <div>temp: <span id="top-temp" data-bind-text="temp"></span></div>
  <div>cond: <span id="top-cond" data-bind-text="cond"></span></div>
  <div>units: <span id="top-units" data-bind-text="units"></span></div>
  <div>note: <span id="top-note" data-bind-text="note"></span></div>
</div>
"""

PANEL_JS = """
(function(){
  function txt(id){
    var el = document.getElementById(id);
    return el ? el.textContent : null;
  }
  function dump(tag){
    var payload = {
      tag: tag,
      href: location.href,
      hyperContext: window.hyperContext || window.hyper_context || null,
      dom: {
        city: txt('top-city'),
        temp: txt('top-temp'),
        cond: txt('top-cond'),
        units: txt('top-units'),
        note: txt('top-note')
      }
    };
    var pre = document.getElementById('dom-dump');
    if (pre) pre.textContent = JSON.stringify(payload, null, 2);
    console.log('[PANEL DEBUG]', payload);
  }
  dump('boot');
  setTimeout(function(){ dump('t+250ms'); }, 250);
  setTimeout(function(){ dump('t+1000ms'); }, 1000);
  setInterval(function(){ dump('interval'); }, 2000);
})();
"""
def pretty(obj) -> str:
    try:
        return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
    except Exception:
        return repr(obj)

def seed_weather(hc: HyperClient) -> None:
    print("[PY] mounting source node weather/nyc")
    hc.write(
        "weather/nyc",
        data={
            "city": "New York",
            "temp": 72,
            "cond": "Sunny",
            "units": "°F",
            "note": "source-node",
        },
    )
    print("[PY] mounting panel weather/nyc/panel")
    hc.mount("weather/nyc/panel", html=PANEL_HTML, js=PANEL_JS)

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="demo111")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--discovery", default="local")
    args = ap.parse_args()

    hc = HyperClient(root=args.root, port=args.port, discovery=args.discovery)
    hc.connect()
    seed_weather(hc)

    base_url = hc.stream_url("weather/nyc/panel")
    leaf_url = hc.render_url(
        "weather/nyc/panel",
        params={
            "city": hc.bind("weather/nyc/data/city"),
            "temp": hc.bind("weather/nyc/data/temp"),
            "cond": hc.bind("weather/nyc/data/cond"),
            "units": "°F",
            "note": "leaf-binds",
        },
    )
    obj_url = hc.render_url(
        "weather/nyc/panel",
        params={
            "weather": hc.bind("weather/nyc"),
            "note": "object-bind",
        },
    )

    print("\n[PY] OPEN THESE URLS")
    print("[PY] base_url =", base_url)
    print("[PY] leaf_url =", leaf_url)
    print("[PY] obj_url  =", obj_url)

    i = 0
    temps = [72, 74, 71, 69, 76]
    conds = ["Sunny", "Breezy", "Cloudy", "Rain", "Partly Cloudy"]

    while True:
        temp = temps[i % len(temps)]
        cond = conds[i % len(conds)]
        payload = {
            "city": "New York",
            "temp": temp,
            "cond": cond,
            "units": "°F",
            "note": f"source-node-{i}",
        }
        print("\n[PY] write weather/nyc data =")
        print(pretty(payload))
        hc.write("weather/nyc", data=payload)

        node = hc.read("weather/nyc")
        panel = hc.read("weather/nyc/panel")
        print("[PY] read weather/nyc =")
        print(pretty(node))
        print("[PY] read weather/nyc/panel =")
        print(pretty(panel))

        i += 1

if __name__ == "__main__":
    main()
