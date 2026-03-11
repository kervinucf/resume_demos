#!/usr/bin/env python3
"""Demo 1 — Continuous Linear Broadcast.

A single channel that cycles through segments on a loop. Data is fetched
in background threads. The screen is never blank.

    Terminal 1:  cd relay && node server.js
    Terminal 2:  cd broadcast && python demos/demo_linear.py
    Browser:     http://localhost:8765/live/
"""

import os, sys, logging, time, random, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# SDK lives at ../sdk relative to this file
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from npctv_v2.app.sdk.scene import SceneWriter
from npctv_v2.app.sdk.timeline import GlobeState, compile, wait, sweep, show, hide, refresh, \
    set_chyron, clear_chyron, set_ticker, set_sidebar, hide_sidebar, \
    show_bumper, clear_layer, init_layer, note
from npctv_v2.app.sdk.chrome import eq_marker, weather_badge, story_marker
from npctv_v2.app.sdk.boot import boot
from npctv_v2.app.sdk.data import earthquakes, weather, news, sports

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("broadcast")

RELAY  = os.environ.get("RELAY", "http://localhost:8765")
BUCKET = os.environ.get("BUCKET", "live")

ROTATION = ["headlines", "sports", "weather", "earthquakes"]

# Globe layer configs
HTML_LAYER  = {"htmlElement": "d=>{const e=document.createElement('div');e.innerHTML=d.html.trim();return e.firstChild}"}
HEX_LAYER   = {"hexBinPointLat":"p=>p.lat","hexBinPointLng":"p=>p.lng","hexBinPointWeight":"p=>p.weight",
                "hexBinResolution":4,"hexMargin":0.15,"hexAltitude":"d=>Math.max(d.sumWeight*0.6,0.012)",
                "hexTopColor":"d=>'#33ff99'","hexSideColor":"d=>'rgba(51,255,153,0.55)'"}
RING_LAYER  = {"ringColor":"d=>d.color","ringMaxRadius":0.75,"ringPropagationSpeed":0.15,"ringRepeatPeriod":1600}
POINT_LAYER = {"pointColor":"()=>'#7dfc00'","pointAltitude":0.01,"pointRadius":0.35}

# Fallback positions for stories without geo data
WORLD_CITIES = [
    (40.7,-74.0),(51.5,-0.1),(35.7,139.7),(-33.9,151.2),
    (48.9,2.4),(25.2,55.3),(39.9,116.4),(19.4,-99.1),
]
LEAGUE_LABELS = {"nba":"🏀 NBA","nhl":"🏒 NHL","nfl":"🏈 NFL","epl":"⚽ EPL","mls":"⚽ MLS","liga":"⚽ La Liga"}


# ── Data buffer ──

class Buffer:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {}
    def store(self, key, val):
        with self._lock: self._data[key] = val
    def get(self, key):
        with self._lock: return self._data.get(key)

buf = Buffer()

def fetch_all():
    log.info("Fetching data...")
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {
            pool.submit(earthquakes): "earthquakes",
            pool.submit(weather):     "weather",
            pool.submit(news):        "headlines",
            pool.submit(sports):      "sports",
        }
        for fut in as_completed(futs, timeout=30):
            key = futs[fut]
            try:
                result = fut.result(timeout=15)
                if result:
                    buf.store(key, result)
                    log.info("  %s: %d items", key, len(result))
            except Exception as e:
                log.warning("  %s: failed (%s)", key, e)


# ── Segment builders — each returns a timeline or None ──

def seg_earthquakes():
    data = buf.get("earthquakes")
    if not data: return None
    tl = [
        note("▶ Earthquakes"),
        clear_layer("html"), clear_layer("hex"), clear_layer("rings"),
        init_layer("html", HTML_LAYER), init_layer("hex", HEX_LAYER), init_layer("rings", RING_LAYER),
        set_chyron("GLOBAL SEISMIC ACTIVITY"),
        set_ticker(" ••• ".join(f"M{e['magnitude']:.1f} {e['location']}" for e in data[:15])),
        sweep(15, 160, duration=6000, altitude=2.0, hold=4),
    ]
    for i, ev in enumerate(data[:12]):
        color = "#d90429" if ev["magnitude"] >= 6.5 else "#fca311" if ev["magnitude"] >= 5 else "#fe9240"
        mid, rid, hid = f"eq{i}", f"r{i}", f"h{i}"
        marker = {"id": mid, "lat": ev["lat"], "lng": ev["lng"], "html": eq_marker(ev["magnitude"], color)}
        ring   = {"id": rid, "lat": ev["lat"], "lng": ev["lng"], "color": color}
        hx     = {"id": hid, "lat": ev["lat"], "lng": ev["lng"], "weight": 0.024, "sign": 1}
        tl.extend([
            set_chyron(f"M{ev['magnitude']:.1f} — {ev['location']}"),
            sweep(ev["lat"], ev["lng"], duration=3000, altitude=random.uniform(0.4, 1.0), hold=3),
            show("html", marker), show("hex", hx), show("rings", ring),
            refresh("html", "hex", "rings"), wait(5),
            hide("html", [mid]), hide("hex", [hid]), hide("rings", [rid]),
            refresh("html", "hex", "rings"), wait(1),
        ])
    tl.extend([
        clear_chyron(), set_ticker("Monitoring for seismic events."),
        sweep(25, 80, duration=5000, altitude=3.0, hold=3),
    ])
    return tl


