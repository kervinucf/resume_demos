# HyperCoreSDK/python/indexes.py
"""
Value indexes for the hypergraph — one concept: IndexEntry.

What you declare:

    ValueIndexSpec(
        name="timezone",              # what the index is called
        path="timezone",              # the record field it reads
        normalize="slug",             # how values are canonicalized
        scopes=[                      # optional: nest under other fields
            ScopeSpec(path="country_code", normalize="upper"),
        ],
        link_projections={            # projected as _links on each entry
            "name": "name",
            "country_code": "country_code",
        },
    )

What gets written for record `locations/nyc-5128581` with
country_code=US, timezone=America/New_York:

    geo/
      index/
        by/
          timezone/
            america-new-york/         ← bucket (auto-materialized directory)
              5128581                 ← ref to the source record
          country_code/
            US/
              5128581
        scoped/                       ← only if scopes are declared
          country_code/
            US/
              timezone/
                america-new-york/
                  5128581
      _meta/
        memberships/
          <sha1(record_path)>         ← sidecar listing paths owned by this record

Bucket nodes (e.g. `by/timezone/america-new-york`) are never written
explicitly. They exist because the directory-model relay materializes any
ancestor when a child is written, and evaporate when the last child under
them is deleted. This means an "index bucket" isn't a thing we maintain —
it's just a parent path with children under it. One less concept.

The relay knows nothing about indexes. These are plain paths the client
agrees to write and clean up together.
"""
from __future__ import annotations

import hashlib
import json
import urllib.parse
from dataclasses import dataclass, field
from itertools import product
from typing import Any, Iterable, Iterator, Literal

from HyperCoreSDK.python.client import HyperClient


NormalizeMode = Literal["none", "lower", "upper", "slug"]


# ---------------------------------------------------------------------------
# Specs (declarations — no behavior)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScopeSpec:
    """
    A secondary axis an index is nested under.

    Indexing `timezone` with scopes=[ScopeSpec(path="country_code")] places
    the entry at `index/scoped/country_code/<cc>/timezone/<tz>/<ref>`
    in addition to the unscoped `index/by/timezone/<tz>/<ref>`.
    """
    path: str
    name: str | None = None
    normalize: NormalizeMode = "slug"
    multi: bool = False


@dataclass(frozen=True)
class ValueIndexSpec:
    """
    A single value index.

    name              — identifier used in the path and entry metadata
    path              — dotted field path on the record to index on
    normalize         — canonicalization applied to each value before use
    multi             — treat list-valued fields as a set of distinct values
    scopes            — extra nesting axes (see ScopeSpec)
    link_projections  — {rel: record_field_path}; becomes _links on each entry
    """
    name: str
    path: str
    normalize: NormalizeMode = "none"
    multi: bool = False
    scopes: list[ScopeSpec] = field(default_factory=list)
    link_projections: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The one type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IndexEntry:
    """
    One path written to the graph on behalf of one record × index × value.

    path   — slash-joined relative path (no root prefix)
    data   — payload dict to write at that path
    links  — {rel: target_dot_path} to attach as _links on the node
    """
    path: str
    data: dict[str, Any]
    links: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Value normalization + path building
# ---------------------------------------------------------------------------

def slugify(value: Any) -> str:
    text = str(value or "").strip().lower()
    out: list[str] = []
    dash = False
    for ch in text:
        if ch.isalnum():
            out.append(ch)
            dash = False
        elif not dash:
            out.append("-")
            dash = True
    return "".join(out).strip("-")


def normalize_value(value: Any, mode: NormalizeMode = "none") -> str:
    if value is None:
        return ""
    text = "true" if value is True else "false" if value is False else str(value).strip()
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
    return urllib.parse.quote(text, safe="._-~") if text else ""


def join_path(*parts: str) -> str:
    """Join non-empty, stripped path segments with '/'."""
    out: list[str] = []
    for part in parts:
        if part is None:
            continue
        for item in str(part).split("/"):
            item = item.strip()
            if item:
                out.append(item)
    return "/".join(out)


def dot_path(root: str, relative_path: str) -> str:
    """Convert a root + slash-relative path into the relay's dotted form."""
    root = str(root).strip().strip(".")
    rel = str(relative_path or "").strip().strip("/")
    if not rel:
        return root
    return f"{root}.{rel.replace('/', '.')}"


# ---------------------------------------------------------------------------
# Field access
# ---------------------------------------------------------------------------

def extract_path_value(source: Any, path: str, default: Any = None) -> Any:
    cur = source
    for part in str(path or "").split("."):
        part = part.strip()
        if not part:
            continue
        if cur is None:
            return default
        cur = cur.get(part, default) if isinstance(cur, dict) else getattr(cur, part, default)
        if cur is default:
            return default
    return cur


def extract_path_values(source: Any, path: str, *, multi: bool = False) -> list[Any]:
    """Return a list of non-None values at `path`. Flattens list-valued fields."""
    val = extract_path_value(source, path)
    if val is None:
        return []
    if isinstance(val, (list, tuple, set)):
        return [v for v in val if v is not None]
    return [val] if multi else [val]


