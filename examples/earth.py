#!/usr/bin/env python3
"""
    python -m examples.weather_globe
    open http://localhost:8765/weather11

Viewer app:
- mounts the globe scene
- browser subscribes directly to data/weather/* in the graph
- rotates through regions locally in the browser

Run weather_feed.py (or any other writer) to populate the data.
"""

import time
from HyperCoreSDK.client import HyperClient

hc = HyperClient(root="weather11", port=8765)
hc.connect()


def mount_globe(hc, key="root/globe"):
    html = '<div id="g" style="width:100%;height:100%;background:#000"></div>'

    js = r"""
(async function () {
  if (window._weatherGlobeInit) return;
  window._weatherGlobeInit = true;

  function loadScript(src) {
    return new Promise(function(resolve, reject) {
      var s = document.createElement('script');
      s.src = src;
      s.onload = resolve;
      s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function markerHtml(city, temp, cond) {
    return (
      "<div style='background:#fff2;border:1px solid #fff3;padding:3px 6px;border-radius:6px'>" +
        "<div style='font-size:10px;color:#fff'>" + esc(city) + "</div>" +
        "<div style='font-size:15px;font-weight:bold;color:#facc15'>" + esc(temp) + "°F</div>" +
        "<div style='font-size:9px;color:#94a3b8'>" + esc(cond) + "</div>" +
      "</div>"
    );
  }

  if (typeof Globe === "undefined") {
    await loadScript("//cdn.jsdelivr.net/npm/globe.gl");
  }

  var el = document.getElementById("g");
  if (!el) return;

  var globe = new Globe(el)
    .globeImageUrl("//cdn.jsdelivr.net/npm/three-globe/example/img/earth-dark.jpg")
    .backgroundColor("rgba(0,0,0,0)")
    .showAtmosphere(true)
    .width(el.offsetWidth || window.innerWidth)
    .height(el.offsetHeight || window.innerHeight)
    .htmlLat("lat")
    .htmlLng("lng")
    .htmlAltitude(0.01)
    .htmlElement(function (d) {
      var e = document.createElement("div");
      e.style.cssText = "transform:translate(-50%,-100%);text-align:center;font-family:sans-serif";
      e.innerHTML = d.html;
      return e;
    });

  window._w = globe;

  new ResizeObserver(function () {
    globe.width(el.offsetWidth || window.innerWidth)
         .height(el.offsetHeight || window.innerHeight);
  }).observe(el);

  var weather = {};
  var currentRegion = null;

  var REGIONS = [
    ["north_america", 38, -98],
    ["europe",        50,  10],
    ["asia",          28, 105],
    ["africa",         5,  25],
    ["oceania",      -28, 145]
  ];

  function redraw() {
    var markers = [];

    Object.keys(weather).forEach(function (key) {
      var v = weather[key];
      if (!v) return;
      if (currentRegion && v.region !== currentRegion) return;

      markers.push({
        lat: Number(v.lat || 0),
        lng: Number(v.lng || 0),
        html: markerHtml(v.city || "?", v.temp || "?", v.cond || "?")
      });
    });

    globe.htmlElementsData(markers);
  }

  function applyWeatherNode(key, data) {
    if (!key || key.indexOf("data/weather/") !== 0) return;

    if (!data || typeof data !== "object") {
      delete weather[key];
      redraw();
      return;
    }

    weather[key] = {
      city:   data.city,
      lat:    data.lat,
      lng:    data.lng,
      region: data.region,
      temp:   data.temp,
      cond:   data.cond
    };

    redraw();
  }

  async function seedFromSnapshot() {
    try {
      var resp = await fetch("/" + window.$bucket + "/api/snapshot", { cache: "no-store" });
      var snap = await resp.json();
      if (!snap || typeof snap !== "object") return;

      Object.keys(snap).forEach(function (key) {
        if (key.indexOf("data/weather/") === 0) {
          applyWeatherNode(key, snap[key]);
        }
      });
    } catch (e) {
      console.warn("[weather seed]", e);
    }
  }

  await seedFromSnapshot();

  window.$scene.map().on(function (data, key) {
    if (!key || key === "_") return;
    if (key.indexOf("data/weather/") !== 0) return;
    applyWeatherNode(key, data);
  });

  var idx = 0;

  function rotate() {
    var r = REGIONS[idx % REGIONS.length];
    currentRegion = r[0];
    globe.pointOfView({ lat: r[1], lng: r[2], altitude: 1.8 }, 1500);
    redraw();
    idx += 1;
  }

  rotate();
  setInterval(rotate, 5800);
})();
"""
    hc.mount(key, html=html, js=js, fixed=True, layer=10)


mount_globe(hc)

# Keep the viewer process alive so its relay stays up.
while True:
    time.sleep(60)