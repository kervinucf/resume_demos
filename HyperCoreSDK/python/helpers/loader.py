# HyperCoreSDK/python/loader.py
"""
One thing: a Graph — a handle onto one root_key of nodes and edges.

You open it one of two ways, depending on whether the live relay is plugged in:

    Graph("weather")              live relay — read, find, add, link, serve
    Graph.build("geo", reset=...) cold offline build straight to SQLite — add only

Both are the *same* object doing the *same* operations; the only difference is
where the bytes land. So read/find/link and pointer-nodes work only when the
relay is plugged in — an offline build can't reach them, and says so.

A node's label in each index bucket is derived from one declared field, `key`,
read off the node's own content — you never hand a label to add(). The bucket
caption is the index projection (PROJECT). So add() is just position + value:

    graph.add(path, content, ...)    put a node. Its shape lives in the node:
                                       indexes=…   places it in search space
                                       target=…    makes it a pointer to another node
    graph.link(source, rel, target)  connect two nodes with an edge

And to read what's there (live only):

    graph.render(...)   iterate nodes already in the graph
    graph.find(...)     search for nodes

One rule for `add`: never hand-write a query. Pass plain content and a `kind`;
the relay infers search text, facets, numbers, times, and refs from the content.

    with Graph("weather") as graph:
        for loc in graph.render(root_key="geo", path="locations", generator=...):
            graph.add("history/x", {"kind": "weather_observation", ...}, indexes=INDEXES)
            graph.add("weather.latest.x", {...}, target="weather.history.x")
            graph.link(source="geo.locations.x", rel="weather_latest", target="weather.latest.x")
        graph.serve()

    with Graph.build("geo", indexes=INDEXES, reset=True, key="geoname_id") as graph:
        graph.add("locations/x", {"kind": "location", "geoname_id": "x", ...})
"""
from __future__ import annotations

import time
from typing import Any

from HyperCoreSDK.python.client import HyperClient
from HyperCoreSDK.python.helpers.server import create_hyper_server
from HyperCoreSDK.python.helpers.storage import create_default_storage_directory
from HyperCoreSDK.python.helpers.indexes import plan_upsert


def projection(*fields: str) -> dict[str, str]:
    """link_projections where each index entry carries a field forward under its own name."""
    return {field: field for field in fields}


