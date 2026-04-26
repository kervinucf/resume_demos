#!/usr/bin/env python3
"""
hyper_display_4545.py

A general-purpose display computer for HyperCore / hypergraph query results.

This server does not know weather, sports, news, finance, atproto, etc.
It renders whatever the hypergraph returns by inspecting:

  - entity_id
  - entity_type
  - canonical_path
  - display
  - facets
  - numbers / measures
  - times
  - refs
  - cells
  - filters returned by /query/entities or /api/query/entities

It supports:

  http://127.0.0.1:4545
  http://127.0.0.1:4545?q=Trump
  http://127.0.0.1:4545?type=news_article
  http://127.0.0.1:4545?facet=source:nyt
  http://127.0.0.1:4545?source=http%3A%2F%2F127.0.0.1%3A8765%2Fquery%2Fentities%3Fq%3DTrump

Core idea:
  A query result is itself a displayable hypermedia state.
  The display computes frames/channels from available graph affordances.
"""

from __future__ import annotations

import hashlib
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
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


# =============================================================================
# Configuration
# =============================================================================

PORT = int(os.getenv("HYPER_DISPLAY_PORT", "4545"))
HYPER_URL = os.getenv("HYPER_URL", "http://127.0.0.1:8765").rstrip("/")
DEFAULT_LIMIT = int(os.getenv("HYPER_DISPLAY_LIMIT", "250"))
DEFAULT_CADENCE_SECONDS = float(os.getenv("HYPER_DISPLAY_CADENCE_SECONDS", "8"))

Json = dict[str, Any]


# =============================================================================
# Relay / HTTP client
# =============================================================================

class Relay:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def get_json(self, path_or_url: str, *, timeout: float = 15.0) -> Json:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
        else:
            path = path_or_url if path_or_url.startswith("/") else "/" + path_or_url
            url = f"{self.base_url}{path}"

        req = urllib.request.Request(url, headers={"Accept": "application/json"})

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw or "{}")

    def query_entities(self, pairs: list[tuple[str, str]]) -> Json:
        query = urllib.parse.urlencode(pairs, doseq=True)
        return self.get_json(f"/api/query/entities?{query}")


# =============================================================================
# Generic entity model
# =============================================================================

@dataclass(frozen=True)
class Entity:
    entity_id: str
    entity_type: str
    canonical_path: str
    display: str
    updated_at: int | None
    commit_seq: int | None
    score: float | None
    facets: dict[str, list[str]]
    numbers: dict[str, float]
    times: dict[str, int]
    refs: dict[str, list[str]]
    cells: dict[str, list[str]]
    matched_by: Json | None
    raw: Json


@dataclass
class QueryState:
    label: str
    source_url: str | None
    request_pairs: list[tuple[str, str]]
    raw_doc: Json
    entities: list[Entity]
    filters: Json


def slug(value: Any, fallback: str = "item") -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "").strip().lower())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or fallback


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


def rows_to_multi_map(rows: Any, *, key_field: str, value_field: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)

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

    if not isinstance(rows, list):
        return out

    for row in rows:
        if not isinstance(row, dict):
            continue

        name = row.get("name")
        value = safe_int(row.get("value_ms"))

        if name is not None and value is not None:
            out[str(name)] = value

    return out


def unwrap_item(raw: Json) -> Json:
    """
    /api/query/entities:
      _embedded.<id>.data = query item

    /query/entities:
      items[] = query item

    direct node docs:
      data = payload
    """
    if isinstance(raw.get("data"), dict):
        data = raw["data"]
        if "entity_id" in data or "canonical_path" in data:
            return data

    return raw


def normalize_entity(raw: Json) -> Entity:
    item = unwrap_item(raw)

    entity_id = str(item.get("entity_id") or item.get("canonical_path") or "")
    canonical_path = str(item.get("canonical_path") or entity_id)
    entity_type = str(item.get("entity_type") or "entity")
    display = str(item.get("display") or canonical_path.rsplit(".", 1)[-1] or entity_type)

    return Entity(
        entity_id=entity_id,
        entity_type=entity_type,
        canonical_path=canonical_path,
        display=display,
        updated_at=safe_int(item.get("updated_at")),
        commit_seq=safe_int(item.get("commit_seq")),
        score=safe_float(item.get("score")),
        facets=rows_to_multi_map(item.get("facets"), key_field="name", value_field="value"),
        numbers=rows_to_number_map(item.get("numbers")),
        times=rows_to_time_map(item.get("times")),
        refs=rows_to_multi_map(item.get("refs"), key_field="rel", value_field="target_id"),
        cells=rows_to_multi_map(item.get("cells"), key_field="scheme", value_field="value"),
        matched_by=item.get("matched_by") if isinstance(item.get("matched_by"), dict) else None,
        raw=item,
    )


def entities_from_doc(doc: Json) -> list[Entity]:
    embedded = doc.get("_embedded") or {}

    if isinstance(embedded, dict) and embedded:
        entities = []
        for value in embedded.values():
            if isinstance(value, dict):
                entities.append(normalize_entity(value))
        return entities

    items = None

    if isinstance(doc.get("items"), list):
        items = doc.get("items")
    elif isinstance(doc.get("data"), dict) and isinstance(doc["data"].get("items"), list):
        items = doc["data"]["items"]

    if isinstance(items, list):
        return [
            normalize_entity(item)
            for item in items
            if isinstance(item, dict)
        ]

    # If someone points source= at a single graph node, turn it into one entity.
    if "_state" in doc or "data" in doc:
        state = doc.get("_state") if isinstance(doc.get("_state"), dict) else {}
        data = doc.get("data") if isinstance(doc.get("data"), dict) else {}

        pseudo = {
            "entity_id": state.get("path") or data.get("entity_id") or data.get("canonical_path") or "source.node",
            "entity_type": data.get("kind") or data.get("type") or state.get("kind") or "node",
            "canonical_path": state.get("path") or data.get("canonical_path") or "source.node",
            "display": data.get("name") or data.get("title") or data.get("display") or state.get("summary") or state.get("path") or "Source Node",
            "facets": [],
            "numbers": [],
            "times": [],
            "refs": [],
            "cells": [],
        }
        return [normalize_entity(pseudo)]

    return []


def filters_from_doc(doc: Json) -> Json:
    if isinstance(doc.get("filters"), dict):
        return doc["filters"]
    if isinstance(doc.get("data"), dict) and isinstance(doc["data"].get("filters"), dict):
        return doc["data"]["filters"]
    return {}


# =============================================================================
# Query input modes
# =============================================================================

PASSTHROUGH_QUERY_KEYS = {
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

CONTROL_PARAMS = {
    "frame",
    "cadence",
    "limit",
    "source",
    "query",
    "auto",
}


def has_user_query(params: dict[str, list[str]]) -> bool:
    if params.get("source") or params.get("query"):
        return True

    for key, values in params.items():
        if key in CONTROL_PARAMS:
            continue
        if key in PASSTHROUGH_QUERY_KEYS and any(str(v).strip() for v in values):
            return True

    return False


def cadence_from_params(params: dict[str, list[str]]) -> float:
    raw = (params.get("cadence") or [DEFAULT_CADENCE_SECONDS])[0]
    try:
        return max(2.0, float(raw))
    except Exception:
        return DEFAULT_CADENCE_SECONDS


def query_pairs_from_params(params: dict[str, list[str]], *, limit: int | None = None) -> list[tuple[str, str]]:
    effective_limit = limit
    if effective_limit is None:
        effective_limit = int((params.get("limit") or [DEFAULT_LIMIT])[0])

    pairs: list[tuple[str, str]] = [
        ("include", "facets,refs,numbers,times,cells"),
        ("limit", str(effective_limit)),
    ]

    if "sort" not in params:
        pairs.append(("sort", "score"))

    for key, values in params.items():
        if key in CONTROL_PARAMS:
            continue
        if key not in PASSTHROUGH_QUERY_KEYS:
            continue

        for value in values:
            if value is not None and str(value).strip():
                pairs.append((key, str(value)))

    return pairs


def params_from_pairs(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlencode(pairs, doseq=True))


def node_data(doc: Json) -> Json:
    """
    Extracts application data from relay node shapes.
    """
    if not isinstance(doc, dict):
        return {}

    data = doc.get("data")

    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]

    return data if isinstance(data, dict) else {}


