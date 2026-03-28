#!/usr/bin/env python3
"""
New-shape hypergraph feed.

Writes nodes like:
    demo3.index
    demo3.services.status
    demo3.services.api
    demo3.weather.index
    demo3.weather.nyc
    demo3.weather.nyc.panel
    demo3.users.alice
    demo3.logs.latest

Each state node uses:
    .write(data={...})

Each render node uses:
    .write(html="...")

So:
    /demo3.weather.nyc
returns the state node

and:
    /demo3.weather.nyc.panel.stream
renders the live panel bound to the nearest parent data node.
"""

import argparse
import json
import random
import time

from HyperCoreSDK.client import HyperClient

p = argparse.ArgumentParser()
p.add_argument("--discovery", default="local")
p.add_argument("--port", type=int, default=8766)
p.add_argument("--bucket", default="demo3")
a = p.parse_args()

hc = HyperClient(root=a.bucket, discovery=a.discovery, port=a.port)
hc.connect()

SERVICES = [
    ("api", "API Gateway"),
    ("db", "Database"),
    ("cache", "Redis Cache"),
    ("queue", "Message Queue"),
    ("storage", "Object Storage"),
]

CITIES = [
    ("nyc", "New York", 40.71, -74.01, "north_america"),
    ("la", "Los Angeles", 34.05, -118.24, "north_america"),
    ("london", "London", 51.51, -0.13, "europe"),
    ("paris", "Paris", 48.86, 2.35, "europe"),
    ("tokyo", "Tokyo", 35.68, 139.69, "asia"),
    ("mumbai", "Mumbai", 19.08, 72.88, "asia"),
    ("sydney", "Sydney", -33.87, 151.21, "oceania"),
]

CONDS = ["Sunny", "Rain", "Cloudy", "Partly Cloudy", "Cold", "Storms"]
COND_COLORS = {
    "Sunny": "#fbbf24",
    "Rain": "#60a5fa",
    "Cloudy": "#94a3b8",
    "Partly Cloudy": "#fcd34d",
    "Cold": "#93c5fd",
    "Storms": "#f87171",
}

USERS = [
    ("alice", "Alice Chen", "Engineering", "Senior SRE"),
    ("bob", "Bob Martinez", "Product", "PM Lead"),
    ("carol", "Carol Okafor", "Design", "UX Director"),
]

LOG_MSGS = [
    ("INFO", "Request processed", "#22c55e"),
    ("INFO", "Cache hit ratio: 94%", "#22c55e"),
    ("WARN", "Slow query detected (340ms)", "#fbbf24"),
    ("ERROR", "Connection pool exhausted", "#ef4444"),
    ("INFO", "Deployment v2.4.1 complete", "#22c55e"),
    ("WARN", "Memory usage above 80%", "#fbbf24"),
    ("INFO", "Health check passed", "#22c55e"),
    ("ERROR", "SSL certificate expiring", "#ef4444"),
    ("INFO", "Backup completed", "#22c55e"),
    ("WARN", "Rate limit approaching", "#fbbf24"),
]

log_buffer = []


def write_index():
    hc.at("index").write(
        data={
            "title": "Hypergraph",
            "sections": ["services.status", "weather.index", "users.index", "logs.latest"],
        },
        html="""
<div style="padding:20px;font-family:sans-serif">
  <h2 style="color:#38bdf8;margin:0 0 12px 0">Hypergraph</h2>
  <p style="color:#94a3b8;margin:0 0 16px 0">
    Every path is a node. Some nodes hold state, some render state.
  </p>
  <div style="display:flex;flex-direction:column;gap:8px">
    <a href="/demo3.services.status" style="color:#38bdf8">services.status</a>
    <a href="/demo3.weather.index" style="color:#38bdf8">weather.index</a>
    <a href="/demo3.users.index" style="color:#38bdf8">users.index</a>
    <a href="/demo3.logs.latest" style="color:#38bdf8">logs.latest</a>
  </div>
</div>
""",
        links=json.dumps(
            [
                {"rel": "services", "path": "services.status"},
                {"rel": "weather", "path": "weather.index"},
                {"rel": "users", "path": "users.index"},
                {"rel": "logs", "path": "logs.latest"},
            ]
        ),
    )


