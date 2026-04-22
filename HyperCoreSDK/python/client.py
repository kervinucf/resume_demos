# HyperCoreSDK/python/client.py
"""
Python client for the HyperCore relay.

Three construction modes:

    c = HyperClient.attach("http://127.0.0.1:8765")
    c = HyperClient.spawn(data_dir="./data")
    c = HyperClient.peer(["http://127.0.0.1:8765", "http://teammate.local:8765"])

The three verbs + subscription + bulk:

    c.read(path)                            -> state doc or None
    c.read_with_embeds(path, embeds=[...])  -> doc with _links rels resolved
    c.write(path, data)
    c.delete(path)
    c.watch(path, callback_or_iter)         -> delivers DeltaEvent objects
    with c.bulk(root="things") as b: ...
    c.batch(root="...", ops=[...])

About watch()
-------------
Unlike the old implementation, watch() does not deliver snapshots — it
delivers changes. The relay streams a cursor frame then delta frames
{op, path, data, commit_seq, updated_at}. The client flattens those into
a sequence of DeltaEvent objects. There is no client-side cache; each
change is delivered once, in order, and the server guarantees that.

If you want the initial state, ask for it explicitly:

    initial = c.read(path)              # bounded: just this node + children
    for delta in c.watch(path):         # changes from now onward
        ...

Or use hydrate_watch() which does both atomically (initial snapshot at
cursor C, then deltas strictly after C):

    for event in c.hydrate_watch(path):
        if event.kind == "initial":    # one per subscription, has .snapshot
            ...
        else:                          # .op in ('put','del'), .path, .data, .commit_seq
            ...
"""
from __future__ import annotations

import atexit
import concurrent.futures
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional


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


# ---------------------------------------------------------------------------
# Delta event — what watch() delivers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeltaEvent:
    """
    One delivered change.

    kind: "initial"  — the one-time snapshot from hydrate_watch()
          "put"      — a write; `data` is the new payload (may be None for
                       ancestor-only writes)
          "del"      — a deletion; `data` is None
          "cursor"   — informational; the subscription has advanced but has
                       no change for this path (rare; filtered out by default)

    path:        dotted path of the changed node
    data:        payload at the path (for put) or None
    commit_seq:  monotonic sequence number — use to dedupe across resyncs
    updated_at:  ms since epoch
    snapshot:    only set when kind == "initial"; full state doc at cursor
    """
    kind: str
    path: str
    data: Any = None
    commit_seq: int = 0
    updated_at: int = 0
    snapshot: Optional[dict] = None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _http(
    url: str,
    method: str = "GET",
    *,
    data: Optional[dict] = None,
    timeout: float = DEFAULT_HTTP_TIMEOUT_S,
) -> Any:
    body = None
    headers = {"Accept": "application/json"}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, method=method, data=body, headers=headers)
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


# ---------------------------------------------------------------------------
# SSE → DeltaEvent stream
# ---------------------------------------------------------------------------

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
            target=self._run, name=f"sse:{self.url}", daemon=True
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


class WatchHandle:
    def __init__(self, stream: _SSEStream) -> None:
        self._stream = stream

    def stop(self) -> None:
        self._stream.stop()

    def __enter__(self) -> "WatchHandle":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


