#!/usr/bin/env python3
"""
GUN-native hypermedia feed.

Writes:
  - content nodes under data/...
  - directory index nodes under dirs/...

This lets an explorer subscribe to:
  - dirs/root
  - dirs/<path>
  - data/<path>

instead of polling relay HTTP snapshot endpoints.

    python -m examples.feed2
    python -m examples.feed2 --discovery lan --port 8766 --bucket demo2
"""

import argparse
import html
import json
import random
import time
from collections import defaultdict

from HyperCoreSDK.client import HyperClient


def esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def link_html(path: str, label: str | None = None) -> str:
    label = label or path
    return f"<a href='#' data-path='{esc(path)}'>{esc(label)}</a>"


class GraphFS:
    """
    Helper that writes scene nodes and maintains a parallel
    directory graph under dirs/... inside the same bucket.
    """

    def __init__(self, hc: HyperClient):
        self.hc = hc
        self.paths: set[str] = set()

    def put(self, path: str, **fields):
        path = path.strip("/")
        self.paths.add(path)
        self.hc.write(path, **fields)

    def rebuild_dirs(self):
        """
        Build virtual directory nodes:
          dirs/root
          dirs/data
          dirs/data/weather
          ...

        Each dir node contains:
          - kind="dir"
          - fs_path="<visible path>"
          - children="<json array>"
          - links="<json array>"
          - html="<renderable fragment>"
        """
        children_by_dir: dict[str, dict[str, str]] = defaultdict(dict)

        for full_path in sorted(self.paths):
            parts = [p for p in full_path.split("/") if p]
            for i, part in enumerate(parts):
                parent_visible = "/".join(parts[:i])          # "" for root
                child_visible = "/".join(parts[: i + 1])     # visible path
                children_by_dir[parent_visible][part] = child_visible

        if "" not in children_by_dir:
            children_by_dir[""] = {}

        for visible_dir in sorted(children_by_dir):
            scene_path = "dirs/root" if not visible_dir else f"dirs/{visible_dir}"
            child_map = children_by_dir[visible_dir]

            children = []
            for name, child_visible in sorted(child_map.items()):
                children.append(
                    {
                        "name": name,
                        "path": child_visible,
                        "rel": "child",
                    }
                )

            links = list(children)
            if visible_dir:
                parent_visible = "/".join(visible_dir.split("/")[:-1])
                links.insert(
                    0,
                    {
                        "name": "..",
                        "path": parent_visible,
                        "rel": "parent",
                    },
                )

            title = "/" if not visible_dir else visible_dir
            items = []
            if visible_dir:
                parent_visible = "/".join(visible_dir.split("/")[:-1])
                items.append(f"<li>{link_html(parent_visible, '..')}</li>")
            for item in children:
                items.append(f"<li>{link_html(item['path'], item['name'])}</li>")

            html_blob = "".join(
                [
                    "<div style='padding:20px;font-family:sans-serif'>",
                    f"<h2 style='margin:0 0 12px 0;color:#111'>{esc(title)}</h2>",
                    "<ul style='margin:0;padding-left:20px'>",
                    "".join(items),
                    "</ul>",
                    "</div>",
                ]
            )

            self.hc.write(
                scene_path,
                kind="dir",
                fs_path=visible_dir,
                children=json.dumps(children),
                links=json.dumps(links),
                html=html_blob,
            )


p = argparse.ArgumentParser()
p.add_argument("--discovery", default="local")
p.add_argument("--port", type=int, default=8766)
p.add_argument("--bucket", default="demo2")
a = p.parse_args()

hc = HyperClient(root=a.bucket, discovery=a.discovery, port=a.port)
hc.connect()

gfs = GraphFS(hc)

SERVICES = [
    ("api", "API Gateway"),
    ("db", "Database"),
    ("cache", "Redis Cache"),
    ("queue", "Message Queue"),
    ("storage", "Object Storage"),
]

WEATHER = [
    ("nyc", "New York", "north_america", 40.71, -74.01),
    ("la", "Los Angeles", "north_america", 34.05, -118.24),
    ("london", "London", "europe", 51.50, -0.12),
    ("paris", "Paris", "europe", 48.86, 2.35),
    ("tokyo", "Tokyo", "asia", 35.68, 139.69),
    ("mumbai", "Mumbai", "asia", 19.08, 72.88),
    ("sydney", "Sydney", "oceania", -33.86, 151.21),
]

USERS = [
    ("alice", "Alice Johnson", "admin"),
    ("bob", "Bob Smith", "operator"),
    ("carol", "Carol Lee", "analyst"),
]

