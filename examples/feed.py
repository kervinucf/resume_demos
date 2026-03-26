#!/usr/bin/env python3
"""
Hypermedia feed - writes HTML fragments, linked paths, and nested
structure to a shared bucket. Demonstrates HATEOAS: every node is
a renderable fragment with links to related nodes.

    python feed.py
    python feed.py --discovery lan --port 8766

Then open the explorer pointed at the same bucket to browse, render,
and edit every fragment in the graph.
"""

import argparse, time, random, json
from HyperCoreSDK.client import HyperClient

p = argparse.ArgumentParser()
p.add_argument("--discovery", default="local")
p.add_argument("--port", type=int, default=8766)
p.add_argument("--bucket", default="demo2")
a = p.parse_args()

hc = HyperClient(root=a.bucket, discovery=a.discovery, port=a.port)
hc.connect()

# -- index: root directory with links to all sections --

hc.write("data/index",
    html="".join([
        "<div style='padding:20px;font-family:sans-serif'>",
        "<h2 style='color:#38bdf8;margin:0 0 12px 0'>Hypermedia Graph</h2>",
        "<p style='color:#94a3b8;margin:0 0 16px 0'>Each node is a renderable fragment. Follow the links.</p>",
        "<div style='display:flex;flex-direction:column;gap:8px'>",
        "<a href='#' style='color:#38bdf8'>data/services/status</a>",
        "<a href='#' style='color:#38bdf8'>data/weather/index</a>",
        "<a href='#' style='color:#38bdf8'>data/users/index</a>",
        "<a href='#' style='color:#38bdf8'>data/logs/latest</a>",
        "</div></div>",
    ]),
    links=json.dumps([
        {"rel": "services", "path": "data/services/status"},
        {"rel": "weather",  "path": "data/weather/index"},
        {"rel": "users",    "path": "data/users/index"},
        {"rel": "logs",     "path": "data/logs/latest"},
    ]),
)

# -- services: status dashboard fragment --

SERVICES = [
    ("api",     "API Gateway"),
    ("db",      "Database"),
    ("cache",   "Redis Cache"),
    ("queue",   "Message Queue"),
    ("storage", "Object Storage"),
]

def write_services():
    rows = ""
    for key, name in SERVICES:
        up = random.random() > 0.15
        latency = random.randint(1, 200) if up else 0
        color = "#22c55e" if up else "#ef4444"
        status = "UP" if up else "DOWN"

        rows += "".join([
            "<div style='display:flex;justify-content:space-between;",
            "padding:8px 12px;background:#1e293b;border-radius:4px;",
            "border-left:3px solid ", color, "'>",
            "<span>", name, "</span>",
            "<span style='color:", color, "'>", status,
            " <span style='color:#64748b;font-size:11px'>",
            str(latency), "ms</span></span></div>",
        ])

        hc.write("data/services/" + key,
            html="".join([
                "<div style='padding:16px;font-family:sans-serif'>",
                "<h3 style='color:#e2e8f0;margin:0 0 8px 0'>", name, "</h3>",
                "<div style='color:", color, ";font-size:24px;font-weight:bold;margin:0 0 8px 0'>",
                status, "</div>",
                "<div style='color:#64748b'>Latency: ", str(latency), "ms</div>",
                "<div style='color:#64748b'>Uptime: ",
                str(random.uniform(95, 99.99)) [:5], "%</div></div>",
            ]),
            status=status,
            latency=latency,
            links=json.dumps([{"rel": "parent", "path": "data/services/status"}]),
        )

    hc.write("data/services/status",
        html="".join([
            "<div style='padding:16px;font-family:sans-serif'>",
            "<h2 style='color:#e2e8f0;margin:0 0 16px 0'>Service Status</h2>",
            "<div style='display:flex;flex-direction:column;gap:6px'>",
            rows, "</div></div>",
        ]),
        links=json.dumps([
            {"rel": "service", "path": "data/services/" + k} for k, _ in SERVICES
        ] + [{"rel": "parent", "path": "data/index"}]),
    )


