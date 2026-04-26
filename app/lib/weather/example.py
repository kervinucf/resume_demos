#!/usr/bin/env python3
"""
weather_cadence_4545.py

A first graph-native dynamic weather segment.

This is intentionally not a hardcoded scene script.

It is a tiny hypermedia frontend service that:
  - queries the hypergraph relay for weather_latest entities
  - infers the available view dimensions from the data itself
  - computes a dynamic broadcast frame for the current cadence tick
  - serves ticker, chyron, rundown, and weather-card HTML at :4545
  - rotates through computed frames: overview, condition bands, country bands,
    warmest, coolest, and data-quality frames when relevant

The point:
  The view is computed from graph state + intent, rather than authored as
  "show this, then show that."

Run:
  python weather_cadence_4545.py

Open:
  http://127.0.0.1:4545

Requires:
  Hypergraph relay running at http://127.0.0.1:8765
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


# =============================================================================
# Configuration
# =============================================================================

PORT = int(os.getenv("WEATHER_SEGMENT_PORT", "4545"))
HYPER_URL = os.getenv("HYPER_URL", "http://127.0.0.1:8765").rstrip("/")
CADENCE_SECONDS = float(os.getenv("WEATHER_CADENCE_SECONDS", "8"))
QUERY_LIMIT = int(os.getenv("WEATHER_QUERY_LIMIT", "250"))

# This is the "intent" layer. Later, a generative model can author or revise this.
# For now it is a graph-computer hint: what should the view try to surface?
BROADCAST_INTENT = {
    "kind": "broadcast_intent",
    "domain": "weather",
    "goal": "surface current weather patterns as rotating broadcast views",
    "preferred_dimensions": ["condition", "country_code", "temperature"],
    "view_contract": ["ticker", "chyron", "rundown", "cards"],
    "cadence_seconds": CADENCE_SECONDS,
    "style": "broadcast_weather_terminal",
}


Json = dict[str, Any]


# =============================================================================
# Hypergraph client, intentionally dumb
# =============================================================================

class HyperRelay:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def get_json(self, path_or_url: str, *, timeout: float = 10.0) -> Json:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
        elif path_or_url.startswith("/"):
            url = f"{self.base_url}{path_or_url}"
        else:
            url = f"{self.base_url}/{path_or_url.lstrip('/')}"

        req = urllib.request.Request(url, headers={"Accept": "application/json"})

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw or "{}")

    def query_weather_latest(self, *, limit: int = QUERY_LIMIT) -> list[Json]:
        params = urllib.parse.urlencode({
            "type": "weather_latest",
            "include": "facets,refs,numbers,times,cells",
            "limit": str(limit),
            "sort": "updated_desc",
        })
        doc = self.get_json(f"/api/query/entities?{params}")
        return list(doc.get("_embedded", {}).values()) or [
            {"data": item}
            for item in ((doc.get("data") or {}).get("items") or doc.get("items") or [])
        ]


# =============================================================================
# Data normalization
# =============================================================================

@dataclass
class WeatherRow:
    id: str
    display: str
    canonical_path: str
    country_code: str
    condition: str
    condition_family: str
    temperature_c: float | None
    lat: float | None
    lon: float | None
    observed_at_ms: int | None
    location_ref: str | None
    target_ref: str | None
    raw: Json


def slug(value: Any, fallback: str = "item") -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "").strip().lower())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or fallback


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        if math.isfinite(out):
            return out
    except Exception:
        pass
    return None


def safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


def rows_to_map(rows: Any, *, key: str = "name", value: str = "value") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(rows, list):
        return out

    for row in rows:
        if not isinstance(row, dict):
            continue
        k = row.get(key)
        if k is None:
            continue
        out[str(k)] = row.get(value)

    return out


def refs_to_map(rows: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(rows, list):
        return out

    for row in rows:
        if not isinstance(row, dict):
            continue
        rel = row.get("rel")
        target = row.get("target_id")
        if rel and target:
            out[str(rel)] = str(target)

    return out


def extract_item_data(wrapper: Json) -> Json:
    """
    Query endpoint embeds items as:
      _embedded[id].data = query result item

    Raw /query/entities can expose:
      items = [query result item]

    This function accepts either.
    """
    if "data" in wrapper and isinstance(wrapper["data"], dict):
        data = wrapper["data"]
        if "entity_id" in data or "canonical_path" in data:
            return data

    return wrapper


def condition_family(condition: str) -> str:
    text = str(condition or "").lower()

    if any(x in text for x in ["thunder", "storm"]):
        return "storms"
    if any(x in text for x in ["rain", "drizzle", "shower", "precip"]):
        return "rain"
    if "snow" in text or "sleet" in text or "ice" in text:
        return "winter"
    if "fog" in text or "mist" in text or "haze" in text:
        return "low visibility"
    if "cloud" in text or "overcast" in text:
        return "cloud cover"
    if "clear" in text or "sun" in text:
        return "clear skies"
    if not text.strip():
        return "unknown"
    return text.split()[0][:24]


def weather_emoji(condition: str) -> str:
    fam = condition_family(condition)
    return {
        "storms": "⛈️",
        "rain": "🌧️",
        "winter": "❄️",
        "low visibility": "🌫️",
        "cloud cover": "☁️",
        "clear skies": "☀️",
        "unknown": "🌐",
    }.get(fam, "🌤️")


def c_to_f(value: float | None) -> float | None:
    if value is None:
        return None
    return value * 9 / 5 + 32


def temp_label(c: float | None) -> str:
    if c is None:
        return "--"
    f = c_to_f(c)
    assert f is not None
    return f"{f:.0f}°F / {c:.0f}°C"


def pick_number(numbers: dict[str, Any], candidates: list[str]) -> float | None:
    for key in candidates:
        if key in numbers:
            val = safe_float(numbers.get(key))
            if val is not None:
                return val

    # final fallback: suffix match
    for key, value in numbers.items():
        key_l = key.lower()
        if any(key_l.endswith("." + cand) or key_l == cand for cand in candidates):
            val = safe_float(value)
            if val is not None:
                return val

    return None


def normalize_weather(wrapper: Json) -> WeatherRow:
    item = extract_item_data(wrapper)

    facets = rows_to_map(item.get("facets"))
    numbers = rows_to_map(item.get("numbers"))
    times = rows_to_map(item.get("times"), value="value_ms")
    refs = refs_to_map(item.get("refs"))

    entity_id = str(item.get("entity_id") or item.get("canonical_path") or "")
    canonical_path = str(item.get("canonical_path") or entity_id)
    display = str(item.get("display") or entity_id.rsplit(".", 1)[-1] or "Weather")

    condition = str(
        facets.get("condition")
        or facets.get("data.condition")
        or facets.get("body.condition")
        or "Current conditions"
    )

    country_code = str(
        facets.get("country_code")
        or facets.get("data.country_code")
        or facets.get("body.country_code")
        or ""
    ).upper()

    temperature_c = pick_number(numbers, [
        "temperature",
        "temperature_c",
        "temp",
        "body.temperature",
        "data.temperature",
        "latest.temperature",
    ])

    lat = pick_number(numbers, ["lat", "latitude", "body.lat", "data.lat"])
    lon = pick_number(numbers, ["lon", "lng", "longitude", "body.lon", "data.lon"])

    observed_at_ms = safe_int(
        times.get("observed_at")
        or times.get("updated_at")
        or times.get("weather_latest_at")
        or times.get("activity_latest_at")
    )

    return WeatherRow(
        id=slug(entity_id or display),
        display=display,
        canonical_path=canonical_path,
        country_code=country_code,
        condition=condition,
        condition_family=condition_family(condition),
        temperature_c=temperature_c,
        lat=lat,
        lon=lon,
        observed_at_ms=observed_at_ms,
        location_ref=refs.get("location"),
        target_ref=refs.get("target"),
        raw=item,
    )


# =============================================================================
# Frame inference: computed view, not a hardcoded program
# =============================================================================

@dataclass
class Frame:
    id: str
    title: str
    subtitle: str
    reason: str
    basis: str
    rows: list[WeatherRow]


def data_capabilities(rows: list[WeatherRow]) -> dict[str, Any]:
    return {
        "has_temperature": any(r.temperature_c is not None for r in rows),
        "has_condition": any(r.condition_family != "unknown" for r in rows),
        "has_country": any(bool(r.country_code) for r in rows),
        "has_geo": any(r.lat is not None and r.lon is not None for r in rows),
        "condition_families": Counter(r.condition_family for r in rows if r.condition_family),
        "countries": Counter(r.country_code for r in rows if r.country_code),
        "count": len(rows),
    }


def select_diverse(rows: list[WeatherRow], *, limit: int = 8) -> list[WeatherRow]:
    """
    Pick a mixed set without assuming a pre-authored scene order.
    """
    out: list[WeatherRow] = []
    seen_countries: set[str] = set()
    seen_conditions: set[str] = set()

    for row in sorted(rows, key=lambda r: (r.country_code, r.condition_family, r.display)):
        score_new = (
            (row.country_code not in seen_countries)
            + (row.condition_family not in seen_conditions)
        )
        if score_new:
            out.append(row)
            seen_countries.add(row.country_code)
            seen_conditions.add(row.condition_family)

        if len(out) >= limit:
            return out

    for row in rows:
        if row not in out:
            out.append(row)
        if len(out) >= limit:
            break

    return out


def infer_frames_from_intent(intent: Json, rows: list[WeatherRow]) -> list[Frame]:
    """
    This is the replaceable intelligence slot.

    Today it is deterministic and transparent.
    Later this can become:
      graph state + intent -> LLM -> frame candidates

    Contract:
      It returns candidate frames based on available graph affordances.
    """
    if not rows:
        return [
            Frame(
                id="empty",
                title="Weather Feed Offline",
                subtitle="No weather_latest entities found in the hypergraph",
                reason="The graph query returned zero weather_latest entities.",
                basis="empty",
                rows=[],
            )
        ]

    caps = data_capabilities(rows)
    frames: list[Frame] = []

    frames.append(Frame(
        id="overview",
        title="Weather Overview",
        subtitle=f"{len(rows)} live observations discovered",
        reason="The graph exposed weather_latest entities; overview gives the broadest current scan.",
        basis="all",
        rows=select_diverse(rows, limit=10),
    ))

    if caps["has_condition"]:
        grouped: dict[str, list[WeatherRow]] = defaultdict(list)
        for row in rows:
            grouped[row.condition_family].append(row)

        # Computed band order: most common first, but only from live graph data.
        for family, count in caps["condition_families"].most_common(8):
            band_rows = grouped.get(family, [])
            if not band_rows:
                continue
            frames.append(Frame(
                id=f"condition-{slug(family)}",
                title=f"{family.title()} Band",
                subtitle=f"{count} observation{'s' if count != 1 else ''}",
                reason=f"The graph currently has a visible weather pattern for {family}.",
                basis=f"condition_family:{family}",
                rows=sorted(
                    band_rows,
                    key=lambda r: (
                        r.temperature_c is None,
                        -(r.temperature_c or -9999),
                        r.display,
                    ),
                )[:10],
            ))

    if caps["has_temperature"]:
        temp_rows = [r for r in rows if r.temperature_c is not None]

        frames.append(Frame(
            id="warmest",
            title="Warmest Readings",
            subtitle="Temperature-ranked broadcast page",
            reason="Temperature values are available, so the view can compute a warm band.",
            basis="temperature:desc",
            rows=sorted(temp_rows, key=lambda r: float(r.temperature_c or -9999), reverse=True)[:10],
        ))

        frames.append(Frame(
            id="coolest",
            title="Coolest Readings",
            subtitle="Temperature-ranked broadcast page",
            reason="Temperature values are available, so the view can compute a cool band.",
            basis="temperature:asc",
            rows=sorted(temp_rows, key=lambda r: float(r.temperature_c or 9999))[:10],
        ))

    if caps["has_country"]:
        grouped_country: dict[str, list[WeatherRow]] = defaultdict(list)
        for row in rows:
            if row.country_code:
                grouped_country[row.country_code].append(row)

        for country, count in caps["countries"].most_common(8):
            country_rows = grouped_country.get(country, [])
            frames.append(Frame(
                id=f"country-{slug(country)}",
                title=f"{country} Weather",
                subtitle=f"{count} indexed observation{'s' if count != 1 else ''}",
                reason=f"country_code is available as a graph facet, so this page is computed from that band.",
                basis=f"country_code:{country}",
                rows=select_diverse(country_rows, limit=10),
            ))

    missing_temp = [r for r in rows if r.temperature_c is None]
    if missing_temp and len(missing_temp) >= max(3, len(rows) // 4):
        frames.append(Frame(
            id="data-quality-temperature",
            title="Weather Metadata Watch",
            subtitle=f"{len(missing_temp)} observations need temperature projection",
            reason="The view noticed a data-quality pattern: many weather records lack numeric temperature.",
            basis="data_quality:missing_temperature",
            rows=missing_temp[:10],
        ))

    # Keep it bounded for demo cadence.
    return frames[:18]


# =============================================================================
# Presentation generation
# =============================================================================

def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def frame_chyron(frame: Frame, all_rows: list[WeatherRow]) -> str:
    if not frame.rows:
        return "WEATHER FEED OFFLINE"

    if frame.id == "warmest":
        row = frame.rows[0]
        return f"WEATHER WATCH: WARMEST {row.display.upper()} {temp_label(row.temperature_c)}"

    if frame.id == "coolest":
        row = frame.rows[0]
        return f"WEATHER WATCH: COOLEST {row.display.upper()} {temp_label(row.temperature_c)}"

    fam_counts = Counter(r.condition_family for r in frame.rows)
    top_family = fam_counts.most_common(1)[0][0] if fam_counts else "conditions"

    with_temp = [r for r in frame.rows if r.temperature_c is not None]
    if with_temp:
        warm = max(with_temp, key=lambda r: float(r.temperature_c or -9999))
        return (
            f"{frame.title.upper()}: {top_family.upper()} • "
            f"{warm.display.upper()} {temp_label(warm.temperature_c)}"
        )

    first = frame.rows[0]
    return f"{frame.title.upper()}: {first.display.upper()} • {first.condition.upper()}"


def frame_ticker(frame: Frame) -> str:
    if not frame.rows:
        return "No weather observations available from the hypergraph."

    parts = []
    for row in frame.rows:
        parts.append(
            f"{weather_emoji(row.condition)} {row.display.upper()}: "
            f"{temp_label(row.temperature_c)}, {row.condition.title()}"
        )

    return "   •••   ".join(parts)


def frame_rundown(frame: Frame, frames: list[Frame]) -> list[dict[str, Any]]:
    stories = []
    for idx, candidate in enumerate(frames, start=1):
        stories.append({
            "id": candidate.id,
            "title": candidate.title,
            "subTitle": candidate.subtitle,
            "subtitle": candidate.subtitle,
            "data": {
                "basis": candidate.basis,
                "active": candidate.id == frame.id,
                "count": len(candidate.rows),
            },
        })
    return stories


def weather_cards_html(frame: Frame) -> str:
    if not frame.rows:
        return """
        <section class="wx-grid empty">
          <article class="wx-card">
            <div class="wx-symbol">⚠️</div>
            <div class="wx-city">No Weather Data</div>
            <div class="wx-condition">The hypergraph returned no weather_latest entities.</div>
          </article>
        </section>
        """.strip()

    cards = []
    for row in frame.rows:
        cards.append(f"""
          <article class="wx-card">
            <div class="wx-symbol">{esc(weather_emoji(row.condition))}</div>
            <div class="wx-main">
              <div class="wx-city">{esc(row.display)}</div>
              <div class="wx-condition">{esc(row.condition.title())}</div>
              <div class="wx-path">{esc(row.canonical_path)}</div>
            </div>
            <div class="wx-temp">{esc(temp_label(row.temperature_c))}</div>
            <div class="wx-meta">
              <span>{esc(row.country_code or "GLOBAL")}</span>
              <span>{esc(row.condition_family)}</span>
            </div>
          </article>
        """)

    return f"""
    <section class="wx-grid">
      {''.join(cards)}
    </section>
    """.strip()


def computed_frame_payload(frame_index: int | None = None) -> Json:
    relay = HyperRelay(HYPER_URL)
    embedded = relay.query_weather_latest(limit=QUERY_LIMIT)

    rows = [normalize_weather(item) for item in embedded]
    rows = [r for r in rows if r.display]

    frames = infer_frames_from_intent(BROADCAST_INTENT, rows)

    if frame_index is None:
        frame_index = int(time.time() // CADENCE_SECONDS)

    frame = frames[frame_index % len(frames)]

    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "relay": HYPER_URL,
        "cadence_seconds": CADENCE_SECONDS,
        "frame_index": frame_index,
        "frame_count": len(frames),
        "intent": BROADCAST_INTENT,
        "capabilities": data_capabilities(rows),
        "frame": {
            "id": frame.id,
            "title": frame.title,
            "subtitle": frame.subtitle,
            "reason": frame.reason,
            "basis": frame.basis,
            "count": len(frame.rows),
        },
        "tickerText": frame_ticker(frame),
        "chyronText": frame_chyron(frame, rows),
        "rundownStories": frame_rundown(frame, frames),
        "cardsHtml": weather_cards_html(frame),
    }


# =============================================================================
# HTTP rendering
# =============================================================================

INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Hypergraph Weather Cadence</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {
      --bg: #020617;
      --panel: rgba(15, 23, 42, 0.78);
      --panel2: rgba(30, 41, 59, 0.78);
      --border: rgba(148, 163, 184, 0.22);
      --text: #f8fafc;
      --muted: #94a3b8;
      --blue: #38bdf8;
      --gold: #fbbf24;
    }

    * { box-sizing: border-box; }

    html, body {
      width: 100%;
      height: 100%;
      margin: 0;
      overflow: hidden;
      background:
        radial-gradient(circle at 20% 10%, rgba(56, 189, 248, 0.22), transparent 30%),
        radial-gradient(circle at 80% 5%, rgba(14, 165, 233, 0.18), transparent 26%),
        linear-gradient(135deg, #020617 0%, #0f172a 55%, #111827 100%);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    .shell {
      display: grid;
      grid-template-columns: 1fr 25vw;
      grid-template-rows: 1fr auto auto;
      width: 100vw;
      height: 100vh;
    }

    .main {
      grid-column: 1;
      grid-row: 1;
      min-width: 0;
      min-height: 0;
      padding: 22px;
      overflow: hidden;
    }

    .sidebar {
      grid-column: 2;
      grid-row: 1 / 4;
      border-left: 1px solid var(--border);
      background: linear-gradient(180deg, rgba(0,0,0,0.55), rgba(15,23,42,0.6));
      padding: 18px;
      overflow: hidden;
    }

    .chyron {
      grid-column: 1;
      grid-row: 2;
      min-height: 16vh;
      border-top: 1px solid rgba(255,255,255,0.16);
      background: linear-gradient(90deg, #020617 0%, #0f172a 55%, #082f49 100%);
      display: grid;
      grid-template-columns: 22% 1fr;
    }

    .ticker {
      grid-column: 1;
      grid-row: 3;
      height: 4.5vh;
      min-height: 34px;
      background: black;
      overflow: hidden;
      border-top: 1px solid rgba(255,255,255,0.08);
      position: relative;
    }

    .brand {
      display: flex;
      align-items: center;
      justify-content: center;
      border-right: 1px solid rgba(255,255,255,0.16);
      font-weight: 950;
      letter-spacing: -0.06em;
      font-size: clamp(28px, 4vw, 58px);
      color: var(--blue);
      text-shadow: 0 0 20px rgba(56,189,248,0.55);
    }

    .chyron-text {
      display: flex;
      align-items: center;
      padding: 0 34px;
      font-weight: 900;
      font-size: clamp(27px, 4.3vw, 76px);
      line-height: 1.05;
      letter-spacing: -0.055em;
      text-transform: uppercase;
      transition: opacity 240ms ease, transform 240ms ease;
    }

    .ticker-text {
      white-space: nowrap;
      position: absolute;
      left: 100vw;
      top: 50%;
      transform: translateY(-50%);
      font-size: 18px;
      font-weight: 600;
      letter-spacing: 0.02em;
      animation: marquee var(--ticker-duration, 55s) linear infinite;
    }

    @keyframes marquee {
      from { transform: translate(0, -50%); }
      to { transform: translate(calc(-100vw - var(--ticker-width, 1800px)), -50%); }
    }

    .topbar {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      padding-bottom: 16px;
      margin-bottom: 18px;
      border-bottom: 1px solid var(--border);
    }

    .frame-title {
      font-size: clamp(36px, 5vw, 86px);
      line-height: 0.95;
      font-weight: 950;
      letter-spacing: -0.075em;
      text-transform: uppercase;
    }

    .frame-meta {
      max-width: 40%;
      color: #bae6fd;
      text-align: right;
      font-size: 13px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }

    .reason {
      color: var(--muted);
      font-size: 13px;
      margin-top: -8px;
      margin-bottom: 18px;
      letter-spacing: 0.04em;
    }

    .cards-host {
      height: calc(100% - 132px);
      overflow: hidden;
    }

    .wx-grid {
      height: 100%;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 14px;
      align-content: start;
      overflow: hidden;
    }

    .wx-card {
      min-height: 132px;
      display: grid;
      grid-template-columns: 62px minmax(0, 1fr) auto;
      grid-template-rows: 1fr auto;
      gap: 10px 14px;
      align-items: center;
      padding: 18px;
      border-radius: 20px;
      border: 1px solid var(--border);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.055), rgba(255,255,255,0.018)),
        var(--panel);
      box-shadow: 0 24px 50px rgba(0,0,0,0.32);
      backdrop-filter: blur(10px);
      overflow: hidden;
    }

    .wx-symbol {
      font-size: 42px;
      filter: drop-shadow(0 0 18px rgba(125,211,252,0.4));
    }

    .wx-city {
      font-size: 25px;
      font-weight: 900;
      line-height: 1.02;
      color: white;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .wx-condition {
      margin-top: 8px;
      color: #bae6fd;
      font-size: 13px;
      font-weight: 800;
      letter-spacing: 0.13em;
      text-transform: uppercase;
    }

    .wx-path {
      margin-top: 7px;
      max-width: 520px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 10px;
      color: #64748b;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .wx-temp {
      font-size: 27px;
      font-weight: 950;
      color: #f8fafc;
      white-space: nowrap;
      text-align: right;
    }

    .wx-meta {
      grid-column: 2 / 4;
      display: flex;
      gap: 10px;
      color: #93c5fd;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.16em;
      text-transform: uppercase;
    }

    .panel-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--border);
      font-weight: 900;
      letter-spacing: 0.2em;
      font-size: 13px;
    }

    .live-dot {
      display: inline-block;
      width: 9px;
      height: 9px;
      background: #7dd3fc;
      margin-right: 8px;
      box-shadow: 0 0 14px #38bdf8;
      animation: pulse 1.4s infinite;
    }

    @keyframes pulse {
      0%,100% { opacity: 1; }
      50% { opacity: .25; }
    }

    .rundown-list {
      margin-top: 14px;
      overflow: hidden;
    }

    .rundown-item {
      display: grid;
      grid-template-columns: 34px 1fr;
      gap: 10px;
      padding: 12px 0;
      border-bottom: 1px solid rgba(255,255,255,0.07);
      opacity: 0.64;
    }

    .rundown-item.active {
      opacity: 1;
      border-left: 3px solid #38bdf8;
      padding-left: 10px;
      background: linear-gradient(90deg, rgba(56,189,248,0.10), transparent);
    }

    .rundown-idx {
      color: #64748b;
      font-weight: 900;
      font-size: 12px;
    }

    .rundown-title {
      color: white;
      font-weight: 900;
      font-size: 15px;
      line-height: 1.1;
      text-transform: uppercase;
    }

    .rundown-sub {
      margin-top: 5px;
      color: #94a3b8;
      font-size: 12px;
      line-height: 1.2;
    }

    .debug {
      position: absolute;
      right: 12px;
      bottom: 8px;
      color: #475569;
      font-size: 11px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }

    .fade {
      opacity: 0.25;
      transform: translateY(4px);
    }
  </style>
</head>
<body>
  <div class="shell">
    <main class="main">
      <div class="topbar">
        <div>
          <div id="frameTitle" class="frame-title">Weather Cadence</div>
        </div>
        <div id="frameMeta" class="frame-meta">Booting hypermedia weather view</div>
      </div>
      <div id="reason" class="reason">The display is computed from graph state and broadcast intent.</div>
      <div id="cardsHost" class="cards-host"></div>
    </main>

    <aside class="sidebar">
      <div class="panel-header">
        <span><span class="live-dot"></span>LIVE RUNDOWN</span>
        <span id="clock">--:-- UTC</span>
      </div>
      <div id="rundownList" class="rundown-list"></div>
      <div id="debug" class="debug"></div>
    </aside>

    <section class="chyron">
      <div class="brand">WX</div>
      <div id="chyronText" class="chyron-text">LOADING WEATHER FEED</div>
    </section>

    <section class="ticker">
      <div id="tickerText" class="ticker-text">Connecting to hypergraph...</div>
    </section>
  </div>

  <script>
    const CADENCE_MS = __CADENCE_MS__;
    let lastFrameId = null;

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[c]));
    }

    function updateClock() {
      const now = new Date();
      document.getElementById('clock').textContent =
        now.toLocaleTimeString('en-GB', { timeZone: 'UTC', hour: '2-digit', minute: '2-digit' }) + ' UTC';
    }

    function renderRundown(stories, activeId) {
      const host = document.getElementById('rundownList');
      host.innerHTML = stories.map((story, i) => {
        const isActive = story.id === activeId;
        const sub = story.subTitle || story.subtitle || '';
        return `
          <div class="rundown-item ${isActive ? 'active' : ''}">
            <div class="rundown-idx">${String(i + 1).padStart(2, '0')}</div>
            <div>
              <div class="rundown-title">${escapeHtml(story.title)}</div>
              <div class="rundown-sub">${escapeHtml(sub)}</div>
            </div>
          </div>
        `;
      }).join('');
    }

    function setTicker(text) {
      const el = document.getElementById('tickerText');
      el.textContent = text || '';
      requestAnimationFrame(() => {
        const width = el.scrollWidth || 1600;
        el.style.setProperty('--ticker-width', width + 'px');
        el.style.setProperty('--ticker-duration', Math.max(35, width / 70) + 's');
        el.style.animation = 'none';
        void el.offsetHeight;
        el.style.animation = '';
      });
    }

    async function loadFrame() {
      const res = await fetch('/api/frame', { cache: 'no-store' });
      const payload = await res.json();

      if (!payload.ok) {
        throw new Error(payload.error || 'frame error');
      }

      const frame = payload.frame || {};
      const frameId = frame.id + ':' + payload.frame_index;

      const titleEl = document.getElementById('frameTitle');
      const metaEl = document.getElementById('frameMeta');
      const reasonEl = document.getElementById('reason');
      const cardsEl = document.getElementById('cardsHost');
      const chyronEl = document.getElementById('chyronText');

      if (lastFrameId && lastFrameId !== frameId) {
        titleEl.classList.add('fade');
        chyronEl.classList.add('fade');
      }

      setTimeout(() => {
        titleEl.textContent = frame.title || 'Weather Cadence';
        metaEl.textContent = `${frame.subtitle || ''} • frame ${(payload.frame_index % payload.frame_count) + 1}/${payload.frame_count}`;
        reasonEl.textContent = frame.reason || '';
        cardsEl.innerHTML = payload.cardsHtml || '';
        chyronEl.textContent = payload.chyronText || '';
        setTicker(payload.tickerText || '');
        renderRundown(payload.rundownStories || [], frame.id);

        document.getElementById('debug').textContent =
          `${payload.generated_at} • ${frame.basis || ''}`;

        titleEl.classList.remove('fade');
        chyronEl.classList.remove('fade');
        lastFrameId = frameId;
      }, lastFrameId ? 180 : 0);
    }

    async function safeLoadFrame() {
      try {
        await loadFrame();
      } catch (err) {
        console.error(err);
        document.getElementById('frameTitle').textContent = 'Weather Feed Error';
        document.getElementById('reason').textContent = err.message;
        document.getElementById('chyronText').textContent = 'WEATHER GRAPH UNAVAILABLE';
        setTicker('Unable to read weather_latest entities from the hypergraph relay.');
      }
    }

    updateClock();
    setInterval(updateClock, 1000);

    safeLoadFrame();
    setInterval(safeLoadFrame, CADENCE_MS);
  </script>
</body>
</html>
""".replace("__CADENCE_MS__", str(int(CADENCE_SECONDS * 1000)))