def pairs_from_query_resource(doc: Json) -> tuple[list[tuple[str, str]], Json]:
    data = node_data(doc)
    params = data.get("params") if isinstance(data.get("params"), dict) else {}

    pairs: list[tuple[str, str]] = []

    for key, value in params.items():
        if isinstance(value, list):
            for item in value:
                pairs.append((str(key), str(item)))
        elif value is not None:
            pairs.append((str(key), str(value)))

    if not any(k == "include" for k, _ in pairs):
        pairs.append(("include", "facets,refs,numbers,times,cells"))

    if not any(k == "limit" for k, _ in pairs):
        pairs.append(("limit", str(DEFAULT_LIMIT)))

    meta = {
        "path": data.get("path"),
        "name": data.get("name") or data.get("title") or "Display Query",
        "description": data.get("description") or "",
        "presentation": data.get("presentation") if isinstance(data.get("presentation"), dict) else {},
        "intent": data.get("intent") if isinstance(data.get("intent"), dict) else {},
        "raw": data,
    }

    return pairs, meta


def load_query_state(relay: Relay, params: dict[str, list[str]], *, limit: int | None = None) -> tuple[QueryState, Json]:
    """
    Modes:
      ?source=http://127.0.0.1:8765/query/entities?q=Trump
      ?query=queries.display.trump
      ?q=Trump
      ?type=news_article
      no params = broad query
    """
    source = (params.get("source") or [None])[0]
    query_path = (params.get("query") or [None])[0]
    query_meta: Json = {}

    if source:
        doc = relay.get_json(source)
        entities = entities_from_doc(doc)
        filters = filters_from_doc(doc)

        return QueryState(
            label=f"source={source}",
            source_url=source,
            request_pairs=[],
            raw_doc=doc,
            entities=entities,
            filters=filters,
        ), query_meta

    if query_path:
        doc = relay.get_json("/" + urllib.parse.quote(query_path, safe="."))
        pairs, query_meta = pairs_from_query_resource(doc)
        query_doc = relay.query_entities(pairs)

        return QueryState(
            label=f"query={query_path}",
            source_url=None,
            request_pairs=pairs,
            raw_doc=query_doc,
            entities=entities_from_doc(query_doc),
            filters=filters_from_doc(query_doc),
        ), query_meta

    pairs = query_pairs_from_params(params, limit=limit)
    query_doc = relay.query_entities(pairs)

    label_parts = [
        f"{key}={value}"
        for key, value in pairs
        if key not in {"include", "limit", "sort"}
    ]

    label = " • ".join(label_parts) if label_parts else "broad graph sample"

    return QueryState(
        label=label,
        source_url=None,
        request_pairs=pairs,
        raw_doc=query_doc,
        entities=entities_from_doc(query_doc),
        filters=filters_from_doc(query_doc),
    ), query_meta


# =============================================================================
# Capability analysis
# =============================================================================

@dataclass
class Capability:
    entities: list[Entity]
    type_counts: Counter[str]
    path_counts: Counter[str]
    facet_counts: dict[str, Counter[str]]
    number_counts: Counter[str]
    time_counts: Counter[str]
    ref_counts: Counter[str]
    cell_counts: Counter[str]
    filter_summary: Json


def path_fragments(path: str) -> list[str]:
    parts = [p for p in str(path or "").split(".") if p]
    out: list[str] = []

    for i in range(1, min(len(parts), 4) + 1):
        out.append(".".join(parts[:i]))

    return out


def merge_filter_summary_into_capability(cap: Capability, filters: Json) -> Capability:
    """
    The relay may already have richer filter summaries than the sampled page.
    Fold those into counts when available.
    """
    for row in filters.get("types") or []:
        if isinstance(row, dict) and row.get("value"):
            cap.type_counts[str(row["value"])] = max(
                cap.type_counts[str(row["value"])],
                safe_int(row.get("count")) or 0,
            )

    for row in filters.get("paths") or []:
        if isinstance(row, dict) and row.get("value"):
            cap.path_counts[str(row["value"])] = max(
                cap.path_counts[str(row["value"])],
                safe_int(row.get("count")) or 0,
            )

    facets = filters.get("facets") or {}
    if isinstance(facets, dict):
        for name, rows in facets.items():
            counter = cap.facet_counts.setdefault(str(name), Counter())
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and row.get("value") is not None:
                        counter[str(row["value"])] = max(
                            counter[str(row["value"])],
                            safe_int(row.get("count")) or 0,
                        )

    for row in filters.get("refs") or []:
        if isinstance(row, dict) and row.get("rel"):
            cap.ref_counts[str(row["rel"])] = max(
                cap.ref_counts[str(row["rel"])],
                safe_int(row.get("count")) or 0,
            )

    for row in filters.get("measures") or []:
        if isinstance(row, dict) and row.get("name"):
            cap.number_counts[str(row["name"])] = max(
                cap.number_counts[str(row["name"])],
                safe_int(row.get("count")) or 0,
            )

    for row in filters.get("times") or []:
        if isinstance(row, dict) and row.get("name"):
            cap.time_counts[str(row["name"])] = max(
                cap.time_counts[str(row["name"])],
                safe_int(row.get("count")) or 0,
            )

    return cap


def analyze(entities: list[Entity], filters: Json | None = None) -> Capability:
    type_counts: Counter[str] = Counter()
    path_counts: Counter[str] = Counter()
    facet_counts: dict[str, Counter[str]] = defaultdict(Counter)
    number_counts: Counter[str] = Counter()
    time_counts: Counter[str] = Counter()
    ref_counts: Counter[str] = Counter()
    cell_counts: Counter[str] = Counter()

    for entity in entities:
        type_counts[entity.entity_type] += 1

        for fragment in path_fragments(entity.canonical_path or entity.entity_id):
            path_counts[fragment] += 1

        for key, values in entity.facets.items():
            for value in values:
                facet_counts[key][value] += 1

        for key in entity.numbers:
            number_counts[key] += 1

        for key in entity.times:
            time_counts[key] += 1

        for key, values in entity.refs.items():
            ref_counts[key] += len(values)

        for key, values in entity.cells.items():
            cell_counts[key] += len(values)

    cap = Capability(
        entities=entities,
        type_counts=type_counts,
        path_counts=path_counts,
        facet_counts=dict(facet_counts),
        number_counts=number_counts,
        time_counts=time_counts,
        ref_counts=ref_counts,
        cell_counts=cell_counts,
        filter_summary=filters or {},
    )

    if filters:
        cap = merge_filter_summary_into_capability(cap, filters)

    return cap


def entropy(counter: Counter[str]) -> float:
    total = sum(counter.values())
    if total <= 0:
        return 0.0

    h = 0.0
    for count in counter.values():
        p = count / total
        h -= p * math.log2(p)

    return h


