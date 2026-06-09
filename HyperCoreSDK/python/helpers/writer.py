from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable


NodeRow = tuple[int | None, str, str, str, str | None, int, int]


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def normalize_dot_path(value: str) -> str:
    return str(value or "").strip().strip(".").replace("/", ".")


def normalize_relative_path(value: str) -> str:
    return str(value or "").strip().strip("/").strip(".")


def make_dot_path(root: str, relative_path: str) -> str:
    root = str(root or "").strip().strip(".")
    rel = normalize_relative_path(relative_path).replace("/", ".")

    if not root:
        return rel
    if not rel:
        return root
    if rel == root or rel.startswith(f"{root}."):
        return rel

    return f"{root}.{rel}"


class HyperDirect:
    """
    Direct local SQLite writer for HyperCore.

    This is an offline/staging bulk-ingest path. It bypasses the HTTP relay and
    writes the relay SQLite database directly.

    Online path:
        client.batch(root="geo", ops=ops)

    Direct path:
        with HyperServer.direct(root="geo", reset=True) as direct:
            direct.prepare_bulk_load()
            direct.write("locations/foo", {"data": {...}})
            direct.finish_bulk_load()

    Important:
      - Stop the relay while writing directly to the live DB.
      - Or write to a staging data_dir and swap DBs later.
      - For huge fresh imports, use reset=True and prepare_bulk_load().
    """

    def __init__(
        self,
        *,
        data_dir: str | Path,
        root: str = "",
        reset: bool = False,
        write_outbox: bool = False,
        flush_every_rows: int = 1_000_000,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.root = str(root or "").strip().strip(".")
        self.reset_on_enter = bool(reset)
        self.write_outbox = bool(write_outbox)
        self.flush_every_rows = max(1, int(flush_every_rows))

        self.sqlite_dir = self.data_dir / "sqlite"
        self.db_path = self.sqlite_dir / "nodes.sqlite"

        self.db: sqlite3.Connection | None = None

        self.now_ms = int(time.time() * 1000)
        self.commit_seq = 0

        self.rows: list[NodeRow] = []
        self.dir_cache: dict[tuple[int | None, str], int] = {}

        self.rows_written = 0
        self.ops_applied = 0

        self.bulk_indexes_dropped = False
        self.parent_lookup_index_dropped = False
        self.assume_empty_root = False

    def __enter__(self) -> "HyperDirect":
        self.open()

        if self.reset_on_enter:
            self.reset_root()
            self.assume_empty_root = True

        self.begin()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.db is None:
            return

        try:
            if exc_type is None:
                self.flush()
            else:
                self.rollback()
        finally:
            self.close()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        if self.db is not None:
            return

        self.sqlite_dir.mkdir(parents=True, exist_ok=True)

        db = sqlite3.connect(str(self.db_path))
        db.execute("PRAGMA journal_mode = OFF")
        db.execute("PRAGMA synchronous = OFF")
        db.execute("PRAGMA temp_store = MEMORY")
        db.execute("PRAGMA cache_size = -524288")
        db.execute("PRAGMA locking_mode = EXCLUSIVE")
        db.execute("PRAGMA busy_timeout = 30000")

        self.db = db
        self.ensure_schema()
        self.commit_seq = self.read_commit_seq()

    def close(self) -> None:
        if self.db is None:
            return

        self.db.close()
        self.db = None

    def begin(self) -> None:
        db = self.require_db()
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        db = self.require_db()
        self.write_commit_seq()
        if db.in_transaction:
            db.commit()

    def rollback(self) -> None:
        db = self.require_db()
        if db.in_transaction:
            db.rollback()

    def flush(self) -> None:
        if self.rows:
            db = self.require_db()
            db.executemany(
                """
                INSERT INTO nodes(parent_id, root, name, path, data, updated_at, commit_seq)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                self.rows,
            )

            self.rows_written += len(self.rows)
            self.rows.clear()

        self.commit()

        self.now_ms = int(time.time() * 1000)
        self.begin()

    # ------------------------------------------------------------------
    # Public direct-write API
    # ------------------------------------------------------------------

    def reset_root(self, root: str | None = None) -> None:
        db = self.require_db()
        effective_root = str(root or self.root or "").strip().strip(".")
        if not effective_root:
            raise ValueError("reset_root requires a non-empty root")

        if db.in_transaction:
            db.commit()

        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM nodes WHERE root = ?", (effective_root,))
        db.execute("DELETE FROM outbox WHERE root = ?", (effective_root,))

        entity_ids = [
            row[0]
            for row in db.execute(
                """
                SELECT entity_id
                FROM q_entities
                WHERE canonical_path = ?
                   OR canonical_path LIKE ?
                   OR canonical_path LIKE ?
                """,
                (
                    effective_root,
                    f"{effective_root}.%",
                    f"{effective_root}/%",
                ),
            )
        ]

        for entity_id in entity_ids:
            self._delete_query_entity(entity_id)

        self.commit_seq = 0
        db.execute(
            """
            INSERT INTO meta(key, int_value)
            VALUES ('commit_seq', 0)
            ON CONFLICT(key) DO UPDATE SET int_value = 0
            """
        )

        self.rows.clear()
        self.dir_cache.clear()
        self.rows_written = 0
        self.ops_applied = 0
        self.assume_empty_root = True

        db.commit()
        self.begin()

    def prepare_bulk_load(
        self,
        *,
        drop_indexes: bool = True,
        drop_parent_lookup_index: bool = True,
    ) -> None:
        """
        Prepare DB for huge append-heavy fresh imports.

        If drop_parent_lookup_index=True, this requires an empty/reset root.
        """
        if not drop_indexes:
            return

        if drop_parent_lookup_index and not self.assume_empty_root:
            raise RuntimeError(
                "drop_parent_lookup_index=True requires reset=True or reset_root() first"
            )

        db = self.require_db()

        if db.in_transaction:
            db.commit()

        statements = [
            "DROP INDEX IF EXISTS idx_nodes_root_path",
            "DROP INDEX IF EXISTS idx_nodes_parent_sort",
            "DROP INDEX IF EXISTS idx_nodes_parent_updated",
            "DROP INDEX IF EXISTS idx_outbox_id",
            "DROP INDEX IF EXISTS idx_outbox_root_id",
        ]

        if drop_parent_lookup_index:
            statements.insert(0, "DROP INDEX IF EXISTS idx_nodes_parent_name")
            self.parent_lookup_index_dropped = True

        db.executescript(";\n".join(statements) + ";")
        db.commit()

        self.bulk_indexes_dropped = True
        self.begin()

    def finish_bulk_load(self, *, recreate_indexes: bool = True) -> None:
        self.flush()

        if recreate_indexes:
            self.recreate_read_indexes()

    def write(self, relative_path: str, payload: dict[str, Any]) -> None:
        self.write_dot(make_dot_path(self.root, relative_path), payload)

    def write_dot(self, full_dot_path: str, payload: dict[str, Any]) -> None:
        path = normalize_dot_path(full_dot_path)
        if not path:
            return

        parts = [p for p in path.split(".") if p]
        if not parts:
            return

        self.queue_leaf(parts, payload)
        self.ops_applied += 1

    def write_ops(
        self,
        ops: Iterable[dict[str, Any]],
        *,
        skip_memberships: bool = False,
    ) -> int:
        count = 0

        for op in ops:
            path = str(op.get("path") or "")
            if not path:
                continue

            if skip_memberships and "._meta.memberships." in path:
                continue

            if op.get("delete"):
                self.delete_dot(path)
            else:
                self.write_dot(path, op.get("data") or {})

            count += 1

        return count

    def delete(self, relative_path: str) -> None:
        self.delete_dot(make_dot_path(self.root, relative_path))

    def delete_dot(self, full_dot_path: str) -> None:
        db = self.require_db()
        path = normalize_dot_path(full_dot_path)
        parts = [p for p in path.split(".") if p]
        if not parts:
            return

        self.flush()

        node_id = self.find_node_id(parts)
        if node_id is None:
            return

        self.commit_seq += 1
        db.execute("DELETE FROM nodes WHERE id = ?", (node_id,))

        if self.write_outbox:
            db.execute(
                """
                INSERT INTO outbox(root, path, op_tag, commit_seq, updated_at, payload)
                VALUES (?, ?, 'del', ?, ?, NULL)
                """,
                (parts[0], "/".join(parts), self.commit_seq, self.now_ms),
            )

        self.write_commit_seq()
        db.commit()
        self.begin()

    # ------------------------------------------------------------------
    # Row writing
    # ------------------------------------------------------------------

    def queue_leaf(self, parts: list[str], payload: dict[str, Any]) -> None:
        root = parts[0]
        parent_id: int | None = None

        for i, name in enumerate(parts[:-1]):
            parent_id = self.ensure_dir(
                parent_id=parent_id,
                root=root,
                name=name,
                path="/".join(parts[: i + 1]),
            )

        self.commit_seq += 1
        slash_path = "/".join(parts)

        self.rows.append(
            (
                parent_id,
                root,
                parts[-1],
                slash_path,
                compact_json(payload),
                self.now_ms,
                self.commit_seq,
            )
        )

        if self.write_outbox:
            self.queue_outbox_put(root, slash_path, payload)

        if len(self.rows) >= self.flush_every_rows:
            self.flush()

    def ensure_dir(
        self,
        *,
        parent_id: int | None,
        root: str,
        name: str,
        path: str,
    ) -> int:
        key = (parent_id, name)
        cached = self.dir_cache.get(key)
        if cached is not None:
            return cached

        db = self.require_db()

        if not self.assume_empty_root:
            row = db.execute(
                """
                SELECT id
                FROM nodes
                WHERE parent_id IS ?
                  AND name = ?
                """,
                (parent_id, name),
            ).fetchone()

            if row:
                node_id = int(row[0])
                self.dir_cache[key] = node_id
                return node_id

        self.commit_seq += 1

        cur = db.execute(
            """
            INSERT INTO nodes(parent_id, root, name, path, data, updated_at, commit_seq)
            VALUES (?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                parent_id,
                root,
                name,
                path,
                self.now_ms,
                self.commit_seq,
            ),
        )

        node_id = int(cur.lastrowid)
        self.dir_cache[key] = node_id
        self.rows_written += 1
        return node_id

    def queue_outbox_put(
        self,
        root: str,
        slash_path: str,
        payload: dict[str, Any],
    ) -> None:
        db = self.require_db()

        db.execute(
            """
            INSERT INTO outbox(root, path, op_tag, commit_seq, updated_at, payload)
            VALUES (?, ?, 'put', ?, ?, ?)
            """,
            (
                root,
                slash_path,
                self.commit_seq,
                self.now_ms,
                compact_json(
                    {
                        "data": payload,
                        "updated_at": self.now_ms,
                        "commit_seq": self.commit_seq,
                    }
                ),
            ),
        )

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def recreate_read_indexes(self) -> None:
        db = self.require_db()

        if db.in_transaction:
            self.write_commit_seq()
            db.commit()

        db.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_parent_name
              ON nodes(parent_id, name);

            CREATE INDEX IF NOT EXISTS idx_nodes_root_path
              ON nodes(root, path);

            CREATE INDEX IF NOT EXISTS idx_nodes_parent_sort
              ON nodes(parent_id, name);

            CREATE INDEX IF NOT EXISTS idx_nodes_parent_updated
              ON nodes(parent_id, updated_at DESC, name DESC);

            CREATE INDEX IF NOT EXISTS idx_outbox_id
              ON outbox(id);

            CREATE INDEX IF NOT EXISTS idx_outbox_root_id
              ON outbox(root, id);
            """
        )

        db.commit()

        self.bulk_indexes_dropped = False
        self.parent_lookup_index_dropped = False
        self.begin()

    # ------------------------------------------------------------------
    # Lookup program
    # ------------------------------------------------------------------

    def find_node_id(self, parts: list[str]) -> int | None:
        db = self.require_db()

        parent_id: int | None = None
        node_id: int | None = None

        for name in parts:
            row = db.execute(
                """
                SELECT id
                FROM nodes
                WHERE parent_id IS ?
                  AND name = ?
                """,
                (parent_id, name),
            ).fetchone()

            if not row:
                return None

            node_id = int(row[0])
            parent_id = node_id

        return node_id

    # ------------------------------------------------------------------
    # Meta/schema
    # ------------------------------------------------------------------

    def read_commit_seq(self) -> int:
        db = self.require_db()
        row = db.execute(
            "SELECT int_value FROM meta WHERE key = 'commit_seq'"
        ).fetchone()
        return int(row[0] or 0) if row else 0

    def write_commit_seq(self) -> None:
        db = self.require_db()
        db.execute(
            """
            INSERT INTO meta(key, int_value)
            VALUES ('commit_seq', ?)
            ON CONFLICT(key) DO UPDATE SET int_value = excluded.int_value
            """,
            (self.commit_seq,),
        )

    def _delete_query_entity(self, entity_id: str) -> None:
        db = self.require_db()

        for table in (
            "q_facets",
            "q_numbers",
            "q_times",
            "q_refs",
            "q_tokens",
            "q_cells",
            "q_entities",
        ):
            db.execute(f"DELETE FROM {table} WHERE entity_id = ?", (entity_id,))

    def ensure_schema(self) -> None:
        db = self.require_db()

        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (
              id          INTEGER PRIMARY KEY,
              parent_id   INTEGER,
              root        TEXT NOT NULL,
              name        TEXT NOT NULL,
              path        TEXT NOT NULL,
              data        TEXT,
              updated_at  INTEGER NOT NULL,
              commit_seq  INTEGER NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_parent_name
              ON nodes(parent_id, name);

            CREATE INDEX IF NOT EXISTS idx_nodes_root_path
              ON nodes(root, path);

            CREATE INDEX IF NOT EXISTS idx_nodes_parent_sort
              ON nodes(parent_id, name);

            CREATE INDEX IF NOT EXISTS idx_nodes_parent_updated
              ON nodes(parent_id, updated_at DESC, name DESC);

            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              int_value INTEGER,
              text_value TEXT
            );

            CREATE TABLE IF NOT EXISTS outbox (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              root TEXT NOT NULL,
              path TEXT NOT NULL,
              op_tag TEXT NOT NULL,
              commit_seq INTEGER NOT NULL,
              updated_at INTEGER NOT NULL,
              payload TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_outbox_id
              ON outbox(id);

            CREATE INDEX IF NOT EXISTS idx_outbox_root_id
              ON outbox(root, id);

            CREATE TABLE IF NOT EXISTS q_entities (
              entity_id       TEXT PRIMARY KEY,
              entity_type     TEXT NOT NULL,
              canonical_path  TEXT NOT NULL,
              display         TEXT,
              updated_at      INTEGER NOT NULL,
              commit_seq      INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_q_entities_type
              ON q_entities(entity_type, updated_at DESC, entity_id);

            CREATE INDEX IF NOT EXISTS idx_q_entities_updated
              ON q_entities(updated_at DESC, entity_id);

            CREATE TABLE IF NOT EXISTS q_facets (
              name       TEXT NOT NULL,
              value      TEXT NOT NULL,
              entity_id  TEXT NOT NULL,
              PRIMARY KEY(name, value, entity_id)
            );

            CREATE INDEX IF NOT EXISTS idx_q_facets_entity
              ON q_facets(entity_id, name, value);

            CREATE TABLE IF NOT EXISTS q_numbers (
              name       TEXT NOT NULL,
              value      REAL NOT NULL,
              entity_id  TEXT NOT NULL,
              PRIMARY KEY(name, entity_id)
            );

            CREATE INDEX IF NOT EXISTS idx_q_numbers_lookup
              ON q_numbers(name, value, entity_id);

            CREATE TABLE IF NOT EXISTS q_times (
              name       TEXT NOT NULL,
              value_ms   INTEGER NOT NULL,
              entity_id  TEXT NOT NULL,
              PRIMARY KEY(name, entity_id)
            );

            CREATE INDEX IF NOT EXISTS idx_q_times_lookup
              ON q_times(name, value_ms, entity_id);

            CREATE TABLE IF NOT EXISTS q_refs (
              rel        TEXT NOT NULL,
              source_id  TEXT NOT NULL,
              entity_id  TEXT NOT NULL,
              PRIMARY KEY(rel, source_id, entity_id)
            );

            CREATE INDEX IF NOT EXISTS idx_q_refs_entity
              ON q_refs(entity_id, rel, source_id);

            CREATE TABLE IF NOT EXISTS q_tokens (
              token      TEXT NOT NULL,
              entity_id  TEXT NOT NULL,
              PRIMARY KEY(token, entity_id)
            );

            CREATE INDEX IF NOT EXISTS idx_q_tokens_entity
              ON q_tokens(entity_id, token);

            CREATE TABLE IF NOT EXISTS q_cells (
              scheme     TEXT NOT NULL,
              value      TEXT NOT NULL,
              entity_id  TEXT NOT NULL,
              PRIMARY KEY(scheme, value, entity_id)
            );

            CREATE INDEX IF NOT EXISTS idx_q_cells_entity
              ON q_cells(entity_id, scheme, value);

            INSERT INTO meta(key, int_value)
            VALUES ('commit_seq', 0)
            ON CONFLICT(key) DO NOTHING;
            """
        )

        db.commit()

    def require_db(self) -> sqlite3.Connection:
        if self.db is None:
            raise RuntimeError("HyperDirect is not open")
        return self.db

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
    