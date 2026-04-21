from __future__ import annotations

import hashlib
import json
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import product
from typing import Any, Literal

from ___.HyperCoreSDK import HyperClient


NormalizeMode = Literal["none", "lower", "upper", "slug"]


@dataclass(frozen=True)
class ScopeSpec:
    path: str
    name: str | None = None
    normalize: NormalizeMode = "slug"
    multi: bool = False


@dataclass(frozen=True)
class ValueIndexSpec:
    name: str
    path: str
    multi: bool = False
    normalize: NormalizeMode = "none"
    scopes: list[ScopeSpec] = field(default_factory=list)
    projections: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FilterSpec:
    name: str
    path: str
    op: Literal["eq", "in", "contains", "prefix", "gte", "lte"]
    value: Any
    normalize: NormalizeMode = "none"
    scopes: list[ScopeSpec] = field(default_factory=list)
    projections: dict[str, str] = field(default_factory=dict)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  STRING HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def slugify(value: Any) -> str:
    text = str(value or "").strip().lower()
    out: list[str] = []
    last_dash = False
    for ch in text:
        if ch.isalnum():
            out.append(ch)
            last_dash = False
        else:
            if not last_dash:
                out.append("-")
                last_dash = True
    return "".join(out).strip("-")


def normalize_value(value: Any, mode: NormalizeMode = "none") -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value).strip()
    if not text:
        return ""
    if mode == "lower":
        return text.lower()
    if mode == "upper":
        return text.upper()
    if mode == "slug":
        return slugify(text)
    return text


def path_segment(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return urllib.parse.quote(text, safe="._-~")


def join_path(*parts: str) -> str:
    cleaned: list[str] = []
    for part in parts:
        if part is None:
            continue
        for item in str(part).split("/"):
            item = item.strip()
            if item:
                cleaned.append(item)
    return "/".join(cleaned)


def dot_path(root: str, relative_path: str) -> str:
    root = str(root).strip().strip(".")
    rel = str(relative_path or "").strip().strip("/")
    if not rel:
        return root
    return root + "." + rel.replace("/", ".")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PATH EXTRACTION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def extract_path_value(source: Any, path: str, default: Any = None) -> Any:
    current = source
    for part in str(path or "").split("."):
        part = part.strip()
        if not part:
            continue
        if current is None:
            return default
        if isinstance(current, dict):
            current = current.get(part, default)
        else:
            current = getattr(current, part, default)
        if current is default:
            return default
    return current


def extract_path_values(source: Any, path: str, *, multi: bool = False) -> list[Any]:
    value = extract_path_value(source, path)
    if value is None:
        return []
    if multi:
        if isinstance(value, (list, tuple, set)):
            return [item for item in value if item is not None]
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if item is not None]
    return [value]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FILTER MATCHING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def match_filter(source: dict[str, Any], spec: FilterSpec) -> bool:
    raw = extract_path_value(source, spec.path)

    if spec.op == "eq":
        left = normalize_value(raw, spec.normalize)
        right = normalize_value(spec.value, spec.normalize)
        return bool(left) and left == right

    if spec.op == "in":
        left = normalize_value(raw, spec.normalize)
        right_values = {
            normalize_value(item, spec.normalize)
            for item in (spec.value if isinstance(spec.value, (list, tuple, set)) else [spec.value])
        }
        return bool(left) and left in right_values

    if spec.op == "contains":
        if raw is None:
            return False
        needle = normalize_value(spec.value, spec.normalize)
        if isinstance(raw, (list, tuple, set)):
            haystack = {normalize_value(item, spec.normalize) for item in raw}
            return needle in haystack
        return needle in normalize_value(raw, spec.normalize)

    if spec.op == "prefix":
        left = normalize_value(raw, spec.normalize)
        right = normalize_value(spec.value, spec.normalize)
        return bool(left) and left.startswith(right)

    if spec.op == "gte":
        try:
            return raw >= spec.value
        except TypeError:
            left = normalize_value(raw, spec.normalize)
            right = normalize_value(spec.value, spec.normalize)
            return bool(left) and left >= right

    if spec.op == "lte":
        try:
            return raw <= spec.value
        except TypeError:
            left = normalize_value(raw, spec.normalize)
            right = normalize_value(spec.value, spec.normalize)
            return bool(left) and left <= right

    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HYPER CLIENT HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@contextmanager