def _normalized_values(record: dict, spec: ValueIndexSpec) -> list[str]:
    """All distinct normalized values the record contributes to this index."""
    raw = extract_path_values(record, spec.path, multi=spec.multi)
    return sorted({
        normalize_value(v, spec.normalize)
        for v in raw
        if normalize_value(v, spec.normalize)
    })


def _scope_bindings(record: dict, scopes: list[ScopeSpec]) -> Iterator[dict[str, str] | None]:
    """
    Yield scope bindings.

    - If there are no scopes, yield None once (represents "the unscoped emission").
    - Otherwise yield one dict per cartesian combination of scope values.
    - If any scope's field has no values, yield nothing (scoped emissions
      suppressed; the unscoped one is emitted by a separate call).

    This is the only place scope logic lives.
    """
    if not scopes:
        yield None
        return

    names = [s.name or s.path.replace(".", "_") for s in scopes]
    values_per = []
    for s in scopes:
        raw = extract_path_values(record, s.path, multi=s.multi)
        norm = sorted({
            normalize_value(v, s.normalize)
            for v in raw
            if normalize_value(v, s.normalize)
        })
        if not norm:
            return
        values_per.append(norm)

    for combo in product(*values_per):
        yield dict(zip(names, combo))


# ---------------------------------------------------------------------------
# Entry emission — the one place paths are built
# ---------------------------------------------------------------------------

def _emit_entries(
    *,
    root: str,
    record_path: str,
    ref_key: str,
    ref_payload: dict[str, Any],
    record_data: dict[str, Any],
    specs: list[ValueIndexSpec],
) -> dict[str, IndexEntry]:
    """
    Produce `{path: IndexEntry}` — every path this record owns in the index
    namespace. One flat dict, one type.

    The body is three nested loops:
      for each spec
        for each normalized value
          emit unscoped + emit one per scope binding
    """
    entries: dict[str, IndexEntry] = {}
    record_dot = dot_path(root, record_path)

    for spec in specs:
        projection_links = _projection_links_for(record_dot, spec.link_projections)

        for value in _normalized_values(record_data, spec):
            # We always emit the unscoped entry (binding=None).
            # If scopes are declared AND the record satisfies all of them,
            # we also emit one scoped entry per cartesian combination.
            bindings: list[dict[str, str] | None] = [None]
            bindings.extend(_scope_bindings(record_data, spec.scopes) if spec.scopes else [])

            for binding in bindings:
                path = _build_path(spec, value, ref_key, binding)
                entries[path] = IndexEntry(
                    path=path,
                    data=_build_entry_data(
                        spec=spec,
                        value=value,
                        binding=binding,
                        ref_key=ref_key,
                        record_path=record_path,
                        record_dot=record_dot,
                        ref_payload=ref_payload,
                    ),
                    links=projection_links,
                )

    return entries


def _build_path(
    spec: ValueIndexSpec,
    value: str,
    ref_key: str,
    binding: dict[str, str] | None,
) -> str:
    """
    Unscoped:   index/by/<index_name>/<value>/<ref_key>
    Scoped:     index/scoped/<scope_name>/<scope_value>/.../<index_name>/<value>/<ref_key>

    ref_key is always the final segment — its parent directory is the bucket
    a consumer enumerates to list members.
    """
    if binding is None:
        return join_path(
            "index", "by",
            path_segment(spec.name),
            path_segment(value),
            path_segment(ref_key),
        )

    scope_parts: list[str] = []
    for scope_name, scope_value in binding.items():
        scope_parts.extend([path_segment(scope_name), path_segment(scope_value)])

    return join_path(
        "index", "scoped",
        *scope_parts,
        path_segment(spec.name),
        path_segment(value),
        path_segment(ref_key),
    )


def _build_entry_data(
    *,
    spec: ValueIndexSpec,
    value: str,
    binding: dict[str, str] | None,
    ref_key: str,
    record_path: str,
    record_dot: str,
    ref_payload: dict[str, Any],
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": "index_ref",
        "index_name": spec.name,
        "index_value": value,
        "ref_key": ref_key,
        "record_path": record_path,
        "record_dot": record_dot,
    }
    if binding:
        data["scope"] = dict(binding)
    # Merge projected fields for cheap rendering without hydrating the record.
    # Control keys above win over projected keys to keep semantics stable.
    for k, v in ref_payload.items():
        if k not in data:
            data[k] = v
    return data


def _projection_links_for(record_dot: str, projections: dict[str, str]) -> dict[str, str]:
    """Every entry gets a `record` link; declared projections get one each."""
    links = {"record": record_dot}
    for rel, field_path in (projections or {}).items():
        field_path = str(field_path or "").strip().strip(".")
        if field_path:
            links[rel] = f"{record_dot}.{field_path}"
    return links


