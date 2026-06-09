# HyperCoreSDK/python/server.py
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
#
from HyperCoreSDK.python.helpers.utils import define_relay_script

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
    tag: str
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
            source=self._run,
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
    client: "HyperServer"
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


class HyperServer:
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
    def attach(cls, url: str = DEFAULT_URL, *, root: str = "") -> "HyperServer":
        return cls(url, root=root)

    @classmethod
    def peer(cls, urls: list[str], *, root: str = "") -> "HyperServer":
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
    ) -> "HyperServer":
        url = f"http://127.0.0.1:{port}"
        host, parsed_port = _parse_url(url)

        define_relay_script()

        if _port_open(host, parsed_port):
            return cls.attach(url, root=root)

        script = relay_script or os.environ.get("HYPER_RELAY_SCRIPT")
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
    # Basic database reads/writes
    # ------------------------------------------------------------------

    def roots(self) -> list[str]:
        doc = self._request("GET", "/")
        links = doc.get("_links", {}) if isinstance(doc, dict) else {}
        return sorted(k for k in links if k not in {"self", "stream", "changes_since", "parent"})

    def read(self, path: str) -> dict | None:
        return self._request("GET", dot(path))

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
            if state.get("tag") != "delta":
                return

            for change in frame.get("changes", []) or []:
                ev = DeltaEvent(
                    tag=change.get("op") or "put",
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
            tag="initial",
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
