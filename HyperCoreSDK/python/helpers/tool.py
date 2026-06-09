#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import json
import math
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Literal


DEFAULT_URL = "http://127.0.0.1:8765"
ORDER_CHOICES = ("key_asc", "key_desc", "updated_desc", "updated_asc")
SAMPLE_MODES = ("first", "reservoir", "random_page")

CONTROL_RELS: set[str] = {
    "self",
    "parent",
    "children",
    "next",
    "prev",
    "search",
    "stream",
    "changes_since",
}

StreamKind = Literal[
    "node",
    "skip",
    "error",
    "progress",
    "summary",
]


# ---------------------------------------------------------------------------
# Stats / policy / config
# ---------------------------------------------------------------------------

@dataclass
class Stats:
    nodes_seen: int = 0
    nodes_printed: int = 0
    node_docs_fetched: int = 0
    child_pages_fetched: int = 0
    search_requests: int = 0
    errors: int = 0
    skipped: int = 0
    filtered: int = 0
    capped: int = 0
    max_depth_seen: int = 0
    by_tag: dict[str, int] = field(default_factory=dict)

    def add_tag(self, tag: str) -> None:
        tag = tag or "unknown"
        self.by_tag[tag] = self.by_tag.get(tag, 0) + 1


@dataclass(frozen=True)
class BranchSample:
    limit: int
    mode: str = "first"
    seed: int | None = None

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ValueError("BranchSample.limit must be >= 0")
        if self.mode not in SAMPLE_MODES:
            raise ValueError(f"BranchSample.mode must be one of {SAMPLE_MODES!r}")


@dataclass(frozen=True)
class DataPredicate:
    field: str
    op: str
    raw_value: str

    def matches(self, body: dict[str, Any]) -> bool:
        actual = get_path_value(body, self.field)

        if self.op == "exists":
            return actual is not None

        expected = parse_scalar(self.raw_value)

        if self.op == "=":
            return actual == expected or str(actual) == self.raw_value

        if self.op == "!=":
            return not (actual == expected or str(actual) == self.raw_value)

        if self.op == "~=":
            return self.raw_value.lower() in str(actual or "").lower()

        if self.op in {">", ">=", "<", "<="}:
            left = number_value(actual)
            right = number_value(expected)

            if left is None or right is None:
                return False

            if self.op == ">":
                return left > right
            if self.op == ">=":
                return left >= right
            if self.op == "<":
                return left < right
            if self.op == "<=":
                return left <= right

        return False


@dataclass
class TraversalPolicy:
    """
    Generic traversal/filtering policy.

    This object intentionally knows nothing about database domains.
    It only knows paths, tags, caller-supplied field predicates,
    caller-supplied samples, and caller-supplied blacklist rules.
    """
    branch_samples: dict[str, BranchSample] = field(default_factory=dict)

    global_limit: int | None = None
    seed: int | None = None

    blacklisted_link_rels: set[str] = field(default_factory=set)
    blacklisted_path_prefixes: tuple[str, ...] = ()
    blacklisted_path_parts: set[str] = field(default_factory=set)

    include_paths: list[str] = field(default_factory=list)
    exclude_paths: list[str] = field(default_factory=list)
    include_tags: set[str] = field(default_factory=set)
    exclude_tags: set[str] = field(default_factory=set)
    where: list[DataPredicate] = field(default_factory=list)

    def sample_for(self, path: str) -> BranchSample | None:
        return self.branch_samples.get(normalize_path(path))


@dataclass(frozen=True)
class StreamConfig:
    per_page: int = 250
    order: str = "key_asc"
    max_depth: int | None = None

    show_details: bool = True
    show_fields: bool = True
    show_controls: bool = True
    show_actions: bool = True
    show_links: bool = False

    show_directories: bool = True
    show_records: bool = True
    show_system: bool = True
    show_directory_fields: bool = False

    max_field_count: int = 12
    progress_every: int = 1000
    emit_summary: bool = True

    # Number of concurrent node fetches per expansion. 1 == serial.
    workers: int = 8


@dataclass(frozen=True)
class StreamEvent:
    kind: StreamKind
    path: str
    depth: int = 0

    tag: str = "unknown"
    data_kind: str = "no-data"
    child_count: int = 0
    commit_seq: int | None = None

    controls: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()

    message: str = ""


@dataclass(frozen=True)
class ExtractConfig:
    """
    Generic selector-based extraction.

    Selectors:
      path
      doc
      state
      state.<field>
      links
      links.<rel>
      actions
      actions.<name>
      data
      data.<field>
      raw_data
      raw_data.<field>
      fields
      field_types
      tag
      data_kind
      child_count
      controls
      application_links

    output:
      objects -> list[dict]
      values  -> list[value] for one selector, otherwise list[list[value]]
      pairs   -> list[{"path": path, "selector": selector, "value": value}]
    """
    selectors: tuple[str, ...] = ("path", "data")
    output: str = "objects"
    include_nulls: bool = True
    flatten_single: bool = True


@dataclass(frozen=True)
class FieldExpectation:
    field: str
    required: bool = False
    non_null: bool = False
    expected_type: str | None = None


@dataclass(frozen=True)
class FieldAuditConfig:
    path: str
    sample: BranchSample | None = None
    per_page: int = 250
    order: str = "key_asc"
    expectations: tuple[FieldExpectation, ...] = ()
    predicates: tuple[DataPredicate, ...] = ()
    max_examples: int = 50


@dataclass
class FieldAuditResult:
    checked: int = 0
    ok: int = 0
    missing: int = 0
    null: int = 0
    wrong_type: int = 0
    predicate_failed: int = 0
    read_errors: int = 0
    empty_data: int = 0
    examples: list[str] = field(default_factory=list)

    def add_example(self, text: str, *, limit: int) -> None:
        if len(self.examples) < limit:
            self.examples.append(text)

    @property
    def failed(self) -> bool:
        return any(
            [
                self.missing,
                self.null,
                self.wrong_type,
                self.predicate_failed,
                self.read_errors,
                self.empty_data,
            ]
        )


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class HyperRelayClient:
    def __init__(self, base_url: str = DEFAULT_URL, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)

    def get_json(self, url: str, *, timeout: float | None = None) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "hypercore-hypermedia-tool/1.0",
            },
        )

        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            body = resp.read().decode("utf-8", "replace")

        return json.loads(body) if body else {}

    def ping(self) -> dict[str, Any]:
        return self.get_json(f"{self.base_url}/health", timeout=2.0)

    def node(
        self,
        path: str,
        *,
        per_page: int | None = None,
        order: str = "key_asc",
    ) -> dict[str, Any]:
        """
        Fetch a node document. The relay embeds the first page of children in
        `_embedded.children`, so passing `per_page` lets a single request return
        the node AND a wide page of its children — no separate /api/children call.
        """
        path = normalize_path(path)

        if path == "/":
            return self.get_json(with_query(f"{self.base_url}/", format="json"))

        params: dict[str, Any] = {"format": "json"}

        if per_page is not None:
            params["per_page"] = per_page
            params["order"] = order

        return self.get_json(
            with_query(
                f"{self.base_url}/{encoded_path(path)}",
                **params,
            )
        )

    def children_page(
        self,
        path: str,
        *,
        page: int,
        per_page: int,
        order: str,
    ) -> dict[str, Any]:
        path = normalize_path(path)

        if path == "/":
            return self.node("/")

        return self.get_json(
            with_query(
                f"{self.base_url}/{encoded_path(path)}/api/children",
                format="json",
                page=page,
                per_page=per_page,
                order=order,
            )
        )

    def search(
        self,
        *,
        q: str | None = None,
        type_: str | None = None,
        limit: int = 50,
        scope: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "format": "json",
            "limit": limit,
        }

        if q:
            params["q"] = q
        if type_:
            params["type"] = type_
        if scope:
            params["scope"] = scope

        for key, value in (extra or {}).items():
            params[key] = value

        return self.get_json(
            with_query(
                f"{self.base_url}/api/search",
                **params,
            )
        )


# ---------------------------------------------------------------------------
# Generic hypermedia document program
# ---------------------------------------------------------------------------