class Handler(BaseHTTPRequestHandler):
    server_version = "HyperWeatherCadence/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{datetime.now().isoformat(timespec='seconds')}] {self.address_string()} {fmt % args}")

    def send_json(self, payload: Json, *, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_html(self, body: str, *, status: int = 200) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        try:
            if path == "/" or path == "/index.html":
                return self.send_html(INDEX_HTML)

            if path == "/health":
                return self.send_json({
                    "ok": True,
                    "service": "weather_cadence",
                    "port": PORT,
                    "hyper_url": HYPER_URL,
                    "cadence_seconds": CADENCE_SECONDS,
                })

            if path == "/api/intent":
                return self.send_json({
                    "ok": True,
                    "intent": BROADCAST_INTENT,
                })

            if path == "/api/frame":
                params = urllib.parse.parse_qs(parsed.query)
                frame_raw = params.get("frame", [None])[0]
                frame_index = int(frame_raw) if frame_raw is not None else None
                return self.send_json(computed_frame_payload(frame_index))

            if path == "/api/debug":
                relay = HyperRelay(HYPER_URL)
                embedded = relay.query_weather_latest(limit=QUERY_LIMIT)
                rows = [normalize_weather(item) for item in embedded]
                return self.send_json({
                    "ok": True,
                    "count": len(rows),
                    "capabilities": data_capabilities(rows),
                    "sample": [
                        {
                            "display": r.display,
                            "country_code": r.country_code,
                            "condition": r.condition,
                            "condition_family": r.condition_family,
                            "temperature_c": r.temperature_c,
                            "canonical_path": r.canonical_path,
                        }
                        for r in rows[:10]
                    ],
                })

            return self.send_json({
                "ok": False,
                "error": "not_found",
                "path": path,
            }, status=404)

        except urllib.error.URLError as exc:
            return self.send_json({
                "ok": False,
                "error": "relay_unavailable",
                "message": str(exc),
                "hyper_url": HYPER_URL,
            }, status=502)

        except Exception as exc:
            traceback.print_exc()
            return self.send_json({
                "ok": False,
                "error": type(exc).__name__,
                "message": str(exc),
            }, status=500)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"weather cadence service: http://127.0.0.1:{PORT}")
    print(f"hypergraph relay:        {HYPER_URL}")
    print(f"cadence:                 {CADENCE_SECONDS:.1f}s")
    print("Ctrl-C to stop")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())