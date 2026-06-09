#!/usr/bin/env python3
"""
HyperCoreSDK/python/program/search.py  (drop-in replacement)

Same public surface as before:
    HyperSearch, SearchOptions, Source, SearchResults, TextMatcher,
    PickMode, ResultType, TextMode, PathLimit

What changed:
    * query()  -> set-based, indexed lookups via a pluggable backend.
                  No directory-by-directory database walk, no N+1 hydration.
    * backend  -> "sql" reads nodes.sqlite directly; "api" calls the integrated
                  relay fast read tier at /api/*. Both build the SAME sql, so
                  they return the SAME rows.

What did NOT change:
    * stream() -> byte-for-byte the original relay traversal. The streaming
                  contract is preserved exactly: it still yields rendered lines
                  via walk_graph_stream + render_stream over the live relay.
    * SearchOptions / Source / SearchResults fields and signatures.

Construct it the way the demos do:
    HyperSearch("http://127.0.0.1:8765", backend="api")
    HyperSearch("http://127.0.0.1:8765", backend="sql",
                db_path="~/.hyper-data/sqlite/nodes.sqlite")

The first positional arg is the single relay URL used by both query() and stream().
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Literal

# Stream contract dependencies — imported exactly as before.
from HyperCoreSDK.python.helpers.tool import (
    StreamConfig,
    TraversalPolicy,
    print_json_array,
    render_stream,
    walk_graph_stream,
)

PickMode = Literal["top", "random", "fast_random", "exact_random"]
ResultType = Literal["records", "folders", "system", "all"]
TextMode = Literal["hybrid", "index", "scan"]

_FIELD_ALIASES = {
    "type": "state.tag",
    "tag": "state.tag",
    "record_type": "state.tag",
}


# ---------------------------------------------------------------------------
# Text matcher (unchanged — guarantees identical text semantics)
# ---------------------------------------------------------------------------

class TextMatcher:
    """
    Raw-row text matcher.

    Supported:
        *                      match everything
        florida                row contains florida
        miami rain             row contains miami AND rain
        "new york" snow        phrase new york AND snow
        weather -archived      weather AND NOT archived
    """

    def __init__(self, text: str | None) -> None:
        import shlex

        raw = (text or "*").strip()
        self.raw = raw

        if raw in {"", "*"}:
            self.positive: tuple[str, ...] = ()
            self.negative: tuple[str, ...] = ()
            return

        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = raw.split()

        positive: list[str] = []
        negative: list[str] = []

        for part in parts:
            value = part.strip().lower()
            if not value:
                continue
            if value.startswith("-") and len(value) > 1:
                negative.append(value[1:])
            else:
                positive.append(value)

        self.positive = tuple(positive)
        self.negative = tuple(negative)

    @property
    def matches_all(self) -> bool:
        return not self.positive and not self.negative

    def matches(self, row: dict[str, Any]) -> bool:
        if self.matches_all:
            return True

        haystack = self._row_text(row)

        if any(term in haystack for term in self.negative):
            return False

        return all(term in haystack for term in self.positive)

    @staticmethod
    def _row_text(row: dict[str, Any]) -> str:
        chunks: list[str] = []

        def add(value: Any) -> None:
            if value is None:
                return
            if isinstance(value, str):
                chunks.append(value)
                return
            try:
                chunks.append(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
            except Exception:
                chunks.append(str(value))

        for value in row.values():
            add(value)

        return "\n".join(chunks).lower()


# ---------------------------------------------------------------------------
# Config dataclasses (unchanged fields/signatures)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    count: int
    pick: PickMode = "top"
    seed: int | None = None
    max_depth: int | None = None
    max_scan: int | None = None
    per_page: int | None = None
    order: str | None = None


PathLimit = Source


@dataclass
class SearchOptions:
    max_depth: int = 5
    per_page: int = 10000
    order: str = "key_asc"

    show: ResultType | Iterable[ResultType] = "records"
    fields: Iterable[str] = ("path", "type", "data")
    exclude: Iterable[str] = ("_meta",)
    sources: dict[str, int | Source] = field(default_factory=dict)

    include_paths: Iterable[str] = ()
    exclude_paths: Iterable[str] = ()

    seed: int | None = 42
    details: bool = False
    summary: bool = False
    max_field_count: int = 32

    source_workers: int = 8

    random_exact: bool = False
    random_page_budget: int = 2000

    text_mode: TextMode = "hybrid"
    min_index_hits: int = 1

    index_page_size: int = 250
    index_max_pages: int = 4000

    scan_budget_records: int | None = None
    hybrid_fill_to_count: bool = True

    # Strict text matching uses json_extract on the unwrapped app-data only,
    # matching the old scan haystack exactly. Off by default (faster, superset).
    strict_text: bool = False

    def normalized_sources(self) -> dict[str, Source]:
        return {
            path: Source(count=source) if isinstance(source, int) else source
            for path, source in self.sources.items()
        }

    def result_types(self) -> set[str]:
        values = {self.show} if isinstance(self.show, str) else set(self.show)
        return {"records", "folders", "system"} if "all" in values else values

    def normalized_fields(self) -> tuple[str, ...]:
        return tuple(_FIELD_ALIASES.get(name, name) for name in self.fields)

    def stream_config(self, *, for_stream: bool = False) -> StreamConfig:
        types = self.result_types()
        return StreamConfig(
            max_depth=self.max_depth,
            per_page=self.per_page,
            order=self.order,
            show_system="system" in types,
            show_directories="folders" in types,
            show_records="records" in types,
            show_details=self.details or for_stream,
            show_fields=self.details or for_stream,
            show_controls=False,
            show_actions=False,
            show_links=False,
            max_field_count=self.max_field_count,
            progress_every=0,
            emit_summary=self.summary if for_stream else False,
        )


class SearchResults:
    def __init__(self, rows: list[Any], errors: int = 0, *, scanned: int = 0, indexed: int = 0) -> None:
        self.rows = rows
        self.errors = errors
        self.scanned = scanned
        self.indexed = indexed

    def __iter__(self) -> Iterator[Any]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> Any:
        return self.rows[index]

    def to_list(self) -> list[Any]:
        return self.rows

    def print_json(self) -> None:
        print_json_array(self.rows)

    def print_count(self) -> None:
        print(len(self.rows))

    def has_errors(self) -> bool:
        return self.errors > 0


# ---------------------------------------------------------------------------
# Path / field / row program
# ---------------------------------------------------------------------------

def _split_source(dotted: str) -> tuple[str, str]:
    parts = [p for p in str(dotted or "").strip().strip("/.").replace("/", ".").split(".") if p]
    if not parts:
        return "", ""
    return parts[0], "/".join(parts)


def _app_data(parsed: Any) -> dict[str, Any]:
    if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict):
        return parsed["data"]
    return parsed if isinstance(parsed, dict) else {}


def _get_path(source: Any, dotted: str) -> Any:
    cur = source
    for part in dotted.split("."):
        if not part:
            continue
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _extract_row(path: str, parsed: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    app: dict[str, Any] | None = None

    for name in fields:
        if name == "path":
            value: Any = path
        elif name == "state.tag":
            value = "record"
        elif name == "data":
            app = _app_data(parsed) if app is None else app
            value = app
        elif name.startswith("data."):
            app = _app_data(parsed) if app is None else app
            value = _get_path(app, name[len("data."):])
        elif name == "state":
            value = {"path": path, "tag": "record"}
        elif name.startswith("state."):
            value = _get_path({"path": path, "tag": "record"}, name[len("state."):])
        elif name == "doc":
            value = parsed
        else:
            value = None

        if value is not None:
            row[name] = value

    return row


# ---------------------------------------------------------------------------
# Shared SQL builders (SqlBackend executes these; fast_read_server.js mirrors)
# ---------------------------------------------------------------------------

def _escape_like(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _order_sql(order: str) -> str:
    return {
        "key_desc": "path DESC",
        "updated_desc": "updated_at DESC, path DESC",
        "updated_asc": "updated_at ASC, path ASC",
        "key_asc": "path ASC",
    }.get(str(order or "key_asc"), "path ASC")


def _base_where(*, root: str, prefix: str, has_data: bool, exclude: Iterable[str], max_depth: int | None) -> tuple[str, list[Any]]:
    where = ["root = ?"]
    args: list[Any] = [root]

    where.append("(path = ? OR path LIKE ? ESCAPE '\\')")
    args += [prefix, _escape_like(prefix) + "/%"]

    if has_data:
        where.append("data IS NOT NULL")

    for part in exclude:
        where.append("('/' || path || '/') NOT LIKE ? ESCAPE '\\'")
        args.append("%/" + _escape_like(part) + "/%")

    if max_depth is not None:
        where.append("((LENGTH(path) - LENGTH(REPLACE(path, '/', ''))) - ?) <= ?")
        args += [prefix.count("/"), max_depth]

    return " AND ".join(where), args


def _build_scan(*, root, prefix, order, limit, offset, has_data, exclude, max_depth, keys_only) -> tuple[str, list[Any]]:
    cols = "path" if keys_only else "path, data"
    body, args = _base_where(root=root, prefix=prefix, has_data=has_data, exclude=exclude, max_depth=max_depth)
    sql = f"SELECT {cols} FROM nodes WHERE {body} ORDER BY {_order_sql(order)}"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    if offset:
        sql += " OFFSET ?"
        args.append(offset)
    return sql, args


def _build_text_scan(*, root, prefix, positives, negatives, order, limit, has_data, exclude, max_depth, strict) -> tuple[str, list[Any]]:
    col = "json_extract(data, '$.data')" if strict else "data"
    body, args = _base_where(root=root, prefix=prefix, has_data=has_data, exclude=exclude, max_depth=max_depth)
    where = [body]

    for term in positives:
        pat = "%" + _escape_like(term) + "%"
        where.append(f"(path LIKE ? ESCAPE '\\' OR {col} LIKE ? ESCAPE '\\' OR 'record' LIKE ? ESCAPE '\\')")
        args += [pat, pat, pat]

    for term in negatives:
        pat = "%" + _escape_like(term) + "%"
        where.append(f"(path NOT LIKE ? ESCAPE '\\' AND {col} NOT LIKE ? ESCAPE '\\' AND 'record' NOT LIKE ? ESCAPE '\\')")
        args += [pat, pat, pat]

    sql = f"SELECT path, data FROM nodes WHERE {' AND '.join(where)} ORDER BY {_order_sql(order)}"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    return sql, args


def _build_text_index(*, root, prefix, positives, order, limit, has_data, exclude, max_depth) -> tuple[str, list[Any]]:
    body, base_args = _base_where(root=root, prefix=prefix, has_data=has_data, exclude=exclude, max_depth=max_depth)
    body = body.replace("root = ?", "n.root = ?").replace("path", "n.path").replace("data", "n.data")
    placeholders = ", ".join("?" for _ in positives)
    sql = f"""
        SELECT n.path AS path, n.data AS data
        FROM q_tokens t
        JOIN q_entities e ON e.entity_id = t.entity_id
        JOIN nodes n ON n.root = ? AND n.path = REPLACE(e.canonical_path, '.', '/')
        WHERE t.token IN ({placeholders})
          AND {body}
        GROUP BY n.path, n.data
        HAVING COUNT(DISTINCT t.token) >= ?
        ORDER BY {_order_sql(order)}
    """
    args: list[Any] = [root, *[t.lower() for t in positives], *base_args, len(positives)]
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    return sql, args


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class SqlBackend:
    """Direct read-only SQLite. One connection per thread."""

    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        self._conns: dict[int, sqlite3.Connection] = {}

    def clone(self) -> "SqlBackend":
        return SqlBackend(self.db_path)

    def _conn(self) -> sqlite3.Connection:
        tid = threading.get_ident()
        conn = self._conns.get(tid)
        if conn is None:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False)
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA mmap_size = 1073741824")
            conn.execute("PRAGMA cache_size = -131072")
            conn.execute("PRAGMA busy_timeout = 30000")
            self._conns[tid] = conn
        return conn

    def _rows(self, sql: str, args: list[Any]) -> list[tuple[str, Any]]:
        return [(p, json.loads(d) if d else None) for p, d in self._conn().execute(sql, args)]

    def roots(self) -> list[str]:
        try:
            return [r for (r,) in self._conn().execute(
                "SELECT DISTINCT root FROM nodes WHERE parent_id IS NULL ORDER BY root"
            )]
        except Exception:
            return []

    def count_records(self, *, root, prefix, has_data, exclude, max_depth) -> int:
        body, args = _base_where(root=root, prefix=prefix, has_data=has_data, exclude=exclude, max_depth=max_depth)
        (c,) = self._conn().execute(f"SELECT COUNT(*) FROM nodes WHERE {body}", args).fetchone()
        return int(c)

    def scan_records(self, **kw: Any) -> list[tuple[str, Any]]:
        sql, args = _build_scan(keys_only=False, **kw)
        return self._rows(sql, args)

    def scan_keys(self, **kw: Any) -> list[str]:
        sql, args = _build_scan(keys_only=True, limit=None, offset=0, **kw)
        return [p for (p,) in self._conn().execute(sql, args)]

    def fetch_records(self, *, root: str, paths: list[str]) -> dict[str, Any]:
        conn = self._conn()
        st = "SELECT data FROM nodes WHERE root = ? AND path = ?"
        out: dict[str, Any] = {}
        for p in paths:
            row = conn.execute(st, (root, p)).fetchone()
            if row and row[0]:
                out[p] = json.loads(row[0])
        return out

    def text_records(self, *, mode: str, strict: bool, **kw: Any) -> list[tuple[str, Any]]:
        if mode == "index":
            kw.pop("negatives", None)
            sql, args = _build_text_index(positives=list(kw.pop("positives")), **kw)
            return self._rows(sql, args)
        sql, args = _build_text_scan(strict=strict, **kw)
        return self._rows(sql, args)


class ApiBackend:
    """Talks to the integrated relay fast read API under /api/*."""

    PAGE = 100000

    def __init__(self, base_url: str = "http://127.0.0.1:8765", timeout: int = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def clone(self) -> "ApiBackend":
        return ApiBackend(self.base_url, self.timeout)

    def _get(self, path: str, params: list[tuple[str, Any]]) -> dict[str, Any]:
        qs = urllib.parse.urlencode([(k, str(v)) for k, v in params if v is not None], doseq=True)
        with urllib.request.urlopen(f"{self.base_url}{path}?{qs}", timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _params(root, prefix, has_data, exclude, max_depth) -> list[tuple[str, Any]]:
        params: list[tuple[str, Any]] = [
            ("root", root), ("prefix", prefix), ("has_data", "1" if has_data else "0"),
        ]
        if exclude:
            params.append(("exclude", ",".join(exclude)))
        if max_depth is not None:
            params.append(("max_depth", max_depth))
        return params

    def roots(self) -> list[str]:
        try:
            return list(self._get("/api/roots", []).get("roots", []))
        except Exception:
            return []

    def count_records(self, *, root, prefix, has_data, exclude, max_depth) -> int:
        return int(self._get("/api/count", self._params(root, prefix, has_data, exclude, max_depth)).get("count", 0))

    def scan_records(self, *, root, prefix, order, limit, offset, has_data, exclude, max_depth) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        remaining = limit
        cur = offset
        while True:
            page = self.PAGE if remaining is None else min(self.PAGE, remaining)
            params = self._params(root, prefix, has_data, exclude, max_depth) + [("order", order), ("limit", page), ("offset", cur)]
            rows = self._get("/api/scan", params).get("rows", [])
            out.extend((r["path"], r["data"]) for r in rows)
            cur += len(rows)
            if remaining is not None:
                remaining -= len(rows)
            if len(rows) < page or (remaining is not None and remaining <= 0):
                break
        return out

    def scan_keys(self, *, root, prefix, order, has_data, exclude, max_depth) -> list[str]:
        out: list[str] = []
        cur = 0
        while True:
            params = self._params(root, prefix, has_data, exclude, max_depth) + [("order", order), ("keys_only", "1"), ("limit", self.PAGE), ("offset", cur)]
            keys = self._get("/api/scan", params).get("keys", [])
            out.extend(keys)
            cur += len(keys)
            if len(keys) < self.PAGE:
                break
        return out

    def fetch_records(self, *, root: str, paths: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for i in range(0, len(paths), self.PAGE):
            rows = self._post("/api/records", {"root": root, "paths": paths[i:i + self.PAGE]}).get("rows", [])
            for r in rows:
                out[r["path"]] = r["data"]
        return out

    def text_records(self, *, root, prefix, positives, negatives, order, limit, mode, strict, has_data, exclude, max_depth) -> list[tuple[str, Any]]:
        params = self._params(root, prefix, has_data, exclude, max_depth) + [
            ("order", order), ("limit", limit), ("mode", mode), ("strict", "1" if strict else "0"),
        ]
        for t in positives:
            params.append(("pos", t))
        for t in negatives:
            params.append(("neg", t))
        rows = self._get("/api/text", params).get("rows", [])
        return [(r["path"], r["data"]) for r in rows]


# ---------------------------------------------------------------------------
# HyperSearch
# ---------------------------------------------------------------------------

class HyperSearch:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8765",
        *,
        backend: str = "api",
        db_path: str | None = None,
        api_url: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.backend_kind = backend

        # Relay client kept for stream() — streaming contract unchanged.
        try:
            from HyperCoreSDK.python.helpers.tool import HyperRelayClient
            self.client = HyperRelayClient(self.base_url)
        except Exception:
            self.client = None

        if backend == "sql":
            if not db_path:
                raise ValueError("backend='sql' requires db_path=")
            self._backend: Any = SqlBackend(os.path.expanduser(db_path))
        elif backend == "api":
            self._backend = ApiBackend(api_url or self.base_url)
        else:
            raise ValueError(f"unknown backend: {backend!r} (use 'sql' or 'api')")

    # -- query (fast) -----------------------------------------------------

    def query(
        self,
        text: str = "*",
        *,
        start: str = "/",
        options: SearchOptions | None = None,
        sources: dict[str, int | Source] | None = None,
        fields: Iterable[str] | None = None,
        show: ResultType | Iterable[ResultType] | None = None,
        exclude: Iterable[str] | None = None,
    ) -> SearchResults:
        options = options or SearchOptions()
        if sources is not None:
            options.sources = sources
        if fields is not None:
            options.fields = fields
        if show is not None:
            options.show = show
        if exclude is not None:
            options.exclude = exclude

        matcher = TextMatcher(text)
        normalized = options.normalized_sources()

        if not normalized:
            normalized = {r: Source(count=10 ** 12) for r in self._backend.roots()}

        if matcher.matches_all:
            return self._collect_sources(options, normalized)
        return self._text_sources(matcher, options, normalized)

    # -- stream (UNCHANGED — original relay traversal, same contract) -----

    def stream(
        self,
        text: str = "*",
        *,
        start: str = "/",
        options: SearchOptions | None = None,
    ) -> Iterable[str]:
        options = options or SearchOptions(details=True, summary=True)
        stream = walk_graph_stream(
            self.client,
            start,
            policy=TraversalPolicy(
                blacklisted_path_parts=set(options.exclude),
                include_paths=list(options.include_paths),
                exclude_paths=list(options.exclude_paths),
                seed=options.seed,
            ),
            config=options.stream_config(for_stream=True),
        )
        return render_stream(stream, flat=not options.stream_config(for_stream=True).show_directories)

    # -- collect ----------------------------------------------------------

    def _collect_sources(self, options: SearchOptions, sources: dict[str, Source]) -> SearchResults:
        items = list(sources.items())
        workers = max(1, min(options.source_workers, len(items)))
        rows: list[Any] = []
        errors = 0

        def run(item):
            path, source = item
            return self._collect_one(self._backend.clone(), path, source, options)

        if workers == 1:
            for it in items:
                try:
                    rows.extend(run(it))
                except Exception:
                    errors += 1
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(run, it) for it in items]
                for f in as_completed(futures):
                    try:
                        rows.extend(f.result())
                    except Exception:
                        errors += 1

        return SearchResults(rows, errors)

    def _collect_one(self, backend: Any, source_path: str, source: Source, options: SearchOptions) -> list[Any]:
        if source.count <= 0:
            return []

        root, prefix = _split_source(source_path)
        fields = options.normalized_fields()
        order = source.order or options.order
        max_depth = source.max_depth if source.max_depth is not None else options.max_depth
        exclude = list(options.exclude)
        seed = source.seed if source.seed is not None else options.seed

        exact = source.pick == "exact_random" or (source.pick == "random" and options.random_exact)
        approx = source.pick in {"random", "fast_random"} and not options.random_exact

        if source.pick == "top":
            raw = backend.scan_records(
                root=root, prefix=prefix, order=order, limit=source.count, offset=0,
                has_data=True, exclude=exclude, max_depth=max_depth,
            )
            return [_extract_row(p, d, fields) for p, d in raw if _app_data(d)]

        if exact or approx:
            total = backend.count_records(root=root, prefix=prefix, has_data=True, exclude=exclude, max_depth=max_depth)

            if source.count >= total:
                raw = backend.scan_records(
                    root=root, prefix=prefix, order="key_asc", limit=source.count, offset=0,
                    has_data=True, exclude=exclude, max_depth=max_depth,
                )
                return [_extract_row(p, d, fields) for p, d in raw if _app_data(d)]

            keys = backend.scan_keys(root=root, prefix=prefix, order="key_asc", has_data=True, exclude=exclude, max_depth=max_depth)
            rng = random.Random(f"{seed}:{source_path}")
            chosen = rng.sample(keys, min(source.count, len(keys)))
            data_map = backend.fetch_records(root=root, paths=chosen)
            rows = [_extract_row(p, data_map[p], fields) for p in chosen if p in data_map and _app_data(data_map[p])]
            rows.sort(key=lambda r: str(r.get("path", "")))
            return rows

        raise ValueError(f"unknown pick mode: {source.pick!r}")

    # -- text -------------------------------------------------------------

    def _text_sources(self, matcher: TextMatcher, options: SearchOptions, sources: dict[str, Source]) -> SearchResults:
        positives = list(matcher.positive)
        negatives = list(matcher.negative)
        fields = options.normalized_fields()
        strict = options.strict_text
        rows: list[Any] = []
        errors = 0
        indexed = 0
        scanned = 0

        for source_path, source in sources.items():
            if source.count <= 0:
                continue
            root, prefix = _split_source(source_path)
            order = source.order or options.order
            max_depth = source.max_depth if source.max_depth is not None else options.max_depth
            exclude = list(options.exclude)
            mode = options.text_mode

            try:
                src_rows: list[tuple[str, Any]] = []

                if mode in {"index", "hybrid"}:
                    idx = self._backend.text_records(
                        root=root, prefix=prefix, positives=positives, negatives=negatives,
                        order=order, limit=source.count, mode="index", strict=strict,
                        has_data=True, exclude=exclude, max_depth=max_depth,
                    )
                    idx = [(p, d) for p, d in idx if matcher.matches(_extract_row(p, d, fields))]
                    indexed += len(idx)
                    src_rows = idx

                need_scan = mode == "scan" or (
                    mode == "hybrid" and (
                        len(src_rows) < options.min_index_hits
                        or (options.hybrid_fill_to_count and len(src_rows) < source.count)
                    )
                )

                if need_scan:
                    seen = {p for p, _ in src_rows}
                    sc = self._backend.text_records(
                        root=root, prefix=prefix, positives=positives, negatives=negatives,
                        order=order, limit=source.count, mode="scan", strict=strict,
                        has_data=True, exclude=exclude, max_depth=max_depth,
                    )
                    scanned += len(sc)
                    for p, d in sc:
                        if p not in seen and _app_data(d):
                            seen.add(p)
                            src_rows.append((p, d))

                src_rows = src_rows[: source.count]
                rows.extend(_extract_row(p, d, fields) for p, d in src_rows if _app_data(d))
            except Exception:
                errors += 1

        total_requested = sum(max(0, s.count) for s in sources.values())
        return SearchResults(rows[:total_requested], errors, scanned=scanned, indexed=indexed)


#!/usr/bin/env python3
def hyper_search_cli():
    import argparse, json, os

    URL = os.getenv("HYPER_RELAY_URL", "http://127.0.0.1:8765").rstrip("/")
    EXCLUDE = ("_meta", "_mdr")

    p = argparse.ArgumentParser(prog="hyper-search")
    p.add_argument("query", nargs="+")
    p.add_argument("--source", "-s", default="/")
    p.add_argument("--count", "-n", type=int, default=20)
    p.add_argument("--backend", choices=("api", "sql"), default="api")
    p.add_argument("--db-path")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    source = args.source.strip().strip("/").replace("/", ".")
    search = HyperSearch(URL, backend=args.backend, db_path=args.db_path)

    res = search.query(
        " ".join(args.query),
        options=SearchOptions(
            exclude=EXCLUDE,
            fields=("path", "data"),
            sources={source: Source(count=args.count)} if source else {},
        ),
    )

    rows = res.to_list()

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return

    for i, row in enumerate(rows, 1):
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        label = data.get("name") or data.get("title") or data.get("display") or ""
        print(f"{i:>3}. {row.get('path')} {label}".rstrip())