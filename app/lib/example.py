#!/usr/bin/env python3
"""
earth_app_show_server.py

Earth.app Show Runtime
======================

A self-contained Python show server that turns a HyperCore / hypergraph relay
into an always-on, TBPN-style live Earth feed.

This file intentionally does NOT hard-code weather/news/sports/social/markets
channels. The show follows the hypergraph:

  1. Start at the relay root.
  2. Discover _links.query.
  3. Discover _actions.search.
  4. Run the advertised search action.
  5. Read returned _embedded results and data.filters.
  6. Build temporary show lanes from discovered types, paths, facets, refs,
     measures, and time fields.
  7. Follow advertised record/self links for detail segments.
  8. Render the current state as a live show.

Run:

  HYPER_URL=http://127.0.0.1:8765 python earth_app_show_server.py

Open:

  http://127.0.0.1:4545
  http://127.0.0.1:4545?q=chicago
  http://127.0.0.1:4545?type=news_latest
  http://127.0.0.1:4545?facet=source:bbc

Environment:

  EARTH_APP_PORT=4545
  HYPER_URL=http://127.0.0.1:8765
  EARTH_APP_LIMIT=220
  EARTH_APP_CADENCE_SECONDS=9
  EARTH_APP_MAX_PAGES=2

The product idea:

  The show is not a playlist.
  The show is a traversal through live application state.
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


# =============================================================================
# Configuration
# =============================================================================

PORT = int(os.getenv("EARTH_APP_PORT", os.getenv("HYPER_DISPLAY_PORT", "4545")))
HYPER_URL = os.getenv("HYPER_URL", "http://127.0.0.1:8765").rstrip("/")
DEFAULT_LIMIT = int(os.getenv("EARTH_APP_LIMIT", "220"))
DEFAULT_CADENCE_SECONDS = float(os.getenv("EARTH_APP_CADENCE_SECONDS", "9"))
MAX_PAGES = max(1, int(os.getenv("EARTH_APP_MAX_PAGES", "2")))
HTTP_TIMEOUT_SECONDS = float(os.getenv("EARTH_APP_HTTP_TIMEOUT_SECONDS", "15"))

Json = dict[str, Any]


# =============================================================================
# URL and primitive helpers
# =============================================================================

def now_ms() -> int:
    return int(time.time() * 1000)


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def slug(value: Any, fallback: str = "node") -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "").strip().lower())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or fallback


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def titleize(value: Any) -> str:
    text = str(value or "").replace("_", " ").replace("-", " ").replace(".", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text.title() if text else "Earth"


def compact_key(value: Any) -> str:
    text = str(value or "")
    tail = text.rsplit(".", 1)[-1]
    return titleize(tail)


def dedup(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def format_number(value: float) -> str:
    if abs(value) >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 10_000:
        return f"{value:,.0f}"
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def iso_label(ms: int | None) -> str:
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return ""


def time_ago(ms: int | None) -> str:
    if not ms:
        return "unknown"
    delta = max(0, now_ms() - ms)
    seconds = delta // 1000
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def parse_query_string(query: str) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(query, keep_blank_values=False)


def add_query(url: str, params: dict[str, Any] | list[tuple[str, Any]]) -> str:
    parsed = urllib.parse.urlparse(url)
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)

    iterable = params.items() if isinstance(params, dict) else params

    for key, value in iterable:
        if value is None or value == "":
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                if item is not None and str(item) != "":
                    pairs.append((str(key), str(item)))
        else:
            pairs.append((str(key), str(value)))

    query = urllib.parse.urlencode(pairs, doseq=True)
    return urllib.parse.urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        query,
        parsed.fragment,
    ))


def href_for_params(params: list[tuple[str, str]]) -> str:
    return "/?" + urllib.parse.urlencode(params, doseq=True) if params else "/"


# =============================================================================
# Hypermedia agent
# =============================================================================

class HypermediaError(RuntimeError):
    pass


class HypermediaAgent:
    """Tiny runtime that discovers and follows relay affordances."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/") + "/"

    def resolve(self, href: str) -> str:
        return urllib.parse.urljoin(self.base_url, href)

    def get_json(self, href: str, *, timeout: float = HTTP_TIMEOUT_SECONDS) -> Json:
        url = self.resolve(href)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return json.loads(raw or "{}")
            except json.JSONDecodeError as exc:
                raise HypermediaError(f"Expected JSON from {url}, got non-JSON") from exc

    def links(self, doc: Json) -> dict[str, str]:
        raw = doc.get("_links") or {}
        out: dict[str, str] = {}
        if not isinstance(raw, dict):
            return out

        for rel, value in raw.items():
            href = None
            if isinstance(value, str):
                href = value
            elif isinstance(value, dict) and isinstance(value.get("href"), str):
                href = value["href"]
            if href:
                out[str(rel)] = self.resolve(href)
        return out

    def link(self, doc: Json, *rels: str) -> str | None:
        links = self.links(doc)
        for rel in rels:
            href = links.get(rel)
            if href:
                return href
        return None

    def actions(self, doc: Json) -> dict[str, Json]:
        raw = doc.get("_actions") or {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def action(self, doc: Json, *names: str) -> Json | None:
        actions = self.actions(doc)
        for name in names:
            action = actions.get(name)
            if action:
                return action
        return None

    def submit_get_action(self, action: Json, params: dict[str, Any] | list[tuple[str, Any]]) -> Json:
        method = str(action.get("method") or "GET").upper()
        href = action.get("href")

        if method != "GET":
            raise HypermediaError(f"Earth.app only knows how to render GET actions; got {method}")
        if not isinstance(href, str) or not href:
            raise HypermediaError("Search action did not advertise a usable href")

        return self.get_json(add_query(self.resolve(href), params))

    def fallback_search_action(self) -> Json:
        return {
            "method": "GET",
            "href": self.resolve("/api/query/entities"),
            "fields": [
                {"name": "q", "hint": "full-text query"},
                {"name": "type", "hint": "entity type"},
                {"name": "facet", "hint": "name:value"},
                {"name": "has_ref", "hint": "rel"},
                {"name": "include", "hint": "facets,refs,numbers,times,cells"},
            ],
            "title": "Fallback entity query",
        }

    def discover(self) -> "Discovery":
        root = self.get_json("/")
        query_home: Json = {}

        query_href = self.link(root, "query")
        if query_href:
            try:
                query_home = self.get_json(query_href)
            except Exception:
                query_home = {}

        search_action = (
            self.action(query_home, "search", "query", "entities")
            or self.action(root, "search", "query", "entities")
            or self.fallback_search_action()
        )

        return Discovery(
            root=root,
            query_home=query_home,
            search_action=search_action,
            root_links=self.links(root),
            query_links=self.links(query_home) if query_home else {},
            root_actions=self.actions(root),
            query_actions=self.actions(query_home) if query_home else {},
        )


@dataclass
class Discovery:
    root: Json
    query_home: Json
    search_action: Json
    root_links: dict[str, str]
    query_links: dict[str, str]
    root_actions: dict[str, Json]
    query_actions: dict[str, Json]

    @property
    def search_href(self) -> str:
        return str(self.search_action.get("href") or "")


# =============================================================================
# Normalized query entities
# =============================================================================

@dataclass(frozen=True)
class Entity:
    entity_id: str
    entity_type: str
    canonical_path: str
    display: str
    text: str
    score: float | None
    updated_at: int | None
    commit_seq: int | None
    facets: dict[str, list[str]]
    numbers: dict[str, float]
    times: dict[str, int]
    refs: dict[str, list[str]]
    cells: dict[str, list[str]]
    links: dict[str, str]
    state: Json
    raw: Json

    @property
    def root(self) -> str:
        return (self.canonical_path or self.entity_id or "earth").split(".", 1)[0] or "earth"

    @property
    def ref_count(self) -> int:
        return sum(len(values) for values in self.refs.values())

    @property
    def newest_ms(self) -> int:
        preferred = (
            "activity_latest_at",
            "received_at",
            "updated_at",
            "fetched_at",
            "observed_at",
            "published_at",
            "created_at",
            "start_time",
            "time",
        )
        for key in preferred:
            if key in self.times:
                return self.times[key]
        if self.times:
            return max(self.times.values())
        return self.updated_at or 0


@dataclass
class QueryState:
    label: str
    request_params: list[tuple[str, str]]
    docs: list[Json]
    entities: list[Entity]
    filters: Json
    next_href: str | None
    total: int | None


@dataclass
class Lane:
    id: str
    label: str
    kind: str
    href: str
    count: int
    reason: str


@dataclass
class Segment:
    id: str
    label: str
    title: str
    dek: str
    why: str
    mode: str
    entities: list[Entity]
    metric_label: str = ""
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecordDetail:
    href: str
    title: str
    summary: str
    kind: str
    path: str
    links: dict[str, str]
    actions: dict[str, Json]
    raw: Json


def rows_to_multi_map(rows: Any, *, key_field: str, value_field: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)

    if isinstance(rows, dict):
        for key, value in rows.items():
            for item in as_list(value):
                if item is not None:
                    out[str(key)].append(str(item))
        return dict(out)

    if not isinstance(rows, list):
        return {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get(key_field)
        value = row.get(value_field)
        if key is None or value is None:
            continue
        out[str(key)].append(str(value))

    return dict(out)


def rows_to_number_map(rows: Any) -> dict[str, float]:
    out: dict[str, float] = {}

    if isinstance(rows, dict):
        for key, value in rows.items():
            n = safe_float(value)
            if n is not None:
                out[str(key)] = n
        return out

    if not isinstance(rows, list):
        return out

    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        value = safe_float(row.get("value"))
        if name is not None and value is not None:
            out[str(name)] = value

    return out


def rows_to_time_map(rows: Any) -> dict[str, int]:
    out: dict[str, int] = {}

    if isinstance(rows, dict):
        for key, value in rows.items():
            n = safe_int(value)
            if n is not None:
                out[str(key)] = n
        return out

    if not isinstance(rows, list):
        return out

    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        value = safe_int(row.get("value_ms") or row.get("value"))
        if name is not None and value is not None:
            out[str(name)] = value

    return out


def normalize_links(raw: Json, agent: HypermediaAgent | None = None) -> dict[str, str]:
    out: dict[str, str] = {}

    for bucket_name in ("_links", "links"):
        bucket = raw.get(bucket_name)
        if not isinstance(bucket, dict):
            continue

        for rel, value in bucket.items():
            href = None
            if isinstance(value, str):
                href = value
            elif isinstance(value, dict) and isinstance(value.get("href"), str):
                href = value["href"]

            if href:
                out[str(rel)] = agent.resolve(href) if agent else href

    return out


def unwrap_query_payload(raw: Json) -> tuple[Json, Json, Json, Json]:
    """
    Returns (payload, data, state, wrapper_links_source).

    Query result shapes vary:
      - _embedded.<id>.data = query item
      - _embedded.<id>.data.query = query item
      - items[] = query item
      - direct node docs with _state and data
    """
    state = raw.get("_state") if isinstance(raw.get("_state"), dict) else {}
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}

    if isinstance(data.get("query"), dict):
        return data["query"], data, state, raw

    if any(key in data for key in ("entity_id", "canonical_path", "entity_type", "display", "facets", "numbers", "times", "refs")):
        return data, data, state, raw

    return raw, data, state, raw


def normalize_entity(raw: Json, agent: HypermediaAgent | None = None) -> Entity:
    payload, data, state, wrapper = unwrap_query_payload(raw)

    entity_id = str(
        payload.get("entity_id")
        or data.get("entity_id")
        or payload.get("canonical_path")
        or data.get("canonical_path")
        or state.get("path")
        or ""
    )
    canonical_path = str(payload.get("canonical_path") or data.get("canonical_path") or entity_id)
    entity_type = str(payload.get("entity_type") or data.get("entity_type") or data.get("kind") or state.get("kind") or "entity")

    display = str(
        payload.get("display")
        or data.get("display")
        or data.get("title")
        or data.get("name")
        or state.get("summary")
        or canonical_path.rsplit(".", 1)[-1]
        or entity_type
    )

    text_parts = [
        payload.get("text"),
        data.get("text"),
        data.get("summary"),
        data.get("description"),
        data.get("title"),
    ]
    text = " ".join(str(x) for x in text_parts if x)

    links = {}
    links.update(normalize_links(wrapper, agent))
    links.update(normalize_links(data, agent))
    links.update(normalize_links(payload, agent))

    return Entity(
        entity_id=entity_id,
        entity_type=entity_type,
        canonical_path=canonical_path,
        display=display,
        text=text,
        score=safe_float(payload.get("score") or data.get("score")),
        updated_at=safe_int(payload.get("updated_at") or data.get("updated_at") or state.get("updated_at")),
        commit_seq=safe_int(payload.get("commit_seq") or data.get("commit_seq") or state.get("commit_seq")),
        facets=rows_to_multi_map(payload.get("facets") or data.get("facets"), key_field="name", value_field="value"),
        numbers=rows_to_number_map(payload.get("numbers") or data.get("numbers")),
        times=rows_to_time_map(payload.get("times") or data.get("times")),
        refs=rows_to_multi_map(payload.get("refs") or data.get("refs"), key_field="rel", value_field="target_id"),
        cells=rows_to_multi_map(payload.get("cells") or data.get("cells"), key_field="scheme", value_field="value"),
        links=links,
        state=state,
        raw=raw,
    )


def embedded_items(doc: Json) -> list[Json]:
    embedded = doc.get("_embedded")
    out: list[Json] = []

    if isinstance(embedded, dict):
        children = embedded.get("children")
        if isinstance(children, dict):
            for value in children.values():
                if isinstance(value, dict):
                    out.append(value)
            return out

        for value in embedded.values():
            if isinstance(value, dict):
                out.append(value)
        return out

    items = None
    if isinstance(doc.get("items"), list):
        items = doc.get("items")
    elif isinstance(doc.get("data"), dict) and isinstance(doc["data"].get("items"), list):
        items = doc["data"]["items"]

    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]

    return []