def informative_facets(cap: Capability) -> list[str]:
    scored: list[tuple[float, str]] = []
    total = max(1, len(cap.entities))

    for name, counter in cap.facet_counts.items():
        variety = len(counter)
        coverage = sum(counter.values()) / total

        if variety <= 1:
            continue

        uniqueness_penalty = max(0.0, (variety / total) - 0.45)
        score = (
            coverage * 1.5
            + min(variety, 12) / 12
            + entropy(counter) / 4
            - uniqueness_penalty
        )

        scored.append((score, name))

    scored.sort(reverse=True)
    return [name for _, name in scored]


def informative_numbers(cap: Capability) -> list[str]:
    scored: list[tuple[float, str]] = []

    for name, count in cap.number_counts.items():
        values = [
            entity.numbers[name]
            for entity in cap.entities
            if name in entity.numbers
        ]

        if len(values) >= 2:
            spread = max(values) - min(values)
            if spread == 0:
                continue
            score = count + min(abs(spread), 1000) / 1000
        else:
            score = count

        scored.append((score, name))

    scored.sort(reverse=True)
    return [name for _, name in scored]


def informative_times(cap: Capability) -> list[str]:
    scored: list[tuple[float, str]] = []

    for name, count in cap.time_counts.items():
        values = [
            entity.times[name]
            for entity in cap.entities
            if name in entity.times
        ]

        if len(values) >= 2:
            spread = max(values) - min(values)
            one_week_ms = 1000 * 60 * 60 * 24 * 7
            score = count + min(spread / one_week_ms, 1.0)
        else:
            score = count

        scored.append((score, name))

    scored.sort(reverse=True)
    return [name for _, name in scored]


# =============================================================================
# Content ranking: generic graph-shape rule
# =============================================================================

def looks_like_path_text(value: str) -> bool:
    text = str(value or "")
    if len(text) > 120:
        return False
    return bool(re.match(r"^[a-zA-Z0-9_:-]+(\.[a-zA-Z0-9_:-]+){2,}$", text))


def is_reference_like(entity: Entity) -> bool:
    et = entity.entity_type.lower()
    path = entity.canonical_path.lower()

    if "index_ref" in et:
        return True

    if et.endswith("_ref") or et in {"topic_ref", "topic_record_ref"}:
        return True

    if ".index." in path or ".refs." in path or "._meta." in path:
        return True

    return False


def content_score(entity: Entity) -> float:
    """
    Generic heuristic:
      - Rich display text beats technical IDs.
      - Primary records beat refs/indexes.
      - Entities with times/refs/facets/numbers are useful.
      - Search score still matters, but should not make index refs dominate.
    """
    score = 0.0

    if entity.score is not None:
        score += min(entity.score, 1000) / 50

    if entity.display and entity.display != entity.entity_id:
        score += 8

    if len(entity.display) > 24:
        score += 5

    if " " in entity.display:
        score += 6

    if looks_like_path_text(entity.display):
        score -= 8

    if is_reference_like(entity):
        score -= 12
    else:
        score += 8

    if entity.times:
        score += 4

    if entity.refs:
        score += 2

    if entity.facets:
        score += 2

    if entity.numbers:
        score += 2

    if entity.canonical_path.startswith("_meta") or "._meta." in entity.canonical_path:
        score -= 20

    return score


def ranked_entities(entities: list[Entity]) -> list[Entity]:
    return sorted(
        entities,
        key=lambda entity: (content_score(entity), entity.updated_at or 0, entity.score or 0),
        reverse=True,
    )


# =============================================================================
# Discovered channels
# =============================================================================

@dataclass(frozen=True)
class Channel:
    id: str
    title: str
    subtitle: str
    href: str
    query_params: list[tuple[str, str]]
    score: float


def titleize_token(value: str) -> str:
    raw = str(value or "").strip()
    raw = raw.replace("_", " ").replace("-", " ").replace(".", " ")
    raw = re.sub(r"\s+", " ", raw)
    return raw.title() if raw else "Hypergraph"


def compact_name(name: str) -> str:
    raw = str(name or "")
    tail = raw.rsplit(".", 1)[-1]
    return tail.replace("_", " ").replace("-", " ").title()


def channel_href(query_params: list[tuple[str, str]]) -> str:
    encoded = urllib.parse.urlencode(query_params, doseq=True)
    return f"/?{encoded}" if encoded else "/"


def generate_channels(cap: Capability) -> list[Channel]:
    channels: list[Channel] = []
    total = max(1, len(cap.entities))

    for entity_type, count in cap.type_counts.most_common(20):
        channels.append(Channel(
            id=f"type:{entity_type}",
            title=titleize_token(entity_type),
            subtitle=f"{count} entities",
            href=channel_href([("type", entity_type)]),
            query_params=[("type", entity_type)],
            score=100 + count,
        ))

    for path, count in cap.path_counts.most_common(24):
        if count < 2:
            continue

        penalty = 20 if count >= total else 0
        channels.append(Channel(
            id=f"path:{path}",
            title=titleize_token(path),
            subtitle=f"{count} path matches",
            href=channel_href([("q", path)]),
            query_params=[("q", path)],
            score=80 + count - penalty,
        ))

    for facet_name, counter in cap.facet_counts.items():
        if len(counter) <= 1:
            continue

        for value, count in counter.most_common(12):
            if count < 2:
                continue

            channels.append(Channel(
                id=f"facet:{facet_name}:{value}",
                title=f"{compact_name(facet_name)}: {titleize_token(value)}",
                subtitle=f"{count} entities",
                href=channel_href([("facet", f"{facet_name}:{value}")]),
                query_params=[("facet", f"{facet_name}:{value}")],
                score=60 + count,
            ))

    for rel, count in cap.ref_counts.most_common(16):
        if count < 2:
            continue

        channels.append(Channel(
            id=f"has_ref:{rel}",
            title=f"Linked {compact_name(rel)}",
            subtitle=f"{count} refs",
            href=channel_href([("has_ref", rel)]),
            query_params=[("has_ref", rel)],
            score=50 + count,
        ))

    for name, count in cap.number_counts.most_common(14):
        if count < 2:
            continue

        channels.append(Channel(
            id=f"measure:{name}",
            title=f"Measure: {compact_name(name)}",
            subtitle=f"{count} numeric values",
            href=channel_href([("q", f"number:{name}")]),
            query_params=[("q", f"number:{name}")],
            score=40 + count,
        ))

    for name, count in cap.time_counts.most_common(14):
        if count < 2:
            continue

        channels.append(Channel(
            id=f"time:{name}",
            title=f"Time: {compact_name(name)}",
            subtitle=f"{count} timestamp values",
            href=channel_href([("q", f"time:{name}")]),
            query_params=[("q", f"time:{name}")],
            score=40 + count,
        ))

    seen: set[str] = set()
    out: list[Channel] = []

    for channel in sorted(channels, key=lambda c: c.score, reverse=True):
        if channel.id in seen:
            continue
        seen.add(channel.id)
        out.append(channel)

    return out[:60]


def discover_broad_channels(relay: Relay) -> list[Channel]:
    doc = relay.query_entities([
        ("include", "facets,refs,numbers,times,cells"),
        ("limit", str(DEFAULT_LIMIT)),
        ("sort", "score"),
    ])
    entities = entities_from_doc(doc)
    filters = filters_from_doc(doc)
    cap = analyze(entities, filters)
    return generate_channels(cap)