# -- weather: city fragments with region grouping --

CITIES = [
    ("nyc",     "New York",     40.71,  -74.01, "north_america"),
    ("la",      "Los Angeles",  34.05, -118.24, "north_america"),
    ("london",  "London",       51.51,   -0.13, "europe"),
    ("paris",   "Paris",        48.86,    2.35, "europe"),
    ("tokyo",   "Tokyo",        35.68,  139.69, "asia"),
    ("mumbai",  "Mumbai",       19.08,   72.88, "asia"),
    ("sydney",  "Sydney",      -33.87,  151.21, "oceania"),
]

CONDS = ["Sunny", "Rain", "Cloudy", "Partly Cloudy", "Cold", "Storms"]
COND_COLORS = {
    "Sunny": "#fbbf24", "Rain": "#60a5fa", "Cloudy": "#94a3b8",
    "Partly Cloudy": "#fcd34d", "Cold": "#93c5fd", "Storms": "#f87171",
}

def write_weather():
    city_links = []
    for key, city, lat, lng, region in CITIES:
        temp = random.randint(30, 100)
        cond = random.choice(CONDS)
        cc = COND_COLORS.get(cond, "#94a3b8")

        hc.write("data/weather/" + key,
            html="".join([
                "<div style='padding:16px;font-family:sans-serif;",
                "background:linear-gradient(135deg,#0f172a,#1e293b);border-radius:8px'>",
                "<div style='font-size:12px;color:#64748b;text-transform:uppercase'>",
                region.replace("_", " "), "</div>",
                "<h3 style='color:#f8fafc;margin:4px 0 12px 0;font-size:20px'>", city, "</h3>",
                "<div style='font-size:48px;font-weight:bold;color:#f8fafc'>",
                str(temp), "F</div>",
                "<div style='color:", cc, ";font-size:16px;margin-top:4px'>", cond, "</div>",
                "<div style='color:#475569;font-size:11px;margin-top:12px'>",
                str(lat), ", ", str(lng), "</div></div>",
            ]),
            city=city, lat=lat, lng=lng, region=region,
            temp=temp, cond=cond,
            links=json.dumps([{"rel": "parent", "path": "data/weather/index"}]),
        )
        city_links.append({"rel": "city", "path": "data/weather/" + key})

    cards = ""
    for key, city, _, _, _ in CITIES:
        cards += "".join([
            "<div style='padding:8px 12px;background:#1e293b;border-radius:4px;",
            "display:flex;justify-content:space-between'>",
            "<span style='color:#e2e8f0'>", city, "</span>",
            "<span style='color:#64748b'>data/weather/", key, "</span></div>",
        ])

    hc.write("data/weather/index",
        html="".join([
            "<div style='padding:16px;font-family:sans-serif'>",
            "<h2 style='color:#e2e8f0;margin:0 0 16px 0'>Weather Stations</h2>",
            "<div style='display:flex;flex-direction:column;gap:6px'>",
            cards, "</div></div>",
        ]),
        links=json.dumps(city_links + [{"rel": "parent", "path": "data/index"}]),
    )


# -- users: profile card fragments --

USERS = [
    ("alice",   "Alice Chen",    "Engineering",  "Senior SRE"),
    ("bob",     "Bob Martinez",  "Product",      "PM Lead"),
    ("carol",   "Carol Okafor",  "Design",       "UX Director"),
]