def entities_from_doc(doc: Json, agent: HypermediaAgent | None = None) -> list[Entity]:
    items = embedded_items(doc)
    if items:
        return [normalize_entity(item, agent) for item in items]

    # Direct record/node document fallback.
    if isinstance(doc, dict) and ("_state" in doc or "data" in doc):
        return [normalize_entity(doc, agent)]

    return []


def filters_from_doc(doc: Json) -> Json:
    if isinstance(doc.get("filters"), dict):
        return doc["filters"]
    data = doc.get("data") if isinstance(doc.get("data"), dict) else {}
    if isinstance(data.get("filters"), dict):
        return data["filters"]
    return {}


def total_from_doc(doc: Json) -> int | None:
    data = doc.get("data") if isinstance(doc.get("data"), dict) else {}
    return safe_int(data.get("total"))


def merge_filters(filters_list: list[Json]) -> Json:
    """Best-effort merge of filter summaries across fetched pages."""
    out: Json = {
        "types": [],
        "paths": [],
        "refs": [],
        "measures": [],
        "times": [],
        "facets": {},
    }

    row_buckets: dict[str, dict[str, dict[str, Any]]] = {
        "types": {},
        "paths": {},
        "refs": {},
        "measures": {},
        "times": {},
    }

    for filters in filters_list:
        for bucket_name in ("types", "paths", "refs", "measures", "times"):
            rows = filters.get(bucket_name) or []
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                key = row.get("value") or row.get("name") or row.get("rel")
                if key is None:
                    continue
                key = str(key)
                old = row_buckets[bucket_name].get(key, {})
                count = max(safe_int(old.get("count")) or 0, safe_int(row.get("count")) or 0)
                row_buckets[bucket_name][key] = {**old, **row, "count": count}

        facets = filters.get("facets") or {}
        if isinstance(facets, dict):
            out_facets = out.setdefault("facets", {})
            for facet_name, rows in facets.items():
                bucket = out_facets.setdefault(str(facet_name), {})
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict) or row.get("value") is None:
                        continue
                    key = str(row["value"])
                    old = bucket.get(key, {})
                    count = max(safe_int(old.get("count")) or 0, safe_int(row.get("count")) or 0)
                    bucket[key] = {**old, **row, "count": count}

    for bucket_name, rows in row_buckets.items():
        out[bucket_name] = list(rows.values())

    facets_out = {}
    for facet_name, row_map in (out.get("facets") or {}).items():
        facets_out[facet_name] = list(row_map.values())
    out["facets"] = facets_out

    return out