def _root(hyper_client: HyperClient, root: str):
    old_root = hyper_client.root
    try:
        hyper_client.root = root
        yield
    finally:
        hyper_client.root = old_root


def _try_parse_json_string(value: Any) -> Any:
    if not isinstance(value, str) or len(value) < 2:
        return value
    fc = value[0]
    if fc in ('{', '['):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            pass
    return value


def _deep_parse_json_strings(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, str):
            out[k] = _try_parse_json_string(v)
        elif isinstance(v, dict):
            out[k] = _deep_parse_json_strings(v)
        else:
            out[k] = v
    return out


def _extract_data(response: Any) -> dict[str, Any]:
    if isinstance(response, dict) and isinstance(response.get("data"), dict):
        return _deep_parse_json_strings(response["data"])
    if isinstance(response, dict) and isinstance(response.get("data"), str):
        parsed = _try_parse_json_string(response["data"])
        if isinstance(parsed, dict):
            return _deep_parse_json_strings(parsed)
        return {}
    if isinstance(response, dict):
        return _deep_parse_json_strings(response)
    return {}


def _read_data(
    hyper_client: HyperClient,
    *,
    root: str,
    relative_path: str,
) -> dict[str, Any]:
    with _root(hyper_client, root):
        return _extract_data(hyper_client.read(relative_path))


def _write_node(
    hyper_client: HyperClient,
    *,
    root: str,
    relative_path: str,
    data: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
    links: dict[str, Any] | None = None,
    actions: dict[str, Any] | None = None,
    events: dict[str, Any] | None = None,
    html: str | None = None,
    css: str | None = None,
    js: str | None = None,
    trust: str | None = None,
) -> None:
    payload: dict[str, Any] = {}
    if manifest:
        payload["manifest"] = manifest
    if schema:
        payload["schema"] = schema
    if links:
        payload["links"] = links
    if actions:
        payload["actions"] = actions
    if events:
        payload["events"] = events
    if html is not None:
        payload["html"] = html
    if css is not None:
        payload["css"] = css
    if js is not None:
        payload["js"] = js
    if trust and trust != "public":
        payload["trust"] = trust
    if data is not None:
        payload["data"] = data

    with _root(hyper_client, root):
        hyper_client.write(relative_path, **payload)


def _delete_node(
    hyper_client: HyperClient,
    *,
    root: str,
    relative_path: str,
) -> None:
    with _root(hyper_client, root):
        hyper_client.remove(relative_path)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  BATCH WRITE — single HTTP round-trip for N operations
