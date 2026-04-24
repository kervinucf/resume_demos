from __future__ import annotations

from typing import Any, Callable, Iterable

from HyperCoreSDK.python.helpers.indexes import ValueIndexSpec, upsert_with_indexes


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
        items = client.children(path, page=page, per_page=per_page).items()
        if not items:
            return

        yield from items
        page += 1


def record_dot_from_ref(entry: dict[str, Any], *, root: str) -> str:
    """
    Resolve an index ref entry to the canonical record dot path.

    Generic index refs may carry either:
        record_dot
        record_path
    """
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
    root: str,
    collection: str,
    from_data: Callable[[dict[str, Any]], Any | None],
    by: dict[str, Any] | None = None,
    where: Callable[[dict[str, Any]], bool] | None = None,
    limit: int | None = None,
    per_page: int = 200,
):
    """
    Select records from either a collection or a single value index.

    Examples:
        select_records(
            client,
            root="geo",
            collection="locations",
            from_data=location_from_data,
            by={"country_code": "US"},
        )

        select_records(
            client,
            root="geo",
            collection="locations",
            from_data=location_from_data,
            by={"population_band": ["2_5M-4_9M", "5M+"]},
            where=lambda data: data.get("country_code") == "US",
            limit=500,
        )
    """
    by = by or {}

    if by:
        if len(by) != 1:
            raise ValueError("select_records supports one index selector at a time")

        index_name, raw_values = next(iter(by.items()))
        values = raw_values if isinstance(raw_values, (list, tuple, set)) else [raw_values]

        refs = iter_index_record_refs(
            client,
            root=root,
            index_name=index_name,
            values=values,
            per_page=per_page,
        )
    else:
        refs = (
            str(entry.get("_state", {}).get("path") or "")
            for entry in iter_children(client, f"{root}.{collection}", per_page=per_page)
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

        item = from_data(data)
        if item is None:
            continue

        yield item

        emitted += 1
        if limit is not None and emitted >= int(limit):
            return


def write_record_with_indexes(
    client,
    *,
    root: str,
    record_path: str,
    record_data: dict[str, Any],
    index_specs: list[ValueIndexSpec] | None = None,
    ref_key: str | None = None,
    ref_payload: dict[str, Any] | None = None,
    links: dict[str, str] | None = None,
    actions: dict[str, Any] | None = None,
) -> int:
    return upsert_with_indexes(
        client,
        root=root,
        record_path=record_path,
        record_data=record_data,
        index_specs=index_specs,
        ref_key=ref_key,
        ref_payload=ref_payload,
        links=links,
        actions=actions,
    )


def write_pointer(
    client,
    *,
    path: str,
    target: str,
    data: dict[str, Any],
    links: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
):
    payload: dict[str, Any] = {
        "data": data,
        "links": {
            "target": target,
            **(links or {}),
        },
    }

    if query:
        payload["query"] = query

    return client.write(path, payload)


def write_backref(
    client,
    *,
    source: str,
    rel: str,
    target: str,
    data: dict[str, Any],
    links: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
):
    return write_pointer(
        client,
        path=f"{source}.refs.{rel}",
        target=target,
        data=data,
        links=links,
        query=query,
    )