def seg_weather():
    data = buf.get("weather")
    if not data: return None
    tl = [
        note("▶ Weather"),
        clear_layer("html"), init_layer("html", HTML_LAYER),
        set_chyron("GLOBAL WEATHER REPORT"),
        sweep(20, -120, duration=6000, altitude=0.5, hold=4),
    ]
    elements = []
    for w in data[:15]:
        el = {"id": f"wx_{w['city']}", "lat": w["lat"], "lng": w["lng"],
              "html": weather_badge(w["emoji"], w["temp_c"], w["city"])}
        elements.append(el)
        tl.extend([
            set_chyron(f"{w['city'].upper()}: {w['condition']}, {w['temp_c']}°C"),
            sweep(w["lat"], w["lng"], duration=4000, altitude=0.4, hold=3),
            show("html", el), refresh("html"),
            set_ticker(f"{w['city']}: {w['condition']}, {w['temp_c']}°C, Wind {w['wind_kph']} km/h"),
            wait(6),
            hide("html", [el["id"]]), refresh("html"), wait(0.5),
        ])
    # Tableau — show all at once
    tl.extend([
        set_chyron("WORLDWIDE CONDITIONS"),
        show("html", elements), refresh("html"),
        sweep(25, 45, duration=8000, altitude=1.8, hold=15),
    ])
    for el in elements:
        tl.append(hide("html", [el["id"]]))
    tl.append(refresh("html"))
    return tl


def seg_headlines():
    data = buf.get("headlines")
    if not data: return None
    tl = [
        note("▶ Headlines"),
        clear_layer("points"), clear_layer("html"),
        init_layer("points", POINT_LAYER), init_layer("html", HTML_LAYER),
        set_ticker(" ••• ".join(a["title"] for a in data[:10])),
        set_sidebar([{"title": a["title"][:50], "subtitle": a["source"]} for a in data[:8]]),
        sweep(20, -30, duration=3000, altitude=3.0, hold=2),
    ]
    for i, article in enumerate(data[:8]):
        fallback = WORLD_CITIES[i % len(WORLD_CITIES)]
        lat = article.get("lat") or fallback[0]
        lng = article.get("lng") or fallback[1]
        mid, pid = f"n{i}", f"p{i}"
        tl.extend([
            set_chyron(article["title"][:80], source=article["source"]),
            sweep(lat, lng, duration=random.randint(2000, 4000), altitude=random.uniform(0.3, 0.7), hold=4),
            show("html", {"id": mid, "lat": lat, "lng": lng, "html": story_marker(i + 1)}),
            show("points", {"id": pid, "lat": lat, "lng": lng}),
            refresh("html", "points"), wait(8),
            hide("html", [mid]), hide("points", [pid]),
            refresh("html", "points"), wait(1),
        ])
    tl.extend([clear_chyron(), hide_sidebar(), sweep(25, 80, duration=5000, altitude=3.5, hold=3)])
    return tl


def seg_sports():
    data = buf.get("sports")
    if not data: return None
    leagues = {}
    for ev in data:
        leagues.setdefault(ev["league"], []).append(ev)
    tl = [
        note("▶ Sports"),
        set_ticker(" ••• ".join(
            f"[{e['status_detail']}] {e['away']} {e['away_score']}-{e['home_score']} {e['home']}"
            for e in data[:15])),
        set_sidebar([{"title": f"{e['away']} vs {e['home']}", "subtitle": e["status_detail"]} for e in data[:8]]),
    ]
    for lid, games in leagues.items():
        tl.append(set_chyron(LEAGUE_LABELS.get(lid, lid.upper()), source="SCORES"))
        tl.append(wait(max(8, len(games) * 3)))
    tl.extend([clear_chyron(), hide_sidebar(), sweep(25, 80, duration=5000, altitude=3.5, hold=3)])
    return tl


HANDLERS = {
    "earthquakes": seg_earthquakes,
    "weather":     seg_weather,
    "headlines":   seg_headlines,
    "sports":      seg_sports,
}


# ── Main ──

def main():
    s = SceneWriter(RELAY, BUCKET)
    globe = GlobeState()

    log.info("=" * 50)
    log.info("CONTINUOUS BROADCAST — %s/%s/", RELAY, BUCKET)
    log.info("=" * 50)

    boot(s, "NPCTV")
    fetch_all()

    # Background refresh every 5 minutes
    threading.Thread(target=lambda: [time.sleep(300) or fetch_all() for _ in iter(int, 1)],
                     daemon=True).start()

    seg_idx = 0
    while True:
        seg_name = ROTATION[seg_idx % len(ROTATION)]
        seg_idx += 1
        log.info("── %s ──", seg_name.upper())

        timeline = HANDLERS.get(seg_name, lambda: None)()
        if not timeline:
            log.info("No data for %s — skipping", seg_name)
            time.sleep(5)
            continue

        compile(s, [show_bumper(seg_name)], globe)
        compile(s, timeline, globe)
        s.wait(3)


if __name__ == "__main__":
    main()