WEATHER_CONDS = [
    ("Sunny", "#facc15"),
    ("Cloudy", "#94a3b8"),
    ("Rain", "#60a5fa"),
    ("Windy", "#cbd5e1"),
    ("Partly Cloudy", "#fcd34d"),
    ("Cold", "#93c5fd"),
]


def write_root_index():
    gfs.put(
        "data/index",
        html="".join(
            [
                "<div style='padding:20px;font-family:sans-serif'>",
                "<h2 style='color:#38bdf8;margin:0 0 12px 0'>Hypermedia Graph</h2>",
                "<p style='color:#94a3b8;margin:0 0 16px 0'>",
                "Each node is a renderable fragment. Follow the links.",
                "</p>",
                "<div style='display:flex;flex-direction:column;gap:8px'>",
                link_html("data/services/status"),
                link_html("data/weather/index"),
                link_html("data/users/index"),
                link_html("data/logs/latest"),
                "</div></div>",
            ]
        ),
        links=json.dumps(
            [
                {"rel": "services", "path": "data/services/status"},
                {"rel": "weather", "path": "data/weather/index"},
                {"rel": "users", "path": "data/users/index"},
                {"rel": "logs", "path": "data/logs/latest"},
            ]
        ),
    )


def write_services():
    rows = []
    links = [{"rel": "home", "path": "data/index"}]

    for key, name in SERVICES:
        up = random.random() > 0.15
        latency = random.randint(3, 220) if up else 0
        color = "#22c55e" if up else "#ef4444"
        status = "UP" if up else "DOWN"

        gfs.put(
            f"data/services/{key}",
            service=name,
            status=status,
            latency=latency,
            html="".join(
                [
                    "<div style='padding:16px;font-family:sans-serif;",
                    "background:#0f172a;border-radius:8px'>",
                    f"<div style='font-size:12px;color:#64748b'>{esc(key)}</div>",
                    f"<h3 style='margin:4px 0 10px 0;color:#f8fafc'>{esc(name)}</h3>",
                    f"<div style='font-size:36px;font-weight:700;color:{color}'>{status}</div>",
                    f"<div style='margin-top:8px;color:#94a3b8'>{latency} ms</div>",
                    "</div>",
                ]
            ),
            links=json.dumps(
                [
                    {"rel": "parent", "path": "data/services/status"},
                    {"rel": "home", "path": "data/index"},
                ]
            ),
        )

        rows.append(
            "".join(
                [
                    "<div style='display:flex;justify-content:space-between;",
                    "padding:8px 12px;background:#1e293b;border-radius:4px;",
                    f"border-left:3px solid {color}'>",
                    f"<span>{esc(name)}</span>",
                    f"<span style='color:{color}'>{status} ",
                    f"<span style='color:#64748b;font-size:11px'>{latency}ms</span></span>",
                    "</div>",
                ]
            )
        )
        links.append({"rel": key, "path": f"data/services/{key}"})

    gfs.put(
        "data/services/status",
        html="".join(
            [
                "<div style='padding:20px;font-family:sans-serif'>",
                "<h2 style='color:#38bdf8;margin:0 0 12px 0'>Service Status</h2>",
                "<div style='display:flex;flex-direction:column;gap:8px'>",
                "".join(rows),
                "</div>",
                "<div style='margin-top:16px;display:flex;gap:12px;flex-wrap:wrap'>",
                "".join(link_html(f"data/services/{key}", name) for key, name in SERVICES),
                "</div>",
                "</div>",
            ]
        ),
        links=json.dumps(links),
    )


