from __future__ import annotations

from typing import Any, Iterable

from HyperCoreSDK.python.helpers.direct import HyperDirect


class HyperHttpWriter:
    """
    Buffered writer that sends relay-style ops through the HTTP relay.

    Use this for normal online writes.
    """

    def __init__(self, client, *, batch_ops: int = 9_000) -> None:
        self.client = client
        self.root = client.root
        self.batch_ops = max(1, int(batch_ops))
        self.pending: list[dict[str, Any]] = []
        self.written = 0

    def __enter__(self) -> "HyperHttpWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.flush()

    def write_ops(self, ops: Iterable[dict[str, Any]]) -> int:
        n = 0

        for op in ops:
            self.pending.append(op)
            n += 1

        if len(self.pending) >= self.batch_ops:
            self.flush()

        return n

    def flush(self) -> int:
        if not self.pending:
            return 0

        result = self.client.batch(root=self.root, ops=self.pending)
        if not result or not result.get("ok"):
            raise RuntimeError(f"batch failed: {result!r}")

        count = int(result.get("count") or 0)
        self.pending.clear()
        self.written += count
        return count


class HyperDirectWriter:
    """
    Buffered writer that applies relay-style ops directly to local SQLite.

    Use this for offline/staging bulk writes.
    """

    def __init__(
        self,
        *,
        root: str,
        data_dir: str,
        reset: bool = False,
        bulk: bool = False,
        batch_ops: int = 9_000,
        flush_every_rows: int = 1_000_000,
        write_outbox: bool = False,
        skip_memberships: bool = False,
        drop_parent_lookup_index: bool = True,
    ) -> None:
        self.root = str(root or "").strip().strip(".")
        self.batch_ops = max(1, int(batch_ops))
        self.bulk = bool(bulk)
        self.skip_memberships = bool(skip_memberships)
        self.drop_parent_lookup_index = bool(drop_parent_lookup_index)

        self.pending: list[dict[str, Any]] = []
        self.written = 0

        self.direct = HyperDirect(
            data_dir=data_dir,
            root=self.root,
            reset=reset,
            write_outbox=write_outbox,
            flush_every_rows=flush_every_rows,
        )

        self._closed = False

    def __enter__(self) -> "HyperDirectWriter":
        self.direct.__enter__()

        if self.bulk:
            self.direct.prepare_bulk_load(
                drop_indexes=True,
                drop_parent_lookup_index=self.drop_parent_lookup_index,
            )

        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._closed:
            return

        try:
            if exc_type is None:
                self.flush()

                if self.bulk:
                    self.direct.finish_bulk_load(recreate_indexes=True)
                else:
                    self.direct.flush()
            else:
                self.direct.rollback()
        finally:
            self._closed = True
            self.direct.close()

    def write_ops(self, ops: Iterable[dict[str, Any]]) -> int:
        n = 0

        for op in ops:
            self.pending.append(op)
            n += 1

        if len(self.pending) >= self.batch_ops:
            self.flush()

        return n

    def flush(self) -> int:
        if not self.pending:
            return 0

        count = self.direct.write_ops(
            self.pending,
            skip_memberships=self.skip_memberships,
        )

        self.pending.clear()
        self.written += count
        return count