def request_params_from_browser(params: dict[str, list[str]], *, limit: int) -> list[tuple[str, str]]:
    passthrough = {
        "q",
        "type",
        "facet",
        "has_ref",
        "ref",
        "cell",
        "time_gte",
        "time_lte",
        "number_gte",
        "number_lte",
        "number_between",
        "bbox",
        "radius",
        "radius_mode",
        "measure_scope",
        "graph_scope",
        "graph_dir",
        "sort",
        "match",
        "exclude_path_fragment",
    }

    pairs: list[tuple[str, str]] = [
        ("include", "facets,refs,numbers,times,cells"),
        ("limit", str(limit)),
    ]

    if "sort" not in params:
        pairs.append(("sort", "score"))

    for key, values in params.items():
        if key not in passthrough:
            continue
        for value in values:
            if value is not None and str(value).strip():
                pairs.append((key, str(value)))

    return pairs


def load_query_state(agent: HypermediaAgent, discovery: Discovery, browser_params: dict[str, list[str]]) -> QueryState:
    limit = int((browser_params.get("limit") or [DEFAULT_LIMIT])[0])
    request_params = request_params_from_browser(browser_params, limit=limit)

    docs: list[Json] = []
    entities: list[Entity] = []
    filters_list: list[Json] = []

    first = agent.submit_get_action(discovery.search_action, request_params)
    docs.append(first)
    entities.extend(entities_from_doc(first, agent))
    filters_list.append(filters_from_doc(first))

    next_href = agent.link(first, "next")
    page = 1
    while next_href and page < MAX_PAGES:
        try:
            doc = agent.get_json(next_href)
        except Exception:
            break
        docs.append(doc)
        entities.extend(entities_from_doc(doc, agent))
        filters_list.append(filters_from_doc(doc))
        next_href = agent.link(doc, "next")
        page += 1

    label_bits = [
        f"{key}={value}"
        for key, value in request_params
        if key not in {"include", "limit", "sort"}
    ]
    label = " • ".join(label_bits) if label_bits else "root-discovered global state"

    return QueryState(
        label=label,
        request_params=request_params,
        docs=docs,
        entities=dedupe_entities(entities),
        filters=merge_filters(filters_list),
        next_href=next_href,
        total=total_from_doc(first),
    )


def dedupe_entities(entities: list[Entity]) -> list[Entity]:
    out: list[Entity] = []
    seen: set[str] = set()
    for entity in entities:
        key = entity.entity_id or entity.canonical_path or f"{entity.entity_type}:{entity.display}"
        if key in seen:
            continue
        seen.add(key)
        out.append(entity)
    return out


# =============================================================================
# Affordance discovery
# =============================================================================

def filter_rows(filters: Json, name: str) -> list[Json]:
    rows = filters.get(name) or []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def filter_facets(filters: Json) -> dict[str, list[Json]]:
    raw = filters.get("facets") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[Json]] = {}
    for name, rows in raw.items():
        if isinstance(rows, list):
            out[str(name)] = [row for row in rows if isinstance(row, dict)]
    return out


def row_count(row: Json) -> int:
    return safe_int(row.get("count")) or 0


def row_value(row: Json) -> str:
    return str(row.get("value") or row.get("name") or row.get("rel") or "")


def discover_lanes(filters: Json, state: QueryState) -> list[Lane]:
    """Turn advertised filter space into temporary show lanes."""
    lanes: list[Lane] = []

    for row in sorted(filter_rows(filters, "types"), key=row_count, reverse=True)[:18]:
        value = row_value(row)
        if not value:
            continue
        lanes.append(Lane(
            id=f"type:{value}",
            label=titleize(value),
            kind="type",
            href=href_for_params([("type", value)]),
            count=row_count(row),
            reason="Advertised by data.filters.types",
        ))

    for row in sorted(filter_rows(filters, "paths"), key=row_count, reverse=True)[:14]:
        value = row_value(row)
        if not value:
            continue
        lanes.append(Lane(
            id=f"path:{value}",
            label=titleize(value),
            kind="path",
            href=href_for_params([("q", value)]),
            count=row_count(row),
            reason="Advertised by data.filters.paths",
        ))

    for facet_name, rows in filter_facets(filters).items():
        sorted_rows = sorted(rows, key=row_count, reverse=True)[:8]
        if len(sorted_rows) <= 1:
            continue
        for row in sorted_rows:
            value = row_value(row)
            if not value:
                continue
            lanes.append(Lane(
                id=f"facet:{facet_name}:{value}",
                label=f"{compact_key(facet_name)}: {value}",
                kind="facet",
                href=href_for_params([("facet", f"{facet_name}:{value}")]),
                count=row_count(row),
                reason="Advertised by data.filters.facets",
            ))

    for row in sorted(filter_rows(filters, "refs"), key=row_count, reverse=True)[:14]:
        rel = str(row.get("rel") or row.get("value") or row.get("name") or "")
        if not rel:
            continue
        lanes.append(Lane(
            id=f"has_ref:{rel}",
            label=f"Linked {compact_key(rel)}",
            kind="ref",
            href=href_for_params([("has_ref", rel)]),
            count=row_count(row),
            reason="Advertised by data.filters.refs",
        ))

    # Add lanes discovered from the actual page in case filters are sparse.
    roots = Counter(entity.root for entity in state.entities)
    for root, count in roots.most_common(8):
        if count < 2:
            continue
        lanes.append(Lane(
            id=f"root:{root}",
            label=titleize(root),
            kind="path-root",
            href=href_for_params([("q", root)]),
            count=count,
            reason="Observed in canonical_path roots",
        ))

    seen: set[str] = set()
    out: list[Lane] = []
    for lane in sorted(lanes, key=lambda lane: (lane.count, lane.kind != "type"), reverse=True):
        if lane.id in seen:
            continue
        seen.add(lane.id)
        out.append(lane)
    return out[:60]


def type_counter(entities: list[Entity]) -> Counter[str]:
    return Counter(entity.entity_type for entity in entities)


def root_counter(entities: list[Entity]) -> Counter[str]:
    return Counter(entity.root for entity in entities)


def facet_counter(entities: list[Entity]) -> dict[str, Counter[str]]:
    out: dict[str, Counter[str]] = defaultdict(Counter)
    for entity in entities:
        for name, values in entity.facets.items():
            for value in values:
                out[name][value] += 1
    return dict(out)


def ref_counter(entities: list[Entity]) -> Counter[str]:
    out: Counter[str] = Counter()
    for entity in entities:
        for rel, values in entity.refs.items():
            out[rel] += len(values)
    return out


def number_counter(entities: list[Entity]) -> Counter[str]:
    out: Counter[str] = Counter()
    for entity in entities:
        for key in entity.numbers:
            out[key] += 1
    return out


def time_counter(entities: list[Entity]) -> Counter[str]:
    out: Counter[str] = Counter()
    for entity in entities:
        for key in entity.times:
            out[key] += 1
    return out


# =============================================================================
# Show intelligence
# =============================================================================

def looks_like_technical_path(text: str) -> bool:
    text = str(text or "")
    if len(text) > 160:
        return False
    return bool(re.match(r"^[a-zA-Z0-9_:-]+(\.[a-zA-Z0-9_:-]+){2,}$", text))