def select_auto_channel(channels: list[Channel], cadence: float) -> Channel | None:
    if not channels:
        return None

    top = channels[:16]
    idx = int(time.time() // (cadence * 4)) % len(top)
    return top[idx]


def channel_json(channel: Channel) -> Json:
    return {
        "id": channel.id,
        "title": channel.title,
        "subtitle": channel.subtitle,
        "href": channel.href,
        "score": channel.score,
    }


# =============================================================================
# Display profile
# =============================================================================

@dataclass
class DisplayProfile:
    title: str
    subtitle: str
    badge: str
    accent: str
    query_label: str
    primary_facets: list[str]
    primary_numbers: list[str]
    primary_times: list[str]


def stable_color(seed: str) -> str:
    palette = [
        "#38bdf8",
        "#a78bfa",
        "#f97316",
        "#eab308",
        "#22c55e",
        "#ef4444",
        "#14b8a6",
        "#ec4899",
        "#84cc16",
        "#06b6d4",
    ]
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    return palette[int(digest[:4], 16) % len(palette)]


def initials(title: str) -> str:
    words = [word for word in re.split(r"\s+", title.strip()) if word]
    if not words:
        return "HG"
    if len(words) == 1:
        return words[0][:2].upper()
    return "".join(word[0] for word in words[:2]).upper()


def infer_profile(cap: Capability, state: QueryState, params: dict[str, list[str]], query_meta: Json | None = None) -> DisplayProfile:
    query_meta = query_meta or {}
    presentation = query_meta.get("presentation") if isinstance(query_meta.get("presentation"), dict) else {}

    if presentation.get("title"):
        title = str(presentation["title"])
        basis = title
    elif params.get("source"):
        title = "Source Query"
        basis = state.source_url or "source"
    elif params.get("query"):
        title = titleize_token(str(params["query"][0]))
        basis = str(params["query"][0])
    elif params.get("type"):
        title = titleize_token(str(params["type"][0]))
        basis = str(params["type"][0])
    elif params.get("q"):
        title = titleize_token(str(params["q"][0]))
        basis = str(params["q"][0])
    elif params.get("facet"):
        title = titleize_token(str(params["facet"][0]))
        basis = str(params["facet"][0])
    elif cap.type_counts:
        dominant_type = cap.type_counts.most_common(1)[0][0]
        title = titleize_token(dominant_type)
        basis = dominant_type
    elif cap.path_counts:
        dominant_path = cap.path_counts.most_common(1)[0][0]
        title = titleize_token(dominant_path)
        basis = dominant_path
    else:
        title = "Hypergraph Cadence"
        basis = "hypergraph"

    primary_facets = informative_facets(cap)
    primary_numbers = informative_numbers(cap)
    primary_times = informative_times(cap)

    subtitle_parts = []
    if cap.entities:
        subtitle_parts.append(f"{len(cap.entities)} entities")
    if cap.type_counts:
        subtitle_parts.append(f"{len(cap.type_counts)} types")
    if primary_facets:
        subtitle_parts.append(f"{len(primary_facets)} facets")
    if primary_numbers:
        subtitle_parts.append(f"{len(primary_numbers)} measures")
    if primary_times:
        subtitle_parts.append(f"{len(primary_times)} times")

    return DisplayProfile(
        title=title,
        subtitle=" • ".join(subtitle_parts) or "No graph entities returned",
        badge=str(presentation.get("badge") or initials(title)),
        accent=str(presentation.get("accent") or stable_color(basis)),
        query_label=state.label,
        primary_facets=primary_facets,
        primary_numbers=primary_numbers,
        primary_times=primary_times,
    )


# =============================================================================
# Frames
# =============================================================================

@dataclass
class Frame:
    id: str
    title: str
    subtitle: str
    reason: str
    basis: str
    entities: list[Entity]

def all_facet_pairs(entity: Entity) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    for key, values in entity.facets.items():
        for value in values:
            out.append((key, value))

    return out
def diverse_select(entities: list[Entity], *, limit: int = 10) -> list[Entity]:
    ranked = ranked_entities(entities)

    out: list[Entity] = []
    seen_types: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()

    for entity in ranked:
        novelty = False

        if entity.entity_type not in seen_types:
            novelty = True

        for pair in all_facet_pairs(entity)[:8]:
            if pair not in seen_pairs:
                novelty = True

        if novelty:
            out.append(entity)
            seen_types.add(entity.entity_type)
            seen_pairs.update(all_facet_pairs(entity))

        if len(out) >= limit:
            return out

    for entity in ranked:
        if entity not in out:
            out.append(entity)
        if len(out) >= limit:
            break

    return out


def frame_overview(cap: Capability, profile: DisplayProfile) -> Frame:
    return Frame(
        id="overview",
        title=profile.title,
        subtitle=profile.subtitle,
        reason="Computed from query results, graph filters, types, paths, facets, measures, times, and refs.",
        basis="overview",
        entities=diverse_select(cap.entities, limit=10),
    )


def frames_by_type(cap: Capability) -> list[Frame]:
    grouped: dict[str, list[Entity]] = defaultdict(list)
    for entity in cap.entities:
        grouped[entity.entity_type].append(entity)

    frames = []
    for entity_type, count in cap.type_counts.most_common(10):
        group = grouped.get(entity_type, [])
        if not group:
            continue

        frames.append(Frame(
            id=f"type-{slug(entity_type)}",
            title=titleize_token(entity_type),
            subtitle=f"{count} entities",
            reason="The query result exposes multiple entity types; this frame isolates one type.",
            basis=f"type:{entity_type}",
            entities=diverse_select(group, limit=10),
        ))

    return frames


def frames_by_path(cap: Capability) -> list[Frame]:
    frames = []

    for path, count in cap.path_counts.most_common(14):
        if count < 2:
            continue

        group = [
            entity for entity in cap.entities
            if entity.canonical_path == path or entity.canonical_path.startswith(path + ".")
        ]

        if not group:
            continue

        frames.append(Frame(
            id=f"path-{slug(path)}",
            title=titleize_token(path),
            subtitle=f"{count} path matches",
            reason="The query result exposes a path cluster; this frame follows that graph branch.",
            basis=f"path:{path}",
            entities=diverse_select(group, limit=10),
        ))

    return frames


def frames_by_facet(cap: Capability, profile: DisplayProfile) -> list[Frame]:
    frames = []

    for facet_name in profile.primary_facets[:10]:
        counter = cap.facet_counts.get(facet_name)
        if not counter:
            continue

        grouped: dict[str, list[Entity]] = defaultdict(list)
        for entity in cap.entities:
            for value in entity.facets.get(facet_name, []):
                grouped[value].append(entity)

        for value, count in counter.most_common(8):
            group = grouped.get(value, [])
            if not group:
                continue

            frames.append(Frame(
                id=f"facet-{slug(facet_name)}-{slug(value)}",
                title=titleize_token(value),
                subtitle=f"{compact_name(facet_name)} • {count} entities",
                reason=f"The result advertises facet {facet_name}={value}; this frame is computed from that band.",
                basis=f"facet:{facet_name}:{value}",
                entities=diverse_select(group, limit=10),
            ))

    return frames


def frames_by_number(cap: Capability, profile: DisplayProfile) -> list[Frame]:
    frames = []

    for number_name in profile.primary_numbers[:8]:
        group = [
            entity for entity in cap.entities
            if number_name in entity.numbers
        ]

        if len(group) < 2:
            continue

        frames.append(Frame(
            id=f"number-{slug(number_name)}-high",
            title=f"High {compact_name(number_name)}",
            subtitle="numeric leaderboard",
            reason=f"The result exposes numeric field {number_name}; this frame ranks high values.",
            basis=f"number:{number_name}:desc",
            entities=sorted(group, key=lambda e: e.numbers[number_name], reverse=True)[:10],
        ))

        frames.append(Frame(
            id=f"number-{slug(number_name)}-low",
            title=f"Low {compact_name(number_name)}",
            subtitle="numeric leaderboard",
            reason=f"The result exposes numeric field {number_name}; this frame ranks low values.",
            basis=f"number:{number_name}:asc",
            entities=sorted(group, key=lambda e: e.numbers[number_name])[:10],
        ))

    return frames


def frames_by_time(cap: Capability, profile: DisplayProfile) -> list[Frame]:
    frames = []

    for time_name in profile.primary_times[:8]:
        group = [
            entity for entity in cap.entities
            if time_name in entity.times
        ]

        if len(group) < 2:
            continue

        frames.append(Frame(
            id=f"time-{slug(time_name)}-recent",
            title=f"Latest {compact_name(time_name)}",
            subtitle="recency page",
            reason=f"The result exposes time field {time_name}; this frame surfaces recent entities.",
            basis=f"time:{time_name}:desc",
            entities=sorted(group, key=lambda e: e.times[time_name], reverse=True)[:10],
        ))

        frames.append(Frame(
            id=f"time-{slug(time_name)}-oldest",
            title=f"Oldest {compact_name(time_name)}",
            subtitle="archive page",
            reason=f"The result exposes time field {time_name}; this frame surfaces older entities.",
            basis=f"time:{time_name}:asc",
            entities=sorted(group, key=lambda e: e.times[time_name])[:10],
        ))

    return frames


def frames_by_ref(cap: Capability) -> list[Frame]:
    frames = []

    for rel, count in cap.ref_counts.most_common(12):
        group = [
            entity for entity in cap.entities
            if rel in entity.refs
        ]

        if not group:
            continue

        frames.append(Frame(
            id=f"ref-{slug(rel)}",
            title=f"Linked {compact_name(rel)}",
            subtitle=f"{count} refs",
            reason=f"The result advertises refs named {rel}; this frame surfaces connected entities.",
            basis=f"ref:{rel}",
            entities=diverse_select(group, limit=10),
        ))

    return frames


def frames_indexes_and_refs(cap: Capability) -> list[Frame]:
    ref_like = [entity for entity in cap.entities if is_reference_like(entity)]

    if not ref_like:
        return []

    return [
        Frame(
            id="references-indexes",
            title="Refs And Indexes",
            subtitle=f"{len(ref_like)} reference-like entities",
            reason="Reference and index nodes are navigational affordances; this frame keeps them available without letting them dominate content.",
            basis="shape:refs_indexes",
            entities=diverse_select(ref_like, limit=10),
        )
    ]


def infer_frames(cap: Capability, profile: DisplayProfile) -> list[Frame]:
    if not cap.entities:
        return [
            Frame(
                id="empty",
                title="No Graph Data",
                subtitle="No entities returned",
                reason="The relay returned no entities for the current query.",
                basis="empty",
                entities=[],
            )
        ]

    frames: list[Frame] = []
    frames.append(frame_overview(cap, profile))
    frames.extend(frames_by_type(cap))
    frames.extend(frames_by_path(cap))
    frames.extend(frames_by_facet(cap, profile))
    frames.extend(frames_by_number(cap, profile))
    frames.extend(frames_by_time(cap, profile))
    frames.extend(frames_by_ref(cap))
    frames.extend(frames_indexes_and_refs(cap))

    seen: set[str] = set()
    out: list[Frame] = []

    for frame in frames:
        if frame.id in seen:
            continue
        seen.add(frame.id)
        out.append(frame)

    return out[:50]


# =============================================================================
# Generic render helpers
# =============================================================================

def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def time_label(ms: int | None) -> str:
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return ""


def newest_time(entity: Entity) -> int | None:
    if not entity.times:
        return entity.updated_at

    preferred = [
        "activity_latest_at",
        "updated_at",
        "received_at",
        "created_at",
        "fetched_at",
        "published_at",
        "observed_at",
        "start_time",
    ]

    for name in preferred:
        if name in entity.times:
            return entity.times[name]

    return max(entity.times.values())


def glyph_for(entity: Entity) -> str:
    haystack = " ".join([
        entity.entity_type,
        entity.canonical_path,
        entity.display,
        " ".join(v for values in entity.facets.values() for v in values),
    ]).lower()

    if any(x in haystack for x in ["weather", "temperature", "rain", "cloud", "forecast"]):
        if "storm" in haystack or "thunder" in haystack:
            return "⛈️"
        if "rain" in haystack:
            return "🌧️"
        if "snow" in haystack:
            return "❄️"
        if "cloud" in haystack or "overcast" in haystack:
            return "☁️"
        if "clear" in haystack or "sun" in haystack:
            return "☀️"
        return "🌤️"

    if any(x in haystack for x in ["sports", "game", "team", "league", "score"]):
        if "live" in haystack or "in_progress" in haystack:
            return "🔴"
        if "future" in haystack or "scheduled" in haystack:
            return "🗓️"
        if "past" in haystack or "final" in haystack or "post" in haystack:
            return "🏁"
        return "🏟️"

    if any(x in haystack for x in ["news", "article", "headline", "published"]):
        return "📰"

    if any(x in haystack for x in ["atproto", "bsky", "jetstream", "post", "topic"]):
        if "topic" in haystack or "tag" in haystack:
            return "#"
        return "🗨️"

    if any(x in haystack for x in ["finance", "market", "asset", "price"]):
        return "💹"

    if any(x in haystack for x in ["earthquake", "quake", "magnitude", "seismic"]):
        return "⛔"

    if any(x in haystack for x in ["location", "geo", "city", "country"]):
        return "📍"

    if is_reference_like(entity):
        return "↗"

    return "◆"


def format_number(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def metric_for(entity: Entity) -> str:
    lower_numbers = {key.lower(): (key, value) for key, value in entity.numbers.items()}

    home_key = next(
        (orig for key, (orig, _) in lower_numbers.items() if key.endswith("home_score") or key == "home_score"),
        None,
    )
    away_key = next(
        (orig for key, (orig, _) in lower_numbers.items() if key.endswith("away_score") or key == "away_score"),
        None,
    )

    if home_key and away_key:
        return f"{int(entity.numbers.get(away_key, 0))} - {int(entity.numbers.get(home_key, 0))}"

    preferred_substrings = [
        "temperature",
        "temp",
        "price",
        "score",
        "count",
        "magnitude",
        "population",
        "rank",
        "value",
        "time_us",
    ]

    ignored = {"lat", "lon", "lng", "latitude", "longitude"}

    for needle in preferred_substrings:
        for key, value in entity.numbers.items():
            key_l = key.lower()
            if any(key_l == x or key_l.endswith("." + x) for x in ignored):
                continue
            if needle in key_l:
                return f"{compact_name(key)} {format_number(value)}"

    for key, value in entity.numbers.items():
        key_l = key.lower()
        if any(key_l == x or key_l.endswith("." + x) for x in ignored):
            continue
        return f"{compact_name(key)} {format_number(value)}"

    when = newest_time(entity)
    if when:
        return time_label(when)

    return ""


def dedup(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)

    return out


def secondary_for(entity: Entity) -> str:
    parts: list[str] = []

    priority_keys = [
        "source",
        "region",
        "league",
        "sport",
        "status",
        "score_bug_mode",
        "kind",
        "index_name",
        "index_value",
        "entity_type",
        "published_day",
        "country_code",
        "condition",
    ]

    for key in priority_keys:
        for value in entity.facets.get(key, []):
            if value and len(value) <= 40:
                parts.append(value)

    for key, values in entity.facets.items():
        for value in values:
            if value and len(value) <= 40:
                parts.append(value)
            if len(parts) >= 4:
                return " • ".join(dedup(parts)[:3])

    if entity.entity_type:
        parts.append(entity.entity_type)

    return " • ".join(dedup(parts)[:3])


def frame_chyron(frame: Frame) -> str:
    if not frame.entities:
        return "NO GRAPH DATA AVAILABLE"

    lead = ranked_entities(frame.entities)[0]
    parts = [frame.title.upper(), lead.display.upper()]

    metric = metric_for(lead)
    secondary = secondary_for(lead)

    if metric:
        parts.append(metric.upper())

    if secondary:
        parts.append(secondary.upper())

    return " • ".join(parts[:4])


def frame_ticker(frame: Frame) -> str:
    if not frame.entities:
        return "No graph entities available."

    segments: list[str] = []

    for entity in ranked_entities(frame.entities):
        seg = f"{glyph_for(entity)} {entity.display.upper()}"

        metric = metric_for(entity)
        secondary = secondary_for(entity)

        if metric:
            seg += f": {metric}"

        if secondary:
            seg += f" ({secondary})"

        segments.append(seg)

    return "   •••   ".join(segments[:16])


def frame_rundown(active: Frame, frames: list[Frame]) -> list[Json]:
    return [
        {
            "id": frame.id,
            "title": frame.title,
            "subtitle": frame.subtitle,
            "basis": frame.basis,
            "active": frame.id == active.id,
            "count": len(frame.entities),
        }
        for frame in frames
    ]


def cards_html(frame: Frame) -> str:
    if not frame.entities:
        return """
        <section class="card-grid">
          <article class="card">
            <div class="symbol">∅</div>
            <div class="card-main">
              <div class="card-kicker">empty</div>
              <div class="card-title">No entities found</div>
              <div class="card-sub">The relay returned no matching records.</div>
            </div>
          </article>
        </section>
        """

    cards: list[str] = []

    for entity in ranked_entities(frame.entities)[:12]:
        chips: list[str] = []

        for key, values in list(entity.facets.items())[:7]:
            for value in values[:2]:
                chips.append(f"<span>{esc(compact_name(key))}: {esc(value)}</span>")
                if len(chips) >= 8:
                    break
            if len(chips) >= 8:
                break

        for key, value in list(entity.numbers.items())[:4]:
            chips.append(f"<span>{esc(compact_name(key))}: {esc(format_number(value))}</span>")
            if len(chips) >= 10:
                break

        refs_count = sum(len(values) for values in entity.refs.values())
        if refs_count:
            chips.append(f"<span>{refs_count} refs</span>")

        when = newest_time(entity)
        if when:
            chips.append(f"<span>{esc(time_label(when))}</span>")

        if entity.matched_by and entity.matched_by.get("terms"):
            terms = entity.matched_by.get("terms") or []
            if terms:
                chips.append(f"<span>matched {esc(str(terms[0]))}</span>")

        cards.append(f"""
          <article class="card">
            <div class="symbol">{esc(glyph_for(entity))}</div>
            <div class="card-main">
              <div class="card-kicker">{esc(entity.entity_type)}</div>
              <div class="card-title">{esc(entity.display)}</div>
              <div class="card-sub">{esc(secondary_for(entity))}</div>
              <div class="card-path">{esc(entity.canonical_path)}</div>
            </div>
            <div class="card-metric">{esc(metric_for(entity))}</div>
            <div class="chips">{''.join(chips)}</div>
          </article>
        """)

    return f"<section class='card-grid'>{''.join(cards)}</section>"


# =============================================================================
# Payload computation
# =============================================================================

def compute_active_state(relay: Relay, params: dict[str, list[str]], cadence: float) -> tuple[QueryState, Json, list[Channel], Channel | None]:
    """
    If query/source params are present, use them.

    If the URL has no user query, auto-select a discovered channel from the broad graph.
    """
    broad_doc = relay.query_entities([
        ("include", "facets,refs,numbers,times,cells"),
        ("limit", str(DEFAULT_LIMIT)),
        ("sort", "score"),
    ])
    broad_entities = entities_from_doc(broad_doc)
    broad_filters = filters_from_doc(broad_doc)
    broad_cap = analyze(broad_entities, broad_filters)
    channels = generate_channels(broad_cap)

    selected_channel: Channel | None = None

    if not has_user_query(params):
        selected_channel = select_auto_channel(channels, cadence)
        if selected_channel:
            selected_params = params_from_pairs(selected_channel.query_params)
            for key in CONTROL_PARAMS:
                if key in params and key not in {"source", "query"}:
                    selected_params[key] = params[key]
            params = selected_params

    state, query_meta = load_query_state(relay, params)

    return state, query_meta, channels, selected_channel


def compute_payload(params: dict[str, list[str]]) -> Json:
    relay = Relay(HYPER_URL)
    cadence = cadence_from_params(params)

    state, query_meta, channels, selected_channel = compute_active_state(relay, params, cadence)

    cap = analyze(state.entities, state.filters)
    profile = infer_profile(cap, state, params, query_meta)
    frames = infer_frames(cap, profile)

    frame_raw = (params.get("frame") or [None])[0]

    if frame_raw is not None:
        frame_index = int(frame_raw)
    else:
        frame_index = int(time.time() // cadence)

    frame = frames[frame_index % len(frames)]

    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "relay": HYPER_URL,
        "cadence_seconds": cadence,
        "auto_channel": channel_json(selected_channel) if selected_channel else None,
        "query": {
            "label": state.label,
            "source_url": state.source_url,
            "request_pairs": state.request_pairs,
            "has_user_query": has_user_query(params),
            "query_meta": query_meta,
        },
        "profile": {
            "title": profile.title,
            "subtitle": profile.subtitle,
            "badge": profile.badge,
            "accent": profile.accent,
            "primary_facets": profile.primary_facets,
            "primary_numbers": profile.primary_numbers,
            "primary_times": profile.primary_times,
        },
        "channels": [channel_json(channel) for channel in channels],
        "capabilities": {
            "entity_count": len(state.entities),
            "types": dict(cap.type_counts.most_common(16)),
            "paths": dict(cap.path_counts.most_common(16)),
            "facets": {
                key: dict(counter.most_common(10))
                for key, counter in list(cap.facet_counts.items())[:16]
            },
            "numbers": dict(cap.number_counts.most_common(16)),
            "times": dict(cap.time_counts.most_common(16)),
            "refs": dict(cap.ref_counts.most_common(16)),
        },
        "frame_index": frame_index,
        "frame_count": len(frames),
        "frame": {
            "id": frame.id,
            "title": frame.title,
            "subtitle": frame.subtitle,
            "reason": frame.reason,
            "basis": frame.basis,
            "count": len(frame.entities),
        },
        "tickerText": frame_ticker(frame),
        "chyronText": frame_chyron(frame),
        "rundownStories": frame_rundown(frame, frames),
        "cardsHtml": cards_html(frame),
    }


# =============================================================================
# Browser runtime
# =============================================================================

INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Generic Hypergraph Display</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {
      --accent: #a78bfa;
      --bg: #020617;
      --panel: rgba(15, 23, 42, 0.76);
      --border: rgba(148, 163, 184, 0.22);
      --text: #f8fafc;
      --muted: #94a3b8;
    }

    * { box-sizing: border-box; }

    html, body {
      width: 100%;
      height: 100%;
      margin: 0;
      overflow: hidden;
      background:
        radial-gradient(circle at 20% 8%, color-mix(in srgb, var(--accent) 24%, transparent), transparent 30%),
        radial-gradient(circle at 88% 18%, rgba(255,255,255,0.08), transparent 24%),
        linear-gradient(135deg, #020617 0%, #0f172a 58%, #111827 100%);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    .shell {
      display: grid;
      grid-template-columns: 1fr 26vw;
      grid-template-rows: 1fr auto auto;
      width: 100vw;
      height: 100vh;
    }

    .main {
      grid-column: 1;
      grid-row: 1;
      padding: 22px;
      overflow: hidden;
    }

    .sidebar {
      grid-column: 2;
      grid-row: 1 / 4;
      position: relative;
      padding: 18px;
      overflow: hidden;
      border-left: 1px solid var(--border);
      background: linear-gradient(180deg, rgba(0,0,0,0.55), rgba(15,23,42,0.68));
    }

    .chyron {
      grid-column: 1;
      grid-row: 2;
      min-height: 16vh;
      display: grid;
      grid-template-columns: 22% 1fr;
      border-top: 1px solid rgba(255,255,255,0.16);
      background: linear-gradient(90deg, #020617 0%, #0f172a 55%, color-mix(in srgb, var(--accent) 30%, #020617) 100%);
    }

    .ticker {
      grid-column: 1;
      grid-row: 3;
      height: 4.5vh;
      min-height: 34px;
      background: black;
      overflow: hidden;
      position: relative;
      border-top: 1px solid rgba(255,255,255,0.08);
    }

    .brand {
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--accent);
      font-size: clamp(28px, 4vw, 58px);
      font-weight: 950;
      letter-spacing: -0.06em;
      border-right: 1px solid rgba(255,255,255,0.16);
      text-shadow: 0 0 22px color-mix(in srgb, var(--accent) 55%, transparent);
    }

    .chyron-text {
      display: flex;
      align-items: center;
      padding: 0 34px;
      font-size: clamp(27px, 4.2vw, 76px);
      font-weight: 950;
      line-height: 1.05;
      letter-spacing: -0.055em;
      text-transform: uppercase;
    }

    .ticker-text {
      position: absolute;
      left: 100vw;
      top: 50%;
      transform: translateY(-50%);
      white-space: nowrap;
      font-size: 18px;
      font-weight: 650;
      animation: marquee var(--ticker-duration, 55s) linear infinite;
    }

    @keyframes marquee {
      from { transform: translate(0, -50%); }
      to { transform: translate(calc(-100vw - var(--ticker-width, 1800px)), -50%); }
    }

    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      padding-bottom: 16px;
      margin-bottom: 18px;
      border-bottom: 1px solid var(--border);
    }

    .title {
      font-size: clamp(36px, 5vw, 86px);
      line-height: .95;
      font-weight: 950;
      letter-spacing: -0.075em;
      text-transform: uppercase;
    }

    .meta {
      max-width: 42%;
      text-align: right;
      color: color-mix(in srgb, var(--accent) 74%, white);
      font-size: 13px;
      letter-spacing: .14em;
      text-transform: uppercase;
    }

    .reason {
      margin-top: -8px;
      margin-bottom: 18px;
      color: var(--muted);
      font-size: 13px;
      letter-spacing: .04em;
    }

    .cards-host {
      height: calc(100% - 132px);
      overflow: hidden;
    }

    .card-grid {
      height: 100%;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(310px, 1fr));
      gap: 14px;
      align-content: start;
      overflow: hidden;
    }

    .card {
      min-height: 144px;
      display: grid;
      grid-template-columns: 56px minmax(0, 1fr) auto;
      grid-template-rows: 1fr auto;
      gap: 10px 14px;
      padding: 18px;
      border: 1px solid var(--border);
      border-radius: 20px;
      background:
        linear-gradient(180deg, rgba(255,255,255,.055), rgba(255,255,255,.018)),
        var(--panel);
      box-shadow: 0 24px 50px rgba(0,0,0,.32);
      overflow: hidden;
    }

    .symbol {
      font-size: 38px;
      filter: drop-shadow(0 0 18px color-mix(in srgb, var(--accent) 55%, transparent));
    }

    .card-kicker {
      color: color-mix(in srgb, var(--accent) 75%, white);
      font-size: 11px;
      font-weight: 900;
      letter-spacing: .16em;
      text-transform: uppercase;
    }

    .card-title {
      margin-top: 4px;
      color: white;
      font-size: 25px;
      font-weight: 950;
      line-height: 1.02;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .card-sub {
      margin-top: 8px;
      color: #cbd5e1;
      font-size: 13px;
      font-weight: 750;
      letter-spacing: .08em;
      text-transform: uppercase;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .card-path {
      margin-top: 7px;
      color: #64748b;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 10px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .card-metric {
      color: white;
      font-size: 27px;
      font-weight: 950;
      white-space: nowrap;
      text-align: right;
    }

    .chips {
      grid-column: 2 / 4;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      max-height: 26px;
      overflow: hidden;
    }

    .chips span {
      color: #cbd5e1;
      background: rgba(255,255,255,.06);
      border: 1px solid rgba(255,255,255,.08);
      border-radius: 999px;
      padding: 4px 8px;
      font-size: 11px;
    }

    .panel-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--border);
      font-weight: 900;
      letter-spacing: .2em;
      font-size: 13px;
    }

    .dot {
      display: inline-block;
      width: 9px;
      height: 9px;
      margin-right: 8px;
      background: var(--accent);
      box-shadow: 0 0 14px var(--accent);
      animation: pulse 1.4s infinite;
    }

    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.25} }

    .rundown-wrap {
      max-height: 46%;
      overflow: hidden;
    }

    .rundown-item {
      display: grid;
      grid-template-columns: 34px 1fr;
      gap: 10px;
      padding: 10px 0;
      border-bottom: 1px solid rgba(255,255,255,.07);
      opacity: .64;
    }

    .rundown-item.active {
      opacity: 1;
      padding-left: 10px;
      border-left: 3px solid var(--accent);
      background: linear-gradient(90deg, color-mix(in srgb, var(--accent) 14%, transparent), transparent);
    }

    .idx { color: #64748b; font-size: 12px; font-weight: 900; }
    .rt { color: white; font-size: 14px; font-weight: 900; text-transform: uppercase; line-height: 1.1; }
    .rs { margin-top: 4px; color: #94a3b8; font-size: 11px; line-height: 1.2; }

    .channels-header {
      margin-top: 18px;
    }

    .channels-wrap {
      max-height: 42%;
      overflow: hidden;
    }

    .channel-link {
      display: block;
      color: #cbd5e1;
      text-decoration: none;
      padding: 8px 0;
      border-bottom: 1px solid rgba(255,255,255,.06);
      opacity: .72;
    }

    .channel-link:hover {
      opacity: 1;
      color: white;
    }

    .channel-title {
      font-size: 12px;
      font-weight: 900;
      line-height: 1.1;
      text-transform: uppercase;
    }

    .channel-sub {
      margin-top: 3px;
      font-size: 11px;
      color: #64748b;
    }

    .debug {
      position: absolute;
      right: 12px;
      bottom: 8px;
      color: #475569;
      font: 11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
  </style>
</head>

<body>
  <div class="shell">
    <main class="main">
      <div class="topbar">
        <div id="title" class="title">Hypergraph Cadence</div>
        <div id="meta" class="meta">Booting computed display</div>
      </div>
      <div id="reason" class="reason">Waiting for relay state.</div>
      <div id="cards" class="cards-host"></div>
    </main>

    <aside class="sidebar">
      <div class="panel-header">
        <span><span class="dot"></span>LIVE RUNDOWN</span>
        <span id="clock">--:-- UTC</span>
      </div>
      <div id="rundown" class="rundown-wrap"></div>

      <div class="panel-header channels-header">
        <span>DISCOVERED CHANNELS</span>
        <span>AUTO</span>
      </div>
      <div id="channels" class="channels-wrap"></div>

      <div id="debug" class="debug"></div>
    </aside>

    <section class="chyron">
      <div id="badge" class="brand">HG</div>
      <div id="chyron" class="chyron-text">CONNECTING TO HYPERGRAPH</div>
    </section>

    <section class="ticker">
      <div id="ticker" class="ticker-text">Waiting for graph state...</div>
    </section>
  </div>

  <script>
    let cadenceMs = 8000;

    function esc(x) {
      return String(x ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[c]));
    }

    function updateClock() {
      document.getElementById('clock').textContent =
        new Date().toLocaleTimeString('en-GB', {
          timeZone: 'UTC',
          hour: '2-digit',
          minute: '2-digit'
        }) + ' UTC';
    }

    function setTicker(text) {
      const el = document.getElementById('ticker');
      el.textContent = text || '';

      requestAnimationFrame(() => {
        const w = el.scrollWidth || 1600;
        el.style.setProperty('--ticker-width', w + 'px');
        el.style.setProperty('--ticker-duration', Math.max(35, w / 70) + 's');
        el.style.animation = 'none';
        void el.offsetHeight;
        el.style.animation = '';
      });
    }

    function setRundown(stories, activeId) {
      document.getElementById('rundown').innerHTML = (stories || []).map((s, i) => `
        <div class="rundown-item ${s.id === activeId ? 'active' : ''}">
          <div class="idx">${String(i + 1).padStart(2, '0')}</div>
          <div>
            <div class="rt">${esc(s.title)}</div>
            <div class="rs">${esc(s.subtitle || '')}</div>
          </div>
        </div>
      `).join('');
    }

    function setChannels(channels) {
      const host = document.getElementById('channels');

      host.innerHTML = (channels || []).slice(0, 18).map(c => `
        <a class="channel-link" href="${esc(c.href)}">
          <div class="channel-title">${esc(c.title)}</div>
          <div class="channel-sub">${esc(c.subtitle || '')}</div>
        </a>
      `).join('');
    }

    async function loadFrame() {
      const res = await fetch('/api/frame' + window.location.search, { cache: 'no-store' });
      const p = await res.json();

      if (!p.ok) {
        throw new Error(p.error || p.message || 'frame error');
      }

      cadenceMs = Math.max(2000, Number(p.cadence_seconds || 8) * 1000);

      document.documentElement.style.setProperty('--accent', p.profile?.accent || '#a78bfa');

      document.getElementById('badge').textContent = p.profile?.badge || 'HG';
      document.getElementById('title').textContent = p.frame?.title || p.profile?.title || 'Hypergraph Cadence';

      const auto = p.auto_channel ? `AUTO: ${p.auto_channel.title}` : p.query?.label || 'graph sample';
      document.getElementById('meta').textContent =
        `${auto} • frame ${(p.frame_index % p.frame_count) + 1}/${p.frame_count}`;

      document.getElementById('reason').textContent = p.frame?.reason || '';
      document.getElementById('cards').innerHTML = p.cardsHtml || '';
      document.getElementById('chyron').textContent = p.chyronText || '';
      document.getElementById('debug').textContent =
        `${p.generated_at} • ${p.frame?.basis || ''}`;

      setTicker(p.tickerText || '');
      setRundown(p.rundownStories || [], p.frame?.id);
      setChannels(p.channels || []);
    }

    async function safeLoad() {
      try {
        await loadFrame();
      } catch (e) {
        console.error(e);
        document.getElementById('title').textContent = 'Graph Display Error';
        document.getElementById('reason').textContent = e.message;
        document.getElementById('chyron').textContent = 'HYPERGRAPH DISPLAY UNAVAILABLE';
        setTicker('Unable to compute a display frame from the relay.');
      }
    }

    updateClock();
    setInterval(updateClock, 1000);

    async function loop() {
      await safeLoad();
      setTimeout(loop, cadenceMs);
    }

    loop();
  </script>
</body>
</html>
"""


# =============================================================================
# HTTP service
# =============================================================================

class Handler(BaseHTTPRequestHandler):
    server_version = "GenericHyperDisplay/0.3"

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
        params = urllib.parse.parse_qs(parsed.query)

        try:
            if parsed.path in {"/", "/index.html"}:
                return self.send_html(INDEX_HTML)

            if parsed.path == "/health":
                return self.send_json({
                    "ok": True,
                    "service": "generic_hyper_display",
                    "port": PORT,
                    "hyper_url": HYPER_URL,
                    "default_limit": DEFAULT_LIMIT,
                    "default_cadence_seconds": DEFAULT_CADENCE_SECONDS,
                })

            if parsed.path == "/api/frame":
                return self.send_json(compute_payload(params))

            if parsed.path == "/api/channels":
                relay = Relay(HYPER_URL)
                channels = discover_broad_channels(relay)
                return self.send_json({
                    "ok": True,
                    "channels": [channel_json(channel) for channel in channels],
                })

            if parsed.path == "/api/debug":
                relay = Relay(HYPER_URL)
                state, query_meta = load_query_state(relay, params)
                cap = analyze(state.entities, state.filters)
                profile = infer_profile(cap, state, params, query_meta)
                frames = infer_frames(cap, profile)
                return self.send_json({
                    "ok": True,
                    "state": {
                        "label": state.label,
                        "source_url": state.source_url,
                        "request_pairs": state.request_pairs,
                        "filters": state.filters,
                    },
                    "query_meta": query_meta,
                    "profile": profile.__dict__,
                    "frame_count": len(frames),
                    "frames": [
                        {
                            "id": f.id,
                            "title": f.title,
                            "basis": f.basis,
                            "count": len(f.entities),
                        }
                        for f in frames
                    ],
                    "capabilities": {
                        "entity_count": len(state.entities),
                        "types": dict(cap.type_counts.most_common(30)),
                        "paths": dict(cap.path_counts.most_common(30)),
                        "facets": {
                            key: dict(counter.most_common(12))
                            for key, counter in list(cap.facet_counts.items())[:30]
                        },
                        "numbers": dict(cap.number_counts.most_common(30)),
                        "times": dict(cap.time_counts.most_common(30)),
                        "refs": dict(cap.ref_counts.most_common(30)),
                    },
                    "sample": [
                        {
                            "entity_id": e.entity_id,
                            "entity_type": e.entity_type,
                            "canonical_path": e.canonical_path,
                            "display": e.display,
                            "content_score": content_score(e),
                            "facets": e.facets,
                            "numbers": e.numbers,
                            "times": e.times,
                            "refs": e.refs,
                        }
                        for e in ranked_entities(state.entities)[:15]
                    ],
                })

            return self.send_json({
                "ok": False,
                "error": "not_found",
                "path": parsed.path,
            }, status=404)

        except urllib.error.URLError as exc:
            return self.send_json({
                "ok": False,
                "error": "relay_unavailable",
                "message": str(exc),
                "hyper_url": HYPER_URL,
            }, status=502)

        except Exception as exc:
            return self.send_json({
                "ok": False,
                "error": type(exc).__name__,
                "message": str(exc),
            }, status=500)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    print(f"generic hypergraph display: http://127.0.0.1:{PORT}")
    print(f"relay:                      {HYPER_URL}")
    print()
    print("Try:")
    print(f"  http://127.0.0.1:{PORT}")
    print(f"  http://127.0.0.1:{PORT}?q=Trump")
    print(f"  http://127.0.0.1:{PORT}?type=news_article&q=Trump")
    print(f"  http://127.0.0.1:{PORT}?source=http%3A%2F%2F127.0.0.1%3A8765%2Fquery%2Fentities%3Fq%3DTrump")
    print(f"  http://127.0.0.1:{PORT}/api/debug?q=Trump")
    print()
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