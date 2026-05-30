# HyperCoreSDK/python/loader.py
"""
Two ways to load the hypergraph, one mental model.

    BulkLoader  — fresh, append-heavy import straight to local SQLite (no reads).
    Loader      — online writes through the live relay, plus reads for enrichment.

Both share one rule: you never hand-write a `query` projection. You pass plain
data and a `kind`. The relay infers search text, facets, numbers, times, and
refs from the data and its links. "It does what it says": one dict per thing.

    with BulkLoader("geo", indexes=INDEXES, reset=True) as geo:
        geo.record("locations/5128581", {"kind": "location", ...}, ref_key="5128581")

    with Loader("weather") as wx:
        for loc in wx.select(root="geo", collection="locations", from_data=...):
            wx.thing(path=..., kind="weather_latest", name=..., body=..., target=...)
        wx.serve()
"""
from __future__ import annotations

import time
from typing import Any

from HyperCoreSDK.python.client import HyperClient
from HyperCoreSDK.python.helpers.server import create_hyper_server
from HyperCoreSDK.python.helpers.storage import create_default_storage_directory
from HyperCoreSDK.python.helpers.indexes import plan_upsert


def projection(*fields: str) -> dict[str, str]:
    """link_projections where each index entry projects a field under its own name."""
    return {field: field for field in fields}


def serve(client: HyperClient) -> None:
    """Block while the relay this client owns keeps running. Replaces keep-alive boilerplate."""
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


class Loader:
    """
    Online loader: one root, one live relay, KISS writes that infer their own query.

    Reads (`select`, `find`) let enrichment loaders look up records in other roots.
    """

    def __init__(self, root: str, *, url: str | None = None, data_dir: str | None = None) -> None:
        self.root = root
        if url is None:
            self.client = create_hyper_server(
                root=root,
                data_path=data_dir or create_default_storage_directory(),
            )
        else:
            self.client = HyperClient.attach(url, root=root)

    # -- reads ---------------------------------------------------------------

    def select(self, **kwargs):
        return self.client.select_records(**kwargs)

    def find(self, **kwargs):
        return self.client.find_things(**kwargs)

    # -- writes --------------------------------------------------------------

    def record(
        self,
        path: str,
        data: dict[str, Any],
        *,
        indexes: list | None = None,
        ref_key: str | None = None,
        ref_payload: dict[str, Any] | None = None,
        links: dict[str, Any] | None = None,
    ) -> int:
        """A canonical record plus its browsable value indexes. `data` should carry a `kind`."""
        return self.client.write_record_with_indexes(
            root=self.root,
            record_path=path,
            record_data=data,
            index_specs=indexes or [],
            ref_key=ref_key,
            ref_payload=ref_payload,
            links=links,
        )

    def thing(
        self,
        *,
        path: str,
        kind: str,
        name: str,
        body: dict[str, Any],
        links: dict[str, Any] | None = None,
        target: str | None = None,
    ):
        """A searchable node. A `target` makes it a pointer/alias to another node."""
        return self.client.put(
            path=path,
            kind=kind,
            name=name,
            body=body,
            links=links,
            target=target,
        )

    def link(
        self,
        *,
        source: str,
        rel: str,
        target: str,
        kind: str = "link",
        name: str | None = None,
        body: dict[str, Any] | None = None,
        links: dict[str, Any] | None = None,
    ):
        """A browsable relationship sidecar at <source>.refs.<rel> -> target."""
        return self.client.link(
            source=source,
            rel=rel,
            target=target,
            kind=kind,
            name=name,
            body=body,
            links=links,
        )

    # -- lifecycle -----------------------------------------------------------

    def serve(self) -> None:
        serve(self.client)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "Loader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.client.close()


class BulkLoader:
    """
    Fresh bulk import into one root via direct local SQLite. Write-only, no reads.

    `record()` writes the canonical node plus its value indexes in one shot.
    Indexes are dropped during load and rebuilt on exit (bulk=True).
    """

    def __init__(
        self,
        root: str,
        *,
        indexes: list | None = None,
        reset: bool = True,
        data_dir: str | None = None,
        batch_ops: int = 9_000,
        progress_every: int = 50_000,
    ) -> None:
        self.root = root
        self.indexes = indexes or []
        self.progress_every = progress_every
        self._count = 0
        self._writer = HyperClient.direct_writer(
            data_dir=data_dir or create_default_storage_directory(),
            root=root,
            reset=reset,
            bulk=True,
            batch_ops=batch_ops,
            flush_every_rows=1_000_000,
            write_outbox=False,
            skip_memberships=True,
            # bulk flush uses ON CONFLICT(parent_id, name); keep that unique index.
            drop_parent_lookup_index=False,
        )

    def record(
        self,
        path: str,
        data: dict[str, Any],
        *,
        ref_key: str | None = None,
        ref_payload: dict[str, Any] | None = None,
        links: dict[str, Any] | None = None,
    ) -> None:
        self._writer.write_ops(plan_upsert(
            root=self.root,
            record_path=path,
            record_data=data,
            index_specs=self.indexes,
            ref_key=ref_key,
            ref_payload=ref_payload or {},
            links=links,
            prior_paths=(),  # fresh import; nothing to reconcile
        ))
        self._count += 1
        if self.progress_every and self._count % self.progress_every == 0:
            print(f"  {self._count:,} records ({self._writer.written:,} writes)", flush=True)

    @property
    def written(self) -> int:
        return self._writer.written

    @property
    def count(self) -> int:
        return self._count

    def __enter__(self) -> "BulkLoader":
        self._writer.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._writer.__exit__(exc_type, exc, tb)