#
#  Sends POST to /<root>/api/batch with:
#    { "ops": [ { "path": "root.scene.path", "data": {...} }, ... ] }
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _batch_write(
    hyper_client: HyperClient,
    *,
    root: str,
    ops: list[dict[str, Any]],
) -> None:
    if not ops:
        return

    url = f"{hyper_client.relay_url}/{urllib.parse.quote(root, safe='.')}/api/batch"
    body = json.dumps({"ops": ops}).encode("utf-8")

    import urllib.request
    req = urllib.request.Request(
        url,
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def _make_write_op(
    *,
    root: str,
    relative_path: str,
    data: dict[str, Any] | None = None,
    links: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
    actions: dict[str, Any] | None = None,
    events: dict[str, Any] | None = None,
    html: str | None = None,
    css: str | None = None,
    js: str | None = None,
    trust: str | None = None,
) -> dict[str, Any]:
    """Build a batch op dict for the relay's /api/batch endpoint."""
    dp = dot_path(root, relative_path)
    payload: dict[str, Any] = {}
    if manifest:
        payload["manifest"] = manifest
    if schema:
        payload["schema"] = schema
    if links:
        payload["links"] = links
    if actions:
        payload["actions"] = actions
    if events:
        payload["events"] = events
    if html is not None:
        payload["html"] = html
    if css is not None:
        payload["css"] = css
    if js is not None:
        payload["js"] = js
    if trust and trust != "public":
        payload["trust"] = trust
    if data is not None:
        payload["data"] = data
    return {"path": dp, "data": payload}


def _make_delete_op(*, root: str, relative_path: str) -> dict[str, Any]:
    dp = dot_path(root, relative_path)
    return {"path": dp, "delete": True}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PROJECTION / SCOPE / MEMBERSHIP HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_projection_links(
    *,
    root: str,
    record_path: str,
    projections: dict[str, str] | None = None,
) -> dict[str, str]:
    record_dot = dot_path(root, record_path)
    links = {
        "record": record_dot,
        "record_stream": f"{record_dot}.stream",
    }
    for name, rel_path in (projections or {}).items():
        rel_path = str(rel_path or "").strip().strip(".")
        if not rel_path:
            continue
        links[name] = f"{record_dot}.{rel_path}.stream"
    return links


def _scope_name(spec: ScopeSpec) -> str:
    if spec.name:
        return spec.name
    return str(spec.path).replace(".", "_")


def _scope_entries(record_data: dict[str, Any], scopes: list[ScopeSpec]) -> list[dict[str, str]]:
    if not scopes:
        return []
    names: list[str] = []
    values_by_scope: list[list[str]] = []
    for scope in scopes:
        raw_values = extract_path_values(record_data, scope.path, multi=scope.multi)
        normalized = sorted({
            normalize_value(value, scope.normalize)
            for value in raw_values
            if normalize_value(value, scope.normalize)
        })
        if not normalized:
            return []
        names.append(_scope_name(scope))
        values_by_scope.append(normalized)
    out: list[dict[str, str]] = []
    for combo in product(*values_by_scope):
        out.append(dict(zip(names, combo)))
    return out


def _membership_path(record_path: str) -> str:
    digest = hashlib.sha1(record_path.encode("utf-8")).hexdigest()
    return f"meta/index_memberships/{digest}"


def _read_memberships(
    hyper_client: HyperClient,
    *,
    root: str,
    record_path: str,
) -> set[str]:
    try:
        data = _read_data(hyper_client, root=root, relative_path=_membership_path(record_path))
    except Exception:
        return set()

    raw_json = data.get("leaf_paths_json")
    if isinstance(raw_json, str) and raw_json.strip():
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, list):
                return {str(item) for item in parsed if str(item).strip()}
        except Exception:
            return set()

    if isinstance(raw_json, list):
        return {str(item) for item in raw_json if str(item).strip()}

    raw = data.get("leaf_paths")
    if isinstance(raw, list):
        return {str(item) for item in raw if str(item).strip()}
    if isinstance(raw, dict):
        out: set[str] = set()
        for value in raw.values():
            if isinstance(value, str) and value.strip():
                out.add(value)
            elif isinstance(value, dict):
                path = value.get("path")
                if isinstance(path, str) and path.strip():
                    out.add(path)
        return out

    return set()


