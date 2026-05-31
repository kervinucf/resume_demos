# HyperCoreSDK/python/client.py
"""
Python client for the HyperCore relay.
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional


DEFAULT_PORT = 8765
DEFAULT_URL = f"http://127.0.0.1:{DEFAULT_PORT}"
DEFAULT_DATA_DIR = str(Path.home() / ".hyper-data")
RELAY_START_TIMEOUT_S = 20.0
HEALTH_TIMEOUT_S = 2.0
DEFAULT_HTTP_TIMEOUT_S = 120.0


def dot(path: str) -> str:
    return str(path or "").strip().lstrip("/").replace("/", ".").strip(".")


def slash(path: str) -> str:
    return str(path or "").strip().strip(".").replace(".", "/").lstrip("/")


class HyperError(Exception):
    pass


class HyperNotFound(HyperError):
    pass


class HyperHttpError(HyperError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class DeltaEvent:
    kind: str
    path: str
    data: Any = None
    commit_seq: int = 0
    updated_at: int = 0
    snapshot: Optional[dict] = None


def _http(
    url: str,
    method: str = "GET",
    *,
    data: Optional[dict] = None,
    timeout: float = DEFAULT_HTTP_TIMEOUT_S,
) -> Any:
    content = None
    headers = {"Accept": "application/json"}

    if data is not None:
        content = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, method=method, data=content, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            if not raw:
                return None

            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw

    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")

        try:
            payload = json.loads(raw)
        except Exception:
            payload = raw

        if exc.code == 404:
            raise HyperNotFound(f"{url}: {payload}") from exc

        raise HyperHttpError(exc.code, f"HTTP {exc.code}: {payload}") from exc


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _parse_url(url: str) -> tuple[str, int]:
    p = urllib.parse.urlparse(url)
    return p.hostname or "127.0.0.1", p.port or DEFAULT_PORT


def _path_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{urllib.parse.quote(dot(path), safe='.')}"


def _sse_url(base: str, path: str, *, scope: str) -> str:
    return (
        f"{base.rstrip('/')}/{urllib.parse.quote(dot(path), safe='.')}"
        f"?stream=true&scope={urllib.parse.quote(scope)}"
    )


def _add_many(params: list[tuple[str, str]], key: str, value: str | Iterable[str] | None) -> None:
    if value is None:
        return

    if isinstance(value, str):
        if value:
            params.append((key, value))
        return

    for item in value:
        text = str(item)
        if text:
            params.append((key, text))


def _pair_params(
    params: list[tuple[str, str]],
    key: str,
    values: dict[str, Any] | None,
    *,
    cast: Callable[[Any], Any] = str,
) -> None:
    for name, value in (values or {}).items():
        if value is None:
            continue
        params.append((key, f"{name}:{cast(value)}"))


class _SSEStream:
    def __init__(
        self,
        url: str,
        on_frame: Callable[[dict], None],
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        self.url = url
        self._on_frame = on_frame
        self._on_error = on_error
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"sse:{self.url}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        backoff = 0.5

        while not self._stop.is_set():
            try:
                self._read_once()
                if self._stop.is_set():
                    return

                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

            except Exception as exc:
                if self._on_error:
                    try:
                        self._on_error(exc)
                    except Exception:
                        pass

                if self._stop.is_set():
                    return

                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    def _read_once(self) -> None:
        req = urllib.request.Request(self.url, headers={"Accept": "text/event-stream"})

        with urllib.request.urlopen(req) as resp:
            buf = ""

            while not self._stop.is_set():
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not chunk:
                    return

                buf += chunk.decode("utf-8", "replace")

                while "\n\n" in buf:
                    event, buf = buf.split("\n\n", 1)
                    self._dispatch(event)

    def _dispatch(self, event: str) -> None:
        lines = []

        for line in event.split("\n"):
            if line.startswith(":") or not line.startswith("data:"):
                continue
            lines.append(line[5:].lstrip())

        if not lines:
            return

        try:
            parsed = json.loads("\n".join(lines))
        except json.JSONDecodeError:
            return

        if isinstance(parsed, dict):
            self._on_frame(parsed)


class ChildrenResult(dict):
    def items(self) -> list[dict[str, Any]]:  # type: ignore[override]
        embedded = self.get("_embedded")

        if isinstance(embedded, dict):
            children = embedded.get("children")

            if isinstance(children, dict):
                return [v for v in children.values() if isinstance(v, dict)]

            return [v for v in embedded.values() if isinstance(v, dict)]

        rows = self.get("rows")
        if isinstance(rows, list):
            return [v for v in rows if isinstance(v, dict)]

        return []


@dataclass
class BulkWriter:
    client: "HyperClient"
    root: str
    ops: list[dict[str, Any]] = field(default_factory=list)

    def write(self, path: str, data: dict[str, Any]) -> None:
        self.ops.append({"path": dot(path), "data": data})

    def delete(self, path: str) -> None:
        self.ops.append({"path": dot(path), "delete": True})

    def flush(self) -> dict[str, Any]:
        if not self.ops:
            return {"ok": True, "count": 0}

        ops = self.ops
        self.ops = []
        return self.client.batch(root=self.root, ops=ops)

    def __enter__(self) -> "BulkWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.flush()


class HyperClient:
    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        root: str = "",
        process: subprocess.Popen | None = None,
        data_dir: str | None = None,
        peers: list[str] | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.root = dot(root)
        self._owned_process = process
        self._owned_data_dir = data_dir
        self._peers = peers or [self.url]
        self._streams: list[_SSEStream] = []

        if process is not None:
            atexit.register(self.close)

    @classmethod
    def attach(cls, url: str = DEFAULT_URL, *, root: str = "") -> "HyperClient":
        return cls(url, root=root)

    @classmethod
    def peer(cls, urls: list[str], *, root: str = "") -> "HyperClient":
        if not urls:
            raise ValueError("peer() requires at least one url")
        return cls(urls[0], root=root, peers=urls)

    @classmethod
    def spawn(
        cls,
        data_dir: str | None = None,
        *,
        root: str = "",
        port: int = DEFAULT_PORT,
        bind: str = "127.0.0.1",
        relay_script: str | None = None,
        workers: int | None = None,
        ensure_node_deps: bool = False,
        env: dict[str, str] | None = None,
    ) -> "HyperClient":
        url = f"http://127.0.0.1:{port}"
        host, parsed_port = _parse_url(url)

        if _port_open(host, parsed_port):
            return cls.attach(url, root=root)

        script = relay_script or os.environ.get("HYPER_RELAY_SCRIPT") or "relay.js"
        data_dir = data_dir or DEFAULT_DATA_DIR

        merged_env = os.environ.copy()
        merged_env.update(env or {})
        merged_env["PORT"] = str(port)
        merged_env["HYPER_BIND_HOST"] = bind
        merged_env["HYPER_DATA_DIR"] = data_dir

        if workers is not None:
            merged_env["HYPER_WORKERS"] = str(workers)

        process = subprocess.Popen(
            ["node", script],
            env=merged_env,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )

        deadline = time.time() + RELAY_START_TIMEOUT_S

        while time.time() < deadline:
            try:
                health = _http(f"{url}/health", timeout=HEALTH_TIMEOUT_S)
                if health and health.get("ok"):
                    return cls(url, root=root, process=process, data_dir=data_dir)
            except Exception:
                pass
            time.sleep(0.1)

        try:
            process.terminate()
        except Exception:
            pass

        raise TimeoutError(f"relay did not start at {url}")

    @staticmethod
    def _health_ok(url: str = DEFAULT_URL) -> bool:
        try:
            health = _http(f"{url.rstrip('/')}/health", timeout=HEALTH_TIMEOUT_S)
            return bool(health and health.get("ok"))
        except Exception:
            return False

    def owns_relay(self) -> bool:
        return self._owned_process is not None

    @property
    def _owned(self):
        class Owned:
            def __init__(self, process):
                self.process = process

        return Owned(self._owned_process)

    def close(self) -> None:
        for stream in list(self._streams):
            try:
                stream.stop()
            except Exception:
                pass

        self._streams.clear()

        if self._owned_process and self._owned_process.poll() is None:
            try:
                self._owned_process.terminate()
                self._owned_process.wait(timeout=3)
            except Exception:
                try:
                    self._owned_process.kill()
                except Exception:
                    pass

        self._owned_process = None

    # ------------------------------------------------------------------
    # URL / HTTP
    # ------------------------------------------------------------------

    def _url(self, path: str = "") -> str:
        if not path:
            return self.url
        return _path_url(self.url, path)

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        data: dict | None = None,
        timeout: float = DEFAULT_HTTP_TIMEOUT_S,
    ) -> Any:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
        elif path_or_url.startswith("/"):
            url = f"{self.url}{path_or_url}"
        else:
            url = self._url(path_or_url)

        return _http(url, method=method, data=data, timeout=timeout)

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    # ------------------------------------------------------------------
    # Basic graph reads/writes
    # ------------------------------------------------------------------

    def roots(self) -> list[str]:
        doc = self._request("GET", "/")
        links = doc.get("_links", {}) if isinstance(doc, dict) else {}
        return sorted(k for k in links if k not in {"self", "stream", "changes_since", "parent"})

    def read(self, path: str) -> dict | None:
        return self._request("GET", dot(path))

    def read_with_embeds(self, path: str, embeds: list[str] | None = None) -> dict | None:
        doc = self.read(path)
        if not doc or not embeds:
            return doc

        out = dict(doc)
        resolved = {}
        links = doc.get("_links", {}) if isinstance(doc, dict) else {}

        for rel in embeds:
            href = links.get(rel)
            if not href:
                continue

            try:
                resolved[rel] = self._request("GET", href)
            except Exception:
                resolved[rel] = None

        out["_resolved"] = resolved
        return out

    def children(
        self,
        path: str,
        *,
        page: int = 1,
        per_page: int = 100,
        order: str = "key_asc",
    ) -> ChildrenResult:
        q = urllib.parse.urlencode({
            "page": int(page),
            "per_page": int(per_page),
            "order": order,
        })
        doc = self._request("GET", f"/{urllib.parse.quote(dot(path), safe='.')}/api/children?{q}")
        return ChildrenResult(doc or {})

    def write(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", dot(path), data=data)

    def delete(self, path: str) -> dict[str, Any]:
        return self._request("DELETE", dot(path))

    def batch(self, *, root: str | None = None, ops: list[dict[str, Any]]) -> dict[str, Any]:
        endpoint = f"/{dot(root)}/api/batch" if root else "/api/batch"
        return self._request("POST", endpoint, data={"ops": ops}, timeout=DEFAULT_HTTP_TIMEOUT_S)

    def bulk(self, *, root: str) -> BulkWriter:
        return BulkWriter(self, root=dot(root))

    # ------------------------------------------------------------------
    # KISS write API
    # ------------------------------------------------------------------

    def write_thing(
        self,
        *,
        path: str,
        kind: str,
        name: str,
        content: dict[str, Any],
        links: dict[str, Any] | None = None,
        search: Iterable[Any] | None = None,
        target: str | None = None,
        properties: dict[str, Any] | None = None,
        values: dict[str, Any] | None = None,
        dates: dict[str, Any] | None = None,
    ):
        """
        KISS write API.

        Write one normal object. The SDK infers how it should be searchable.
        """
        from HyperCoreSDK.python.helpers.records import write_thing

        return write_thing(
            self,
            path=path,
            kind=kind,
            name=name,
            content=content,
            links=links,
            search=search,
            target=target,
            properties=properties,
            values=values,
            dates=dates,
        )

    def put(
        self,
        *,
        path: str,
        kind: str,
        name: str,
        content: dict[str, Any],
        links: dict[str, Any] | None = None,
        search: Iterable[Any] | None = None,
        target: str | None = None,
        properties: dict[str, Any] | None = None,
        values: dict[str, Any] | None = None,
        dates: dict[str, Any] | None = None,
    ):
        """
        Short alias for write_thing().
        """
        return self.write_thing(
            path=path,
            kind=kind,
            name=name,
            content=content,
            links=links,
            search=search,
            target=target,
            properties=properties,
            values=values,
            dates=dates,
        )

    def write_link(
        self,
        *,
        source: str,
        rel: str,
        target: str,
        content: dict[str, Any] | None = None,
        links: dict[str, Any] | None = None,
        kind: str = "link",
        name: str | None = None,
        search: Iterable[Any] | None = None,
    ):
        """
        Write a browsable relationship sidecar:
            <source>.refs.<rel> -> target
        """
        from HyperCoreSDK.python.helpers.records import write_link

        return write_link(
            self,
            source=source,
            rel=rel,
            target=target,
            content=content,
            links=links,
            kind=kind,
            name=name,
            search=search,
        )

    def link(
        self,
        *,
        source: str,
        rel: str,
        target: str,
        content: dict[str, Any] | None = None,
        links: dict[str, Any] | None = None,
        kind: str = "link",
        name: str | None = None,
        search: Iterable[Any] | None = None,
    ):
        """
        Short alias for write_link().
        """
        return self.write_link(
            source=source,
            rel=rel,
            target=target,
            content=content,
            links=links,
            kind=kind,
            name=name,
            search=search,
        )

   # ------------------------------------------------------------------
    # Legacy higher-level helpers
    # ------------------------------------------------------------------

    def select_records(
        self,
        *,
        root_key: str,
        path: str,
        _return,
        index: dict | None = None,
        where=None,
        limit: int | None = None,
        per_page: int = 200,
    ):
        from HyperCoreSDK.python.helpers.records import select_records

        return select_records(
            self,
            root=root_key,
            collection=path,
            from_data=_return,
            by=index,
            where=where,
            limit=limit,
            per_page=per_page,
        )

    def write_record_with_indexes(
        self,
        *,
        root: str,
        record_path: str,
        record_data: dict,
        index_specs: list | None = None,
        ref_key: str | None = None,
        ref_payload: dict | None = None,
        links: dict | None = None,
        actions: dict | None = None,
    ) -> int:
        from HyperCoreSDK.python.helpers.records import write_record_with_indexes

        return write_record_with_indexes(
            self,
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
        self,
        *,
        path: str,
        target: str,
        data: dict,
        links: dict | None = None,
        query: dict | None = None,
    ):
        from HyperCoreSDK.python.helpers.records import write_pointer

        return write_pointer(
            self,
            path=path,
            target=target,
            data=data,
            links=links,
            query=query,
        )

    def write_backref(
        self,
        *,
        source: str,
        rel: str,
        target: str,
        data: dict,
        links: dict | None = None,
        query: dict | None = None,
    ):
        from HyperCoreSDK.python.helpers.records import write_backref

        return write_backref(
            self,
            source=source,
            rel=rel,
            target=target,
            data=data,
            links=links,
            query=query,
        )

    # ------------------------------------------------------------------
    # Writers
    # ------------------------------------------------------------------

    @classmethod
    def direct(
        cls,
        *,
        data_dir: str | None = None,
        root: str = "",
        reset: bool = False,
        write_outbox: bool = False,
        flush_every_rows: int = 1_000_000,
    ):
        """
        Open a trusted local direct-SQLite writer for high-throughput bulk ingest.
        """
        from HyperCoreSDK.python.helpers.direct import HyperDirect

        return HyperDirect(
            data_dir=data_dir or DEFAULT_DATA_DIR,
            root=root,
            reset=reset,
            write_outbox=write_outbox,
            flush_every_rows=flush_every_rows,
        )

    def writer(self, *, batch_ops: int = 9_000):
        """
        Create a buffered HTTP writer.
        """
        from HyperCoreSDK.python.helpers.writer import HyperHttpWriter

        return HyperHttpWriter(self, batch_ops=batch_ops)

    @classmethod
    def direct_writer(
        cls,
        *,
        data_dir: str | None = None,
        root: str = "",
        reset: bool = False,
        bulk: bool = False,
        batch_ops: int = 9_000,
        flush_every_rows: int = 1_000_000,
        write_outbox: bool = False,
        skip_memberships: bool = False,
        drop_parent_lookup_index: bool = True,
    ):
        """
        Create a buffered direct-SQLite writer.
        """
        from HyperCoreSDK.python.helpers.writer import HyperDirectWriter

        return HyperDirectWriter(
            data_dir=data_dir or DEFAULT_DATA_DIR,
            root=root,
            reset=reset,
            bulk=bulk,
            batch_ops=batch_ops,
            flush_every_rows=flush_every_rows,
            write_outbox=write_outbox,
            skip_memberships=skip_memberships,
            drop_parent_lookup_index=drop_parent_lookup_index,
        )

    # ------------------------------------------------------------------
    # Query APIs
    # ------------------------------------------------------------------

    def find_things(
        self,
        *,
        kind: str | None = None,
        search: str | list[str] | None = None,
        properties: dict[str, str | bool | int | float] | None = None,
        linked_to: dict[str, str] | None = None,
        has_link: str | list[str] | None = None,
        value_gte: dict[str, float] | None = None,
        value_lte: dict[str, float] | None = None,
        value_between: dict[str, tuple[float, float]] | None = None,
        near: tuple[str, str, float, float, str | float] | None = None,
        near_mode: str = "auto",
        connected: int | str = 0,
        date_gte: dict[str, int] | None = None,
        date_lte: dict[str, int] | None = None,
        include: str = "facets,refs,numbers,times",
        limit: int = 100,
    ) -> dict[str, Any]:
        """
        Friendly query API.

        Examples:
            client.find_things(search="chicago")

            client.find_things(
                kind="weather_latest",
                properties={"country_code": "US"},
            )

            client.find_things(
                near=("lat", "lon", 41.8781, -87.6298, "100km"),
                near_mode="geo",
                connected=2,
            )
        """
        params: list[tuple[str, str]] = []

        if kind:
            params.append(("type", kind))

        params.append(("limit", str(int(limit))))
        params.append(("include", include))

        for name, value in (properties or {}).items():
            if value is True:
                encoded_value = "true"
            elif value is False:
                encoded_value = "false"
            else:
                encoded_value = str(value)
            params.append(("facet", f"{name}:{encoded_value}"))

        _add_many(params, "q", search)
        _add_many(params, "has_ref", has_link)

        _pair_params(params, "ref", linked_to, cast=str)
        _pair_params(params, "number_gte", value_gte, cast=float)
        _pair_params(params, "number_lte", value_lte, cast=float)
        _pair_params(params, "time_gte", date_gte, cast=int)
        _pair_params(params, "time_lte", date_lte, cast=int)

        for name, pair in (value_between or {}).items():
            lo, hi = pair
            params.append(("number_between", f"{name}:{float(lo)}:{float(hi)}"))

        if near is not None:
            x_name, y_name, x, y, distance = near
            params.append(("radius", f"{x_name}:{y_name}:{float(x)}:{float(y)}:{distance}"))
            params.append(("radius_mode", near_mode))

            if isinstance(connected, int):
                params.append(("measure_scope", "direct" if connected <= 0 else f"refs:{connected}"))
            else:
                params.append(("measure_scope", str(connected)))

        suffix = urllib.parse.urlencode(params)
        return self._request("GET", f"/api/query/entities?{suffix}")

    def query_entities(
        self,
        *,
        entity_type: str | None = None,
        facets: dict[str, str | bool | int | float] | None = None,
        q: str | list[str] | None = None,
        has_ref: str | list[str] | None = None,
        refs: dict[str, str] | None = None,
        cells: dict[str, str] | None = None,
        number_gte: dict[str, float] | None = None,
        number_lte: dict[str, float] | None = None,
        number_between: dict[str, tuple[float, float]] | None = None,
        radius: tuple[str, str, float, float, str | float] | None = None,
        radius_mode: str = "auto",
        measure_scope: str = "direct",
        time_gte: dict[str, int] | None = None,
        time_lte: dict[str, int] | None = None,
        include: str = "facets,refs,numbers,times",
        limit: int = 100,
    ) -> dict[str, Any]:
        """
        Lower-level compatibility query API.

        Prefer find_things() in new scripts.
        """
        params: list[tuple[str, str]] = []

        if entity_type:
            params.append(("type", entity_type))

        params.append(("limit", str(int(limit))))
        params.append(("include", include))

        _pair_params(
            params,
            "facet",
            facets,
            cast=lambda v: "true" if v is True else "false" if v is False else str(v),
        )
        _add_many(params, "q", q)
        _add_many(params, "has_ref", has_ref)

        _pair_params(params, "ref", refs, cast=str)
        _pair_params(params, "cell", cells, cast=str)
        _pair_params(params, "number_gte", number_gte, cast=float)
        _pair_params(params, "number_lte", number_lte, cast=float)
        _pair_params(params, "time_gte", time_gte, cast=int)
        _pair_params(params, "time_lte", time_lte, cast=int)

        for name, pair in (number_between or {}).items():
            lo, hi = pair
            params.append(("number_between", f"{name}:{float(lo)}:{float(hi)}"))

        if radius is not None:
            x_name, y_name, x, y, distance = radius
            params.append(("radius", f"{x_name}:{y_name}:{float(x)}:{float(y)}:{distance}"))
            params.append(("radius_mode", radius_mode))
            params.append(("measure_scope", measure_scope))

        suffix = urllib.parse.urlencode(params)
        return self._request("GET", f"/api/query/entities?{suffix}")

    # ------------------------------------------------------------------
    # Watch APIs
    # ------------------------------------------------------------------

    def watch(
        self,
        path: str,
        callback: Callable[[DeltaEvent], None] | None = None,
        *,
        scope: str = "subtree",
        on_error: Callable[[Exception], None] | None = None,
    ) -> Iterator[DeltaEvent] | None:
        q: queue.Queue[DeltaEvent] = queue.Queue()

        def on_frame(frame: dict) -> None:
            state = frame.get("_state", {}) if isinstance(frame, dict) else {}
            if state.get("kind") != "delta":
                return

            for change in frame.get("changes", []) or []:
                ev = DeltaEvent(
                    kind=change.get("op") or "put",
                    path=change.get("path") or "",
                    data=change.get("data"),
                    commit_seq=int(change.get("commit_seq") or 0),
                    updated_at=int(change.get("updated_at") or 0),
                )

                if callback:
                    callback(ev)
                else:
                    q.put(ev)

        stream = _SSEStream(_sse_url(self.url, path, scope=scope), on_frame, on_error=on_error)
        self._streams.append(stream)
        stream.start()

        if callback:
            return None

        def iterator() -> Iterator[DeltaEvent]:
            try:
                while True:
                    yield q.get()
            finally:
                stream.stop()
                try:
                    self._streams.remove(stream)
                except ValueError:
                    pass

        return iterator()

    def hydrate_watch(
        self,
        path: str,
        *,
        scope: str = "subtree",
        on_error: Callable[[Exception], None] | None = None,
    ) -> Iterator[DeltaEvent]:
        snapshot = self.read(path)
        initial_seq = int(((snapshot or {}).get("_state") or {}).get("commit_seq") or 0)

        yield DeltaEvent(
            kind="initial",
            path=dot(path),
            data=snapshot.get("data") if isinstance(snapshot, dict) else None,
            commit_seq=initial_seq,
            snapshot=snapshot if isinstance(snapshot, dict) else None,
        )

        stream = self.watch(path, scope=scope, on_error=on_error)
        assert stream is not None

        for ev in stream:
            if ev.commit_seq and ev.commit_seq <= initial_seq:
                continue
            yield ev


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import signal

    argv = argv or sys.argv[1:]

    p = argparse.ArgumentParser(prog="client.py")
    p.add_argument("--url", default=os.getenv("HYPER_URL", DEFAULT_URL))
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("spawn")
    sp.add_argument("data_dir", nargs="?", default=None)
    sp.add_argument("--port", type=int, default=DEFAULT_PORT)
    sp.add_argument("--workers", type=int, default=int(os.getenv("HYPER_WORKERS", "1")))
    sp.add_argument("--relay-script", default=None)

    sub.add_parser("status")
    sub.add_parser("roots")

    sp = sub.add_parser("inspect")
    sp.add_argument("path")

    sp = sub.add_parser("children")
    sp.add_argument("path")
    sp.add_argument("--page", type=int, default=1)
    sp.add_argument("--per-page", type=int, default=100)

    sp = sub.add_parser("watch")
    sp.add_argument("path")
    sp.add_argument("--scope", choices=["exact", "subtree"], default="subtree")

    sp = sub.add_parser("write")
    sp.add_argument("path")
    sp.add_argument("payload")

    sp = sub.add_parser("delete")
    sp.add_argument("path")

    sp = sub.add_parser("query-entities")
    sp.add_argument("--type", default=None)
    sp.add_argument("--facet", action="append", default=[])
    sp.add_argument("--q", action="append", default=[])
    sp.add_argument("--has-ref", action="append", default=[])
    sp.add_argument("--ref", action="append", default=[])
    sp.add_argument("--cell", action="append", default=[])
    sp.add_argument("--number-gte", action="append", default=[])
    sp.add_argument("--number-lte", action="append", default=[])
    sp.add_argument("--number-between", action="append", default=[])
    sp.add_argument("--radius", default=None)
    sp.add_argument("--radius-mode", default="auto")
    sp.add_argument("--measure-scope", default="direct")
    sp.add_argument("--time-gte", action="append", default=[])
    sp.add_argument("--time-lte", action="append", default=[])
    sp.add_argument("--limit", type=int, default=100)

    sp = sub.add_parser("find")
    sp.add_argument("--kind", default=None)
    sp.add_argument("--search", action="append", default=[])
    sp.add_argument("--property", action="append", default=[])
    sp.add_argument("--linked-to", action="append", default=[])
    sp.add_argument("--has-link", action="append", default=[])
    sp.add_argument("--value-gte", action="append", default=[])
    sp.add_argument("--value-lte", action="append", default=[])
    sp.add_argument("--value-between", action="append", default=[])
    sp.add_argument("--near", default=None)
    sp.add_argument("--near-mode", default="auto")
    sp.add_argument("--connected", default="0")
    sp.add_argument("--limit", type=int, default=100)

    args = p.parse_args(argv)

    if args.cmd == "spawn":
        data_dir = str(Path(args.data_dir or DEFAULT_DATA_DIR).expanduser().resolve())
        c = HyperClient.spawn(
            data_dir,
            port=args.port,
            workers=args.workers,
            relay_script=args.relay_script,
        )
        print(f"hyper: healthy at {c.url}")

        def shutdown(*_):
            c.close()
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        if c._owned_process:
            c._owned_process.wait()

        return 0

    if args.cmd == "status":
        if not HyperClient._health_ok(args.url):
            print(f"no relay at {args.url}", file=sys.stderr)
            return 1

        c = HyperClient.attach(args.url)
        print(f"relay:  {args.url}")
        print(f"health: {json.dumps(c.health())}")
        print(f"roots:  {', '.join(c.roots()) or '(empty)'}")
        return 0

    if args.cmd == "roots":
        for r in HyperClient.attach(args.url).roots():
            print(r)
        return 0

    if args.cmd == "inspect":
        doc = HyperClient.attach(args.url).read(args.path)
        if doc is None:
            print(f"nothing at {args.path}", file=sys.stderr)
            return 1
        _print_json(doc)
        return 0

    if args.cmd == "children":
        _print_json(HyperClient.attach(args.url).children(
            args.path,
            page=args.page,
            per_page=args.per_page,
        ))
        return 0

    if args.cmd == "watch":
        c = HyperClient.attach(args.url)
        count = 0

        try:
            stream = c.watch(args.path, scope=args.scope)
            assert stream is not None

            for ev in stream:
                count += 1
                print(f"[{count}] seq={ev.commit_seq} {ev.kind} {ev.path}")

        except KeyboardInterrupt:
            print(f"\nstopped after {count} events")

        return 0

    if args.cmd == "write":
        raw = sys.stdin.read() if args.payload == "-" else args.payload

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"bad JSON: {exc}", file=sys.stderr)
            return 1

        result = HyperClient.attach(args.url).write(args.path, data)
        _print_json(result)
        return 0 if result.get("ok") else 1

    if args.cmd == "delete":
        result = HyperClient.attach(args.url).delete(args.path)
        _print_json(result)
        return 0 if result.get("ok") else 1

    def pairs(items: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for raw in items:
            if ":" in raw:
                k, v = raw.split(":", 1)
                out[k] = v
        return out

    def int_pairs(items: list[str]) -> dict[str, int]:
        out: dict[str, int] = {}
        for raw in items:
            if ":" in raw:
                k, v = raw.split(":", 1)
                out[k] = int(v)
        return out

    def float_pairs(items: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for raw in items:
            if ":" in raw:
                k, v = raw.split(":", 1)
                out[k] = float(v)
        return out

    def between_pairs(items: list[str]) -> dict[str, tuple[float, float]]:
        out: dict[str, tuple[float, float]] = {}
        for raw in items:
            parts = raw.split(":")
            if len(parts) >= 3:
                out[parts[0]] = (float(parts[1]), float(parts[2]))
        return out

    def parse_radius(raw: str | None):
        if not raw:
            return None

        parts = raw.split(":")
        if len(parts) < 5:
            raise ValueError("radius/near must be x:y:center_x:center_y:distance")

        return (parts[0], parts[1], float(parts[2]), float(parts[3]), parts[4])

    if args.cmd == "query-entities":
        result = HyperClient.attach(args.url).query_entities(
            entity_type=args.type,
            facets=pairs(args.facet),
            q=args.q or None,
            has_ref=args.has_ref or None,
            refs=pairs(args.ref),
            cells=pairs(args.cell),
            number_gte=float_pairs(args.number_gte),
            number_lte=float_pairs(args.number_lte),
            number_between=between_pairs(args.number_between),
            radius=parse_radius(args.radius),
            radius_mode=args.radius_mode,
            measure_scope=args.measure_scope,
            time_gte=int_pairs(args.time_gte),
            time_lte=int_pairs(args.time_lte),
            limit=args.limit,
        )
        _print_json(result)
        return 0

    if args.cmd == "find":
        connected: int | str
        try:
            connected = int(args.connected)
        except ValueError:
            connected = args.connected

        result = HyperClient.attach(args.url).find_things(
            kind=args.kind,
            search=args.search or None,
            properties=pairs(args.property),
            linked_to=pairs(args.linked_to),
            has_link=args.has_link or None,
            value_gte=float_pairs(args.value_gte),
            value_lte=float_pairs(args.value_lte),
            value_between=between_pairs(args.value_between),
            near=parse_radius(args.near),
            near_mode=args.near_mode,
            connected=connected,
            limit=args.limit,
        )
        _print_json(result)
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(_main())