def write_services():
    rows = []
    links = []

    for key, name in SERVICES:
        up = random.random() > 0.15
        latency = random.randint(1, 200) if up else 0
        color = "#22c55e" if up else "#ef4444"
        status = "UP" if up else "DOWN"
        uptime = round(random.uniform(95, 99.99), 2)

        hc.at(f"services.{key}").write(
            data={
                "key": key,
                "name": name,
                "status": status,
                "latency": latency,
                "uptime": uptime,
                "color": color,
            },
            html="""
<div style="padding:16px;font-family:sans-serif">
  <h3 style="color:#e2e8f0;margin:0 0 8px 0" data-bind-text="name"></h3>
  <div style="font-size:24px;font-weight:bold;margin:0 0 8px 0" data-bind-text="status"></div>
  <div style="color:#64748b">Latency: <span data-bind-text="latency"></span>ms</div>
  <div style="color:#64748b">Uptime: <span data-bind-text="uptime"></span>%</div>
</div>
""",
            links=json.dumps([{"rel": "parent", "path": "services.status"}]),
        )

        rows.append(
            "".join(
                [
                    "<div style='display:flex;justify-content:space-between;",
                    "padding:8px 12px;background:#1e293b;border-radius:4px;",
                    "border-left:3px solid ",
                    color,
                    "'>",
                    "<span>",
                    name,
                    "</span>",
                    "<span style='color:",
                    color,
                    "'>",
                    status,
                    " <span style='color:#64748b;font-size:11px'>",
                    str(latency),
                    "ms</span></span></div>",
                ]
            )
        )
        links.append({"rel": "service", "path": f"services.{key}"})

    hc.at("services.status").write(
        data={"count": len(SERVICES)},
        html="""
<div style="padding:16px;font-family:sans-serif">
  <h2 style="color:#e2e8f0;margin:0 0 16px 0">Service Status</h2>
  <div style="display:flex;flex-direction:column;gap:6px">
    %s
  </div>
</div>
""" % "".join(rows),
        links=json.dumps(links + [{"rel": "parent", "path": "index"}]),
    )


def write_weather():
    cards = []
    city_links = []

    for key, city, lat, lng, region in CITIES:
        temp = random.randint(30, 100)
        cond = random.choice(CONDS)
        cc = COND_COLORS.get(cond, "#94a3b8")

        hc.at(f"weather.{key}").write(
            data={
                "key": key,
                "city": city,
                "lat": lat,
                "lng": lng,
                "region": region,
                "temp": temp,
                "cond": cond,
                "color": cc,
            },
            links=json.dumps([{"rel": "parent", "path": "weather.index"}]),
        )

        hc.at(f"weather.{key}.panel").write(
            html="""
<div style="padding:16px;font-family:sans-serif;background:linear-gradient(135deg,#0f172a,#1e293b);border-radius:8px">
  <div style="font-size:12px;color:#64748b;text-transform:uppercase" data-bind-text="region"></div>
  <h3 style="color:#f8fafc;margin:4px 0 12px 0;font-size:20px" data-bind-text="city"></h3>
  <div style="font-size:48px;font-weight:bold;color:#f8fafc">
    <span data-bind-text="temp"></span>F
  </div>
  <div style="font-size:16px;margin-top:4px" data-bind-text="cond"></div>
  <div style="color:#475569;font-size:11px;margin-top:12px">
    <span data-bind-text="lat"></span>, <span data-bind-text="lng"></span>
  </div>
</div>
""",
            links=json.dumps([{"rel": "state", "path": f"weather.{key}"}]),
        )

        cards.append(
            "".join(
                [
                    "<div style='padding:8px 12px;background:#1e293b;border-radius:4px;",
                    "display:flex;justify-content:space-between'>",
                    "<span style='color:#e2e8f0'>",
                    city,
                    "</span>",
                    "<span style='color:#64748b'>weather.",
                    key,
                    "</span></div>",
                ]
            )
        )
        city_links.append({"rel": "city", "path": f"weather.{key}"})

    hc.at("weather.index").write(
        data={"count": len(CITIES)},
        html="""
<div style="padding:16px;font-family:sans-serif">
  <h2 style="color:#e2e8f0;margin:0 0 16px 0">Weather Stations</h2>
  <div style="display:flex;flex-direction:column;gap:6px">
    %s
  </div>
</div>
""" % "".join(cards),
        links=json.dumps(city_links + [{"rel": "parent", "path": "index"}]),
    )