def write_weather():
    weather_links = [{"rel": "home", "path": "data/index"}]
    cards = []

    for slug, city, region, lat, lng in WEATHER:
        cond, cond_color = random.choice(WEATHER_CONDS)
        temp = random.randint(45, 96)

        gfs.put(
            f"data/weather/{slug}",
            city=city,
            region=region,
            lat=lat,
            lng=lng,
            temp=temp,
            cond=cond,
            html="".join(
                [
                    "<div style='padding:16px;font-family:sans-serif;",
                    "background:linear-gradient(135deg,#0f172a,#1e293b);border-radius:8px'>",
                    f"<div style='font-size:12px;color:#64748b;text-transform:uppercase'>{esc(region)}</div>",
                    f"<h3 style='color:#f8fafc;margin:4px 0 12px 0;font-size:20px'>{esc(city)}</h3>",
                    f"<div style='font-size:48px;font-weight:bold;color:#f8fafc'>{temp}F</div>",
                    f"<div style='color:{cond_color};font-size:16px;margin-top:4px'>{esc(cond)}</div>",
                    f"<div style='color:#475569;font-size:11px;margin-top:12px'>{lat}, {lng}</div>",
                    "</div>",
                ]
            ),
            links=json.dumps(
                [
                    {"rel": "parent", "path": "data/weather/index"},
                    {"rel": "home", "path": "data/index"},
                ]
            ),
        )

        weather_links.append({"rel": slug, "path": f"data/weather/{slug}"})
        cards.append(
            "".join(
                [
                    "<div style='padding:12px;border:1px solid #334155;border-radius:8px;",
                    "display:flex;justify-content:space-between;align-items:center'>",
                    f"<span>{link_html(f'data/weather/{slug}', city)}</span>",
                    f"<span>{temp}F · <span style='color:{cond_color}'>{esc(cond)}</span></span>",
                    "</div>",
                ]
            )
        )

    gfs.put(
        "data/weather/index",
        html="".join(
            [
                "<div style='padding:20px;font-family:sans-serif'>",
                "<h2 style='color:#38bdf8;margin:0 0 12px 0'>Weather</h2>",
                "<div style='display:flex;flex-direction:column;gap:8px'>",
                "".join(cards),
                "</div>",
                "</div>",
            ]
        ),
        links=json.dumps(weather_links),
    )


def write_users():
    user_links = [{"rel": "home", "path": "data/index"}]
    rows = []

    for slug, name, role in USERS:
        online = random.random() > 0.35
        color = "#22c55e" if online else "#94a3b8"
        state = "online" if online else "idle"

        gfs.put(
            f"data/users/{slug}",
            name=name,
            role=role,
            state=state,
            html="".join(
                [
                    "<div style='padding:16px;font-family:sans-serif;background:#f8fafc;",
                    "border:1px solid #e2e8f0;border-radius:8px'>",
                    f"<div style='font-size:12px;color:#64748b'>{esc(role)}</div>",
                    f"<h3 style='margin:4px 0 10px 0;color:#0f172a'>{esc(name)}</h3>",
                    f"<div style='color:{color};font-weight:600'>{state}</div>",
                    "</div>",
                ]
            ),
            links=json.dumps(
                [
                    {"rel": "parent", "path": "data/users/index"},
                    {"rel": "home", "path": "data/index"},
                ]
            ),
        )

        user_links.append({"rel": slug, "path": f"data/users/{slug}"})
        rows.append(
            "".join(
                [
                    "<div style='display:flex;justify-content:space-between;",
                    "padding:8px 12px;border-bottom:1px solid #e2e8f0'>",
                    f"<span>{link_html(f'data/users/{slug}', name)}</span>",
                    f"<span style='color:{color}'>{state}</span>",
                    "</div>",
                ]
            )
        )

    gfs.put(
        "data/users/index",
        html="".join(
            [
                "<div style='padding:20px;font-family:sans-serif;background:#fff'>",
                "<h2 style='color:#2563eb;margin:0 0 12px 0'>Users</h2>",
                "<div style='border:1px solid #e2e8f0;border-radius:8px;overflow:hidden'>",
                "".join(rows),
                "</div>",
                "</div>",
            ]
        ),
        links=json.dumps(user_links),
    )


def write_logs(tick: int):
    level = random.choice(["INFO", "WARN", "ERROR"])
    color = {"INFO": "#38bdf8", "WARN": "#f59e0b", "ERROR": "#ef4444"}[level]
    line = f"[{tick:04d}] {level} replicated update across scene graph"

    gfs.put(
        "data/logs/latest",
        tick=tick,
        level=level,
        line=line,
        html="".join(
            [
                "<div style='padding:16px;font-family:monospace;background:#020617;",
                "color:#cbd5e1;border-radius:8px'>",
                "<div style='color:#64748b;font-size:12px;margin-bottom:8px'>latest log</div>",
                f"<div><span style='color:{color}'>{level}</span> {esc(line)}</div>",
                "</div>",
            ]
        ),
        links=json.dumps(
            [
                {"rel": "home", "path": "data/index"},
            ]
        ),
    )


def seed_static():
    write_root_index()
    write_services()
    write_weather()
    write_users()
    write_logs(0)
    gfs.rebuild_dirs()


seed_static()

tick = 0
while True:
    tick += 1
    write_services()
    write_weather()
    write_users()
    write_logs(tick)
    gfs.rebuild_dirs()
    time.sleep(2.0)