def is_ref_or_index(entity: Entity) -> bool:
    kind = entity.entity_type.lower()
    path = entity.canonical_path.lower()
    if kind.endswith("_ref") or kind in {"ref", "index_ref", "topic_ref", "topic_record_ref"}:
        return True
    if ".refs." in path or ".index." in path or "._meta." in path:
        return True
    return False


def freshness_score(ms: int) -> float:
    if not ms:
        return 0.0
    age = max(0, now_ms() - ms)
    hour = 1000 * 60 * 60
    return max(0.0, 18.0 - (age / hour))


def signal_score(entity: Entity) -> float:
    score = 0.0

    if entity.score is not None:
        score += min(entity.score, 1000.0) / 40.0

    if entity.display and entity.display != entity.entity_id:
        score += 8
    if " " in entity.display:
        score += 4
    if len(entity.display) >= 24:
        score += 2
    if looks_like_technical_path(entity.display):
        score -= 9

    score += freshness_score(entity.newest_ms)
    score += min(entity.ref_count, 12) * 1.2
    score += min(len(entity.facets), 8) * 0.8
    score += min(len(entity.numbers), 8) * 1.0
    score += min(len(entity.times), 6) * 0.9

    if is_ref_or_index(entity):
        score -= 12
    else:
        score += 4

    if entity.root in {"_meta", "index"}:
        score -= 20

    return score


def ranked(entities: list[Entity]) -> list[Entity]:
    return sorted(entities, key=lambda entity: (signal_score(entity), entity.newest_ms, entity.score or 0), reverse=True)


def diverse_entities(entities: list[Entity], *, limit: int = 12) -> list[Entity]:
    ordered = ranked(entities)
    out: list[Entity] = []
    seen_types: set[str] = set()
    seen_roots: set[str] = set()
    seen_facets: set[tuple[str, str]] = set()

    for entity in ordered:
        facet_pairs = [(k, v) for k, values in entity.facets.items() for v in values[:2]]
        novelty = (
            entity.entity_type not in seen_types
            or entity.root not in seen_roots
            or any(pair not in seen_facets for pair in facet_pairs[:4])
        )
        if novelty:
            out.append(entity)
            seen_types.add(entity.entity_type)
            seen_roots.add(entity.root)
            seen_facets.update(facet_pairs)
        if len(out) >= limit:
            return out

    for entity in ordered:
        if entity not in out:
            out.append(entity)
        if len(out) >= limit:
            break

    return out


def metric_for(entity: Entity) -> str:
    # Score pair, if present.
    lower = {key.lower(): key for key in entity.numbers}
    home = lower.get("home_score") or next((orig for key, orig in lower.items() if key.endswith("home_score")), None)
    away = lower.get("away_score") or next((orig for key, orig in lower.items() if key.endswith("away_score")), None)
    if home and away:
        return f"{int(entity.numbers.get(away, 0))} - {int(entity.numbers.get(home, 0))}"

    preferred = (
        "magnitude",
        "temperature",
        "temp",
        "price",
        "last_price",
        "score",
        "population",
        "count",
        "location_count",
        "depth",
        "leader_count",
        "byte_length",
        "value",
    )
    ignored = {"lat", "lon", "lng", "latitude", "longitude"}

    for needle in preferred:
        for key, value in entity.numbers.items():
            key_l = key.lower()
            if key_l in ignored:
                continue
            if needle in key_l:
                return f"{compact_key(key)} {format_number(value)}"

    for key, value in entity.numbers.items():
        key_l = key.lower()
        if key_l in ignored:
            continue
        return f"{compact_key(key)} {format_number(value)}"

    if entity.newest_ms:
        return time_ago(entity.newest_ms)

    if entity.ref_count:
        return f"{entity.ref_count} refs"

    return ""


def chip_values(entity: Entity, limit: int = 7) -> list[str]:
    chips: list[str] = []

    priority = (
        "source",
        "region",
        "country_code",
        "status",
        "kind",
        "league",
        "sport",
        "published_day",
        "condition",
        "collection",
        "record_type",
    )

    for key in priority:
        for value in entity.facets.get(key, []):
            chips.append(f"{compact_key(key)}: {value}")
            if len(chips) >= limit:
                return dedup(chips)

    for key, values in entity.facets.items():
        for value in values[:2]:
            if len(str(value)) <= 48:
                chips.append(f"{compact_key(key)}: {value}")
            if len(chips) >= limit:
                return dedup(chips)

    for key, value in list(entity.numbers.items())[:3]:
        chips.append(f"{compact_key(key)}: {format_number(value)}")
        if len(chips) >= limit:
            return dedup(chips)

    if entity.ref_count:
        chips.append(f"{entity.ref_count} refs")

    if entity.newest_ms:
        chips.append(time_ago(entity.newest_ms))

    return dedup(chips)[:limit]


def subtitle_for(entity: Entity) -> str:
    parts: list[str] = []
    if entity.entity_type:
        parts.append(titleize(entity.entity_type))
    if entity.root and entity.root != entity.entity_type:
        parts.append(titleize(entity.root))
    if entity.ref_count:
        parts.append(f"{entity.ref_count} refs")
    if entity.newest_ms:
        parts.append(time_ago(entity.newest_ms))
    return " • ".join(dedup(parts)[:4])


def shape_icon(entity: Entity) -> str:
    if entity.ref_count >= 3:
        return "↗"
    if entity.numbers and entity.times:
        return "↯"
    if entity.numbers:
        return "#"
    if entity.times:
        return "◷"
    if entity.facets:
        return "◈"
    if is_ref_or_index(entity):
        return "⇢"
    return "◆"


def reason_for(entity: Entity) -> str:
    reasons: list[str] = []
    if entity.score is not None:
        reasons.append(f"search score {format_number(entity.score)}")
    if entity.newest_ms:
        reasons.append(f"fresh {time_ago(entity.newest_ms)}")
    if entity.ref_count:
        reasons.append(f"{entity.ref_count} graph refs")
    if entity.numbers:
        reasons.append(f"measures: {', '.join(list(entity.numbers)[:3])}")
    if entity.times:
        reasons.append(f"times: {', '.join(list(entity.times)[:3])}")
    if entity.facets:
        reasons.append(f"facets: {', '.join(list(entity.facets)[:3])}")
    return " • ".join(reasons) or "selected from embedded hypermedia result"