class Graph:
    """
    A handle onto one root_key of the graph — nodes and edges.

    Opened live (`Graph(ns)`) it talks to the relay; opened for a build
    (`Graph.build(ns)`) it writes straight to SQLite for a cold offline import.
    Same operations either way; the build path simply has no relay, so reads,
    links, and pointer-nodes are unavailable until you open it live.

    `key` names the field on each node's content whose value is the node's label
    in every index bucket. Declared once here, derived per node — never passed to add().
    """

    def __init__(
        self,
        root_key: str,
        *,
        key: str | None = None,
        url: str | None = None,
        data_dir: str | None = None,
    ) -> None:
        self.root_key = root_key
        self.key = key
        self.indexes: list = []
        self._builder = None  # set only on .build()
        self._count = 0
        self.progress_every = 0
        if url is None:
            self.client = create_hyper_server(
                root=root_key,
                data_path=data_dir or create_default_storage_directory(),
            )
        else:
            self.client = HyperClient.attach(url, root=root_key)

    @classmethod
    def build(
        cls,
        root_key: str,
        *,
        indexes: list | None = None,
        key: str | None = None,
        reset: bool = True,
        data_dir: str | None = None,
        batch_ops: int = 9_000,
        progress_every: int = 50_000,
    ) -> "Graph":
        """Open a cold offline build: nodes go straight to SQLite, no live relay."""
        self = cls.__new__(cls)
        self.root_key = root_key
        self.key = key
        self.client = None
        self.indexes = indexes or []
        self.progress_every = progress_every
        self._count = 0
        self._builder = HyperClient.direct_writer(
            data_dir=data_dir or create_default_storage_directory(),
            root=root_key,
            reset=reset,
            bulk=True,
            batch_ops=batch_ops,
            flush_every_rows=1_000_000,
            write_outbox=False,
            skip_memberships=True,
            # bulk flush uses ON CONFLICT(parent_id, name); keep that unique index.
            drop_parent_lookup_index=False,
        )
        return self

    @property
    def building(self) -> bool:
        return self._builder is not None

    def _need_relay(self, what: str) -> None:
        if self.building:
            raise NotImplementedError(
                f"{what} needs the live relay; this Graph was opened with .build() for an offline import"
            )

    def _label(self, content: dict[str, Any]) -> str | None:
        """The node's id in each index bucket: content[key], or None to fall back to the path tail."""
        if self.key and content.get(self.key) is not None:
            return str(content[self.key])
        return None

    # -- the one operation on nodes ------------------------------------------

    def add(
        self,
        path: str,
        content: dict[str, Any],
        *,
        indexes: list | None = None,
        links: dict[str, Any] | None = None,
        target: str | None = None,
        kind: str | None = None,
        name: str | None = None,
    ):
        """
        Put a node into the graph: position (path) + value (content).

        The node's label in each index bucket is derived from content[key]; its
        bucket caption is the index projection. Neither is passed here.

          - default: a record (its `kind` is in content); `indexes=` places it in search space
          - `target=`: a pointer node standing in for another node (live relay only)
        """
        if target is not None:
            self._need_relay("pointer nodes (target=)")
            return self.client.put(
                path=path,
                kind=kind or str(content.get("kind") or "pointer"),
                name=name or str(content.get("name") or path.rsplit(".", 1)[-1]),
                content=content,
                links=links,
                target=target,
            )

        if self.building:
            self._builder.write_ops(plan_upsert(
                root=self.root_key,
                record_path=path,
                record_data=content,
                index_specs=indexes if indexes is not None else self.indexes,
                ref_key=self._label(content),
                links=links,
                prior_paths=(),  # fresh import; nothing to reconcile
            ))
            self._count += 1
            if self.progress_every and self._count % self.progress_every == 0:
                print(f"  {self._count:,} nodes ({self._builder.written:,} writes)", flush=True)
            return None

        return self.client.write_record_with_indexes(
            root=self.root_key,
            record_path=path,
            record_data=content,
            index_specs=indexes or [],
            ref_key=self._label(content),
            links=links,
        )

    # -- the one operation on edges ------------------------------------------

    def link(
        self,
        *,
        source: str,
        rel: str,
        target: str,
        kind: str = "link",
        name: str | None = None,
        content: dict[str, Any] | None = None,
        links: dict[str, Any] | None = None,
    ):
        """Connect two nodes with an edge: a sidecar at <source>.refs.<rel> -> target."""
        self._need_relay("link")
        return self.client.link(
            source=source, rel=rel, target=target,
            kind=kind, name=name, content=content, links=links,
        )

    # -- reads (live only) ---------------------------------------------------

    def render(self, **kwargs):
        """Iterate nodes already in the graph."""
        self._need_relay("render")
        return self.client.select_records(**kwargs)

    def find(self, **kwargs):
        """Search the graph for matching nodes."""
        self._need_relay("find")
        return self.client.find_things(**kwargs)

    # -- counts (build) ------------------------------------------------------

    @property
    def written(self) -> int:
        return self._builder.written if self.building else self._count

    @property
    def count(self) -> int:
        return self._count

    # -- lifecycle -----------------------------------------------------------

    def serve(self) -> None:
        """Block while the relay this handle owns keeps running (live only)."""
        self._need_relay("serve")
        print(f"relay at {self.client.url} — Ctrl-C to stop", flush=True)
        try:
            if self.client.owns_relay():
                self.client._owned.process.wait()
            else:
                print("attached to existing relay", flush=True)
                while True:
                    time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            self.client.close()

    def close(self) -> None:
        if self.building:
            return
        self.client.close()

    def __enter__(self) -> "Graph":
        if self.building:
            self._builder.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.building:
            self._builder.__exit__(exc_type, exc, tb)
        else:
            self.client.close()