def write_users():
    rows = []
    user_links = []

    for key, name, dept, role in USERS:
        online = random.random() > 0.3
        dot = "#22c55e" if online else "#64748b"

        hc.at(f"users.{key}").write(
            data={
                "key": key,
                "name": name,
                "dept": dept,
                "role": role,
                "online": online,
                "dot": dot,
            },
            html="""
<div style="padding:20px;font-family:sans-serif">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
    <div style="width:48px;height:48px;border-radius:50%;background:#334155;display:flex;align-items:center;justify-content:center;font-size:20px;color:#e2e8f0">
      ?
    </div>
    <div>
      <div style="color:#f8fafc;font-size:16px;font-weight:bold" data-bind-text="name"></div>
      <div style="color:#64748b;font-size:13px">
        <span data-bind-text="role"></span> - <span data-bind-text="dept"></span>
      </div>
    </div>
  </div>
  <div style="display:flex;gap:8px;align-items:center">
    <div style="width:8px;height:8px;border-radius:50%;background:#64748b"></div>
    <span style="color:#94a3b8;font-size:13px" data-bind-text="online"></span>
  </div>
</div>
""",
            links=json.dumps([{"rel": "parent", "path": "users.index"}]),
        )

        rows.append(
            "".join(
                [
                    "<div style='padding:8px 12px;background:#1e293b;border-radius:4px;",
                    "display:flex;justify-content:space-between'>",
                    "<span style='color:#e2e8f0'>",
                    name,
                    "</span>",
                    "<span style='color:#64748b'>",
                    dept,
                    "</span></div>",
                ]
            )
        )
        user_links.append({"rel": "user", "path": f"users.{key}"})

    hc.at("users.index").write(
        data={"count": len(USERS)},
        html="""
<div style="padding:16px;font-family:sans-serif">
  <h2 style="color:#e2e8f0;margin:0 0 16px 0">Team</h2>
  <div style="display:flex;flex-direction:column;gap:6px">
    %s
  </div>
</div>
""" % "".join(rows),
        links=json.dumps(user_links + [{"rel": "parent", "path": "index"}]),
    )


def write_logs():
    level, msg, color = random.choice(LOG_MSGS)
    ts = time.strftime("%H:%M:%S")
    log_buffer.append((ts, level, msg, color))
    if len(log_buffer) > 12:
        log_buffer.pop(0)

    rows = []
    for ts, lvl, m, c in reversed(log_buffer):
        rows.append(
            "".join(
                [
                    "<div style='display:flex;gap:12px;padding:4px 0;",
                    "border-bottom:1px solid #1e293b;font-size:13px'>",
                    "<span style='color:#475569;min-width:60px'>",
                    ts,
                    "</span>",
                    "<span style='color:",
                    c,
                    ";min-width:44px;font-weight:bold'>",
                    lvl,
                    "</span>",
                    "<span style='color:#cbd5e1'>",
                    m,
                    "</span></div>",
                ]
            )
        )

    hc.at("logs.latest").write(
        data={"count": len(log_buffer)},
        html="""
<div style="padding:16px;font-family:monospace">
  <h3 style="color:#e2e8f0;margin:0 0 12px 0;font-family:sans-serif">Live Logs</h3>
  <div>%s</div>
</div>
""" % "".join(rows),
        links=json.dumps([{"rel": "parent", "path": "index"}]),
    )


print("Writing hypergraph nodes to:", a.bucket)
print("Try:")
print("  ", f"http://127.0.0.1:{a.port}/{a.bucket}.weather.nyc")
print("  ", f"http://127.0.0.1:{a.port}/{a.bucket}.weather.nyc.panel.stream")
print("  ", f"http://127.0.0.1:{a.port}/{a.bucket}.weather.nyc.events")

cycle = 0
while True:
    write_index()
    write_services()
    write_weather()
    write_users()
    write_logs()

    cycle += 1
    snap = hc.snapshot() or {}
    print("cycle", cycle, "| nodes:" , len(snap))