# ---------------------------------------------------------------------------
# Membership sidecar — one flat list of owned paths
# ---------------------------------------------------------------------------

def _membership_path(record_path: str) -> str:
    """
    `_meta/memberships/<sha1(record_path)>` — leading underscore flags it as
    system-maintained. It's still a browsable directory in the relay.
    """
    digest = hashlib.sha1(record_path.encode("utf-8")).hexdigest()
    return f"_meta/memberships/{digest}"


def _read_owned_paths(hc: HyperClient, *, root: str, record_path: str) -> set[str]:
    try:
        doc = hc.read(dot_path(root, _membership_path(record_path)))
    except Exception:
        return set()

    if not isinstance(doc, dict):
        return set()
    data = doc.get("data")
    if not isinstance(data, dict):
        return set()

    raw = data.get("paths")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    if not isinstance(raw, (list, tuple)):
        return set()

    return {str(p) for p in raw if str(p).strip()}


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

def plan_upsert(
    *,
    root: str,
    record_path: str,
    record_data: dict[str, Any],
    index_specs: list[ValueIndexSpec] | None = None,
    ref_key: str | None = None,
    ref_payload: dict[str, Any] | None = None,
    links: dict[str, str] | None = None,
    actions: dict[str, Any] | None = None,
    prior_paths: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Pure planner. Given a record + the set of paths it previously owned,
    return the minimal op list to:

      1. write the record itself,
      2. delete paths the record no longer owns,
      3. write the paths it now owns,
      4. rewrite the membership sidecar.

    No I/O. The ops list can be inspected before execution.
    """
    index_specs = index_specs or []
    ref_key = str(ref_key or record_path.rsplit("/", 1)[-1]).strip()
    prior = {str(p) for p in (prior_paths or []) if str(p).strip()}

    entries = _emit_entries(
        root=root,
        record_path=record_path,
        ref_key=ref_key,
        ref_payload=ref_payload or {},
        record_data=record_data,
        specs=index_specs,
    )
    current = set(entries.keys())

    ops: list[dict[str, Any]] = []

    # 1. The record itself.
    record_payload: dict[str, Any] = {"data": record_data}
    if links:
        record_payload["links"] = {k: v for k, v in links.items() if v}
    if actions:
        record_payload["actions"] = actions
    ops.append({"path": dot_path(root, record_path), "data": record_payload})

    # 2. Delete stale paths. The directory-model relay auto-prunes empty
    #    parents, so we never issue deletes for bucket nodes — removing the
    #    last child is enough to make the bucket disappear.
    for p in sorted(prior - current):
        ops.append({"path": dot_path(root, p), "delete": True})

    # 3. Write current entries.
    for p in sorted(current):
        entry = entries[p]
        payload: dict[str, Any] = {"data": entry.data}
        if entry.links:
            payload["links"] = entry.links
        ops.append({"path": dot_path(root, p), "data": payload})

    # 4. Sidecar: one sorted list of paths this record owns.
    ops.append({
        "path": dot_path(root, _membership_path(record_path)),
        "data": {
            "data": {
                "record_path": record_path,
                "paths": sorted(current),
            }
        },
    })

    return ops


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------

def upsert_with_indexes(
    hc: HyperClient,
    *,
    record_path: str,
    record_data: dict[str, Any],
    index_specs: list[ValueIndexSpec] | None = None,
    ref_key: str | None = None,
    ref_payload: dict[str, Any] | None = None,
    links: dict[str, str] | None = None,
    actions: dict[str, Any] | None = None,
    root: str | None = None,
) -> int:
    """
    Write a record and its derived index entries in one batch. Reads prior
    ownership, deletes orphans, rewrites the sidecar. Returns op count.
    """
    effective_root = root or hc.root

    ops = plan_upsert(
        root=effective_root,
        record_path=record_path,
        record_data=record_data,
        index_specs=index_specs,
        ref_key=ref_key,
        ref_payload=ref_payload,
        links=links,
        actions=actions,
        prior_paths=_read_owned_paths(hc, root=effective_root, record_path=record_path),
    )

    result = hc.batch(root=effective_root, ops=ops)
    return int(result.get("count") or 0)


def delete_with_indexes(
    hc: HyperClient,
    *,
    record_path: str,
    root: str | None = None,
) -> int:
    """
    Delete a record and every path registered to it; remove the sidecar.
    Empty bucket directories evaporate automatically via the relay's
    ancestor pruning.
    """
    effective_root = root or hc.root
    owned = _read_owned_paths(hc, root=effective_root, record_path=record_path)

    ops: list[dict[str, Any]] = []
    for p in sorted(owned):
        ops.append({"path": dot_path(effective_root, p), "delete": True})
    ops.append({"path": dot_path(effective_root, record_path), "delete": True})
    ops.append({"path": dot_path(effective_root, _membership_path(record_path)), "delete": True})

    result = hc.batch(root=effective_root, ops=ops)
    return int(result.get("count") or 0)