def with_query(url: str, **params: Any) -> str:
    parsed = urllib.parse.urlparse(url)
    query = dict(urllib.parse.parse_qsl(parsed.query))

    for key, value in params.items():
        if value is not None:
            query[key] = str(value)

    return urllib.parse.urlunparse(
        parsed._replace(query=urllib.parse.urlencode(query))
    )


def encoded_path(path: str) -> str:
    return urllib.parse.quote(
        normalize_path(path).strip(".").replace("/", "."),
        safe=".",
    )


def normalize_path(value: str | None) -> str:
    value = str(value or "").strip()

    if value in {"", ".", "/"}:
        return "/"

    return value.strip("/").strip(".").replace("/", ".")


def path_parts(path: str) -> list[str]:
    path = normalize_path(path)
    if path == "/":
        return []
    return [part for part in path.split(".") if part]


def parent_path(path: str) -> str:
    parts = path_parts(path)
    if len(parts) <= 1:
        return "/"
    return ".".join(parts[:-1])


def state(doc: Any) -> dict[str, Any]:
    return doc.get("_state", {}) if isinstance(doc, dict) else {}


def links(doc: Any) -> dict[str, Any]:
    value = doc.get("_links") if isinstance(doc, dict) else {}
    return value if isinstance(value, dict) else {}


def actions(doc: Any) -> dict[str, Any]:
    value = doc.get("_actions") if isinstance(doc, dict) else {}
    return value if isinstance(value, dict) else {}


def raw_data(doc: Any) -> Any:
    return doc.get("data") if isinstance(doc, dict) else None


def app_data(doc: Any) -> dict[str, Any]:
    value = raw_data(doc)

    if isinstance(value, dict) and isinstance(value.get("data"), dict):
        return value["data"]

    return value if isinstance(value, dict) else {}


def child_count(doc: dict[str, Any]) -> int:
    st = state(doc)

    for key in ("children_total", "child_count", "children_count"):
        try:
            value = st.get(key)
            if value is not None:
                return int(value)
        except Exception:
            pass

    return 0


def embedded_children(doc: Any, *, is_root: bool = False) -> dict[str, dict[str, Any]]:
    if not isinstance(doc, dict):
        return {}

    embedded = doc.get("_embedded")
    if not isinstance(embedded, dict):
        return {}

    if is_root:
        roots = embedded.get("roots")
        if isinstance(roots, dict):
            return {
                key: value
                for key, value in roots.items()
                if isinstance(value, dict)
            }

    children = embedded.get("children")
    if isinstance(children, dict):
        return {
            key: value
            for key, value in children.items()
            if isinstance(value, dict)
        }

    return {}


def doc_path(name: str, doc: dict[str, Any], current_path: str) -> str:
    st = state(doc)
    path = str(st.get("path") or "").strip()

    if path and path != "/":
        return normalize_path(path)

    current_path = normalize_path(current_path)

    if current_path == "/":
        return normalize_path(name)

    return normalize_path(f"{current_path}.{name.strip('.')}")


def tag_of(doc: dict[str, Any]) -> str:
    return str(state(doc).get("tag") or "unknown")


def data_kind(value: Any) -> str:
    if value is None:
        return "no-data"
    if isinstance(value, dict):
        if isinstance(value.get("data"), dict):
            return "wrapped-data"
        return "data"
    return type(value).__name__


def compact_mapping_keys(mapping: dict[str, Any], *, max_items: int = 12) -> str:
    keys = sorted(str(k) for k in mapping.keys())
    shown = keys[:max_items]
    suffix = f", +{len(keys) - max_items} more" if len(keys) > max_items else ""
    return ", ".join(shown) + suffix


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float" if math.isfinite(value) else "float_nonfinite"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def short_value(value: Any, *, limit: int = 80) -> str:
    text = repr(value)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def get_path_value(source: Any, dotted_path: str) -> Any:
    cur = source

    for part in str(dotted_path or "").split("."):
        part = part.strip()
        if not part:
            continue

        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None

    return cur


def parse_scalar(value: str) -> Any:
    text = str(value).strip()

    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    if text.lower() in {"none", "null"}:
        return None

    try:
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        if re.fullmatch(r"-?\d+\.\d+", text):
            return float(text)
    except Exception:
        pass

    return text


