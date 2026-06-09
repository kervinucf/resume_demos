from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable

from HyperCoreSDK.python.helpers.indexes import ValueIndexSpec, upsert_with_indexes


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def node_data(doc: Any) -> dict[str, Any]:
    """
    Extract application data from a relay document.

    Supports both shapes:
        {"data": {...}}
        {"data": {"data": {...}, "links": {...}}}
    """
    if not isinstance(doc, dict):
        return {}

    data = doc.get("data")

    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]

    return data if isinstance(data, dict) else {}


def iter_children(client, path: str, *, per_page: int = 200):
    page = 1

    while True:
        doc = client.children(path, page=page, per_page=per_page)

        state = doc.get("_state", {}) if isinstance(doc, dict) else {}
        current_page = int(state.get("children_page") or page)
        num_pages = int(state.get("children_num_pages") or current_page)

        items = doc.items()
        if not items:
            return

        yield from items

        if current_page >= num_pages:
            return

        page = current_page + 1
def record_dot_from_ref(entry: dict[str, Any], *, root: str) -> str:
    data = node_data(entry)

    if data.get("record_dot"):
        return str(data["record_dot"])

    if data.get("record_path"):
        return f"{root}.{str(data['record_path']).replace('/', '.')}"

    return str(entry.get("_state", {}).get("path") or "")


def iter_index_record_refs(
    client,
    *,
    root: str,
    index_name: str,
    values: Iterable[Any],
    per_page: int = 200,
):
    seen: set[str] = set()

    for value in values:
        index_path = f"{root}.index.by.{index_name}.{value}"

        for entry in iter_children(client, index_path, per_page=per_page):
            ref = record_dot_from_ref(entry, root=root)

            if ref and ref not in seen:
                seen.add(ref)
                yield ref


def select_records(
    client,
    *,
    namespace: str,
    collection: str,
    by: dict[str, Any] | None = None,
    where: Callable[[dict[str, Any]], bool] | None = None,
    limit: int | None = None,
    per_page: int = 200,
):
    """
    Select records from either a collection or a single explicit index.
    """
    by = by or {}

    if by:
        if len(by) != 1:
            raise ValueError("select_records supports one index selector at a time")

        index_name, raw_values = next(iter(by.items()))
        values = raw_values if isinstance(raw_values, (list, tuple, set)) else [raw_values]

        refs = iter_index_record_refs(
            client,
            root=namespace,
            index_name=index_name,
            values=values,
            per_page=per_page,
        )
    else:
        refs = (
            str(entry.get("_state", {}).get("path") or "")
            for entry in iter_children(client, f"{namespace}.{collection}", per_page=per_page)
        )

    seen: set[str] = set()
    emitted = 0

    for ref in refs:
        if not ref or ref in seen:
            continue

        seen.add(ref)

        data = node_data(client.read(ref) or {})
        if not data:
            continue

        if where is not None and not where(data):
            continue

        if data is None:
            continue

        yield data

        emitted += 1
        if limit is not None and emitted >= int(limit):
            return


# ---------------------------------------------------------------------------
# Inference program
# ---------------------------------------------------------------------------