class WatchIterator:
    """Iterator that blocks on new DeltaEvent objects until stop() is called."""
    def __init__(self, factory: Callable[[Callable[[dict], None]], _SSEStream]) -> None:
        self._q: "queue.Queue[Any]" = queue.Queue()
        self._sentinel = object()

        def handle_frame(frame: dict) -> None:
            for ev in _frames_to_events(frame, include_initial=False):
                self._q.put(ev)

        self._stream = factory(handle_frame)
        self._stream.start()

    def __iter__(self) -> Iterator[DeltaEvent]:
        return self

    def __next__(self) -> DeltaEvent:
        item = self._q.get()
        if item is self._sentinel:
            raise StopIteration
        return item

    def stop(self) -> None:
        self._stream.stop()
        self._q.put(self._sentinel)

    def __enter__(self) -> "WatchIterator":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def _frames_to_events(frame: dict, *, include_initial: bool) -> list[DeltaEvent]:
    """Translate a relay SSE frame into DeltaEvent objects."""
    state = frame.get("_state") if isinstance(frame, dict) else None
    if not isinstance(state, dict):
        return []
    kind = state.get("kind")

    if kind == "cursor":
        return []

    if kind == "delta":
        out: list[DeltaEvent] = []
        for c in frame.get("changes") or []:
            if not isinstance(c, dict):
                continue
            out.append(DeltaEvent(
                kind=str(c.get("op") or "put"),
                path=str(c.get("path") or ""),
                data=c.get("data"),
                commit_seq=int(c.get("commit_seq") or 0),
                updated_at=int(c.get("updated_at") or 0),
            ))
        return out

    return []


# ---------------------------------------------------------------------------
# Bulk
# ---------------------------------------------------------------------------

class BulkBatcher:
    def __init__(
        self,
        client: "HyperClient",
        *,
        root: Optional[str] = None,
        flush_every: int = 5000,
        flush_bytes: int = 8_000_000,
    ) -> None:
        self._c = client
        self._root = root
        self._flush_every = max(1, int(flush_every))
        self._flush_bytes = max(1, int(flush_bytes))
        self._ops: list[dict] = []
        self._bytes = 0
        self.total_flushed = 0

    @property
    def pending(self) -> int:
        return len(self._ops)

    def put(self, path: str, data: dict) -> None:
        self._append({"path": dot(path), "data": data})

    def delete(self, path: str) -> None:
        self._append({"path": dot(path), "delete": True})

    def extend(self, ops) -> None:
        for op in ops:
            if op and op.get("path"):
                self._append(dict(op))

    def flush(self) -> int:
        if not self._ops:
            return 0
        ops, self._ops, self._bytes = self._ops, [], 0
        root = self._root or ops[0]["path"].split(".", 1)[0]
        result = self._c.batch(root=root, ops=ops)
        count = int(result.get("count") or len(ops))
        self.total_flushed += count
        return count

    def __enter__(self) -> "BulkBatcher":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.flush()

    def _append(self, op: dict) -> None:
        size = len(json.dumps(op, ensure_ascii=False, separators=(",", ":")))
        if self._ops and (
            len(self._ops) >= self._flush_every
            or self._bytes + size > self._flush_bytes
        ):
            self.flush()
        self._ops.append(op)
        self._bytes += size


# ---------------------------------------------------------------------------
# Relay subprocess
# ---------------------------------------------------------------------------

@dataclass
class RelayProcess:
    process: subprocess.Popen
    data_dir: str
    port: int
    url: str


def _is_executable(path: str | None) -> bool:
    return bool(path) and Path(path).is_file() and os.access(path, os.X_OK)


def _find_node() -> str:
    candidates: list[Optional[str]] = [
        os.environ.get("HYPER_NODE_PATH"),
        os.environ.get("NODE_BINARY"),
        shutil.which("node"),
        "/opt/homebrew/bin/node",
        "/usr/local/bin/node",
        "/usr/bin/node",
    ]

    for candidate in candidates:
        if _is_executable(candidate):
            return str(Path(candidate).resolve())

    raise RuntimeError(
        "Node.js not found. Install Node.js, add `node` to PATH, "
        "or set HYPER_NODE_PATH."
    )


def _find_npm() -> str:
    candidates: list[Optional[str]] = [
        os.environ.get("HYPER_NPM_PATH"),
        shutil.which("npm"),
        "/opt/homebrew/bin/npm",
        "/usr/local/bin/npm",
        "/usr/bin/npm",
        "/opt/homebrew/lib/node_modules/npm/bin/npm-cli.js",
        "/usr/local/lib/node_modules/npm/bin/npm-cli.js",
    ]

    for candidate in candidates:
        if not candidate:
            continue
        p = Path(candidate)
        if p.is_file():
            return str(p.resolve())

    raise RuntimeError(
        "npm not found. Install Node.js/npm, add `npm` to PATH, "
        "or set HYPER_NPM_PATH."
    )