def build_segments(state: QueryState, lanes: list[Lane], record_detail: RecordDetail | None) -> list[Segment]:
    entities = state.entities
    segments: list[Segment] = []

    if not entities:
        return [Segment(
            id="empty",
            label="NO SIGNAL",
            title="No Live State Returned",
            dek="The hypergraph returned no embedded entities for this query.",
            why="The show follows advertised state; no state was returned for the current request.",
            mode="empty",
            entities=[],
        )]

    types = type_counter(entities)
    roots = root_counter(entities)
    refs = ref_counter(entities)
    numbers = number_counter(entities)
    times = time_counter(entities)
    facets = facet_counter(entities)

    segments.append(Segment(
        id="earth-open",
        label="EARTH OPEN",
        title="Earth Is Live",
        dek="A live sweep of the strongest state the graph is advertising right now.",
        why="Built from embedded results plus discovered types, roots, facets, refs, measures, and times.",
        mode="overview",
        entities=diverse_entities(entities, limit=12),
        stats={
            "entities": len(entities),
            "types": len(types),
            "roots": len(roots),
            "lanes": len(lanes),
        },
    ))

    segments.append(Segment(
        id="hot-nodes",
        label="HOT NODES",
        title="Highest Signal Nodes",
        dek="The entities with the strongest mix of relevance, freshness, refs, measures, and readable display.",
        why="Ranked by a generic signal score; no domain-specific channel logic is used.",
        mode="ranked",
        entities=ranked(entities)[:12],
        stats={"scoring": "score + freshness + refs + measures + times + facets"},
    ))

    fresh = [entity for entity in entities if entity.newest_ms]
    if fresh:
        segments.append(Segment(
            id="fresh-state",
            label="STATE CHANGE",
            title="Freshest State On The Tape",
            dek="The newest timestamped entities in the current hypermedia state.",
            why="Generated only because entities expose time fields or updated_at values.",
            mode="time",
            entities=sorted(fresh, key=lambda entity: entity.newest_ms, reverse=True)[:12],
            metric_label="freshness",
        ))

    ref_heavy = [entity for entity in entities if entity.ref_count]
    if ref_heavy:
        segments.append(Segment(
            id="follow-the-refs",
            label="FOLLOW THE REFS",
            title="Most Connected State",
            dek="The nodes with the most graph relationships available to follow.",
            why="Generated from refs advertised by the query result, not from hard-coded domains.",
            mode="refs",
            entities=sorted(ref_heavy, key=lambda entity: (entity.ref_count, signal_score(entity)), reverse=True)[:12],
            metric_label="refs",
            stats={"top_refs": dict(refs.most_common(8))},
        ))

    # Type clusters discovered from returned entities.
    for entity_type, count in types.most_common(10):
        group = [entity for entity in entities if entity.entity_type == entity_type]
        if len(group) < 2:
            continue
        segments.append(Segment(
            id=f"type-{slug(entity_type)}",
            label="TYPE LANE",
            title=titleize(entity_type),
            dek=f"{count} entities in this discovered type lane.",
            why="Generated from entity_type values returned by the graph.",
            mode="cluster",
            entities=diverse_entities(group, limit=12),
            stats={"type": entity_type, "count": count},
        ))

    # Root/path clusters discovered from canonical paths.
    for root, count in roots.most_common(10):
        group = [entity for entity in entities if entity.root == root]
        if len(group) < 2:
            continue
        segments.append(Segment(
            id=f"root-{slug(root)}",
            label="PATH LANE",
            title=titleize(root),
            dek=f"{count} entities under this canonical path root.",
            why="Generated from canonical_path structure exposed by the graph.",
            mode="cluster",
            entities=diverse_entities(group, limit=12),
            stats={"root": root, "count": count},
        ))

    # Facet clusters with variety.
    facet_candidates: list[tuple[int, str, str, list[Entity]]] = []
    for facet_name, counter in facets.items():
        if len(counter) <= 1:
            continue
        for value, count in counter.most_common(6):
            group = [entity for entity in entities if value in entity.facets.get(facet_name, [])]
            if len(group) >= 2:
                facet_candidates.append((count, facet_name, value, group))

    for count, facet_name, value, group in sorted(facet_candidates, reverse=True)[:14]:
        segments.append(Segment(
            id=f"facet-{slug(facet_name)}-{slug(value)}",
            label="FACET LANE",
            title=f"{compact_key(facet_name)}: {value}",
            dek=f"{count} entities share this advertised facet value.",
            why="Generated from facet values returned in entity payloads and/or data.filters.facets.",
            mode="facet",
            entities=diverse_entities(group, limit=12),
            stats={"facet": facet_name, "value": value, "count": count},
        ))

    # Numeric leaderboards discovered from measures.
    for number_name, count in numbers.most_common(8):
        group = [entity for entity in entities if number_name in entity.numbers]
        values = [entity.numbers[number_name] for entity in group]
        if len(values) < 2 or max(values) == min(values):
            continue
        high = sorted(group, key=lambda entity: entity.numbers[number_name], reverse=True)[:12]
        segments.append(Segment(
            id=f"measure-high-{slug(number_name)}",
            label="MEASURE TAPE",
            title=f"High {compact_key(number_name)}",
            dek="A leaderboard generated from a numeric field the graph exposed.",
            why=f"Generated from discovered numeric measure `{number_name}`.",
            mode="measure",
            entities=high,
            metric_label=number_name,
            stats={"measure": number_name, "count": count},
        ))

    # Time lanes discovered from timestamps.
    for time_name, count in times.most_common(8):
        group = [entity for entity in entities if time_name in entity.times]
        if len(group) < 2:
            continue
        newest = sorted(group, key=lambda entity: entity.times[time_name], reverse=True)[:12]
        segments.append(Segment(
            id=f"time-{slug(time_name)}",
            label="TIME TAPE",
            title=f"Latest {compact_key(time_name)}",
            dek="A recency lane generated from a timestamp field the graph exposed.",
            why=f"Generated from discovered time field `{time_name}`.",
            mode="time",
            entities=newest,
            metric_label=time_name,
            stats={"time_field": time_name, "count": count},
        ))

    # Record detail segment from an advertised record/self link.
    if record_detail:
        segments.insert(1, Segment(
            id="record-open",
            label="RECORD OPEN",
            title=record_detail.title or "Opened Record",
            dek=record_detail.summary or "The show followed an advertised record/self link into detail state.",
            why="Generated by opening the first high-signal embedded result with an advertised record/self link.",
            mode="record",
            entities=[],
            stats={
                "kind": record_detail.kind,
                "path": record_detail.path,
                "links": len(record_detail.links),
                "actions": len(record_detail.actions),
            },
        ))

    # Deduplicate while preserving order.
    seen: set[str] = set()
    out: list[Segment] = []
    for segment in segments:
        if segment.id in seen:
            continue
        seen.add(segment.id)
        out.append(segment)

    return out[:80]


# =============================================================================
# Record following
# =============================================================================

def advertised_record_href(entity: Entity) -> str | None:
    for rel in ("record", "self", "item", "canonical", "target"):
        href = entity.links.get(rel)
        if href:
            return href
    return None


def open_record_detail(agent: HypermediaAgent, entities: list[Entity]) -> RecordDetail | None:
    for entity in ranked(entities)[:16]:
        href = advertised_record_href(entity)
        if not href:
            continue
        try:
            doc = agent.get_json(href)
        except Exception:
            continue

        state = doc.get("_state") if isinstance(doc.get("_state"), dict) else {}
        data = doc.get("data") if isinstance(doc.get("data"), dict) else {}
        title = str(
            data.get("display")
            or data.get("title")
            or data.get("name")
            or state.get("summary")
            or entity.display
            or "Opened Record"
        )
        summary = str(
            state.get("summary")
            or data.get("summary")
            or data.get("description")
            or entity.text
            or "Advertised record opened through hypermedia link."
        )
        kind = str(state.get("kind") or data.get("kind") or data.get("entity_type") or entity.entity_type)
        path = str(state.get("path") or data.get("canonical_path") or data.get("path") or entity.canonical_path)

        return RecordDetail(
            href=href,
            title=title,
            summary=summary,
            kind=kind,
            path=path,
            links=agent.links(doc),
            actions=agent.actions(doc),
            raw=doc,
        )
    return None


# =============================================================================
# Rendering payload
# =============================================================================

def entity_card(entity: Entity) -> Json:
    href = advertised_record_href(entity)
    return {
        "icon": shape_icon(entity),
        "title": entity.display,
        "subtitle": subtitle_for(entity),
        "type": entity.entity_type,
        "path": entity.canonical_path or entity.entity_id,
        "root": entity.root,
        "metric": metric_for(entity),
        "chips": chip_values(entity),
        "reason": reason_for(entity),
        "href": href,
        "score": signal_score(entity),
        "refs": entity.ref_count,
        "fresh": time_ago(entity.newest_ms) if entity.newest_ms else "",
    }


def segment_payload(segment: Segment) -> Json:
    return {
        "id": segment.id,
        "label": segment.label,
        "title": segment.title,
        "dek": segment.dek,
        "why": segment.why,
        "mode": segment.mode,
        "metric_label": segment.metric_label,
        "stats": segment.stats,
        "cards": [entity_card(entity) for entity in segment.entities[:12]],
        "count": len(segment.entities),
    }


def lane_payload(lane: Lane) -> Json:
    return {
        "id": lane.id,
        "label": lane.label,
        "kind": lane.kind,
        "href": lane.href,
        "count": lane.count,
        "reason": lane.reason,
    }