def _leaf_payload(
    *,
    root: str,
    record_path: str,
    ref_key: str,
    base_data: dict[str, Any] | None = None,
    links: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    record_dot = dot_path(root, record_path)
    data = {
        "record_path": record_path,
        "record_dot": record_dot,
        "ref_key": ref_key,
    }
    if base_data:
        data.update(base_data)
    return data, (links or {"record": record_dot, "record_stream": f"{record_dot}.stream"})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  INDEX / FILTER ENTRY BUILDERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _index_entries_for_record(
    *,
    root: str,
    record_path: str,
    ref_key: str,
    record_data: dict[str, Any],
    index_specs: list[ValueIndexSpec],
    ref_payload: dict[str, Any] | None,
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    entries: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    for spec in index_specs:
        values = extract_path_values(record_data, spec.path, multi=spec.multi)
        normalized_values = sorted({
            normalize_value(value, spec.normalize)
            for value in values
            if normalize_value(value, spec.normalize)
        })

        for value in normalized_values:
            leaf_path = join_path("index", "by_property", path_segment(spec.name), path_segment(value), path_segment(ref_key))
            base_data = {"kind": "index", "index_name": spec.name, "index_value": value}
            if ref_payload:
                base_data.update(ref_payload)
            entries[leaf_path] = _leaf_payload(
                root=root, record_path=record_path, ref_key=ref_key,
                base_data=base_data,
                links=build_projection_links(root=root, record_path=record_path, projections=spec.projections),
            )

            for scope_entry in _scope_entries(record_data, spec.scopes):
                scope_parts: list[str] = []
                for scope_name, scope_value in scope_entry.items():
                    scope_parts.extend([path_segment(scope_name), path_segment(scope_value)])
                scoped_leaf_path = join_path("index", "by_scope", *scope_parts, path_segment(spec.name), path_segment(value), path_segment(ref_key))
                scoped_data = {"kind": "scoped_index", "index_name": spec.name, "index_value": value, "scope": scope_entry}
                if ref_payload:
                    scoped_data.update(ref_payload)
                entries[scoped_leaf_path] = _leaf_payload(
                    root=root, record_path=record_path, ref_key=ref_key,
                    base_data=scoped_data,
                    links=build_projection_links(root=root, record_path=record_path, projections=spec.projections),
                )

    return entries


def _filter_entries_for_record(
    *,
    root: str,
    record_path: str,
    ref_key: str,
    record_data: dict[str, Any],
    filter_specs: list[FilterSpec],
    ref_payload: dict[str, Any] | None,
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    entries: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    for spec in filter_specs:
        if not match_filter(record_data, spec):
            continue

        leaf_path = join_path("filters", path_segment(spec.name), path_segment(ref_key))
        base_data = {"kind": "filter", "filter_name": spec.name, "filter_path": spec.path, "filter_op": spec.op, "filter_value": spec.value}
        if ref_payload:
            base_data.update(ref_payload)
        entries[leaf_path] = _leaf_payload(
            root=root, record_path=record_path, ref_key=ref_key,
            base_data=base_data,
            links=build_projection_links(root=root, record_path=record_path, projections=spec.projections),
        )

        for scope_entry in _scope_entries(record_data, spec.scopes):
            scope_parts: list[str] = []
            for scope_name, scope_value in scope_entry.items():
                scope_parts.extend([path_segment(scope_name), path_segment(scope_value)])
            scoped_leaf_path = join_path("filters", "by_scope", *scope_parts, path_segment(spec.name), path_segment(ref_key))
            scoped_data = {"kind": "scoped_filter", "filter_name": spec.name, "filter_path": spec.path, "filter_op": spec.op, "filter_value": spec.value, "scope": scope_entry}
            if ref_payload:
                scoped_data.update(ref_payload)
            entries[scoped_leaf_path] = _leaf_payload(
                root=root, record_path=record_path, ref_key=ref_key,
                base_data=scoped_data,
                links=build_projection_links(root=root, record_path=record_path, projections=spec.projections),
            )

    return entries


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PUBLIC API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def remove_record_surfaces(
    hyper_client: HyperClient,
    *,
    root: str,
    record_path: str,
    remove_canonical: bool = False,
) -> None:
    old_leaf_paths = _read_memberships(hyper_client, root=root, record_path=record_path)

    ops: list[dict[str, Any]] = []
    for leaf_path in sorted(old_leaf_paths):
        ops.append(_make_delete_op(root=root, relative_path=leaf_path))
    ops.append(_make_delete_op(root=root, relative_path=_membership_path(record_path)))
    if remove_canonical:
        ops.append(_make_delete_op(root=root, relative_path=record_path))

    if ops:
        _batch_write(hyper_client, root=root, ops=ops)


def upsert_canonical_node(
    hyper_client: HyperClient,
    *,
    root: str,
    record_path: str,
    record_data: dict[str, Any],
    links: dict[str, Any] | None = None,
    actions: dict[str, Any] | None = None,
    events: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
    html: str | None = None,
    css: str | None = None,
    js: str | None = None,
    trust: str | None = None,
) -> None:
    _write_node(
        hyper_client, root=root, relative_path=record_path, data=record_data,
        links=links, actions=actions, events=events, manifest=manifest,
        schema=schema, html=html, css=css, js=js, trust=trust,
    )


def upsert_state_node(
    hyper_client: HyperClient,
    *,
    root: str,
    state_path: str,
    state_data: dict[str, Any],
    links: dict[str, Any] | None = None,
    actions: dict[str, Any] | None = None,
    events: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
    html: str | None = None,
    css: str | None = None,
    js: str | None = None,
    trust: str | None = None,
) -> None:
    _write_node(
        hyper_client, root=root, relative_path=state_path, data=state_data,
        links=links, actions=actions, events=events, manifest=manifest,
        schema=schema, html=html, css=css, js=js, trust=trust,
    )


def upsert_record_with_indexes(
    hyper_client: HyperClient,
    *,
    root: str,
    record_path: str,
    record_data: dict[str, Any],
    index_specs: list[ValueIndexSpec] | None = None,
    filter_specs: list[FilterSpec] | None = None,
    ref_key: str | None = None,
    ref_payload: dict[str, Any] | None = None,
    links: dict[str, Any] | None = None,
    actions: dict[str, Any] | None = None,
    events: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
    html: str | None = None,
    css: str | None = None,
    js: str | None = None,
    trust: str | None = None,
) -> None:
    index_specs = index_specs or []
    filter_specs = filter_specs or []
    ref_key = str(ref_key or record_path.rsplit("/", 1)[-1]).strip()

    # Read old memberships (1 HTTP GET)
    old_leaf_paths = _read_memberships(hyper_client, root=root, record_path=record_path)

    # Compute all index/filter entries (pure CPU, no I/O)
    entries: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    entries.update(_index_entries_for_record(
        root=root, record_path=record_path, ref_key=ref_key,
        record_data=record_data, index_specs=index_specs, ref_payload=ref_payload,
    ))
    entries.update(_filter_entries_for_record(
        root=root, record_path=record_path, ref_key=ref_key,
        record_data=record_data, filter_specs=filter_specs, ref_payload=ref_payload,
    ))

    new_leaf_paths = set(entries.keys())

    # Build ALL operations into a single batch
    ops: list[dict[str, Any]] = []

    # 1. Canonical node
    ops.append(_make_write_op(
        root=root, relative_path=record_path, data=record_data,
        links=links, actions=actions, events=events, manifest=manifest,
        schema=schema, html=html, css=css, js=js, trust=trust,
    ))

    # 2. Delete stale leaves
    for leaf_path in sorted(old_leaf_paths - new_leaf_paths):
        ops.append(_make_delete_op(root=root, relative_path=leaf_path))

    # 3. Write index/filter leaves
    for leaf_path, (leaf_data, leaf_links) in entries.items():
        ops.append(_make_write_op(root=root, relative_path=leaf_path, data=leaf_data, links=leaf_links))

    # 4. Membership record
    membership_data = {
        "record_path": record_path,
        "leaf_paths_json": json.dumps(sorted(new_leaf_paths), ensure_ascii=False),
        "leaf_path_count": len(new_leaf_paths),
    }
    ops.append(_make_write_op(root=root, relative_path=_membership_path(record_path), data=membership_data))

    # Single HTTP POST — all writes in one round-trip
    _batch_write(hyper_client, root=root, ops=ops)