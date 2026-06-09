from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any, TypeVar
import os
from HyperCoreSDK.python.helpers.server import HyperServer
from HyperCoreSDK.python.helpers.indexes import *
from HyperCoreSDK.python.helpers.records import select_records
from HyperCoreSDK.python.helpers.search import HyperSearch, SearchOptions, Source
from HyperCoreSDK.python.helpers.writer import HyperDirectWriter
from HyperCoreSDK.python.helpers.utils import dot, define_relay_script, create_default_storage_directory, projection

T = TypeVar("T")


def _record_body(doc: Any) -> dict[str, Any]:
    """
    Canonical record unwrap. Peels the storage envelope ({"data": {...}},
    possibly double-wrapped) down to the application body.

    Every read — find(), walk(), resolve()/get_record() — funnels through this
    one function, so they all return the SAME shape. The only envelope key is
    "data"; anything else a body carries is the body's own (a reader cannot and
    should not guess which domain keys are wrappers).
    """
    body = doc
    for _ in range(2):  # at most two "data" envelope levels
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            body = body["data"]
        else:
            break
    return body if isinstance(body, dict) else {}


class HyperClient:
    def __init__(
            self,
            root_key: str=None,
            *,
            key: str | None = None,
            data_dir: str | None = None,
    ) -> None:
        if root_key is None:
            root_key = ""

        self.root_key = dot(root_key)
        self.key = key
        self.data_dir = data_dir
        self.index: list[Any] = []
        self._builder: Any | None = None
        self._count = 0
        self.progress_every = 0
        #
        self.client = None
        self._search_engine: HyperSearch | None = None

    # ------------------------------------------------------------------
    # Mode
    # ------------------------------------------------------------------

    @property
    def building(self) -> bool:
        """True when this handle was opened for an offline SQLite build."""
        # EV: if this is wrong, walk()/serve() will misfire about "live relay"
        return self._builder is not None

    @classmethod
    def open_sqlite_file(
            cls,
            root_key: str,
            *,
            index: list[Any] | None = None,
            key: str | None = None,
            reset: bool = True,
            path: str | None = None,
            batch_ops: int = 9_000,
            progress_every: int = 50_000,
    ) -> "HyperClient":
        """Open a cold offline build: nodes go straight to SQLite, no live relay."""
        self = cls.__new__(cls)
        self.root_key = dot(root_key)
        self.key = key
        self.data_dir = path or create_default_storage_directory()
        self.client = None  # type: ignore[assignment]
        self._search_engine = None
        self.index = index or []
        self.progress_every = progress_every
        self._count = 0

        self._builder = HyperDirectWriter(
            data_dir=self.data_dir,
            root=self.root_key,
            reset=reset,
            bulk=True,
            batch_ops=batch_ops,
            flush_every_rows=1_000,
            write_outbox=False,
            skip_memberships=False,
            drop_parent_lookup_index=False,
        )
        # EV: builder set => self.building is now True
        print(f"[db] open_sqlite_file root={self.root_key} reset={reset} -> building={self.building}", flush=True)
        return self

    # ------------------------------------------------------------------
    # Live relay client (lazy)
    # ------------------------------------------------------------------

    def _ensure_client(self) -> HyperServer:
        """Spawn the relay client on first read/write in live mode."""
        if self.client is not None:
            return self.client

        define_relay_script()
        self.client = HyperServer.spawn(
            data_dir=self.data_dir or create_default_storage_directory(),
            root=self.root_key,
        )
        # EV: confirms a live relay actually came up and where
        print(f"[db] spawned relay client url={getattr(self.client, 'url', '?')} root={self.root_key}", flush=True)
        return self.client

    def _ensure_search(self) -> HyperSearch:
        """
        Build the search/read engine on first find().

        Prefers the on-disk SQLite (fast, no relay round trips); falls back to
        the live relay's fast-read API only when there is no SQLite file yet.
        """
        if self._search_engine is not None:
            return self._search_engine

        data_dir = self.data_dir or create_default_storage_directory()
        db_path = os.path.join(data_dir, "sqlite", "nodes.sqlite")

        if os.path.exists(db_path):
            url = getattr(self.client, "url", None) or "http://127.0.0.1:8765"
            self._search_engine = HyperSearch(url, backend="sql", db_path=db_path)
            # EV: which tier answered find(); sql == direct mmap'd reads
            print(f"[db] search backend=sql db={db_path}", flush=True)
        else:
            client = self._ensure_client()
            self._search_engine = HyperSearch(client.url, backend="api")
            print(f"[db] search backend=api url={client.url}", flush=True)

        return self._search_engine

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def save_record(
            self,
            path: str,
            data: dict[str, Any],
            *,
            indexes: list[Any] | None = None,  # ValueIndexSpec list
            links: dict[str, str] | None = None,
            root: str | None = None,
    ) -> int:
        """
        Write one record and its derived index entries.

            path     where it lives, e.g. "locations/nyc-5128581"
            data     the record body
            indexes  how it can be found later (ValueIndexSpec list)

        Derived, so you never pass them:
            * the record id is the last path segment — plan_upsert uses it as the
              index entries' final key;
            * each index entry snapshots `data`, so it lists/renders without a re-read.

        root defaults to this client's root; override only for a cross-root write.
        """
        effective_root = root or self.root_key

        ops = plan_upsert(
            root=effective_root,
            record_path=path,
            record_data=data,
            index_specs=indexes or [],
            ref_payload=data,  # the entry remembers the record it points at
            links=links,
            prior_paths=(),
            # ref_key intentionally omitted — plan_upsert derives it from the path tail
        )

        n = self.write_ops(ops, root=effective_root)
        self._count += 1
        print(f"[db] save {effective_root}.{path} ops={n} total={self._count}", flush=True)
        return n
    
    def write_ops(self, ops: list[dict[str, Any]], *, root: str | None = None) -> int:
        """Route ops to the offline builder or the live relay batch endpoint."""
        if self.building:
            return self._builder.write_ops(ops)

        client = self._ensure_client()
        result = client.batch(root=root or self.root_key, ops=ops)
        if not result or not result.get("ok"):
            raise RuntimeError(f"batch failed: {result!r}")
        return int(result.get("count") or 0)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def find(
            self,
            *,
            terms: Iterable[str] | None = None,
            query: str | None = None,
            namespace: str | None = None,
            read_path: str = "",
            where: Callable[[dict[str, Any]], bool] | None = None,
            limit: int | None = None,
            per_term: int | None = None,
            with_path: bool = False,
            max_depth: int | None = None,
            exclude: Iterable[str] = ("_meta", "_mdr"),
    ) -> Iterator[Any]:
        """
        Primary read. Resolve a set of terms (or a free-text sentence) into the
        records to iterate over, hydrated in one indexed pass — no N+1 reads.

        Typical use: hand it the things you care about and loop the matches.

            for place in db.find(
                    terms=["New York", "Miami", "Dubai"],
                    namespace="geo",
                    read_path="locations",
            ):
                ...

        Semantics:
          * terms      -> each entry is matched independently and the results
                          are unioned (New York OR Miami OR Dubai). Words inside
                          one entry are ANDed ("New York" => new AND york).
          * query      -> a single free-text sentence using the engine's text
                          rules (AND words, "quoted phrases", -negation). Use
                          this OR terms, not both.
          * (neither)  -> iterate the whole scope ("*"), the search-tier
                          equivalent of walk().
          * where      -> optional residual Python filter on each record body,
                          for what the index can't express (strict field
                          equality, ranges, cross-field logic). Runs over the
                          already-narrowed candidate set, not every row.
          * limit      -> counts records actually yielded (post-filter).
          * per_term   -> cap per individual term query (defaults to limit, or
                          a large bound when limit is None).

        Yields each record body dict, or (path, body) when with_path=True.
        """
        if self.building:
            raise NotImplementedError(
                "find() reads committed data; it is not available during an "
                "offline .open_sqlite_file() build"
            )

        root = namespace or self.root_key
        source_path = f"{root}.{read_path}".strip(".") if read_path else root

        if terms is not None:
            queries = [str(t).strip() for t in terms if str(t).strip()]
        elif query is not None and str(query).strip():
            queries = [str(query).strip()]
        else:
            queries = ["*"]  # whole-scope iteration via the search tier

        per = per_term if per_term is not None else (limit if limit is not None else 10_000)

        options = SearchOptions(
            exclude=tuple(exclude),
            fields=("path", "data"),
            max_depth=max_depth if max_depth is not None else 5,
            sources={source_path: Source(count=per)} if source_path else {},
        )

        search = self._ensure_search()

        seen: set[str] = set()
        emitted = 0

        for text in queries:
            results = search.query(text, options=options)
            # EV: per-term hit count; a thin loop usually shows up here first
            print(f"[db] find term={text!r} source={source_path} hits={len(results)}", flush=True)

            for row in results.to_list():
                path = str(row.get("path") or "")
                if path and path in seen:
                    continue
                if path:
                    seen.add(path)

                data = _record_body(row.get("data"))
                if not data:
                    continue

                if where is not None and not where(data):
                    continue

                yield (path, data) if with_path else data

                emitted += 1
                if limit is not None and emitted >= int(limit):
                    return

    def get_record(self, *, root: str, path: str) -> dict[str, Any] | None:
        """
        Read a single record body by exact node path.

        `path` is slash-delimited and each segment is taken verbatim, so record
        keys containing dots (e.g. "St. Louis-123") read correctly. Returns the
        unwrapped body dict, or None when the node is missing or has no data.
        Uses the same read tier as find() — no relay round trips when SQLite exists.

        This is the join primitive: given a weather event's `origin`, read its
        location with get_record(root="geo", path=f"locations/{origin}").
        """
        if self.building:
            raise NotImplementedError(
                "get_record() reads committed data; it is not available during "
                "an offline .open_sqlite_file() build"
            )

        slash = str(path).strip("/")
        if not slash:
            return None

        search = self._ensure_search()
        rows = search._backend.fetch_records(root=str(root), paths=[slash])
        doc = rows.get(slash)
        # EV: a None here means the slug join missed — usually a name/key mismatch
        print(f"[db] get_record root={root} path={slash} hit={doc is not None}", flush=True)
        if doc is None:
            return None
        body = _record_body(doc)
        return body or None

    def resolve(
            self,
            records: Iterable[dict[str, Any]],
            *,
            to: Callable[[dict[str, Any]], tuple[str, str] | str | None],
            dedup_on: Callable[[dict[str, Any]], Any] | None = None,
            keep_unresolved: bool = False,
            limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """
        Generic join. Follow each record to a related record and yield the
        hydrated targets. Knows nothing about any domain — the relationship is
        entirely in the `to` function the caller supplies.

        `records` is any iterable of bodies, typically from find() or walk(), so
        joins compose:  db.resolve(db.find(...), to=...).

            to(body) -> (root, path)   read that record (path = verbatim slash form)
            to(body) -> path           read under this HyperClient's root_key
            to(body) -> None           no target

        dedup_on:        collapse many source rows that point at the same target
                         (e.g. many events per location -> dedup on a slug field).
                         Defaults to the resolved (root, path).
        keep_unresolved: when True, yield the original body if `to` returns None
                         or the target is missing, instead of dropping it.
        limit:           caps yielded records.

        The relationship is expressed per call via a `to` function, so the SAME
        method serves any "this record points at that record by some field"
        link. Define it as a small function, e.g.:

            def to_location(event):
                return ("geo", f"locations/{event['origin']}")    # event -> location

            def to_author(row):
                return ("authors", f"by-id/{row['author_id']}")   # row -> author
        """
        seen: set[Any] = set()
        emitted = 0

        for body in records:
            target = to(body)

            if dedup_on is not None:
                key = dedup_on(body)
            elif target is None:
                key = None
            else:
                root_, path_ = target if isinstance(target, tuple) else (self.root_key, target)
                key = (str(root_), str(path_).strip("/"))

            if key is not None:
                if key in seen:
                    continue
                seen.add(key)

            if target is None:
                hydrated = None
            else:
                root, path = target if isinstance(target, tuple) else (self.root_key, target)
                hydrated = self.get_record(root=str(root), path=str(path))

            if hydrated is None and not keep_unresolved:
                continue

            yield hydrated if hydrated is not None else body

            emitted += 1
            if limit is not None and emitted >= int(limit):
                return

    def _need_relay(self, what: str) -> None:
        if self.building:
            raise NotImplementedError(
                f"{what} need the live relay; this HyperClient was opened with .open_sqlite_file()"
            )

    def walk(
            self,
            *,
            namespace: str,
            read_path: str,
            in_index: dict[str, Any] | None = None,
            where: Callable[[dict[str, Any]], bool] | None = None,
            limit: int | None = None,
            per_page: int = 200,
    ) -> Iterator[T]:
        """
        Low-level row iteration over nodes already in the database (live relay).

        For most reads prefer find(): it pushes the selection into the index
        and hydrates in one pass. Reach for walk() when you genuinely want to
        stream every row of a collection, or select by an explicit index
        (in_index) that find()'s text/term matching can't express.
        """
        self._need_relay("walk")
        client = self._ensure_client()
        # EV: walk reads from root_key (e.g. geo) via the spawned client
        print(f"[db] walk root={namespace} path={read_path} by={in_index} limit={limit}", flush=True)

        records = select_records(
            client,
            namespace=namespace,
            collection=read_path,
            by=in_index,
            where=where,
            limit=limit,
            per_page=per_page,
        )
        return (_record_body(r) for r in records)

    # ------------------------------------------------------------------
    # Counters / lifecycle
    # ------------------------------------------------------------------

    @property
    def written(self) -> int:
        return self._builder.written if self.building else self._count

    @property
    def count(self) -> int:
        return self._count

    def serve(
            self,
            http_addr: str | None = None,
            sqlite_file_location: str | None = None,
    ) -> None:
        """Block while the relay this handle owns keeps running."""
        self._need_relay("serve")

        if sqlite_file_location:
            self.data_dir = sqlite_file_location

        if http_addr is None:
            client = self._ensure_client()  # reuse the one walk/save already spawned
        else:
            self.client = HyperServer.attach(http_addr, root=self.root_key)
            client = self.client

        print(f"relay at {client.url} — Ctrl-C to stop", flush=True)

        try:
            if client.owns_relay():
                client._owned.process.wait()
            else:
                print("attached to existing relay", flush=True)
                while True:
                    time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            client.close()

    def close(self) -> None:
        if self.building:
            return
        if self.client is not None:
            self.client.close()

    def __enter__(self) -> "HyperClient":
        if self.building:
            self._builder.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.building:
            self._builder.__exit__(exc_type, exc, tb)
        elif self.client is not None:
            self.client.close()