def number_value(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None

    if isinstance(value, (int, float)):
        if math.isfinite(float(value)):
            return float(value)
        return None

    if isinstance(value, str):
        text = value.strip().replace(",", "")
        try:
            return float(text)
        except ValueError:
            return None

    return None


def field_summary(body: dict[str, Any], *, max_fields: int = 12) -> tuple[str, ...]:
    if not body:
        return ()

    parts: list[str] = []

    for key in sorted(body.keys())[:max_fields]:
        value = body.get(key)
        parts.append(f"{key}:{type_name(value)}")

    if len(body) > max_fields:
        parts.append(f"+{len(body) - max_fields} fields")

    return tuple(parts)


# ---------------------------------------------------------------------------
# Predicate parsing
# ---------------------------------------------------------------------------

PREDICATE_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*(~=|!=|>=|<=|=|>|<)\s*(.*)$")


def parse_data_predicate(raw: str) -> DataPredicate:
    raw = str(raw or "").strip()

    if raw.endswith("?") and len(raw) > 1:
        return DataPredicate(field=raw[:-1], op="exists", raw_value="")

    m = PREDICATE_RE.match(raw)
    if not m:
        raise argparse.ArgumentTypeError(
            f"invalid predicate {raw!r}; expected field=value, field>=N, field~=text, or field?"
        )

    field_name, op, value = m.groups()
    return DataPredicate(field=field_name.strip(), op=op.strip(), raw_value=value.strip())


def parse_path_int_pairs(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}

    for raw in values:
        if "=" not in raw:
            raise argparse.ArgumentTypeError(f"expected PATH=N, got {raw!r}")

        path, n = raw.split("=", 1)
        path = normalize_path(path)

        try:
            count = int(n)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid count in {raw!r}") from exc

        if count < 0:
            raise argparse.ArgumentTypeError(f"count must be >= 0 in {raw!r}")

        out[path] = count

    return out


def parse_branch_samples(values: Iterable[str], *, mode: str, seed: int | None) -> dict[str, BranchSample]:
    pairs = parse_path_int_pairs(values)
    return {
        path: BranchSample(limit=count, mode=mode, seed=seed)
        for path, count in pairs.items()
    }


def parse_sample_specs(values: Iterable[str], *, seed: int | None) -> dict[str, BranchSample]:
    """
    Accept:
      PATH=N
      PATH=N:first
      PATH=N:reservoir
      PATH=N:random_page
    """
    out: dict[str, BranchSample] = {}

    for raw in values:
        if "=" not in raw:
            raise argparse.ArgumentTypeError(f"expected PATH=N[:MODE], got {raw!r}")

        path, right = raw.split("=", 1)
        mode = "first"

        if ":" in right:
            count_text, mode = right.rsplit(":", 1)
        else:
            count_text = right

        path = normalize_path(path)

        try:
            count = int(count_text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid count in {raw!r}") from exc

        if mode not in SAMPLE_MODES:
            raise argparse.ArgumentTypeError(f"invalid sample mode {mode!r}; expected one of {SAMPLE_MODES!r}")

        out[path] = BranchSample(limit=count, mode=mode, seed=seed)

    return out


def parse_search_params(values: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}

    for raw in values:
        if "=" not in raw:
            raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {raw!r}")

        key, value = raw.split("=", 1)
        key = key.strip()

        if not key:
            raise argparse.ArgumentTypeError(f"empty key in {raw!r}")

        out[key] = value.strip()

    return out


def parse_field_expectations(
    *,
    required: Iterable[str],
    non_null: Iterable[str],
    typed: Iterable[str],
) -> tuple[FieldExpectation, ...]:
    merged: dict[str, FieldExpectation] = {}

    def update(field_name: str, **kwargs: Any) -> None:
        old = merged.get(field_name, FieldExpectation(field=field_name))
        merged[field_name] = FieldExpectation(
            field=field_name,
            required=bool(kwargs.get("required", old.required)),
            non_null=bool(kwargs.get("non_null", old.non_null)),
            expected_type=kwargs.get("expected_type", old.expected_type),
        )

    for item in required:
        update(item, required=True)

    for item in non_null:
        update(item, required=True, non_null=True)

    for raw in typed:
        if ":" not in raw:
            raise argparse.ArgumentTypeError(f"expected FIELD:TYPE, got {raw!r}")
        field_name, expected_type = raw.split(":", 1)
        update(field_name.strip(), required=True, expected_type=expected_type.strip())

    return tuple(merged.values())


# ---------------------------------------------------------------------------
# Generic filters
# ---------------------------------------------------------------------------

def is_blacklisted_path(path: str, policy: TraversalPolicy) -> bool:
    path = normalize_path(path)

    if path == "/":
        return False

    for prefix in policy.blacklisted_path_prefixes:
        prefix = normalize_path(prefix)
        if prefix != "/" and (path == prefix or path.startswith(prefix + ".")):
            return True

    for part in path_parts(path):
        if part in policy.blacklisted_path_parts:
            return True

    return False


def path_included(path: str, policy: TraversalPolicy) -> bool:
    path = normalize_path(path)

    if policy.include_paths:
        if not any(fnmatch.fnmatch(path, pattern) for pattern in policy.include_paths):
            return False

    if policy.exclude_paths:
        if any(fnmatch.fnmatch(path, pattern) for pattern in policy.exclude_paths):
            return False

    return True


def doc_included(path: str, doc: dict[str, Any], policy: TraversalPolicy) -> bool:
    if not path_included(path, policy):
        return False

    tag = tag_of(doc)

    if policy.include_tags and tag not in policy.include_tags:
        return False

    if policy.exclude_tags and tag in policy.exclude_tags:
        return False

    if policy.where:
        body = app_data(doc)
        if not isinstance(body, dict):
            return False

        return all(pred.matches(body) for pred in policy.where)

    return True


def filtered_links(doc: dict[str, Any], policy: TraversalPolicy) -> dict[str, Any]:
    return {
        rel: href
        for rel, href in links(doc).items()
        if rel not in policy.blacklisted_link_rels
    }


def control_links(doc: dict[str, Any], policy: TraversalPolicy) -> dict[str, Any]:
    return {
        rel: href
        for rel, href in filtered_links(doc, policy).items()
        if rel in CONTROL_RELS
    }


def application_links(doc: dict[str, Any], policy: TraversalPolicy) -> dict[str, Any]:
    return {
        rel: href
        for rel, href in filtered_links(doc, policy).items()
        if rel not in CONTROL_RELS
    }


# ---------------------------------------------------------------------------
# Child traversal / sampling
#
# Two layers:
#   * iter_child_pages / direct_children — page-walking program that fetch
#     /api/children explicitly. Still used by profile/audit, which start from a
#     branch root they have not already fetched.
#   * _child_entries_stream / sample_child_entries — reuse a node doc that is
#     ALREADY in hand (its embedded page 1) and only paginate when there is
#     more than one page. Used by the hot walk path.
# ---------------------------------------------------------------------------

def iter_child_pages(
    client: HyperRelayClient,
    path: str,
    *,
    per_page: int,
    order: str,
    stats: Stats,
) -> Iterator[tuple[int, dict[str, Any], dict[str, dict[str, Any]]]]:
    path = normalize_path(path)
    page = 1

    while True:
        if path == "/":
            doc = client.node("/")
            stats.node_docs_fetched += 1
            children = embedded_children(doc, is_root=True)
            yield page, doc, children
            return

        doc = client.children_page(path, page=page, per_page=per_page, order=order)
        stats.child_pages_fetched += 1

        children = embedded_children(doc, is_root=False)
        yield page, doc, children

        st = state(doc)
        current_page = int(st.get("children_page") or page)
        num_pages = int(st.get("children_num_pages") or current_page)

        if current_page >= num_pages:
            return

        page = current_page + 1


def direct_children(
    client: HyperRelayClient,
    path: str,
    *,
    per_page: int,
    order: str,
    stats: Stats,
    policy: TraversalPolicy,
) -> list[tuple[str, dict[str, Any]]]:
    path = normalize_path(path)
    sample = policy.sample_for(path)

    if sample is None:
        return all_direct_children(
            client,
            path,
            per_page=per_page,
            order=order,
            stats=stats,
            policy=policy,
        )

    if sample.mode == "first":
        return first_direct_children(
            client,
            path,
            n=sample.limit,
            per_page=per_page,
            order=order,
            stats=stats,
            policy=policy,
        )

    if sample.mode == "reservoir":
        return reservoir_direct_children(
            client,
            path,
            n=sample.limit,
            per_page=per_page,
            order=order,
            stats=stats,
            policy=policy,
            seed=sample.seed,
        )

    if sample.mode == "random_page":
        return random_page_direct_children(
            client,
            path,
            n=sample.limit,
            per_page=per_page,
            order=order,
            stats=stats,
            policy=policy,
            seed=sample.seed,
        )

    raise ValueError(f"unknown sample mode: {sample.mode!r}")


def all_direct_children(
    client: HyperRelayClient,
    path: str,
    *,
    per_page: int,
    order: str,
    stats: Stats,
    policy: TraversalPolicy,
) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []

    for _, _, children in iter_child_pages(
        client,
        path,
        per_page=per_page,
        order=order,
        stats=stats,
    ):
        for name, child in children.items():
            child_path = doc_path(name, child, path)

            if is_blacklisted_path(child_path, policy):
                stats.skipped += 1
                continue

            out.append((name, child))

    return out


def first_direct_children(
    client: HyperRelayClient,
    path: str,
    *,
    n: int,
    per_page: int,
    order: str,
    stats: Stats,
    policy: TraversalPolicy,
) -> list[tuple[str, dict[str, Any]]]:
    if n <= 0:
        return []

    out: list[tuple[str, dict[str, Any]]] = []

    for _, _, children in iter_child_pages(
        client,
        path,
        per_page=per_page,
        order=order,
        stats=stats,
    ):
        for name, child in children.items():
            child_path = doc_path(name, child, path)

            if is_blacklisted_path(child_path, policy):
                stats.skipped += 1
                continue

            out.append((name, child))

            if len(out) >= n:
                stats.capped += 1
                return out

    return out


def reservoir_direct_children(
    client: HyperRelayClient,
    path: str,
    *,
    n: int,
    per_page: int,
    order: str,
    stats: Stats,
    policy: TraversalPolicy,
    seed: int | None,
) -> list[tuple[str, dict[str, Any]]]:
    if n <= 0:
        return []

    rng_seed = f"{seed}:{normalize_path(path)}" if seed is not None else None
    rng = random.Random(rng_seed)

    reservoir: list[tuple[str, dict[str, Any]]] = []
    seen = 0

    for _, _, children in iter_child_pages(
        client,
        path,
        per_page=per_page,
        order=order,
        stats=stats,
    ):
        for name, child in children.items():
            child_path = doc_path(name, child, path)

            if is_blacklisted_path(child_path, policy):
                stats.skipped += 1
                continue

            seen += 1
            item = (name, child)

            if len(reservoir) < n:
                reservoir.append(item)
                continue

            j = rng.randint(1, seen)
            if j <= n:
                reservoir[j - 1] = item

    stats.capped += max(0, seen - len(reservoir))
    reservoir.sort(key=lambda item: item[0])
    return reservoir


def random_page_direct_children(
    client: HyperRelayClient,
    path: str,
    *,
    n: int,
    per_page: int,
    order: str,
    stats: Stats,
    policy: TraversalPolicy,
    seed: int | None,
) -> list[tuple[str, dict[str, Any]]]:
    """
    Approximate random sampling by choosing random child pages.

    This is much cheaper than reservoir sampling for huge branches, but it is
    only page-random, not perfectly item-random.
    """
    path = normalize_path(path)

    if n <= 0:
        return []

    rng_seed = f"{seed}:{path}" if seed is not None else None
    rng = random.Random(rng_seed)

    if path == "/":
        all_items = all_direct_children(
            client,
            path,
            per_page=per_page,
            order=order,
            stats=stats,
            policy=policy,
        )
        rng.shuffle(all_items)
        stats.capped += max(0, len(all_items) - n)
        return sorted(all_items[:n], key=lambda item: item[0])

    first_doc = client.children_page(path, page=1, per_page=per_page, order=order)
    stats.child_pages_fetched += 1

    return _random_page_from_first_doc(
        client,
        path,
        first_doc=first_doc,
        n=n,
        per_page=per_page,
        order=order,
        stats=stats,
        policy=policy,
        rng=rng,
    )


def _random_page_from_first_doc(
    client: HyperRelayClient,
    path: str,
    *,
    first_doc: dict[str, Any],
    n: int,
    per_page: int,
    order: str,
    stats: Stats,
    policy: TraversalPolicy,
    rng: random.Random,
) -> list[tuple[str, dict[str, Any]]]:
    path = normalize_path(path)
    first_children = embedded_children(first_doc, is_root=(path == "/"))
    st = state(first_doc)
    num_pages = int(st.get("children_num_pages") or 1)

    items: list[tuple[str, dict[str, Any]]] = []

    def add_children(children_items: list[tuple[str, dict[str, Any]]]) -> None:
        for name, child in children_items:
            if len(items) >= n:
                return

            child_path = doc_path(name, child, path)

            if is_blacklisted_path(child_path, policy):
                stats.skipped += 1
                continue

            items.append((name, child))

    page_numbers = list(range(1, num_pages + 1))
    rng.shuffle(page_numbers)

    for page in page_numbers:
        if len(items) >= n:
            break

        if page == 1:
            children = first_children
        else:
            doc = client.children_page(path, page=page, per_page=per_page, order=order)
            stats.child_pages_fetched += 1
            children = embedded_children(doc, is_root=False)

        page_items = list(children.items())
        rng.shuffle(page_items)
        add_children(page_items)

    stats.capped += max(0, child_count(first_doc) - len(items))
    items.sort(key=lambda item: item[0])
    return items


def _child_entries_stream(
    client: HyperRelayClient,
    path: str,
    first_doc: dict[str, Any],
    *,
    per_page: int,
    order: str,
    stats: Stats,
    policy: TraversalPolicy,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """
    Stream (name, child_doc) reusing `first_doc`'s embedded page 1, paginating
    via /api/children ONLY when the node reports more than one page.
    """
    path = normalize_path(path)
    is_root = path == "/"

    for name, child in embedded_children(first_doc, is_root=is_root).items():
        if is_blacklisted_path(doc_path(name, child, path), policy):
            stats.skipped += 1
            continue
        yield name, child

    if is_root:
        return

    st = state(first_doc)
    try:
        num_pages = int(st.get("children_num_pages") or 1)
        cur = int(st.get("children_page") or 1)
    except Exception:
        num_pages, cur = 1, 1

    for page in range(cur + 1, num_pages + 1):
        cdoc = client.children_page(path, page=page, per_page=per_page, order=order)
        stats.child_pages_fetched += 1
        for name, child in embedded_children(cdoc, is_root=False).items():
            if is_blacklisted_path(doc_path(name, child, path), policy):
                stats.skipped += 1
                continue
            yield name, child


def sample_child_entries(
    client: HyperRelayClient,
    path: str,
    first_doc: dict[str, Any],
    *,
    policy: TraversalPolicy,
    config: StreamConfig,
    stats: Stats,
) -> list[tuple[str, dict[str, Any]]]:
    """
    Resolve the children to descend into, reusing the already-fetched `first_doc`.
    Honours the same first/reservoir/random_page sampling as direct_children.
    """
    path = normalize_path(path)
    sample = policy.sample_for(path)
    per_page = max(1, config.per_page)

    def stream() -> Iterator[tuple[str, dict[str, Any]]]:
        return _child_entries_stream(
            client,
            path,
            first_doc,
            per_page=per_page,
            order=config.order,
            stats=stats,
            policy=policy,
        )

    if sample is None:
        return list(stream())

    if sample.limit <= 0:
        return []

    if sample.mode == "first":
        out: list[tuple[str, dict[str, Any]]] = []
        for item in stream():
            out.append(item)
            if len(out) >= sample.limit:
                stats.capped += 1
                break
        return out

    if sample.mode == "reservoir":
        rng = random.Random(f"{sample.seed}:{path}" if sample.seed is not None else None)
        reservoir: list[tuple[str, dict[str, Any]]] = []
        seen = 0
        for item in stream():
            seen += 1
            if len(reservoir) < sample.limit:
                reservoir.append(item)
            else:
                j = rng.randint(1, seen)
                if j <= sample.limit:
                    reservoir[j - 1] = item
        stats.capped += max(0, seen - len(reservoir))
        reservoir.sort(key=lambda item: item[0])
        return reservoir

    if sample.mode == "random_page":
        rng = random.Random(f"{sample.seed}:{path}" if sample.seed is not None else None)
        return _random_page_from_first_doc(
            client,
            path,
            first_doc=first_doc,
            n=sample.limit,
            per_page=per_page,
            order=config.order,
            stats=stats,
            policy=policy,
            rng=rng,
        )

    raise ValueError(f"unknown sample mode: {sample.mode!r}")


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

def stream_doc_category(path: str, doc: dict[str, Any]) -> str:
    tag = tag_of(doc)

    if tag in {"system", "system_root"}:
        return "system"

    if raw_data(doc) is not None:
        return "record"

    return "directory"


def stream_should_emit(path: str, doc: dict[str, Any], *, config: StreamConfig) -> bool:
    category = stream_doc_category(path, doc)

    if category == "system" and not config.show_system:
        return False

    if category == "directory" and not config.show_directories:
        return False

    if category == "record" and not config.show_records:
        return False

    return True


def stream_fields(
    path: str,
    doc: dict[str, Any],
    *,
    config: StreamConfig,
) -> tuple[str, ...]:
    body = app_data(doc)

    if not body:
        return ()

    category = stream_doc_category(path, doc)

    if category == "directory" and not config.show_directory_fields:
        return ()

    return field_summary(body, max_fields=config.max_field_count)


def stream_event_from_doc(
    path: str,
    doc: dict[str, Any],
    *,
    depth: int,
    config: StreamConfig,
    policy: TraversalPolicy,
) -> StreamEvent:
    st = state(doc)

    controls: tuple[str, ...] = ()
    app_links: tuple[str, ...] = ()
    acts: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()

    if config.show_details:
        if config.show_controls:
            controls = tuple(sorted(control_links(doc, policy).keys()))

        if config.show_links:
            app_links = tuple(sorted(application_links(doc, policy).keys()))

        if config.show_actions:
            acts = tuple(sorted(actions(doc).keys()))

        if config.show_fields:
            fields = stream_fields(path, doc, config=config)

    commit = st.get("commit_seq")

    try:
        commit_seq = int(commit) if commit is not None else None
    except Exception:
        commit_seq = None

    return StreamEvent(
        kind="node",
        path=path,
        depth=depth,
        tag=tag_of(doc),
        data_kind=data_kind(raw_data(doc)),
        child_count=child_count(doc),
        commit_seq=commit_seq,
        controls=controls,
        links=app_links,
        actions=acts,
        fields=fields,
    )


# ---------------------------------------------------------------------------
# Core walk: one fetch per node, embedded children reused, parallel descent
# ---------------------------------------------------------------------------

@dataclass
class WalkItem:
    """A single traversal event carrying the node doc already in hand."""
    kind: StreamKind
    path: str
    depth: int = 0
    doc: dict[str, Any] | None = None
    included: bool = False
    message: str = ""


def walk_docs(
    client: HyperRelayClient,
    start: str = "/",
    *,
    policy: TraversalPolicy | None = None,
    config: StreamConfig | None = None,
    stats: Stats | None = None,
) -> Iterator[WalkItem]:
    """
    Depth-first walk that fetches each node EXACTLY once.

    The relay embeds the first page of children in every node doc, so the single
    node fetch also yields page 1 of its children — no separate /api/children
    round trip for that page. Sibling subtrees are fetched concurrently (bounded
    by config.workers); recursion order is preserved so output stays stable.

    Yields WalkItem objects (node/skip/error/progress/summary) carrying the doc,
    so downstream consumers never re-fetch.
    """
    policy = policy or TraversalPolicy()
    config = config or StreamConfig()
    stats = stats or Stats()

    visited: set[str] = set()
    stop = False
    pool = (
        ThreadPoolExecutor(max_workers=max(1, config.workers))
        if config.workers and config.workers > 1
        else None
    )

    def fetch(path: str) -> dict[str, Any]:
        doc = client.node(path, per_page=max(1, config.per_page), order=config.order)
        stats.node_docs_fetched += 1
        return doc

    def safe_fetch(path: str) -> tuple[dict[str, Any] | None, Exception | None]:
        try:
            return fetch(path), None
        except Exception as exc:  # noqa: BLE001
            return None, exc

    def rec(path: str, doc: dict[str, Any], depth: int) -> Iterator[WalkItem]:
        nonlocal stop

        if stop:
            return

        stats.nodes_seen += 1
        stats.add_tag(tag_of(doc))
        stats.max_depth_seen = max(stats.max_depth_seen, depth)

        included = (
            doc_included(path, doc, policy)
            and stream_should_emit(path, doc, config=config)
        )
        if included:
            stats.nodes_printed += 1
        else:
            stats.filtered += 1

        yield WalkItem(kind="node", path=path, depth=depth, doc=doc, included=included)

        if config.progress_every and stats.nodes_seen % config.progress_every == 0:
            yield WalkItem(
                kind="progress",
                path=path,
                depth=depth,
                message=(
                    f"seen={stats.nodes_seen} "
                    f"printed={stats.nodes_printed} "
                    f"node_docs={stats.node_docs_fetched} "
                    f"child_pages={stats.child_pages_fetched} "
                    f"errors={stats.errors}"
                ),
            )

        if policy.global_limit is not None and stats.nodes_printed >= policy.global_limit:
            stop = True
            return

        if config.max_depth is not None and depth >= config.max_depth:
            return

        try:
            entries = sample_child_entries(
                client, path, doc, policy=policy, config=config, stats=stats
            )
        except Exception as exc:  # noqa: BLE001
            stats.errors += 1
            yield WalkItem(kind="error", path=path, depth=depth, message=f"children failed: {exc}")
            return

        targets: list[str] = []
        for name, child in entries:
            cp = doc_path(name, child, path)
            if cp in visited:
                continue
            if is_blacklisted_path(cp, policy):
                stats.skipped += 1
                continue
            visited.add(cp)
            targets.append(cp)

        if not targets:
            return

        fetched = (
            list(pool.map(safe_fetch, targets))
            if pool is not None
            else [safe_fetch(p) for p in targets]
        )

        for cp, (cdoc, err) in zip(targets, fetched):
            if stop:
                return
            if err is not None or cdoc is None:
                stats.errors += 1
                yield WalkItem(kind="error", path=cp, depth=depth + 1, message=str(err))
                continue
            yield from rec(cp, cdoc, depth + 1)

    start = normalize_path(start)
    visited.add(start)
    try:
        if is_blacklisted_path(start, policy):
            stats.skipped += 1
            yield WalkItem(kind="skip", path=start, depth=0, message="blacklisted")
        else:
            root_doc, err = safe_fetch(start)
            if err is not None or root_doc is None:
                stats.errors += 1
                yield WalkItem(kind="error", path=start, depth=0, message=str(err))
            else:
                yield from rec(start, root_doc, 0)
    finally:
        if pool is not None:
            pool.shutdown(wait=True)

    if config.emit_summary:
        tag_summary = ", ".join(
            f"{tag}:{count}"
            for tag, count in sorted(stats.by_tag.items(), key=lambda x: (-x[1], x[0]))
        )
        yield WalkItem(
            kind="summary",
            path=start,
            message=(
                f"seen={stats.nodes_seen} "
                f"printed={stats.nodes_printed} "
                f"node_docs={stats.node_docs_fetched} "
                f"child_pages={stats.child_pages_fetched} "
                f"search_requests={stats.search_requests} "
                f"skipped={stats.skipped} "
                f"filtered={stats.filtered} "
                f"capped={stats.capped} "
                f"errors={stats.errors} "
                f"max_depth={stats.max_depth_seen} "
                f"tags=[{tag_summary}]"
            ),
        )


def walk_graph_stream(
    client: HyperRelayClient,
    start: str = "/",
    *,
    policy: TraversalPolicy | None = None,
    config: StreamConfig | None = None,
    stats: Stats | None = None,
) -> Iterator[StreamEvent]:
    """Adapt walk_docs() into StreamEvents without any additional fetch."""
    policy = policy or TraversalPolicy()
    config = config or StreamConfig()
    stats = stats or Stats()

    for item in walk_docs(client, start, policy=policy, config=config, stats=stats):
        if item.kind == "node":
            if item.included and item.doc is not None:
                yield stream_event_from_doc(
                    item.path,
                    item.doc,
                    depth=item.depth,
                    config=config,
                    policy=policy,
                )
            continue

        yield StreamEvent(
            kind=item.kind,
            path=item.path,
            depth=item.depth,
            message=item.message,
        )


def render_stream(events: Iterator[StreamEvent], *, flat: bool = False) -> Iterator[str]:
    for event in events:
        if event.kind == "progress":
            yield f"# progress {event.message}"
            continue

        if event.kind == "summary":
            yield ""
            yield f"# summary {event.message}"
            continue

        if event.kind == "skip":
            continue

        indent = "" if flat else "  " * event.depth

        if event.kind == "error":
            yield f"{indent}# ERROR {event.path}: {event.message}"
            continue

        marker = "DATA" if event.data_kind != "no-data" else "NODE"

        bits = [event.tag, event.data_kind]

        if event.child_count:
            bits.append(f"children={event.child_count}")

        if event.commit_seq is not None:
            bits.append(f"commit={event.commit_seq}")

        yield f"{indent}{marker} {event.path}  [{', '.join(bits)}]"

        detail_indent = "  " if flat else indent + "  "

        if event.controls:
            yield f"{detail_indent}controls: {', '.join(event.controls)}"

        if event.links:
            yield f"{detail_indent}links: {', '.join(event.links)}"

        if event.actions:
            yield f"{detail_indent}actions: {', '.join(event.actions)}"

        if event.fields:
            yield f"{detail_indent}fields: {', '.join(event.fields)}"


# ---------------------------------------------------------------------------
# Generic extraction / collection
# ---------------------------------------------------------------------------

def extract_selector(path: str, doc: dict[str, Any], selector: str) -> Any:
    selector = str(selector or "").strip()

    if selector in {"", "."}:
        return doc

    if selector == "path":
        return path

    if selector == "doc":
        return doc

    if selector == "state":
        return state(doc)

    if selector.startswith("state."):
        return get_path_value(state(doc), selector.removeprefix("state."))

    if selector == "links":
        return links(doc)

    if selector.startswith("links."):
        return get_path_value(links(doc), selector.removeprefix("links."))

    if selector == "actions":
        return actions(doc)

    if selector.startswith("actions."):
        return get_path_value(actions(doc), selector.removeprefix("actions."))

    if selector == "data":
        return app_data(doc)

    if selector.startswith("data."):
        return get_path_value(app_data(doc), selector.removeprefix("data."))

    if selector == "raw_data":
        return raw_data(doc)

    if selector.startswith("raw_data."):
        raw = raw_data(doc)
        return get_path_value(raw, selector.removeprefix("raw_data."))

    if selector == "fields":
        body = app_data(doc)
        if isinstance(body, dict):
            return sorted(body.keys())
        return []

    if selector == "field_types":
        body = app_data(doc)
        if isinstance(body, dict):
            return {key: type_name(value) for key, value in sorted(body.items())}
        return {}

    if selector == "tag":
        return tag_of(doc)

    if selector == "data_kind":
        return data_kind(raw_data(doc))

    if selector == "child_count":
        return child_count(doc)

    if selector == "controls":
        return sorted(rel for rel in links(doc).keys() if rel in CONTROL_RELS)

    if selector == "application_links":
        return sorted(rel for rel in links(doc).keys() if rel not in CONTROL_RELS)

    raise KeyError(f"unknown selector: {selector!r}")


def extract_record(
    *,
    path: str,
    doc: dict[str, Any],
    config: ExtractConfig,
) -> Any:
    selectors = tuple(config.selectors or ("path", "data"))

    values = {
        selector: extract_selector(path, doc, selector)
        for selector in selectors
    }

    if not config.include_nulls:
        values = {
            key: value
            for key, value in values.items()
            if value is not None
        }

    if config.output == "objects":
        return values

    if config.output == "values":
        ordered = [values.get(selector) for selector in selectors]

        if config.flatten_single and len(ordered) == 1:
            return ordered[0]

        return ordered

    if config.output == "pairs":
        if len(selectors) != 1:
            return {
                "path": path,
                "values": values,
            }

        selector = selectors[0]
        return {
            "path": path,
            "selector": selector,
            "value": values.get(selector),
        }

    raise ValueError(f"unknown ExtractConfig.output: {config.output!r}")


def collect_graph(
    client: HyperRelayClient,
    start: str = "/",
    *,
    policy: TraversalPolicy | None = None,
    stream_config: StreamConfig | None = None,
    extract_config: ExtractConfig | None = None,
    stats: Stats | None = None,
) -> tuple[list[Any], Stats]:
    """
    Collect selected values. Extraction reuses the doc the walk already fetched,
    so each node is fetched exactly once for the whole collect.
    """
    policy = policy or TraversalPolicy()
    stream_config = stream_config or StreamConfig()
    extract_config = extract_config or ExtractConfig()
    stats = stats or Stats()

    out: list[Any] = []

    for item in walk_docs(
        client,
        start,
        policy=policy,
        config=stream_config,
        stats=stats,
    ):
        if item.kind == "error":
            out.append({"path": item.path, "error": item.message})
            continue

        if item.kind != "node" or not item.included or item.doc is None:
            continue

        out.append(
            extract_record(
                path=item.path,
                doc=item.doc,
                config=extract_config,
            )
        )

    return out, stats


def print_json_array(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


# ---------------------------------------------------------------------------
# Printing walk wrapper
# ---------------------------------------------------------------------------

def walk_graph(
    client: HyperRelayClient,
    start: str = "/",
    *,
    per_page: int = 250,
    order: str = "key_asc",
    max_depth: int | None = None,
    show_details: bool = False,
    show_fields: bool = False,
    print_every: int = 1000,
    workers: int = 8,
    policy: TraversalPolicy | None = None,
    stats: Stats | None = None,
) -> Stats:
    policy = policy or TraversalPolicy()
    stats = stats or Stats()

    config = StreamConfig(
        per_page=per_page,
        order=order,
        max_depth=max_depth,
        show_details=show_details,
        show_fields=show_fields,
        show_controls=True,
        show_actions=True,
        show_links=False,
        show_system=True,
        progress_every=print_every,
        emit_summary=False,
        workers=workers,
    )

    for line in render_stream(
        walk_graph_stream(
            client,
            start,
            policy=policy,
            config=config,
            stats=stats,
        )
    ):
        print(line)

    return stats


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def extract_search_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    rows = payload.get("rows")
    if isinstance(rows, list):
        return [x for x in rows if isinstance(x, dict)]

    data = payload.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    embedded = payload.get("_embedded")
    if isinstance(embedded, dict):
        for key in ("results", "items", "entities", "children"):
            value = embedded.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict):
                return [x for x in value.values() if isinstance(x, dict)]

    results = payload.get("results")
    if isinstance(results, list):
        return [x for x in results if isinstance(x, dict)]

    return []


def result_path(item: dict[str, Any]) -> str:
    st = state(item)

    for key in ("path", "canonical_path", "entity_id", "record_dot", "record_path"):
        value = st.get(key) if key in st else item.get(key)
        if value:
            return normalize_path(str(value))

    return ""


def search_graph(
    client: HyperRelayClient,
    *,
    q: str | None,
    type_: str | None = None,
    scope: str | None = None,
    limit: int = 50,
    extra: dict[str, str] | None = None,
    hydrate: bool = False,
    show_details: bool = False,
    show_fields: bool = False,
    policy: TraversalPolicy | None = None,
    stats: Stats | None = None,
) -> Stats:
    policy = policy or TraversalPolicy()
    stats = stats or Stats()

    try:
        payload = client.search(q=q, type_=type_, limit=limit, scope=scope, extra=extra)
        stats.search_requests += 1
    except Exception as exc:
        stats.errors += 1
        print(f"# ERROR search failed: {exc}", file=sys.stderr)
        return stats

    items = extract_search_items(payload)

    if not items:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return stats

    config = StreamConfig(
        show_details=show_details,
        show_fields=show_fields,
        show_controls=True,
        show_actions=True,
        show_links=False,
        emit_summary=False,
    )

    for item in items:
        path = result_path(item)

        if not path:
            print(json.dumps(item, ensure_ascii=False))
            continue

        if is_blacklisted_path(path, policy):
            stats.skipped += 1
            continue

        doc = item

        if hydrate:
            try:
                doc = client.node(path)
                stats.node_docs_fetched += 1
            except Exception as exc:
                stats.errors += 1
                print(f"# ERROR hydrating {path}: {exc}", file=sys.stderr)
                continue

        stats.nodes_seen += 1
        stats.add_tag(tag_of(doc))

        if not doc_included(path, doc, policy):
            stats.filtered += 1
            continue

        event = stream_event_from_doc(
            path,
            doc,
            depth=0,
            config=config,
            policy=policy,
        )

        for line in render_stream(iter([event])):
            print(line)

        stats.nodes_printed += 1

    return stats


# ---------------------------------------------------------------------------
# Generic profiling
# ---------------------------------------------------------------------------

def flatten_schema(
    value: Any,
    *,
    prefix: str = "",
    out: dict[str, Counter[str]] | None = None,
    max_depth: int = 2,
    depth: int = 0,
) -> dict[str, Counter[str]]:
    if out is None:
        out = defaultdict(Counter)

    if not isinstance(value, dict) or depth >= max_depth:
        if prefix:
            out[prefix][type_name(value)] += 1
        return out

    for key, child in value.items():
        child_path = f"{prefix}.{key}" if prefix else str(key)

        if isinstance(child, dict) and depth + 1 < max_depth:
            flatten_schema(
                child,
                prefix=child_path,
                out=out,
                max_depth=max_depth,
                depth=depth + 1,
            )
        else:
            out[child_path][type_name(child)] += 1

    return out


def profile_graph(
    client: HyperRelayClient,
    path: str,
    *,
    sample: BranchSample | None = None,
    per_page: int = 250,
    order: str = "key_asc",
    policy: TraversalPolicy | None = None,
    stats: Stats | None = None,
) -> Stats:
    policy = policy or TraversalPolicy()
    stats = stats or Stats()

    path = normalize_path(path)

    if sample is not None:
        policy.branch_samples[path] = sample

    try:
        doc = client.node(path)
        stats.node_docs_fetched += 1
    except Exception as exc:
        stats.errors += 1
        print(f"# ERROR reading {path}: {exc}", file=sys.stderr)
        return stats

    print(path)
    print(f"  tag: {tag_of(doc)}")
    print(f"  children: {child_count(doc)}")
    print(f"  has_data: {raw_data(doc) is not None}")
    print(f"  controls: {compact_mapping_keys(control_links(doc, policy)) if control_links(doc, policy) else '-'}")
    print(f"  actions: {compact_mapping_keys(actions(doc)) if actions(doc) else '-'}")

    children = direct_children(
        client,
        path,
        per_page=per_page,
        order=order,
        stats=stats,
        policy=policy,
    )

    schema: dict[str, Counter[str]] = defaultdict(Counter)
    examples: dict[str, list[str]] = defaultdict(list)
    records = 0

    for name, child in children:
        child_path = doc_path(name, child, path)

        try:
            full_doc = client.node(child_path)
            stats.node_docs_fetched += 1
        except Exception as exc:
            stats.errors += 1
            print(f"# ERROR reading {child_path}: {exc}", file=sys.stderr)
            continue

        if not doc_included(child_path, full_doc, policy):
            stats.filtered += 1
            continue

        body = app_data(full_doc)
        if not body:
            continue

        records += 1
        item_schema = flatten_schema(body)

        for field_name, counts in item_schema.items():
            schema[field_name].update(counts)

            if len(examples[field_name]) < 3:
                value = get_path_value(body, field_name)
                examples[field_name].append(short_value(value))

    print(f"  sampled_records: {records}")
    print("  fields:")
    for field_name in sorted(schema):
        counts = schema[field_name]
        rendered = ", ".join(f"{kind}:{count}" for kind, count in counts.most_common())
        print(f"    {field_name}: {rendered}")

    print("  examples:")
    for field_name in sorted(examples):
        print(f"    {field_name}: {', '.join(examples[field_name])}")

    return stats


# ---------------------------------------------------------------------------
# Generic field audit
# ---------------------------------------------------------------------------

def audit_fields(
    client: HyperRelayClient,
    *,
    config: FieldAuditConfig,
    policy: TraversalPolicy | None = None,
    stats: Stats | None = None,
) -> tuple[FieldAuditResult, Stats]:
    policy = policy or TraversalPolicy()
    stats = stats or Stats()

    path = normalize_path(config.path)

    if config.sample is not None:
        policy.branch_samples[path] = config.sample

    result = FieldAuditResult()

    children = direct_children(
        client,
        path,
        per_page=max(1, config.per_page),
        order=config.order,
        stats=stats,
        policy=policy,
    )

    for name, child in children:
        child_path = doc_path(name, child, path)

        try:
            doc = client.node(child_path)
            stats.node_docs_fetched += 1
        except Exception as exc:
            stats.errors += 1
            result.read_errors += 1
            result.add_example(f"{child_path}: read error: {exc}", limit=config.max_examples)
            continue

        body = app_data(doc)

        if not isinstance(body, dict) or not body:
            result.empty_data += 1
            result.add_example(f"{child_path}: empty or non-dict data", limit=config.max_examples)
            continue

        result.checked += 1
        failed = False

        for expectation in config.expectations:
            value = get_path_value(body, expectation.field)

            if expectation.required and value is None and expectation.field not in body:
                result.missing += 1
                failed = True
                result.add_example(
                    f"{child_path}: missing field {expectation.field}",
                    limit=config.max_examples,
                )
                continue

            if expectation.non_null and value is None:
                result.null += 1
                failed = True
                result.add_example(
                    f"{child_path}: field {expectation.field} is null",
                    limit=config.max_examples,
                )
                continue

            if expectation.expected_type is not None:
                actual_type = type_name(value)
                if actual_type != expectation.expected_type:
                    result.wrong_type += 1
                    failed = True
                    result.add_example(
                        f"{child_path}: field {expectation.field} type={actual_type} expected={expectation.expected_type}",
                        limit=config.max_examples,
                    )
                    continue

        for predicate in config.predicates:
            if not predicate.matches(body):
                result.predicate_failed += 1
                failed = True
                result.add_example(
                    f"{child_path}: predicate failed: {predicate.field}{predicate.op}{predicate.raw_value}",
                    limit=config.max_examples,
                )

        if not failed:
            result.ok += 1

    return result, stats


def print_field_audit(result: FieldAuditResult, stats: Stats) -> None:
    print("field audit")
    print(f"  checked: {result.checked}")
    print(f"  ok: {result.ok}")
    print("  problems:")
    print(f"    missing: {result.missing}")
    print(f"    null: {result.null}")
    print(f"    wrong_type: {result.wrong_type}")
    print(f"    predicate_failed: {result.predicate_failed}")
    print(f"    read_errors: {result.read_errors}")
    print(f"    empty_data: {result.empty_data}")

    if result.examples:
        print("  examples:")
        for item in result.examples:
            print(f"    - {item}")

    print("  traversal:")
    print(f"    node_docs_fetched: {stats.node_docs_fetched}")
    print(f"    child_pages_fetched: {stats.child_pages_fetched}")
    print(f"    errors: {stats.errors}")
    print(f"    skipped: {stats.skipped}")
    print(f"    capped: {stats.capped}")


# ---------------------------------------------------------------------------
# CLI program
# ---------------------------------------------------------------------------

def print_stats(stats: Stats) -> None:
    print()
    print("# summary")
    print(f"nodes_seen: {stats.nodes_seen}")
    print(f"nodes_printed: {stats.nodes_printed}")
    print(f"node_docs_fetched: {stats.node_docs_fetched}")
    print(f"child_pages_fetched: {stats.child_pages_fetched}")
    print(f"search_requests: {stats.search_requests}")
    print(f"errors: {stats.errors}")
    print(f"skipped: {stats.skipped}")
    print(f"filtered: {stats.filtered}")
    print(f"capped: {stats.capped}")
    print(f"max_depth_seen: {stats.max_depth_seen}")

    if stats.by_tag:
        print("tags:")
        for tag, count in sorted(stats.by_tag.items(), key=lambda x: (-x[1], x[0])):
            print(f"  {tag}: {count}")


def build_policy_from_args(args: argparse.Namespace) -> TraversalPolicy:
    branch_samples: dict[str, BranchSample] = {}

    for path, sample in parse_sample_specs(getattr(args, "sample_branch", []) or [], seed=getattr(args, "seed", None)).items():
        branch_samples[path] = sample

    for path, sample in parse_branch_samples(getattr(args, "cap", []) or [], mode="first", seed=getattr(args, "seed", None)).items():
        branch_samples[path] = sample

    for path, sample in parse_branch_samples(getattr(args, "random", []) or [], mode="reservoir", seed=getattr(args, "seed", None)).items():
        branch_samples[path] = sample

    for path, sample in parse_branch_samples(getattr(args, "random_page", []) or [], mode="random_page", seed=getattr(args, "seed", None)).items():
        branch_samples[path] = sample

    return TraversalPolicy(
        branch_samples=branch_samples,
        global_limit=getattr(args, "limit", None),
        seed=getattr(args, "seed", None),
        blacklisted_link_rels=set(getattr(args, "blacklist_rel", []) or []),
        blacklisted_path_prefixes=tuple(normalize_path(x) for x in (getattr(args, "blacklist_prefix", []) or [])),
        blacklisted_path_parts=set(getattr(args, "blacklist_part", []) or []),
        include_paths=list(getattr(args, "include_path", []) or []),
        exclude_paths=list(getattr(args, "exclude_path", []) or []),
        include_tags=set(getattr(args, "tag", []) or []),
        exclude_tags=set(getattr(args, "exclude_tag", []) or []),
        where=[parse_data_predicate(x) for x in (getattr(args, "where", []) or [])],
    )


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retry", type=int, default=20)
    parser.add_argument("--retry-sleep", type=float, default=0.25)

    parser.add_argument("--details", action="store_true")
    parser.add_argument("--fields", action="store_true")
    parser.add_argument("--no-summary", action="store_true")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent node fetches per expansion (1 = serial).")

    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--sample-branch", action="append", default=[], metavar="PATH=N[:MODE]")
    parser.add_argument("--cap", action="append", default=[], metavar="PATH=N")
    parser.add_argument("--random", action="append", default=[], metavar="PATH=N")
    parser.add_argument("--random-page", action="append", default=[], metavar="PATH=N")
    parser.add_argument("--seed", type=int, default=None)

    parser.add_argument("--include-path", action="append", default=[], metavar="GLOB")
    parser.add_argument("--exclude-path", action="append", default=[], metavar="GLOB")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--exclude-tag", action="append", default=[])
    parser.add_argument("--where", action="append", default=[], metavar="PREDICATE")

    parser.add_argument("--blacklist-prefix", action="append", default=[])
    parser.add_argument("--blacklist-part", action="append", default=[])
    parser.add_argument("--blacklist-rel", action="append", default=[])


def connect_with_retry(args: argparse.Namespace) -> HyperRelayClient:
    client = HyperRelayClient(args.url, timeout=args.timeout)
    last_error: Exception | None = None

    for _ in range(max(1, args.retry)):
        try:
            health = client.ping()
            if health.get("ok"):
                return client
        except Exception as exc:
            last_error = exc

        time.sleep(args.retry_sleep)

    message = f"relay is not healthy at {client.base_url}/health"
    if last_error:
        message += f"; last error: {last_error}"

    raise RuntimeError(message)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generic HyperCore hypermedia database tool."
    )

    sub = parser.add_subparsers(dest="command")

    walk_p = sub.add_parser("walk", help="Walk the hypergraph from a start path.")
    add_common_args(walk_p)
    walk_p.add_argument("--start", default="/")
    walk_p.add_argument("--per-page", type=int, default=250)
    walk_p.add_argument("--order", choices=ORDER_CHOICES, default="key_asc")
    walk_p.add_argument("--max-depth", type=int, default=None)
    walk_p.add_argument("--print-every", type=int, default=1000)

    stream_p = sub.add_parser("stream", help="Stream a tunable hypergraph view.")
    add_common_args(stream_p)
    stream_p.add_argument("--start", default="/")
    stream_p.add_argument("--per-page", type=int, default=250)
    stream_p.add_argument("--order", choices=ORDER_CHOICES, default="key_asc")
    stream_p.add_argument("--max-depth", type=int, default=5)
    stream_p.add_argument("--progress-every", type=int, default=250)
    stream_p.add_argument("--show-links", action="store_true")
    stream_p.add_argument("--hide-controls", action="store_true")
    stream_p.add_argument("--hide-actions", action="store_true")
    stream_p.add_argument("--hide-system", action="store_true")
    stream_p.add_argument("--hide-directories", action="store_true")
    stream_p.add_argument("--hide-records", action="store_true")
    stream_p.add_argument("--show-directory-fields", action="store_true")
    stream_p.add_argument("--max-field-count", type=int, default=12)
    stream_p.add_argument("--flat", action="store_true")

    collect_p = sub.add_parser("collect", help="Collect selected values from the hypergraph as JSON.")
    add_common_args(collect_p)
    collect_p.add_argument("--start", default="/")
    collect_p.add_argument("--per-page", type=int, default=250)
    collect_p.add_argument("--order", choices=ORDER_CHOICES, default="key_asc")
    collect_p.add_argument("--max-depth", type=int, default=5)
    collect_p.add_argument("--progress-every", type=int, default=0)
    collect_p.add_argument("--select", action="append", default=[])
    collect_p.add_argument("--output", choices=("objects", "values", "pairs"), default="objects")
    collect_p.add_argument("--exclude-nulls", action="store_true")
    collect_p.add_argument("--only-records", action="store_true")
    collect_p.add_argument("--only-directories", action="store_true")

    search_p = sub.add_parser("search", help="Search the relay query API.")
    add_common_args(search_p)
    search_p.add_argument("q", nargs="?", default=None)
    search_p.add_argument("--type", dest="type_", default=None)
    search_p.add_argument("--scope", default=None)
    search_p.add_argument("--search-limit", type=int, default=50)
    search_p.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    search_p.add_argument("--hydrate", action="store_true")

    profile_p = sub.add_parser("profile", help="Profile data field types under a branch.")
    add_common_args(profile_p)
    profile_p.add_argument("--path", required=True)
    profile_p.add_argument("--sample", type=int, default=None)
    profile_p.add_argument("--sample-mode", choices=SAMPLE_MODES, default="first")
    profile_p.add_argument("--per-page", type=int, default=250)
    profile_p.add_argument("--order", choices=ORDER_CHOICES, default="key_asc")

    audit_p = sub.add_parser("audit-fields", help="Generic field integrity audit under a branch.")
    add_common_args(audit_p)
    audit_p.add_argument("--path", required=True)
    audit_p.add_argument("--sample", type=int, default=None)
    audit_p.add_argument("--sample-mode", choices=SAMPLE_MODES, default="first")
    audit_p.add_argument("--per-page", type=int, default=250)
    audit_p.add_argument("--order", choices=ORDER_CHOICES, default="key_asc")
    audit_p.add_argument("--require-field", action="append", default=[])
    audit_p.add_argument("--non-null", action="append", default=[])
    audit_p.add_argument("--type-field", action="append", default=[], metavar="FIELD:TYPE")
    audit_p.add_argument("--assert", dest="assertions", action="append", default=[], metavar="PREDICATE")
    audit_p.add_argument("--max-examples", type=int, default=50)

    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv[0].startswith("-"):
        argv = ["stream", *argv]

    args = parser.parse_args(argv)

    try:
        client = connect_with_retry(args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    policy = build_policy_from_args(args)
    stats = Stats()

    if args.command == "walk":
        walk_graph(
            client,
            args.start,
            per_page=max(1, args.per_page),
            order=args.order,
            max_depth=args.max_depth,
            show_details=args.details,
            show_fields=args.fields,
            print_every=max(0, args.print_every),
            workers=max(1, args.workers),
            policy=policy,
            stats=stats,
        )

    elif args.command == "stream":
        config = StreamConfig(
            per_page=max(1, args.per_page),
            order=args.order,
            max_depth=args.max_depth,
            show_details=args.details or args.fields,
            show_fields=args.fields,
            show_controls=not args.hide_controls,
            show_actions=not args.hide_actions,
            show_links=args.show_links,
            show_directories=not args.hide_directories,
            show_records=not args.hide_records,
            show_system=not args.hide_system,
            show_directory_fields=args.show_directory_fields,
            max_field_count=max(1, args.max_field_count),
            progress_every=max(0, args.progress_every),
            emit_summary=not args.no_summary,
            workers=max(1, args.workers),
        )

        for line in render_stream(
            walk_graph_stream(
                client,
                args.start,
                policy=policy,
                config=config,
                stats=stats,
            ),
            flat=args.flat,
        ):
            print(line)

    elif args.command == "collect":
        stream_config = StreamConfig(
            per_page=max(1, args.per_page),
            order=args.order,
            max_depth=args.max_depth,
            show_details=False,
            show_fields=False,
            show_controls=False,
            show_actions=False,
            show_links=False,
            show_system=not args.only_records,
            show_directories=not args.only_records,
            show_records=not args.only_directories,
            progress_every=max(0, args.progress_every),
            emit_summary=False,
            workers=max(1, args.workers),
        )

        extract_config = ExtractConfig(
            selectors=tuple(args.select or ["path", "data"]),
            output=args.output,
            include_nulls=not args.exclude_nulls,
        )

        rows, stats = collect_graph(
            client,
            args.start,
            policy=policy,
            stream_config=stream_config,
            extract_config=extract_config,
            stats=stats,
        )

        print_json_array(rows)

    elif args.command == "search":
        search_graph(
            client,
            q=args.q,
            type_=args.type_,
            scope=args.scope,
            limit=max(1, args.search_limit),
            extra=parse_search_params(args.param or []),
            hydrate=args.hydrate,
            show_details=args.details,
            show_fields=args.fields,
            policy=policy,
            stats=stats,
        )

    elif args.command == "profile":
        sample = None
        if args.sample is not None:
            sample = BranchSample(
                limit=max(0, args.sample),
                mode=args.sample_mode,
                seed=args.seed,
            )

        profile_graph(
            client,
            args.path,
            sample=sample,
            per_page=max(1, args.per_page),
            order=args.order,
            policy=policy,
            stats=stats,
        )

    elif args.command == "audit-fields":
        sample = None
        if args.sample is not None:
            sample = BranchSample(
                limit=max(0, args.sample),
                mode=args.sample_mode,
                seed=args.seed,
            )

        expectations = parse_field_expectations(
            required=args.require_field or [],
            non_null=args.non_null or [],
            typed=args.type_field or [],
        )

        audit_config = FieldAuditConfig(
            path=args.path,
            sample=sample,
            per_page=max(1, args.per_page),
            order=args.order,
            expectations=expectations,
            predicates=tuple(parse_data_predicate(x) for x in (args.assertions or [])),
            max_examples=max(1, args.max_examples),
        )

        result, stats = audit_fields(
            client,
            config=audit_config,
            policy=policy,
            stats=stats,
        )

        print_field_audit(result, stats)

        if result.failed:
            return 1

    else:
        parser.print_help()
        return 2

    if args.command not in {"stream", "audit-fields", "collect"} and not args.no_summary:
        print_stats(stats)

    if stats.errors:
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())