def _clean_dict(value: dict[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}

    out: dict[str, Any] = {}

    for key, item in value.items():
        if item is None:
            continue
        if item == "":
            continue
        out[str(key)] = item

    return out


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_number(value: Any) -> bool:
    if _is_bool(value):
        return False

    if isinstance(value, (int, float)):
        return math.isfinite(float(value))

    return False


def _is_small_text(value: Any) -> bool:
    if not isinstance(value, str):
        return False

    text = value.strip()
    if not text:
        return False

    return len(text) <= 160


def _looks_like_path(value: Any) -> bool:
    if not isinstance(value, str):
        return False

    text = value.strip()
    if not text or "/" in text:
        return False

    parts = text.split(".")
    return len(parts) >= 3 and all(parts)


def _datetime_to_ms(value: Any) -> int | None:
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    if isinstance(value, date):
        dt = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    if _is_number(value):
        return int(value)

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    iso = text
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return int(dt.timestamp() * 1000)


def _walk_simple_values(value: Any) -> Iterable[Any]:
    if value is None:
        return

    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_simple_values(child)
        return

    if isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _walk_simple_values(child)
        return

    yield value


def _text_join(items: Iterable[Any]) -> str:
    parts: list[str] = []

    for item in items:
        if item is None:
            continue

        if isinstance(item, (dict, list, tuple, set)):
            for child in _walk_simple_values(item):
                if isinstance(child, str) and child.strip():
                    parts.append(child.strip())
            continue

        text = str(item).strip()
        if text:
            parts.append(text)

    return " ".join(parts)


def _infer_properties(body: dict[str, Any]) -> dict[str, Any]:
    """
    Exact filters inferred from body.

    KISS rule:
      - short strings and booleans become filterable fields
      - numbers become searchable numeric values
      - path-looking strings belong in links/search, not exact properties
      - nested objects/lists are not exact filters by default
    """
    out: dict[str, Any] = {}

    for key, value in body.items():
        if value is None:
            continue

        if _is_bool(value):
            out[key] = value
            continue

        if _is_number(value):
            continue

        if _looks_like_path(value):
            continue

        if _is_small_text(value):
            out[key] = value

    return out


def _infer_values(body: dict[str, Any]) -> dict[str, float]:
    """
    Numeric values inferred from body.
    """
    out: dict[str, float] = {}

    for key, value in body.items():
        if _is_number(value):
            out[key] = float(value)

    return out


def _infer_dates(body: dict[str, Any]) -> dict[str, int]:
    """
    Date-like values inferred from body.

    Keys must sound temporal to avoid accidental parsing.
    """
    out: dict[str, int] = {}

    for key, value in body.items():
        key_l = str(key).lower()

        if not (
            key_l.endswith("_at")
            or key_l.endswith("_time")
            or key_l.endswith("_date")
            or key_l in {"date", "time", "created", "updated", "published", "observed"}
        ):
            continue

        parsed = _datetime_to_ms(value)
        if parsed is not None:
            out[key] = parsed

    return out


def _infer_search(tag: str, name: str, body: dict[str, Any], search: Iterable[Any] | None) -> list[Any]:
    terms: list[Any] = [tag, name]

    for key, value in body.items():
        if value is None:
            continue

        if isinstance(value, str):
            terms.append(value)
            continue

        if isinstance(value, (list, tuple, set)):
            for item in value:
                if isinstance(item, str):
                    terms.append(item)

    if search:
        terms.extend(search)

    return [x for x in terms if x is not None and str(x).strip()]


def _query_links(links: dict[str, Any]) -> dict[str, Any]:
    """
    Query links may be scalar paths or lists of paths.
    """
    out: dict[str, Any] = {}

    for rel, source in links.items():
        if source is None:
            continue

        if isinstance(source, (list, tuple, set)):
            clean = [str(x) for x in source if x is not None and str(x).strip()]
            if clean:
                out[str(rel)] = clean
            continue

        text = str(source).strip()
        if text:
            out[str(rel)] = text

    return out


def _node_links(links: dict[str, Any]) -> dict[str, str]:
    """
    Node links should be simple href/path strings for HTML rendering.

    If a link value is a list, keep it out of _links. It is still written
    to the query relationship index through _query_links().
    """
    out: dict[str, str] = {}

    for rel, source in links.items():
        if source is None:
            continue

        if isinstance(source, (list, tuple, set)):
            clean = [str(x) for x in source if x is not None and str(x).strip()]
            if len(clean) == 1:
                out[str(rel)] = clean[0]
            continue

        text = str(source).strip()
        if text:
            out[str(rel)] = text

    return out


def _query_for_thing(
    *,
    path: str,
    tag: str,
    name: str,
    body: dict[str, Any],
    links: dict[str, Any] | None = None,
    search: Iterable[Any] | None = None,
    properties: dict[str, Any] | None = None,
    values: dict[str, Any] | None = None,
    dates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = _clean_dict(body)
    links = _clean_dict(links)

    final_properties = {
        **_infer_properties(body),
        **_clean_dict(properties),
    }

    final_values = {
        **_infer_values(body),
        **{
            str(k): float(v)
            for k, v in _clean_dict(values).items()
            if _is_number(v)
        },
    }

    final_dates = dict(_infer_dates(body))

    for key, value in _clean_dict(dates).items():
        parsed = _datetime_to_ms(value)
        if parsed is not None:
            final_dates[str(key)] = parsed

    return {
        "entity_id": path,
        "entity_type": tag,
        "canonical_path": path,
        "display": name,
        "text": _text_join(_infer_search(tag, name, body, search)),
        "facets": final_properties,
        "numbers": final_values,
        "times": final_dates,
        "refs": _query_links(links),
        "tokens": _infer_search(tag, name, body, search),
    }


# ---------------------------------------------------------------------------
# Existing advanced writes
# ---------------------------------------------------------------------------


def write_pointer(
    client,
    *,
    path: str,
    source: str,
    data: dict[str, Any],
    links: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
):
    links = links or {}

    payload: dict[str, Any] = {
        "data": data,
        "links": {
            "source": source,
            **_node_links(links),
        },
    }

    if query:
        payload["query"] = query

    return client.write(path, payload)


# ---------------------------------------------------------------------------
# KISS writes
# ---------------------------------------------------------------------------

def write_thing(
    client,
    *,
    path: str,
    tag: str,
    name: str,
    body: Any,
    links: dict[str, Any] | None = None,
    search: Iterable[Any] | None = None,
    properties: dict[str, Any] | None = None,
    values: dict[str, Any] | None = None,
    dates: dict[str, Any] | None = None,
):
    """
    KISS write API.

    Normal loader code should only need:
        path, tag, name, body, links

    Advanced overrides are available, but should be rare:
        search, properties, values, dates
    """
    body = _clean_dict(body)
    links = _clean_dict(links)

    final_source = str(links.get("source") or path)

    query = _query_for_thing(
        path=path,
        tag=tag,
        name=name,
        body=body,
        links=links,
        search=search,
        properties=properties,
        values=values,
        dates=dates,
    )

    return write_pointer(
        client,
        path=path,
        source=final_source,
        data=body,
        links=links,
        query=query,
    )