def discovery_payload(discovery: Discovery) -> Json:
    return {
        "root_links": discovery.root_links,
        "query_links": discovery.query_links,
        "root_actions": sorted(discovery.root_actions.keys()),
        "query_actions": sorted(discovery.query_actions.keys()),
        "search_action": {
            "method": discovery.search_action.get("method"),
            "href": discovery.search_action.get("href"),
            "fields": discovery.search_action.get("fields") or [],
            "title": discovery.search_action.get("title"),
        },
    }


def active_index(params: dict[str, list[str]], count: int) -> int:
    if count <= 0:
        return 0
    raw = (params.get("segment") or params.get("frame") or [None])[0]
    if raw is not None:
        try:
            return int(raw) % count
        except Exception:
            pass
    cadence = float((params.get("cadence") or [DEFAULT_CADENCE_SECONDS])[0])
    cadence = max(2.0, cadence)
    return int(time.time() // cadence) % count


def tape_text(segment: Segment, lanes: list[Lane], state: QueryState) -> str:
    parts: list[str] = []
    for entity in segment.entities[:16]:
        metric = metric_for(entity)
        chunk = f"{shape_icon(entity)} {entity.display.upper()}"
        if metric:
            chunk += f" — {metric}"
        if entity.ref_count:
            chunk += f" — {entity.ref_count} REFS"
        parts.append(chunk)

    if not parts:
        for lane in lanes[:12]:
            parts.append(f"{lane.kind.upper()} LANE: {lane.label.upper()} ({lane.count})")

    if not parts:
        parts.append(f"EARTH.APP FOLLOWING HYPERMEDIA STATE — {state.label.upper()}")

    return "   •••   ".join(parts)


def lower_third(segment: Segment, state: QueryState) -> str:
    bits = [segment.label, segment.title]
    if state.label:
        bits.append(state.label)
    if segment.entities:
        lead = segment.entities[0]
        metric = metric_for(lead)
        if metric:
            bits.append(metric)
    return " • ".join(bits[:4]).upper()


def trail_payload(discovery: Discovery, state: QueryState, record_detail: RecordDetail | None) -> list[Json]:
    trail = [
        {
            "label": "Root",
            "kind": "_links",
            "detail": f"{len(discovery.root_links)} links / {len(discovery.root_actions)} actions",
        },
        {
            "label": "Query Home",
            "kind": "_actions",
            "detail": f"{len(discovery.query_links)} links / {len(discovery.query_actions)} actions",
        },
        {
            "label": "Search Action",
            "kind": str(discovery.search_action.get("method") or "GET"),
            "detail": str(discovery.search_action.get("href") or "/api/query/entities"),
        },
        {
            "label": "Result State",
            "kind": "_embedded",
            "detail": f"{len(state.entities)} entities / total {state.total if state.total is not None else 'unknown'}",
        },
    ]
    if state.next_href:
        trail.append({
            "label": "Next Page",
            "kind": "_links.next",
            "detail": state.next_href,
        })
    if record_detail:
        trail.append({
            "label": "Record Open",
            "kind": record_detail.kind,
            "detail": record_detail.path,
        })
    return trail


def compute_show(params: dict[str, list[str]]) -> Json:
    agent = HypermediaAgent(HYPER_URL)
    discovery = agent.discover()
    state = load_query_state(agent, discovery, params)
    lanes = discover_lanes(state.filters, state)
    record_detail = open_record_detail(agent, state.entities)
    segments = build_segments(state, lanes, record_detail)
    idx = active_index(params, len(segments))
    segment = segments[idx]

    return {
        "ok": True,
        "brand": "EARTH.APP",
        "tagline": "LIVE HYPERMEDIA SHOW",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "relay": HYPER_URL,
        "query": {
            "label": state.label,
            "params": state.request_params,
            "total": state.total,
            "pages_loaded": len(state.docs),
            "next_href": state.next_href,
        },
        "discovery": discovery_payload(discovery),
        "affordances": {
            "lanes": [lane_payload(lane) for lane in lanes],
            "types": [row for row in filter_rows(state.filters, "types")[:16]],
            "paths": [row for row in filter_rows(state.filters, "paths")[:16]],
            "refs": [row for row in filter_rows(state.filters, "refs")[:16]],
            "measures": [row for row in filter_rows(state.filters, "measures")[:16]],
            "times": [row for row in filter_rows(state.filters, "times")[:16]],
        },
        "trail": trail_payload(discovery, state, record_detail),
        "segments": [
            {
                "id": s.id,
                "label": s.label,
                "title": s.title,
                "dek": s.dek,
                "mode": s.mode,
                "count": len(s.entities),
                "active": s.id == segment.id,
            }
            for s in segments
        ],
        "active_index": idx,
        "active_segment": segment_payload(segment),
        "lower_third": lower_third(segment, state),
        "tape": tape_text(segment, lanes, state),
    }


# =============================================================================
# HTML/CSS/JS front end
# =============================================================================

INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Earth.app — Live Hypermedia Show</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {
      --bg: #020617;
      --panel: rgba(15, 23, 42, 0.82);
      --panel2: rgba(2, 6, 23, 0.72);
      --line: rgba(148, 163, 184, 0.23);
      --text: #f8fafc;
      --muted: #94a3b8;
      --hot: #f97316;
      --hot2: #facc15;
      --blue: #38bdf8;
      --green: #22c55e;
      --purple: #a78bfa;
      --danger: #ef4444;
    }

    * { box-sizing: border-box; }

    html, body {
      width: 100%;
      height: 100%;
      margin: 0;
      overflow: hidden;
      color: var(--text);
      background:
        radial-gradient(circle at 18% 10%, rgba(249, 115, 22, 0.22), transparent 28%),
        radial-gradient(circle at 70% 18%, rgba(56, 189, 248, 0.15), transparent 28%),
        linear-gradient(135deg, #020617 0%, #0f172a 58%, #111827 100%);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    a { color: inherit; }

    .app {
      width: 100vw;
      height: 100vh;
      display: grid;
      grid-template-columns: minmax(260px, 19vw) 1fr minmax(300px, 23vw);
      grid-template-rows: 72px 1fr 134px 42px;
      overflow: hidden;
    }

    .top {
      grid-column: 1 / 4;
      grid-row: 1;
      display: grid;
      grid-template-columns: minmax(260px, 19vw) 1fr minmax(300px, 23vw);
      border-bottom: 1px solid var(--line);
      background: rgba(2, 6, 23, 0.72);
      backdrop-filter: blur(18px);
    }

    .brand {
      display: flex;
      align-items: center;
      padding: 0 22px;
      font-size: 28px;
      font-weight: 1000;
      letter-spacing: -0.08em;
      text-transform: uppercase;
      color: white;
      border-right: 1px solid var(--line);
    }

    .brand .live-dot {
      width: 10px;
      height: 10px;
      margin-right: 12px;
      border-radius: 999px;
      background: var(--hot);
      box-shadow: 0 0 24px var(--hot);
      animation: pulse 1.2s ease-in-out infinite;
    }

    .searchbar {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 0 20px;
      border-right: 1px solid var(--line);
    }

    .searchbar form {
      width: 100%;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
    }

    .searchbar input {
      width: 100%;
      height: 42px;
      border: 1px solid rgba(148,163,184,.25);
      border-radius: 999px;
      background: rgba(15, 23, 42, 0.8);
      color: white;
      padding: 0 16px;
      outline: none;
      font-size: 14px;
      font-weight: 700;
    }

    .searchbar button {
      height: 42px;
      border: 0;
      border-radius: 999px;
      background: linear-gradient(90deg, var(--hot), var(--hot2));
      color: black;
      padding: 0 18px;
      font-weight: 1000;
      letter-spacing: .04em;
      cursor: pointer;
    }

    .clock {
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: flex-end;
      padding: 0 20px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: .16em;
      text-transform: uppercase;
    }

    .clock strong {
      color: white;
      font-size: 18px;
      letter-spacing: -0.03em;
    }

    .left {
      grid-column: 1;
      grid-row: 2 / 4;
      min-width: 0;
      overflow: hidden;
      border-right: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(2,6,23,.74), rgba(15,23,42,.56));
    }

    .main {
      grid-column: 2;
      grid-row: 2;
      min-width: 0;
      padding: 24px;
      overflow: hidden;
      position: relative;
    }

    .right {
      grid-column: 3;
      grid-row: 2 / 4;
      min-width: 0;
      overflow: hidden;
      border-left: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(2,6,23,.80), rgba(15,23,42,.64));
    }

    .lower {
      grid-column: 2;
      grid-row: 3;
      min-width: 0;
      display: grid;
      grid-template-columns: 220px 1fr;
      border-top: 1px solid rgba(255,255,255,.14);
      background:
        linear-gradient(90deg, rgba(2,6,23,.98), rgba(15,23,42,.94) 55%, rgba(249,115,22,.32));
    }

    .ticker {
      grid-column: 1 / 4;
      grid-row: 4;
      min-width: 0;
      overflow: hidden;
      background: #000;
      border-top: 1px solid rgba(255,255,255,.1);
      position: relative;
    }

    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }

    .panel {
      padding: 18px;
      border-bottom: 1px solid var(--line);
    }

    .panel-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
      color: white;
      font-size: 12px;
      font-weight: 1000;
      letter-spacing: .16em;
      text-transform: uppercase;
    }

    .panel-title span:last-child {
      color: var(--muted);
      font-size: 10px;
    }

    .trail-item, .lane, .segment-item {
      display: block;
      text-decoration: none;
      padding: 10px 0;
      border-bottom: 1px solid rgba(255,255,255,.075);
    }

    .trail-kind, .lane-kind, .segment-label {
      color: var(--hot2);
      font-size: 10px;
      font-weight: 1000;
      letter-spacing: .14em;
      text-transform: uppercase;
    }

    .trail-label, .lane-label, .segment-title {
      margin-top: 3px;
      color: white;
      font-size: 13px;
      font-weight: 950;
      line-height: 1.08;
      text-transform: uppercase;
    }

    .trail-detail, .lane-reason, .segment-dek {
      margin-top: 4px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }

    .lane-count {
      float: right;
      color: white;
      background: rgba(255,255,255,.08);
      border: 1px solid rgba(255,255,255,.09);
      border-radius: 999px;
      padding: 2px 7px;
      font-size: 10px;
      font-weight: 900;
    }

    .scroll-area {
      max-height: calc(100vh - 250px);
      overflow: hidden;
    }

    .rundown-area {
      max-height: calc(100vh - 210px);
      overflow: hidden;
    }

    .segment-item.active {
      margin-left: -18px;
      margin-right: -18px;
      padding-left: 18px;
      padding-right: 18px;
      background: linear-gradient(90deg, rgba(249,115,22,.22), transparent);
      border-left: 4px solid var(--hot);
    }

    .hero {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 24px;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--line);
    }

    .kicker {
      color: var(--hot2);
      font-size: 13px;
      font-weight: 1000;
      letter-spacing: .18em;
      text-transform: uppercase;
    }

    .headline {
      margin-top: 7px;
      max-width: 980px;
      font-size: clamp(46px, 6.3vw, 112px);
      line-height: .86;
      font-weight: 1000;
      letter-spacing: -0.08em;
      text-transform: uppercase;
    }

    .dek {
      margin-top: 14px;
      max-width: 760px;
      color: #cbd5e1;
      font-size: 16px;
      line-height: 1.35;
      font-weight: 700;
    }

    .why {
      min-width: 260px;
      max-width: 360px;
      padding: 14px;
      border: 1px solid rgba(249,115,22,.28);
      border-radius: 18px;
      background: rgba(249,115,22,.08);
    }

    .why-title {
      color: var(--hot2);
      font-size: 10px;
      font-weight: 1000;
      letter-spacing: .18em;
      text-transform: uppercase;
      margin-bottom: 8px;
    }

    .why-text {
      color: #fed7aa;
      font-size: 12px;
      line-height: 1.35;
      font-weight: 750;
    }

    .cards {
      height: calc(100% - 205px);
      margin-top: 18px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(310px, 1fr));
      gap: 14px;
      align-content: start;
      overflow: hidden;
    }

    .card {
      min-height: 160px;
      display: grid;
      grid-template-columns: 54px minmax(0, 1fr) auto;
      grid-template-rows: auto auto 1fr auto;
      gap: 7px 12px;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 20px;
      background:
        linear-gradient(180deg, rgba(255,255,255,.055), rgba(255,255,255,.018)),
        var(--panel);
      box-shadow: 0 24px 70px rgba(0,0,0,.35);
      overflow: hidden;
    }

    .card-icon {
      grid-row: 1 / 4;
      font-size: 35px;
      font-weight: 1000;
      color: var(--hot2);
      text-shadow: 0 0 24px rgba(250,204,21,.4);
    }

    .card-type {
      color: var(--blue);
      font-size: 10px;
      font-weight: 1000;
      letter-spacing: .16em;
      text-transform: uppercase;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .card-title {
      color: white;
      font-size: 22px;
      font-weight: 1000;
      line-height: 1.02;
      letter-spacing: -0.045em;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .card-sub {
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      line-height: 1.2;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .card-metric {
      grid-column: 3;
      grid-row: 1 / 3;
      color: white;
      font-size: 25px;
      font-weight: 1000;
      letter-spacing: -0.05em;
      white-space: nowrap;
      text-align: right;
    }

    .card-path {
      grid-column: 2 / 4;
      color: #64748b;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 10px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .chips {
      grid-column: 2 / 4;
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      max-height: 28px;
      overflow: hidden;
    }

    .chip {
      color: #dbeafe;
      background: rgba(56,189,248,.08);
      border: 1px solid rgba(56,189,248,.16);
      border-radius: 999px;
      padding: 4px 7px;
      font-size: 10px;
      font-weight: 800;
    }

    .card-reason {
      grid-column: 1 / 4;
      color: #94a3b8;
      font-size: 11px;
      line-height: 1.25;
      max-height: 30px;
      overflow: hidden;
    }

    .lower-badge {
      display: flex;
      align-items: center;
      justify-content: center;
      border-right: 1px solid rgba(255,255,255,.15);
      color: var(--hot2);
      font-size: 38px;
      font-weight: 1000;
      letter-spacing: -0.09em;
      text-shadow: 0 0 22px rgba(250,204,21,.35);
    }

    .lower-text {
      display: flex;
      align-items: center;
      padding: 0 26px;
      font-size: clamp(26px, 3.2vw, 64px);
      line-height: .95;
      font-weight: 1000;
      letter-spacing: -0.065em;
      text-transform: uppercase;
      overflow: hidden;
    }

    .ticker-text {
      position: absolute;
      left: 100vw;
      top: 50%;
      transform: translateY(-50%);
      white-space: nowrap;
      font-size: 17px;
      font-weight: 850;
      letter-spacing: .01em;
      color: white;
      animation: marquee var(--ticker-duration, 55s) linear infinite;
    }

    @keyframes marquee {
      from { transform: translate(0, -50%); }
      to { transform: translate(calc(-100vw - var(--ticker-width, 1800px)), -50%); }
    }

    .empty {
      padding: 24px;
      border: 1px dashed var(--line);
      border-radius: 20px;
      color: var(--muted);
      font-weight: 800;
    }

    .debug {
      position: absolute;
      right: 12px;
      bottom: 48px;
      color: #475569;
      font: 10px ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }
  </style>
</head>
<body>
  <div class="app">
    <header class="top">
      <div class="brand"><span class="live-dot"></span>EARTH.APP</div>
      <div class="searchbar">
        <form id="search-form">
          <input id="search-input" name="q" placeholder="Ask the graph what Earth is doing…" autocomplete="off" />
          <button type="submit">FOLLOW STATE</button>
        </form>
      </div>
      <div class="clock">
        <span>Live UTC</span>
        <strong id="clock">--:--:--</strong>
      </div>
    </header>

    <aside class="left">
      <div class="panel">
        <div class="panel-title"><span>State Trail</span><span>Hypermedia</span></div>
        <div id="trail"></div>
      </div>
      <div class="panel">
        <div class="panel-title"><span>Discovered Lanes</span><span>Not hard-coded</span></div>
        <div id="lanes" class="scroll-area"></div>
      </div>
    </aside>

    <main class="main">
      <section class="hero">
        <div>
          <div id="kicker" class="kicker">Booting Show</div>
          <div id="headline" class="headline">Following The Graph</div>
          <div id="dek" class="dek">Earth.app asks the hypergraph what state is available, then turns that state into a live show.</div>
        </div>
        <div class="why">
          <div class="why-title">Why This Is On Screen</div>
          <div id="why" class="why-text">Waiting for discovered state.</div>
        </div>
      </section>
      <section id="cards" class="cards"></section>
      <div id="debug" class="debug"></div>
    </main>

    <aside class="right">
      <div class="panel">
        <div class="panel-title"><span>Live Rundown</span><span>Generated</span></div>
        <div id="segments" class="rundown-area"></div>
      </div>
    </aside>

    <section class="lower">
      <div class="lower-badge">LIVE</div>
      <div id="lower" class="lower-text">EARTH.APP • FOLLOW THE STATE</div>
    </section>

    <section class="ticker">
      <div id="ticker" class="ticker-text">Waiting for the global tape…</div>
    </section>
  </div>

  <script>
    const $ = (id) => document.getElementById(id);
    let cadenceMs = 9000;

    function esc(x) {
      return String(x ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[c]));
    }

    function updateClock() {
      $('clock').textContent = new Date().toLocaleTimeString('en-GB', {
        timeZone: 'UTC', hour: '2-digit', minute: '2-digit', second: '2-digit'
      });
    }

    function currentParams() {
      return new URLSearchParams(window.location.search);
    }

    function setTicker(text) {
      const el = $('ticker');
      el.textContent = text || '';
      requestAnimationFrame(() => {
        const w = el.scrollWidth || 1800;
        el.style.setProperty('--ticker-width', w + 'px');
        el.style.setProperty('--ticker-duration', Math.max(36, w / 74) + 's');
        el.style.animation = 'none';
        void el.offsetHeight;
        el.style.animation = '';
      });
    }

    function renderTrail(items) {
      $('trail').innerHTML = (items || []).map(item => `
        <div class="trail-item">
          <div class="trail-kind">${esc(item.kind)}</div>
          <div class="trail-label">${esc(item.label)}</div>
          <div class="trail-detail">${esc(item.detail)}</div>
        </div>
      `).join('');
    }

    function renderLanes(lanes) {
      $('lanes').innerHTML = (lanes || []).slice(0, 30).map(lane => `
        <a class="lane" href="${esc(lane.href)}">
          <span class="lane-count">${esc(lane.count)}</span>
          <div class="lane-kind">${esc(lane.kind)}</div>
          <div class="lane-label">${esc(lane.label)}</div>
          <div class="lane-reason">${esc(lane.reason)}</div>
        </a>
      `).join('');
    }

    function renderSegments(segments) {
      $('segments').innerHTML = (segments || []).map((segment, index) => `
        <div class="segment-item ${segment.active ? 'active' : ''}">
          <div class="segment-label">${String(index + 1).padStart(2, '0')} • ${esc(segment.label)}</div>
          <div class="segment-title">${esc(segment.title)}</div>
          <div class="segment-dek">${esc(segment.dek)}</div>
        </div>
      `).join('');
    }

    function renderCards(cards) {
      if (!cards || !cards.length) {
        $('cards').innerHTML = '<div class="empty">No cards for this segment. The show may be displaying record-level detail or an empty state.</div>';
        return;
      }

      $('cards').innerHTML = cards.map(card => `
        <article class="card">
          <div class="card-icon">${esc(card.icon)}</div>
          <div class="card-type">${esc(card.type || card.root || 'entity')}</div>
          <div class="card-title" title="${esc(card.title)}">${card.href ? `<a href="${esc(card.href)}" target="_blank" rel="noopener noreferrer">${esc(card.title)}</a>` : esc(card.title)}</div>
          <div class="card-sub">${esc(card.subtitle)}</div>
          <div class="card-metric">${esc(card.metric)}</div>
          <div class="card-path">${esc(card.path)}</div>
          <div class="chips">${(card.chips || []).map(chip => `<span class="chip">${esc(chip)}</span>`).join('')}</div>
          <div class="card-reason">${esc(card.reason)}</div>
        </article>
      `).join('');
    }

    async function loadShow() {
      const response = await fetch('/api/show' + window.location.search, { cache: 'no-store' });
      const payload = await response.json();
      if (!payload.ok) throw new Error(payload.message || payload.error || 'show payload failed');

      const segment = payload.active_segment || {};
      $('kicker').textContent = segment.label || 'EARTH.APP';
      $('headline').textContent = segment.title || 'Earth Is Live';
      $('dek').textContent = segment.dek || '';
      $('why').textContent = segment.why || '';
      $('lower').textContent = payload.lower_third || 'EARTH.APP • LIVE HYPERMEDIA SHOW';
      $('debug').textContent = `${payload.generated_at} • ${payload.query?.label || ''}`;

      renderCards(segment.cards || []);
      renderTrail(payload.trail || []);
      renderLanes(payload.affordances?.lanes || []);
      renderSegments(payload.segments || []);
      setTicker(payload.tape || '');
    }

    async function safeLoad() {
      try {
        await loadShow();
      } catch (error) {
        console.error(error);
        $('kicker').textContent = 'SHOW ERROR';
        $('headline').textContent = 'Relay State Unavailable';
        $('dek').textContent = 'Earth.app could not discover or render the hypergraph state.';
        $('why').textContent = error.message;
        $('lower').textContent = 'EARTH.APP • HYPERMEDIA RELAY ERROR';
        setTicker('Relay unavailable or returned an unexpected payload.');
      }
    }

    $('search-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const q = $('search-input').value.trim();
      const params = new URLSearchParams();
      if (q) params.set('q', q);
      window.location.search = params.toString();
    });

    const existingQ = currentParams().get('q');
    if (existingQ) $('search-input').value = existingQ;

    updateClock();
    setInterval(updateClock, 1000);

    async function loop() {
      await safeLoad();
      window.setTimeout(loop, cadenceMs);
    }

    loop();
  </script>
</body>
</html>
"""


# =============================================================================
# HTTP server
# =============================================================================

class Handler(BaseHTTPRequestHandler):
    server_version = "EarthAppShow/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{datetime.now().isoformat(timespec='seconds')}] {self.address_string()} {fmt % args}")

    def send_json(self, payload: Json, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_html(self, payload: str, status: int = 200) -> None:
        raw = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = parse_query_string(parsed.query)

        try:
            if parsed.path in {"/", "/index.html"}:
                return self.send_html(INDEX_HTML)

            if parsed.path == "/api/show":
                return self.send_json(compute_show(params))

            if parsed.path == "/api/health":
                return self.send_json({
                    "ok": True,
                    "service": "earth_app_show_server",
                    "port": PORT,
                    "hyper_url": HYPER_URL,
                    "limit": DEFAULT_LIMIT,
                    "max_pages": MAX_PAGES,
                })

            return self.send_json({
                "ok": False,
                "error": "not_found",
                "path": parsed.path,
            }, status=404)

        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:2000]
            except Exception:
                body = ""
            return self.send_json({
                "ok": False,
                "error": "relay_http_error",
                "status": exc.code,
                "message": str(exc),
                "body": body,
            }, status=502)

        except Exception as exc:
            return self.send_json({
                "ok": False,
                "error": type(exc).__name__,
                "message": str(exc),
            }, status=500)


def main() -> int:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("=" * 72)
    print("Earth.app Show Runtime")
    print(f"open:      http://127.0.0.1:{PORT}")
    print(f"api:       http://127.0.0.1:{PORT}/api/show")
    print(f"relay:     {HYPER_URL}")
    print("mode:      hypermedia discovery, no hard-coded channels")
    print("=" * 72)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