def write_users():
    user_links = []
    for key, name, dept, role in USERS:
        online = random.random() > 0.3
        dot = "#22c55e" if online else "#64748b"

        hc.write("data/users/" + key,
            html="".join([
                "<div style='padding:20px;font-family:sans-serif'>",
                "<div style='display:flex;align-items:center;gap:12px;margin-bottom:16px'>",
                "<div style='width:48px;height:48px;border-radius:50%;",
                "background:#334155;display:flex;align-items:center;justify-content:center;",
                "font-size:20px;color:#e2e8f0'>", name[0], "</div>",
                "<div><div style='color:#f8fafc;font-size:16px;font-weight:bold'>",
                name, "</div>",
                "<div style='color:#64748b;font-size:13px'>", role, " - ", dept, "</div>",
                "</div></div>",
                "<div style='display:flex;gap:8px;align-items:center'>",
                "<div style='width:8px;height:8px;border-radius:50%;background:", dot, "'></div>",
                "<span style='color:", dot, ";font-size:13px'>",
                "Online" if online else "Offline", "</span></div></div>",
            ]),
            name=name, dept=dept, role=role,
            online="true" if online else "false",
            links=json.dumps([{"rel": "parent", "path": "data/users/index"}]),
        )
        user_links.append({"rel": "user", "path": "data/users/" + key})

    rows = ""
    for key, name, dept, role in USERS:
        rows += "".join([
            "<div style='padding:8px 12px;background:#1e293b;border-radius:4px;",
            "display:flex;justify-content:space-between'>",
            "<span style='color:#e2e8f0'>", name, "</span>",
            "<span style='color:#64748b'>", dept, "</span></div>",
        ])

    hc.write("data/users/index",
        html="".join([
            "<div style='padding:16px;font-family:sans-serif'>",
            "<h2 style='color:#e2e8f0;margin:0 0 16px 0'>Team</h2>",
            "<div style='display:flex;flex-direction:column;gap:6px'>",
            rows, "</div></div>",
        ]),
        links=json.dumps(user_links + [{"rel": "parent", "path": "data/index"}]),
    )


# -- logs: rolling log fragment --

LOG_MSGS = [
    ("INFO",  "Request processed",           "#22c55e"),
    ("INFO",  "Cache hit ratio: 94%",         "#22c55e"),
    ("WARN",  "Slow query detected (340ms)",  "#fbbf24"),
    ("ERROR", "Connection pool exhausted",    "#ef4444"),
    ("INFO",  "Deployment v2.4.1 complete",   "#22c55e"),
    ("WARN",  "Memory usage above 80%",       "#fbbf24"),
    ("INFO",  "Health check passed",          "#22c55e"),
    ("ERROR", "SSL certificate expiring",     "#ef4444"),
    ("INFO",  "Backup completed",             "#22c55e"),
    ("WARN",  "Rate limit approaching",       "#fbbf24"),
]

log_buffer = []

def write_logs():
    level, msg, color = random.choice(LOG_MSGS)
    ts = time.strftime("%H:%M:%S")
    entry = (ts, level, msg, color)
    log_buffer.append(entry)
    if len(log_buffer) > 12:
        log_buffer.pop(0)

    rows = ""
    for ts, lvl, m, c in reversed(log_buffer):
        rows += "".join([
            "<div style='display:flex;gap:12px;padding:4px 0;",
            "border-bottom:1px solid #1e293b;font-size:13px'>",
            "<span style='color:#475569;min-width:60px'>", ts, "</span>",
            "<span style='color:", c, ";min-width:44px;font-weight:bold'>", lvl, "</span>",
            "<span style='color:#cbd5e1'>", m, "</span></div>",
        ])

    hc.write("data/logs/latest",
        html="".join([
            "<div style='padding:16px;font-family:monospace'>",
            "<h3 style='color:#e2e8f0;margin:0 0 12px 0;font-family:sans-serif'>Live Logs</h3>",
            "<div>", rows, "</div></div>",
        ]),
        links=json.dumps([{"rel": "parent", "path": "data/index"}]),
    )


# -- main loop --

print("Writing hypermedia fragments to:", a.bucket)
print("Open explorer pointed at this bucket to browse.")

cycle = 0
while True:
    write_services()
    write_weather()
    write_users()
    write_logs()

    cycle += 1
    print("cycle", cycle, "| nodes:",
          len([k for k in (hc.snapshot() or {}) if k.startswith("data/")]))