def _npm_command(*args: str) -> list[str]:
    npm = _find_npm()
    npm_path = Path(npm)

    if npm_path.suffix == ".js":
        return [_find_node(), str(npm_path), *args]

    return [str(npm_path), *args]


def _sdk_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _relay_dir() -> Path:
    return _sdk_root() / "src"


def _package_json_path() -> Path:
    return _relay_dir() / "package.json"


def _package_lock_path() -> Path:
    return _relay_dir() / "package-lock.json"


def _node_modules_path() -> Path:
    return _relay_dir() / "node_modules"


def _required_module_markers() -> list[Path]:
    node_modules = _node_modules_path()
    return [
        node_modules / "better-sqlite3",
    ]


def _should_skip_npm_install() -> bool:
    raw = os.environ.get("HYPER_SKIP_NPM_INSTALL", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _node_dependencies_ready() -> bool:
    package_json = _package_json_path()
    if not package_json.exists():
        return True

    node_modules = _node_modules_path()
    if not node_modules.exists():
        return False

    markers = _required_module_markers()
    if not markers:
        return True

    return all(marker.exists() for marker in markers)


def _ensure_node_dependencies_installed() -> None:
    if _should_skip_npm_install():
        return

    package_json = _package_json_path()
    if not package_json.exists():
        return

    if _node_dependencies_ready():
        return

    relay_dir = _relay_dir()
    install_cmd = _npm_command("ci") if _package_lock_path().exists() else _npm_command("install")

    env = os.environ.copy()
    node_bin = str(Path(_find_node()).resolve().parent)
    existing_path = env.get("PATH", "")
    env["PATH"] = f"{node_bin}{os.pathsep}{existing_path}" if existing_path else node_bin

    try:
        proc = subprocess.run(
            install_cmd,
            cwd=str(relay_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(
            f"Failed to execute npm in {relay_dir}: {exc}"
        ) from exc

    if proc.returncode != 0:
        raise RuntimeError(
            "Failed to install relay Node.js dependencies.\n"
            f"Directory: {relay_dir}\n"
            f"Command: {' '.join(install_cmd)}\n\n"
            f"{proc.stdout}"
        )

    if not _node_dependencies_ready():
        raise RuntimeError(
            "Relay dependency install finished, but required modules are still missing "
            f"in {relay_dir / 'node_modules'}."
        )


def _find_relay_script() -> str:
    candidates: list[Path] = []

    env = os.getenv("HYPER_RELAY_SCRIPT")
    if env:
        candidates.append(Path(env))

    candidates.append(_relay_dir() / "relay.js")

    here = Path(__file__).resolve().parent
    candidates.extend([
        here / "relay.js",
        here.parent / "relay.js",
        here.parent.parent / "relay.js",
    ])

    home = os.getenv("HYPER_HOME")
    if home:
        home_path = Path(home)
        candidates.extend([
            home_path / "relay.js",
            home_path / "src" / "relay.js",
        ])

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())

    raise RuntimeError(
        "relay.js not found. Set HYPER_RELAY_SCRIPT or place relay.js in the relay package."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class HyperClient:
    """Construct via .attach / .spawn / .peer. Three intents, no magic fallbacks."""

    def __init__(
        self,
        url: str,
        *,
        root: str = "default",
        owned_relay: Optional[RelayProcess] = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.root = root
        self._owned = owned_relay
        self._watches: list[_SSEStream] = []
        self._closed = False
        if owned_relay:
            atexit.register(self.close)

    @classmethod
    def attach(cls, url: str = DEFAULT_URL, *, root: str = "default") -> "HyperClient":
        if not cls._health_ok(url):
            host, port = _parse_url(url)
            raise RuntimeError(
                f"No relay responding at {url} ({host}:{port}). "
                f"Use .spawn(data_dir=...) to start one or .peer([...]) to try a list."
            )
        return cls(url, root=root)

    @classmethod
    def spawn(
        cls,
        data_dir: str = DEFAULT_DATA_DIR,
        *,
        port: int = DEFAULT_PORT,
        workers: int = 1,
        env: Optional[dict] = None,
        wait: bool = True,
        root: str = "default",
        ensure_node_deps: bool = True,
    ) -> "HyperClient":
        if _port_open("127.0.0.1", port):
            raise RuntimeError(
                f"Port {port} is already in use. "
                f"Use .attach('http://127.0.0.1:{port}') if that's your relay."
            )

        data_dir = str(Path(data_dir).expanduser().resolve())
        Path(data_dir).mkdir(parents=True, exist_ok=True)

        if ensure_node_deps:
            _ensure_node_dependencies_installed()

        proc_env = os.environ.copy()
        proc_env["PORT"] = str(port)
        proc_env["HYPER_BIND_HOST"] = proc_env.get("HYPER_BIND_HOST", "127.0.0.1")
        proc_env["HYPER_DATA_DIR"] = data_dir
        proc_env["HYPER_WORKERS"] = str(workers)
        if env:
            proc_env.update({k: str(v) for k, v in env.items()})

        proc = subprocess.Popen(
            [_find_node(), _find_relay_script()],
            stdout=sys.stdout,
            stderr=sys.stderr,
            env=proc_env,
            start_new_session=True,
            cwd=str(_relay_dir()),
        )

        url = f"http://127.0.0.1:{port}"
        owned = RelayProcess(process=proc, data_dir=data_dir, port=port, url=url)

        if wait:
            cls._wait_for_health(url, proc, timeout=RELAY_START_TIMEOUT_S)

        return cls(url, root=root, owned_relay=owned)

    @classmethod
    def peer(cls, urls: list[str], *, root: str = "default") -> "HyperClient":
        if not urls:
            raise ValueError("peer() requires at least one URL")
        for url in urls:
            if cls._health_ok(url):
                return cls(url.rstrip("/"), root=root)
        raise RuntimeError(
            f"No relay responded among {len(urls)} peer(s): {', '.join(urls)}."
        )

    @staticmethod
    def _health_ok(url: str) -> bool:
        try:
            r = _http(f"{url.rstrip('/')}/health", timeout=HEALTH_TIMEOUT_S)
            return bool(r and isinstance(r, dict) and r.get("ok"))
        except Exception:
            return False

    @staticmethod
    def _wait_for_health(url: str, proc: subprocess.Popen, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"Relay exited early with code {proc.returncode}")
            if HyperClient._health_ok(url):
                return
            time.sleep(0.2)
        raise RuntimeError(f"Timed out waiting for relay at {url}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        for w in self._watches:
            try:
                w.stop()
            except Exception:
                pass
        self._watches.clear()

        if self._owned and self._owned.process.poll() is None:
            try:
                self._owned.process.terminate()
                self._owned.process.wait(timeout=5)
            except Exception:
                try:
                    self._owned.process.kill()
                except Exception:
                    pass

    def __enter__(self) -> "HyperClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def owns_relay(self) -> bool:
        return self._owned is not None

    def health(self) -> dict:
        return _http(f"{self.url}/health") or {}

    def roots(self) -> list[str]:
        doc = _http(self.url) or {}
        data = doc.get("data") if isinstance(doc, dict) else None
        if isinstance(data, dict) and isinstance(data.get("roots"), list):
            return list(data["roots"])
        return []

    def read(self, path: str = "", **params) -> Any:
        url = _path_url(self.url, path) if path else self.url
        if params:
            qs = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None and v != ""},
                doseq=True,
            )
            url = f"{url}?{qs}" if qs else url
        try:
            return _http(url)
        except HyperNotFound:
            return None

    def read_with_embeds(
        self,
        path: str,
        embeds: list[str],
        *,
        parallel: int = 8,
    ) -> Any:
        doc = self.read(path)
        if not isinstance(doc, dict) or not embeds:
            return doc

        links = doc.get("_links") or {}
        to_fetch = [(rel, links[rel]) for rel in embeds if rel in links]
        if not to_fetch:
            return doc

        def fetch(rel_href: tuple[str, str]) -> tuple[str, Any]:
            rel, href = rel_href
            try:
                return rel, _http(href)
            except Exception:
                return rel, None

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(parallel, len(to_fetch)))
        ) as ex:
            resolved = list(ex.map(fetch, to_fetch))

        embedded = dict(doc.get("_embedded") or {})
        for rel, sub in resolved:
            if sub is not None:
                embedded[rel] = sub
        doc["_embedded"] = embedded
        return doc

    def write(self, path: str, data: Any) -> dict:
        url = _path_url(self.url, path)
        payload = data if isinstance(data, dict) else {"data": data}
        return _http(url, "PUT", data=payload) or {}

    def delete(self, path: str) -> dict:
        return _http(_path_url(self.url, path), "DELETE") or {}

    def children(
        self, path: str, *, page: int = 1, per_page: int = 200, order: str = "key_asc"
    ) -> dict:
        url = f"{_path_url(self.url, path)}/api/children"
        qs = urllib.parse.urlencode({"page": page, "per_page": per_page, "order": order})
        return _http(f"{url}?{qs}") or {}

    def changes_since(
        self, path: str, cursor: int, *, limit: int = 1024
    ) -> dict:
        """
        HTTP resync. Returns a page of deltas strictly after `cursor`.
        Follow `_links.next` until no changes remain to catch up fully.
        """
        url = f"{_path_url(self.url, path)}/api/changes-since"
        qs = urllib.parse.urlencode({"cursor": cursor, "limit": limit})
        return _http(f"{url}?{qs}") or {"changes": [], "_state": {"next_cursor": cursor}}

    def bulk(
        self,
        *,
        root: Optional[str] = None,
        flush_every: int = 5000,
        flush_bytes: int = 8_000_000,
    ) -> BulkBatcher:
        return BulkBatcher(
            self, root=root or self.root,
            flush_every=flush_every, flush_bytes=flush_bytes,
        )

    def batch(self, *, root: str, ops: list) -> dict:
        url = f"{self.url}/{urllib.parse.quote(root, safe='.')}/api/batch"
        return _http(url, "POST", data={"ops": list(ops)}) or {}

    def watch(
        self,
        path: str,
        callback: Optional[Callable[[DeltaEvent], None]] = None,
        *,
        scope: str = "subtree",
        on_error: Optional[Callable[[Exception], None]] = None,
    ):
        """
        Subscribe to live deltas at `path`.

            # callback form
            handle = c.watch("geo.locations", lambda ev: print(ev.op, ev.path))
            ...
            handle.stop()

            # iterator form
            for ev in c.watch("geo.locations"):
                print(ev.op, ev.path, ev.data)

        scope="subtree" (default) receives every change under `path`.
        scope="exact" receives only changes to `path` itself.

        Because the stream is delta-only, there is no cache and no dedup.
        Each change is delivered once in commit_seq order.
        """
        url = _sse_url(self.url, path, scope=scope)

        if callback is not None:
            def handle_frame(frame: dict) -> None:
                for ev in _frames_to_events(frame, include_initial=False):
                    try:
                        callback(ev)
                    except Exception as exc:
                        if on_error:
                            try:
                                on_error(exc)
                            except Exception:
                                pass

            stream = _SSEStream(url, handle_frame, on_error=on_error)
            stream.start()
            self._watches.append(stream)
            return WatchHandle(stream)

        it = WatchIterator(lambda on_msg: _SSEStream(url, on_msg, on_error=on_error))
        self._watches.append(it._stream)
        return it

    def hydrate_watch(
        self,
        path: str,
        *,
        scope: str = "subtree",
        on_error: Optional[Callable[[Exception], None]] = None,
    ):
        """
        Like watch(), but the first event is an `initial` DeltaEvent carrying
        the state doc at the cursor. Subsequent events are the live deltas.
        """
        q: "queue.Queue[Any]" = queue.Queue()
        sentinel = object()

        initial_doc = self.read(path) or {}
        initial_seq = int(
            ((initial_doc.get("_state") or {}).get("commit_seq")) or 0
        )

        q.put(DeltaEvent(
            kind="initial",
            path=dot(path),
            data=initial_doc.get("data") if isinstance(initial_doc, dict) else None,
            commit_seq=initial_seq,
            updated_at=0,
            snapshot=initial_doc if isinstance(initial_doc, dict) else None,
        ))

        def handle_frame(frame: dict) -> None:
            for ev in _frames_to_events(frame, include_initial=False):
                if ev.commit_seq and ev.commit_seq <= initial_seq:
                    continue
                q.put(ev)

        stream = _SSEStream(
            _sse_url(self.url, path, scope=scope),
            handle_frame,
            on_error=on_error,
        )
        stream.start()
        self._watches.append(stream)

        class _HydrateIter:
            def __iter__(self) -> Iterator[DeltaEvent]:
                return self

            def __next__(self) -> DeltaEvent:
                item = q.get()
                if item is sentinel:
                    raise StopIteration
                return item

            def stop(self) -> None:
                stream.stop()
                q.put(sentinel)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.stop()

        return _HydrateIter()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_json(value) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import signal

    p = argparse.ArgumentParser(prog="python -m client")
    p.add_argument("--url", default=os.getenv("HYPER_URL", DEFAULT_URL))
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("spawn", help="start a relay (blocks)")
    sp.add_argument("data_dir", nargs="?", default=None)
    sp.add_argument("--port", type=int, default=DEFAULT_PORT)
    sp.add_argument("--workers", type=int, default=int(os.getenv("HYPER_WORKERS", "1")))
    sp.add_argument(
        "--skip-node-deps",
        action="store_true",
        help="skip automatic npm install/npm ci before starting the relay",
    )

    sub.add_parser("status")
    sub.add_parser("roots")

    sp = sub.add_parser("inspect")
    sp.add_argument("path")

    sp = sub.add_parser("children")
    sp.add_argument("path")
    sp.add_argument("--page", type=int, default=1)
    sp.add_argument("--per-page", type=int, default=200, dest="per_page")

    sp = sub.add_parser("watch")
    sp.add_argument("path")
    sp.add_argument("--scope", choices=["subtree", "exact"], default="subtree")

    sp = sub.add_parser("write")
    sp.add_argument("path")
    sp.add_argument("payload")

    sp = sub.add_parser("delete")
    sp.add_argument("path")

    args = p.parse_args(argv)

    if args.cmd == "spawn":
        data_dir = (
            str(Path(args.data_dir).expanduser().resolve())
            if args.data_dir else DEFAULT_DATA_DIR
        )
        print(f"hyper: spawning relay on port {args.port} (data: {data_dir})")
        c = HyperClient.spawn(
            data_dir,
            port=args.port,
            workers=args.workers,
            ensure_node_deps=not args.skip_node_deps,
        )
        print(f"hyper: healthy at {c.url}")

        def shutdown(*_):
            print("\nhyper: shutting down")
            c.close()
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
        if c._owned:
            c._owned.process.wait()
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
            args.path, page=args.page, per_page=args.per_page
        ))
        return 0

    if args.cmd == "watch":
        c = HyperClient.attach(args.url)
        print(f"watching {args.path} (scope={args.scope})")
        count = 0
        try:
            for ev in c.watch(args.path, scope=args.scope):
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

    return 2


if __name__ == "__main__":
    sys.exit(_main())