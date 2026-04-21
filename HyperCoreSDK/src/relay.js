const cluster = require('cluster');
const http = require('http');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const Database = require('better-sqlite3');

// ---------------------------------------------------------------------------
// Why this rewrite
// ---------------------------------------------------------------------------
//
// The old storage layer had two deep problems:
//
//   1. Flat-keyed storage with string-prefix counters. Every node lived as one
//      row `(root, full_slash_path)`. Children enumeration used `instr(substr(
//      key, ?), '/')` tricks. Writing `a/b/c/d/e/f` incremented five prefix
//      counters inside the transaction. The hierarchy was a fiction maintained
//      by clever SQL.
//
//   2. Live streams rebuilt full collection state docs on every commit and sent
//      the whole page each frame. Subscribers had to cache + diff (the
//      `DedupReactor` smell) because the stream frames weren't changes — they
//      were snapshots over and over.
//
// The rewrite below makes the storage model literally a directory tree:
//
//   nodes(id, parent_id, root, name, path, data, updated_at, commit_seq)
//   UNIQUE(parent_id, name)
//
// A node is just a row. Writing `geo.index.by_property.timezone.utc.5128581`
// walks the segments and INSERTs each missing one. No `has_data` flag, no
// synthetic-vs-real distinction — either a node is there or it isn't. Children
// listing is `WHERE parent_id = ? ORDER BY name`. Deleting a leaf walks up
// pruning empty ancestors (data IS NULL AND no children).
//
// The stream protocol is now delta-based with a cursor. Subscribe frame is a
// single `commit_seq` + links. Each subsequent frame is a list of changes
// ({op, path, data, commit_seq}). Clients don't cache, because the server only
// sends each change once. If the stream drops, clients resync via GET
// `/<path>/api/changes-since?cursor=N`.
// ---------------------------------------------------------------------------

const ROLE = String(process.env.HYPER_ROLE || '').trim().toLowerCase();
const PORT = parseInt(process.env.PORT || '8765', 10);
const BIND = process.env.HYPER_BIND_HOST || '0.0.0.0';
const RDIR = process.env.HYPER_DATA_DIR || path.join(process.cwd(), '.hyper-data');
const NUM_LOCAL_WORKERS = Number.isFinite(parseInt(process.env.HYPER_WORKERS || '2', 10))
  ? parseInt(process.env.HYPER_WORKERS || '2', 10)
  : 2;

const DEFAULT_PAGE_SIZE = parseInt(process.env.HYPER_PAGE_SIZE || '100', 10);
const MAX_PAGE_SIZE = parseInt(process.env.HYPER_MAX_PAGE_SIZE || '500', 10);
const MAX_HTTP_BODY_BYTES = parseInt(process.env.HYPER_MAX_HTTP_BODY_BYTES || '8000000', 10);
const MASTER_BATCH_MAX_OPS = parseInt(process.env.HYPER_MASTER_BATCH_MAX_OPS || '2000', 10);
const MASTER_BATCH_MAX_BYTES = parseInt(process.env.HYPER_MASTER_BATCH_MAX_BYTES || '4000000', 10);
const WORKER_BATCH_MAX_OPS = parseInt(process.env.HYPER_WORKER_BATCH_MAX_OPS || '2000', 10);
const WORKER_BATCH_MAX_BYTES = parseInt(process.env.HYPER_WORKER_BATCH_MAX_BYTES || '4000000', 10);
const MAX_MASTER_ACTIVE_BATCHES = parseInt(process.env.HYPER_MASTER_ACTIVE_BATCHES || '8', 10);
const MAX_WORKER_ACTIVE_BATCHES = parseInt(process.env.HYPER_WORKER_ACTIVE_BATCHES || '4', 10);
const MAX_RESPONSE_BYTES = parseInt(process.env.HYPER_MAX_RESPONSE_BYTES || '16777216', 10);

// Bulk loader dial: `normal` is WAL-safe and ~5x faster than `full`. Set to
// `full` for serving workloads that need fsync-per-commit durability.
const SQLITE_SYNCHRONOUS = String(process.env.HYPER_SQLITE_SYNCHRONOUS || 'normal').trim().toLowerCase();

const CHANGE_POLL_LIMIT = parseInt(process.env.HYPER_CHANGE_POLL_LIMIT || '1024', 10);
const CHANGE_POLL_INTERVAL_MS = parseInt(process.env.HYPER_CHANGE_POLL_INTERVAL_MS || '150', 10);
const LIVE_ENABLED = !parseBool(process.env.HYPER_DISABLE_LIVE, false);

// Delta stream batches changes across a polling window. Tune per deployment.
const DELTA_FRAME_MAX_CHANGES = parseInt(process.env.HYPER_DELTA_FRAME_MAX || '200', 10);

const WORKER_AGENT = new http.Agent({
  keepAlive: true,
  maxSockets: Math.max(32, Math.max(1, NUM_LOCAL_WORKERS) * 8),
});

function log(tag, ...a) { console.log(new Date().toISOString(), `[${tag}]`, ...a); }
function isObj(v) { return !!v && typeof v === 'object' && !Array.isArray(v); }

const GUN_META = new Set(['_', '#', '>']);

function parseBool(v, fallback = false) {
  if (v == null) return fallback;
  const s = String(v).trim().toLowerCase();
  if (!s) return fallback;
  return s === '1' || s === 'true' || s === 'yes' || s === 'on';
}

function parseIntPositive(v, fallback) {
  const n = parseInt(String(v ?? ''), 10);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

function clamp(n, lo, hi) {
  return Math.max(lo, Math.min(hi, n));
}

// ---------------------------------------------------------------------------
// Path model
// ---------------------------------------------------------------------------
//
// Public path format is dotted: `geo.locations.arans-3041541`. Internally we
// work with:
//   - root:   the top segment, e.g. "geo"
//   - segments: ["locations", "arans-3041541"]
//   - full path: stored as slash-joined for lookup, e.g. "geo/locations/arans-3041541"
//
// We keep the full slash path on each row so outbox topics, ancestor walks,
// and existence checks remain cheap single-row queries.
// ---------------------------------------------------------------------------

function dp2parts(dp) {
  const parts = String(dp || '').split('.').filter(Boolean);
  return parts;
}

function parts2dp(parts) {
  return (parts || []).join('.');
}

function parts2slash(parts) {
  return (parts || []).join('/');
}

function slash2dp(slashPath) {
  const parts = String(slashPath || '').split('/').filter(Boolean);
  return parts2dp(parts);
}

function ancestorsOf(parts) {
  // For ["a","b","c","d"] returns [["a"], ["a","b"], ["a","b","c"]].
  // The node itself is NOT included.
  const out = [];
  for (let i = 1; i < parts.length; i += 1) out.push(parts.slice(0, i));
  return out;
}

function parentParts(parts) {
  return parts.length > 1 ? parts.slice(0, -1) : null;
}

function normalizeDotPath(value) {
  return String(value || '').trim().replace(/^\/+|\/+$/g, '').replace(/\//g, '.');
}

function sanitize(payload) {
  // Strip Gun-style metadata keys; shallow clean of `data` subkey if present.
  if (!isObj(payload)) return payload;
  const out = {};
  for (const [k, v] of Object.entries(payload)) {
    if (GUN_META.has(k)) continue;
    if (k === 'data' && isObj(v)) {
      const s = {};
      for (const [dk, dv] of Object.entries(v)) {
        if (!GUN_META.has(dk) && dv !== undefined) s[dk] = dv;
      }
      out.data = s;
      continue;
    }
    out[k] = v;
  }
  return out;
}

function fnv1a(str) {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i += 1) {
    h ^= str.charCodeAt(i);
    h = (h * 0x01000193) >>> 0;
  }
  return h;
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

function readBody(req, maxBytes = MAX_HTTP_BODY_BYTES) {
  return new Promise((resolve, reject) => {
    let total = 0;
    const chunks = [];
    let done = false;
    req.on('data', chunk => {
      if (done) return;
      total += chunk.length;
      if (total > maxBytes) {
        done = true;
        const err = new Error('payload too large');
        err.statusCode = 413;
        reject(err);
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      if (done) return;
      done = true;
      resolve(Buffer.concat(chunks).toString('utf8'));
    });
    req.on('error', err => {
      if (done) return;
      done = true;
      reject(err);
    });
    req.on('aborted', () => {
      if (done) return;
      done = true;
      const err = new Error('request aborted');
      err.statusCode = 400;
      reject(err);
    });
  });
}

function safeStringify(obj) {
  try {
    const s = JSON.stringify(obj);
    if (s && s.length > MAX_RESPONSE_BYTES) {
      return { error: new Error(`response too large (${s.length} > ${MAX_RESPONSE_BYTES})`) };
    }
    return { body: s };
  } catch (err) {
    return { error: err };
  }
}

function J(res, obj, st) {
  if (res.headersSent) return;
  const { body, error } = safeStringify(obj);
  if (error) {
    res.writeHead(500, {
      'Content-Type': 'application/json;charset=utf-8',
      'Access-Control-Allow-Origin': '*'
    });
    res.end(JSON.stringify({
      error: 'response_too_large',
      message: error.message || 'serialization failed'
    }));
    return;
  }
  res.writeHead(st || 200, {
    'Content-Type': 'application/json;charset=utf-8',
    'Access-Control-Allow-Origin': '*'
  });
  res.end(body);
}

function writeSSE(res, payload) {
  const { body, error } = safeStringify(payload);
  if (error) {
    try { res.write(`data: ${JSON.stringify({ error: 'frame_too_large' })}\n\n`); } catch (_) {}
    return;
  }
  try { res.write(`data: ${body}\n\n`); } catch (_) {}
}

// ---------------------------------------------------------------------------
// Hypermedia link/action builders
// ---------------------------------------------------------------------------

function stateHref(origin, dp, query = {}) {
  const u = new URL(`${origin}/${encodeURIComponent(dp)}`);
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '' || v === false) continue;
    u.searchParams.set(k, String(v));
  }
  return u.toString();
}

function apiHref(origin, dp, op, query = {}) {
  const u = new URL(`${origin}/${encodeURIComponent(dp)}/api/${op}`);
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '' || v === false) continue;
    u.searchParams.set(k, String(v));
  }
  return u.toString();
}

function streamHref(origin, dp, query = {}) {
  const u = new URL(`${origin}/${encodeURIComponent(dp)}`);
  u.searchParams.set('stream', 'true');
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '' || v === false) continue;
    u.searchParams.set(k, String(v));
  }
  return u.toString();
}

function buildAction(method, href, fields = [], title = '') {
  return { method, href, fields, title };
}

function randomId() {
  return crypto.randomBytes(8).toString('hex');
}

// ===========================================================================
// WORKER
// ===========================================================================

if (ROLE === 'worker' || cluster.isWorker) {
  const WID = parseInt(process.env.HYPER_WORKER_ID || '0', 10);
  const WPORT = parseInt(process.env.HYPER_WORKER_PORT || String(PORT + 1 + WID), 10);
  const WBIND = process.env.HYPER_WORKER_BIND || (cluster.isWorker ? '127.0.0.1' : BIND);
  const WDIR = process.env.HYPER_WORKER_DATA_DIR || path.join(RDIR, `shard-${WID}`);

  const SQLITE_DIR = path.join(WDIR, 'sqlite');
  fs.mkdirSync(WDIR, { recursive: true });
  fs.mkdirSync(SQLITE_DIR, { recursive: true });

  // Two handles on the same WAL-mode file: `db` for writes, `dbRead` for
  // concurrent reads that don't block behind write transactions.
  const dbPath = path.join(SQLITE_DIR, 'nodes.sqlite');
  const db = new Database(dbPath, { timeout: 30000, fileMustExist: false });
  db.pragma('journal_mode = WAL');
  db.pragma(`synchronous = ${SQLITE_SYNCHRONOUS === 'full' ? 'FULL' : SQLITE_SYNCHRONOUS === 'off' ? 'OFF' : 'NORMAL'}`);
  db.pragma('temp_store = MEMORY');
  db.pragma('cache_size = -65536');
  db.pragma('mmap_size = 268435456');
  db.pragma('wal_autocheckpoint = 1000');
  db.pragma('busy_timeout = 30000');

  const dbRead = new Database(dbPath, { timeout: 30000, fileMustExist: true, readonly: true });
  dbRead.pragma('journal_mode = WAL');
  dbRead.pragma('cache_size = -32768');
  dbRead.pragma('mmap_size = 268435456');
  dbRead.pragma('busy_timeout = 30000');

  // -------------------------------------------------------------------------
  // Schema
  //
  // A node is a row. parent_id is NULL for roots. (parent_id, name) is unique
  // so we can upsert a path segment cheaply. `path` is the full slash-joined
  // path; it's denormalized for ancestor walks and existence checks but
  // enforced by the write code, not by SQLite.
  // -------------------------------------------------------------------------
  db.exec(`
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

    -- Outbox now records one row per commit (exact path). Prefix fanout is
    -- resolved at delivery time by walking ancestors. This is the single
    -- biggest write-path win vs. the old model, which inserted N outbox rows
    -- per commit (one per ancestor prefix).
    CREATE TABLE IF NOT EXISTS outbox (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      root TEXT NOT NULL,
      path TEXT NOT NULL,
      op_kind TEXT NOT NULL,
      commit_seq INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      payload TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_outbox_id ON outbox(id);

    INSERT INTO meta(key, int_value) VALUES ('commit_seq', 0) ON CONFLICT(key) DO NOTHING;
  `);

  // --- WRITE statements ----------------------------------------------------

  const stInsertNode = db.prepare(`
    INSERT INTO nodes(parent_id, root, name, path, data, updated_at, commit_seq)
    VALUES (?, ?, ?, ?, ?, ?, ?)
  `);

  const stUpdateData = db.prepare(`
    UPDATE nodes
    SET data = ?, updated_at = ?, commit_seq = ?
    WHERE id = ?
  `);

  const stDeleteById = db.prepare(`DELETE FROM nodes WHERE id = ?`);

  const stCountChildren = db.prepare(`SELECT COUNT(*) AS c FROM nodes WHERE parent_id = ?`);

  const stNextCommitSeq = db.prepare(`
    INSERT INTO meta(key, int_value)
    VALUES ('commit_seq', 1)
    ON CONFLICT(key) DO UPDATE SET int_value = int_value + 1
    RETURNING int_value
  `);

  const stInsertOutbox = db.prepare(`
    INSERT INTO outbox(root, path, op_kind, commit_seq, updated_at, payload)
    VALUES (?, ?, ?, ?, ?, ?)
  `);

  const stOutboxTrim = db.prepare(`DELETE FROM outbox WHERE id <= ?`);

  // Need the writer handle for these — they participate in write transactions.
  const stFindByParentNameW = db.prepare(`
    SELECT id, data FROM nodes WHERE parent_id IS ? AND name = ?
  `);

  // --- READ statements (all on dbRead) -------------------------------------

  const stFindByParentName = dbRead.prepare(`
    SELECT id, data, updated_at, commit_seq
    FROM nodes WHERE parent_id IS ? AND name = ?
  `);

  const stFindByPath = dbRead.prepare(`
    SELECT id, parent_id, data, updated_at, commit_seq
    FROM nodes WHERE root = ? AND path = ?
  `);

  const stRoots = dbRead.prepare(`
    SELECT name FROM nodes WHERE parent_id IS NULL ORDER BY name ASC
  `);

  const stChildrenPage = dbRead.prepare(`
    SELECT name, data, updated_at, commit_seq
    FROM nodes WHERE parent_id = ?
    ORDER BY name ASC
    LIMIT ? OFFSET ?
  `);

  const stChildrenPageDesc = dbRead.prepare(`
    SELECT name, data, updated_at, commit_seq
    FROM nodes WHERE parent_id = ?
    ORDER BY name DESC
    LIMIT ? OFFSET ?
  `);

  const stChildrenPageUpdated = dbRead.prepare(`
    SELECT name, data, updated_at, commit_seq
    FROM nodes WHERE parent_id = ?
    ORDER BY updated_at DESC, name DESC
    LIMIT ? OFFSET ?
  `);

  const stChildrenPageUpdatedAsc = dbRead.prepare(`
    SELECT name, data, updated_at, commit_seq
    FROM nodes WHERE parent_id = ?
    ORDER BY updated_at ASC, name ASC
    LIMIT ? OFFSET ?
  `);

  const stCountChildrenRead = dbRead.prepare(`SELECT COUNT(*) AS c FROM nodes WHERE parent_id = ?`);

  const stNodeMeta = dbRead.prepare(`
    SELECT id, parent_id, name, path, data, updated_at, commit_seq
    FROM nodes WHERE id = ?
  `);

  const stOutboxAfter = dbRead.prepare(`
    SELECT id, root, path, op_kind, commit_seq, updated_at, payload
    FROM outbox
    WHERE id > ?
    ORDER BY id ASC
    LIMIT ?
  `);

  const stOutboxAfterForRoot = dbRead.prepare(`
    SELECT id, root, path, op_kind, commit_seq, updated_at, payload
    FROM outbox
    WHERE id > ? AND root = ?
    ORDER BY id ASC
    LIMIT ?
  `);

  const stMetaGetInt = dbRead.prepare(`SELECT int_value FROM meta WHERE key = ?`);

  // -------------------------------------------------------------------------
  // Core node operations
  //
  // `ensurePath` walks segments, inserting any missing ancestor. Each inserted
  // ancestor is a real node with data=NULL. The terminal node gets the payload.
  //
  // `deleteNode` removes the node and then walks upward pruning any parent
  // that is now empty (no data, no children).
  // -------------------------------------------------------------------------

  function findChildIdW(parentId, name) {
    // `parent_id IS ?` handles NULL correctly for root-level lookups.
    const row = stFindByParentNameW.get(parentId, name);
    return row ? row.id : null;
  }

  function ensurePath(parts, data, updatedAt, commitSeq) {
    // Returns { id, wasNewTerminal, hadData }.
    if (!parts.length) throw new Error('empty path');

    const root = parts[0];
    let parentId = null;
    let currentId = null;

    for (let i = 0; i < parts.length; i += 1) {
      const name = parts[i];
      const isTerminal = i === parts.length - 1;
      const slashPath = parts.slice(0, i + 1).join('/');

      const existingId = findChildIdW(parentId, name);

      if (existingId == null) {
        // Insert a new node. Non-terminal gets NULL data. Terminal gets payload.
        const info = stInsertNode.run(
          parentId,
          root,
          name,
          slashPath,
          isTerminal && data !== undefined ? JSON.stringify(data) : null,
          updatedAt,
          commitSeq
        );
        currentId = info.lastInsertRowid;
      } else if (isTerminal) {
        // Overwrite the terminal node's data.
        stUpdateData.run(
          data !== undefined ? JSON.stringify(data) : null,
          updatedAt,
          commitSeq,
          existingId
        );
        currentId = existingId;
      } else {
        currentId = existingId;
      }

      parentId = currentId;
    }

    return currentId;
  }

  function deleteNodeByPath(parts) {
    // Returns true if something was actually deleted.
    if (!parts.length) return false;

    // Walk down to find the node.
    let parentId = null;
    let nodeId = null;
    for (const name of parts) {
      const row = stFindByParentNameW.get(parentId, name);
      if (!row) return false;
      nodeId = row.id;
      parentId = row.id;
    }

    // Delete the node itself.
    stDeleteById.run(nodeId);

    // Walk up pruning empty ancestors. An ancestor is "empty" iff it has no
    // data AND no remaining children. We stop as soon as we hit one that
    // still has either.
    for (let i = parts.length - 2; i >= 0; i -= 1) {
      // Re-resolve the ancestor id fresh (parent_id chain may have shifted
      // conceptually but nodes stay addressable by (parent_id, name)).
      let pId = null;
      let aId = null;
      for (let j = 0; j <= i; j += 1) {
        const arow = stFindByParentNameW.get(pId, parts[j]);
        if (!arow) { aId = null; break; }
        aId = arow.id;
        pId = arow.id;
      }
      if (aId == null) break;

      const meta = stNodeMeta.get(aId);
      if (!meta) break;
      const childCount = stCountChildren.get(aId).c;
      const hasData = meta.data != null;

      if (childCount === 0 && !hasData) {
        stDeleteById.run(aId);
      } else {
        break;
      }
    }

    return true;
  }

  function dbGetByPath(root, slashPath) {
    const row = stFindByPath.get(root, slashPath);
    if (!row) return null;
    return {
      id: row.id,
      data: row.data ? JSON.parse(row.data) : null,
      updated_at: row.updated_at,
      commit_seq: row.commit_seq
    };
  }

  function dbGetByParts(parts) {
    if (!parts.length) return null;
    return dbGetByPath(parts[0], parts2slash(parts));
  }

  function dbChildrenPage(parentId, limit, offset, order) {
    const stmts = {
      key_asc: stChildrenPage,
      key_desc: stChildrenPageDesc,
      updated_desc: stChildrenPageUpdated,
      updated_asc: stChildrenPageUpdatedAsc
    };
    const stmt = stmts[order] || stChildrenPage;
    const rows = [];
    for (const row of stmt.iterate(parentId, limit, offset)) {
      rows.push({
        name: row.name,
        data: row.data ? JSON.parse(row.data) : null,
        updated_at: row.updated_at,
        commit_seq: row.commit_seq
      });
    }
    return rows;
  }

  function dbChildrenCount(parentId) {
    return stCountChildrenRead.get(parentId).c;
  }

  function dbResolvePathId(parts) {
    if (!parts.length) return null;
    const row = stFindByPath.get(parts[0], parts2slash(parts));
    return row ? row.id : null;
  }

  function dbRoots() {
    const out = [];
    for (const row of stRoots.iterate()) out.push(row.name);
    return out;
  }

  // -------------------------------------------------------------------------
  // Transactional apply: put / delete
  // -------------------------------------------------------------------------

  const txApplyOps = db.transaction(ops => {
    let lastSeq = 0;
    let count = 0;
    for (const op of ops) {
      const parts = dp2parts(op.path || '');
      if (!parts.length) continue;

      const seq = stNextCommitSeq.get().int_value;
      const now = Date.now();
      lastSeq = seq;

      if (op.delete) {
        const existed = deleteNodeByPath(parts);
        if (existed) {
          stInsertOutbox.run(
            parts[0],
            parts2slash(parts),
            'del',
            seq,
            now,
            null
          );
        }
      } else {
        const clean = sanitize(op.data || {});
        ensurePath(parts, clean, now, seq);
        stInsertOutbox.run(
          parts[0],
          parts2slash(parts),
          'put',
          seq,
          now,
          JSON.stringify({ data: clean, updated_at: now, commit_seq: seq })
        );
      }
      count += 1;
    }
    return { count, commit_seq: lastSeq };
  });

  function chunkOps(ops, maxOps, maxBytes) {
    const out = [];
    let cur = [];
    let curBytes = 0;
    for (const op of ops) {
      const bytes = Buffer.byteLength(JSON.stringify(op), 'utf8');
      if (cur.length && (cur.length + 1 > maxOps || curBytes + bytes > maxBytes)) {
        out.push(cur);
        cur = [];
        curBytes = 0;
      }
      cur.push(op);
      curBytes += bytes;
    }
    if (cur.length) out.push(cur);
    return out;
  }

  function applyMany(ops) {
    let count = 0;
    let commitSeq = 0;
    for (const chunk of chunkOps(ops, WORKER_BATCH_MAX_OPS, WORKER_BATCH_MAX_BYTES)) {
      const r = txApplyOps(chunk);
      count += r.count;
      if (r.commit_seq > commitSeq) commitSeq = r.commit_seq;
    }
    return { count, commit_seq: commitSeq };
  }

  // -------------------------------------------------------------------------
  // HTTP surface
  // -------------------------------------------------------------------------

  let activeWorkerBatches = 0;

  const wServer = http.createServer(async (req, res) => {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
    if (req.method === 'OPTIONS') {
      res.writeHead(200);
      res.end();
      return;
    }

    const u = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
    const pn = u.pathname.replace(/\/+$/, '') || '/';

    try {
      if (req.method === 'GET' && pn === '/health') {
        return J(res, {
          ok: true,
          worker: WID,
          commit_seq: ((stMetaGetInt.get('commit_seq') || {}).int_value || 0)
        });
      }

      if (req.method === 'GET' && pn === '/read') {
        const parts = dp2parts(u.searchParams.get('path') || '');
        const node = dbGetByParts(parts);
        if (!node) return J(res, null);
        return J(res, {
          data: node.data,
          updated_at: node.updated_at,
          commit_seq: node.commit_seq
        });
      }

      if (req.method === 'GET' && pn === '/roots') {
        return J(res, dbRoots());
      }

      if (req.method === 'GET' && pn === '/children') {
        const parts = dp2parts(u.searchParams.get('path') || '');
        const limit = clamp(parseIntPositive(u.searchParams.get('limit'), DEFAULT_PAGE_SIZE), 1, MAX_PAGE_SIZE);
        const offset = Math.max(0, parseInt(u.searchParams.get('offset') || '0', 10) || 0);
        const order = u.searchParams.get('order') || 'key_asc';

        // For root listing, parent_id is NULL; we have to match `parent_id IS NULL`.
        let parentId;
        if (!parts.length) {
          parentId = null;
        } else {
          parentId = dbResolvePathId(parts);
          if (parentId == null) return J(res, { total: 0, rows: [] });
        }

        // Hack: children queries take parentId directly but we need to handle
        // the null-parent case using a different query path.
        if (parentId === null) {
          const rows = [];
          let idx = 0;
          for (const row of dbRead.prepare(`
            SELECT name, data, updated_at, commit_seq
            FROM nodes WHERE parent_id IS NULL
            ORDER BY name ASC LIMIT ? OFFSET ?
          `).iterate(limit, offset)) {
            rows.push({
              name: row.name,
              data: row.data ? JSON.parse(row.data) : null,
              updated_at: row.updated_at,
              commit_seq: row.commit_seq
            });
            idx += 1;
          }
          const total = dbRead.prepare(`SELECT COUNT(*) AS c FROM nodes WHERE parent_id IS NULL`).get().c;
          return J(res, { total, rows });
        }

        const rows = dbChildrenPage(parentId, limit, offset, order);
        const total = dbChildrenCount(parentId);
        return J(res, { total, rows });
      }

      if (req.method === 'GET' && pn === '/exists') {
        const parts = dp2parts(u.searchParams.get('path') || '');
        if (!parts.length) return J(res, false);
        return J(res, dbResolvePathId(parts) != null);
      }

      if (req.method === 'GET' && pn === '/changes') {
        const after = Math.max(0, parseInt(u.searchParams.get('after') || '0', 10) || 0);
        const limit = clamp(parseIntPositive(u.searchParams.get('limit'), CHANGE_POLL_LIMIT), 1, 5000);
        const root = u.searchParams.get('root') || '';
        const rows = root
          ? stOutboxAfterForRoot.all(after, root, limit)
          : stOutboxAfter.all(after, limit);
        return J(res, { rows });
      }

      if (req.method === 'POST' && pn === '/trim-changes') {
        let body;
        try { body = JSON.parse(await readBody(req, MAX_HTTP_BODY_BYTES)); }
        catch (err) { return J(res, { error: err.message || 'bad json' }, err.statusCode || 400); }
        const throughId = Math.max(0, parseInt(body.through_id || '0', 10) || 0);
        stOutboxTrim.run(throughId);
        return J(res, { ok: true, through_id: throughId });
      }

      if (req.method === 'PUT' && pn === '/write') {
        if (activeWorkerBatches >= MAX_WORKER_ACTIVE_BATCHES) return J(res, { error: 'worker busy' }, 429);
        let payload;
        try { payload = JSON.parse(await readBody(req, MAX_HTTP_BODY_BYTES)); }
        catch (err) { return J(res, { error: err.message || 'bad json' }, err.statusCode || 400); }
        activeWorkerBatches += 1;
        try {
          const r = applyMany([{ path: u.searchParams.get('path') || '', data: payload }]);
          return J(res, { ok: true, count: r.count, commit_seq: r.commit_seq });
        } finally { activeWorkerBatches -= 1; }
      }

      if (req.method === 'POST' && pn === '/batch') {
        if (activeWorkerBatches >= MAX_WORKER_ACTIVE_BATCHES) return J(res, { error: 'worker busy' }, 429);
        let body;
        try { body = JSON.parse(await readBody(req, MAX_HTTP_BODY_BYTES)); }
        catch (err) { return J(res, { error: err.message || 'bad json' }, err.statusCode || 400); }
        const ops = body.ops || body;
        if (!Array.isArray(ops)) return J(res, { error: 'ops must be array' }, 400);
        activeWorkerBatches += 1;
        try {
          const r = applyMany(ops);
          return J(res, { ok: true, count: r.count, commit_seq: r.commit_seq });
        } finally { activeWorkerBatches -= 1; }
      }

      if (req.method === 'DELETE' && pn === '/delete') {
        if (activeWorkerBatches >= MAX_WORKER_ACTIVE_BATCHES) return J(res, { error: 'worker busy' }, 429);
        activeWorkerBatches += 1;
        try {
          const r = applyMany([{ path: u.searchParams.get('path') || '', delete: true }]);
          return J(res, { ok: true, count: r.count, commit_seq: r.commit_seq });
        } finally { activeWorkerBatches -= 1; }
      }

      if (req.method === 'POST' && pn === '/clear') {
        const root = u.searchParams.get('root') || '';
        db.transaction(r => {
          // Delete all nodes under the root (including the root node itself).
          dbRead.prepare(`SELECT id FROM nodes WHERE root = ?`).all(r).forEach(row => {
            stDeleteById.run(row.id);
          });
          db.prepare(`DELETE FROM outbox WHERE root = ?`).run(r);
        })(root);
        return J(res, { ok: true });
      }

      return J(res, { error: 'unknown' }, 404);
    } catch (err) {
      log(`W${WID}`, 'request failed:', err && err.message ? err.message : err);
      return J(res, { error: err && err.message ? err.message : 'internal_error' }, err && err.statusCode ? err.statusCode : 500);
    }
  });

  process.on('exit', () => { try { db.close(); } catch (_) {} try { dbRead.close(); } catch (_) {} });
  process.on('SIGTERM', () => { try { db.close(); } catch (_) {} try { dbRead.close(); } catch (_) {} process.exit(0); });
  process.on('unhandledRejection', (err) => log(`W${WID}`, 'unhandledRejection:', err && err.message ? err.message : err));

  wServer.listen(WPORT, WBIND, () => log(
    `W${WID}`,
    `http://127.0.0.1:${WPORT}`,
    `sync=${SQLITE_SYNCHRONOUS}`
  ));
  return;
}

// ===========================================================================
// MASTER
// ===========================================================================

log('MASTER', `Starting ${NUM_LOCAL_WORKERS} local workers...`);
const localWorkers = [];
for (let i = 0; i < Math.max(0, NUM_LOCAL_WORKERS); i += 1) {
  const workerPort = PORT + 1 + i;
  localWorkers.push({
    id: `local:${i}`,
    port: workerPort,
    internalUrl: `http://127.0.0.1:${workerPort}`,
    process: cluster.fork({
      ...process.env,
      HYPER_ROLE: 'worker',
      HYPER_WORKER_ID: String(i),
      HYPER_WORKER_PORT: String(workerPort),
      HYPER_WORKER_BIND: '127.0.0.1',
      HYPER_WORKER_DATA_DIR: path.join(RDIR, `shard-${i}`)
    })
  });
}

cluster.on('exit', (w, code) => {
  const idx = localWorkers.findIndex(x => x.process === w);
  if (idx >= 0) {
    log('MASTER', `Worker ${idx} died (${code}), restarting`);
    const wp = PORT + 1 + idx;
    localWorkers[idx].process = cluster.fork({
      ...process.env,
      HYPER_ROLE: 'worker',
      HYPER_WORKER_ID: String(idx),
      HYPER_WORKER_PORT: String(wp),
      HYPER_WORKER_BIND: '127.0.0.1',
      HYPER_WORKER_DATA_DIR: path.join(RDIR, `shard-${idx}`)
    });
  }
});

function workerUrls() {
  return localWorkers.map(w => w.internalUrl);
}

function shardFor(dp) {
  const parts = dp2parts(dp);
  if (!parts.length) return localWorkers[0];
  // Shard by root name — keeps a root's whole subtree co-located on one worker.
  return localWorkers[fnv1a(parts[0]) % localWorkers.length];
}

function wFetch(baseUrl, p, opts = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(baseUrl + p);
    const r = http.request({
      hostname: u.hostname,
      port: u.port,
      path: u.pathname + u.search,
      method: opts.method || 'GET',
      agent: WORKER_AGENT,
      headers: { 'Content-Type': 'application/json' }
    }, res => {
      let d = '';
      res.on('data', c => d += c);
      res.on('end', () => {
        if (res.statusCode >= 400) {
          const err = new Error(d || `HTTP ${res.statusCode}`);
          err.statusCode = res.statusCode;
          return reject(err);
        }
        try { resolve(JSON.parse(d)); } catch (_) { resolve(d || null); }
      });
    });
    r.on('error', reject);
    if (opts.body) r.write(opts.body);
    r.end();
  });
}

async function waitForLocalWorkersReady(timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const checks = await Promise.allSettled(workerUrls().map(u => wFetch(u, '/health')));
    if (checks.every(r => r.status === 'fulfilled' && r.value && r.value.ok)) {
      log('MASTER', 'All local workers healthy');
      return;
    }
    await new Promise(r => setTimeout(r, 200));
  }
  throw new Error('Timed out waiting for local workers');
}

async function mRead(dp) {
  return wFetch(shardFor(dp).internalUrl, `/read?path=${encodeURIComponent(dp)}`);
}

async function mChildren(dp, { limit, offset, order }) {
  return wFetch(
    shardFor(dp).internalUrl,
    `/children?path=${encodeURIComponent(dp)}&limit=${limit}&offset=${offset}&order=${encodeURIComponent(order)}`
  );
}

async function mRoots() {
  const merged = new Set();
  const rs = await Promise.all(workerUrls().map(u => wFetch(u, '/roots').catch(() => [])));
  for (const arr of rs) if (Array.isArray(arr)) for (const r of arr) merged.add(r);
  return Array.from(merged).sort();
}

async function mBatch(ops) {
  const buckets = new Map();
  for (const op of ops) {
    if (!op.path) continue;
    const t = shardFor(op.path);
    if (!buckets.has(t.id)) buckets.set(t.id, { target: t, ops: [] });
    buckets.get(t.id).ops.push(op);
  }
  let total = 0, commitSeq = 0;
  for (const { target, ops: to } of buckets.values()) {
    // Chunk to keep master→worker payloads bounded.
    const chunks = [];
    let cur = [], curBytes = 0;
    for (const op of to) {
      const bytes = Buffer.byteLength(JSON.stringify(op), 'utf8');
      if (cur.length && (cur.length + 1 > MASTER_BATCH_MAX_OPS || curBytes + bytes > MASTER_BATCH_MAX_BYTES)) {
        chunks.push(cur); cur = []; curBytes = 0;
      }
      cur.push(op); curBytes += bytes;
    }
    if (cur.length) chunks.push(cur);

    for (const chunk of chunks) {
      const r = await wFetch(target.internalUrl, '/batch', { method: 'POST', body: JSON.stringify({ ops: chunk }) });
      if (r && r.count) total += r.count;
      if (r && r.commit_seq && r.commit_seq > commitSeq) commitSeq = r.commit_seq;
    }
  }
  return { count: total, commit_seq: commitSeq };
}

async function mChangesSince(root, cursor, limit) {
  // Aggregate change rows across shards that hold this root. In practice, a
  // single root lives on a single shard (we shard by root), so this is one
  // worker call — but we query all to be safe against future rebalancing.
  const all = [];
  const rs = await Promise.all(workerUrls().map(u =>
    wFetch(u, `/changes?after=${cursor}&limit=${limit}&root=${encodeURIComponent(root)}`).catch(() => ({ rows: [] }))
  ));
  for (const r of rs) if (r && Array.isArray(r.rows)) for (const row of r.rows) all.push(row);
  all.sort((a, b) => a.id - b.id);
  return all;
}

// ---------------------------------------------------------------------------
// State-doc builder (directory-model, self-describing, HATEOAS-honest)
//
// Rules the builder follows, in priority order:
//
//   1. `_links` and `_embedded` are the state. They describe what's inside
//      a directory. `data` carries the node's own payload — nothing else.
//      When a node has no stored payload, `data` is null. We do NOT
//      synthesize a second representation of the children under `data`.
//
//   2. Classifications ASSERT only what the current request observed. The
//      node we fetched has its children enumerated, so we can classify it
//      with confidence (`collection`, `index_bucket`, `record`, etc.).
//      Embedded children are DIFFERENT — we saw their row but not their
//      children, so we never claim a count or a shelf/bucket classification
//      about them. Embedded kinds are one of: `record`, `index_ref`, or
//      `reference` (i.e. "follow self to learn more").
//
//   3. Summaries are only produced from data we actually fetched. No
//      "0 groups" guesses about nodes we didn't enumerate.
// ---------------------------------------------------------------------------

// Classify the node this request is *about*. Uses children we fetched,
// so `index_shelf`/`collection`/`index_bucket` classifications are real.
function classifyNode(node, children, parts) {
  const hasData = !!(node && node.data != null);
  const hasChildren = (children && children.total) > 0;
  const sample = (children && children.rows) || [];

  if (parts.length === 0) {
    return { kind: 'system_root', item_kind: 'root', display_as: 'list', primary_link: 'self', sort_by: 'name' };
  }
  if (parts[0] === '_meta' || parts.includes('_meta')) {
    return { kind: 'system', item_kind: hasChildren ? 'directory' : 'record', display_as: 'detail', primary_link: 'self', sort_by: 'name' };
  }

  if (hasData && node.data.kind === 'index_ref') {
    return { kind: 'index_ref', item_kind: 'ref', display_as: 'detail', primary_link: 'record', sort_by: 'name' };
  }

  // Children are all index_refs → this is an index bucket.
  if (!hasData && hasChildren && sample.length > 0 &&
      sample.every(c => c.data && c.data.kind === 'index_ref')) {
    return { kind: 'index_bucket', item_kind: 'ref', display_as: 'list', primary_link: 'record', sort_by: 'name' };
  }

  // We're somewhere under `index/` and our children are all directories
  // (no own data). This is the `by`, `scoped`, `by/timezone` etc. level.
  const idxPos = parts.indexOf('index');
  if (idxPos >= 0 && !hasData) {
    const childrenAllDirs = sample.length === 0 || sample.every(c => c.data == null);
    if (childrenAllDirs) {
      return { kind: 'index_shelf', item_kind: 'directory', display_as: 'list', primary_link: 'self', sort_by: 'name' };
    }
  }

  // Children are records (have their own data, not as index_refs).
  if (!hasData && hasChildren && sample.length > 0 &&
      sample.some(c => c.data != null && c.data.kind !== 'index_ref')) {
    return { kind: 'collection', item_kind: 'record', display_as: 'list', primary_link: 'self', sort_by: 'name' };
  }

  if (hasData && !hasChildren) {
    return { kind: 'record', item_kind: 'field', display_as: 'detail', primary_link: 'self', sort_by: 'name' };
  }
  if (hasData && hasChildren) {
    return { kind: 'record_with_children', item_kind: 'mixed', display_as: 'detail', primary_link: 'self', sort_by: 'name' };
  }

  // Fallback: a plain directory (empty or we can't tell). Used for top-level
  // roots like `geo` whose children we enumerated but whose shape is a mix.
  return { kind: 'directory', item_kind: 'directory', display_as: 'list', primary_link: 'self', sort_by: 'name' };
}

// Summarize the node this request is about. We have its full children page.
function summarizeNode(cls, node, children, parts) {
  const total = (children && children.total) || 0;
  const name = parts.length ? parts[parts.length - 1] : '/';

  switch (cls.kind) {
    case 'system_root':
      return `Hypergraph root. ${total} root namespace${total === 1 ? '' : 's'}.`;

    case 'system':
      return total
        ? `System namespace: ${name} (${total} entr${total === 1 ? 'y' : 'ies'}).`
        : `System namespace: ${name}.`;

    case 'index_ref': {
      const d = (node && node.data) || {};
      const bits = [];
      if (d.index_name && d.index_value) bits.push(`${d.index_name}=${d.index_value}`);
      if (d.record_path) bits.push(`→ ${d.record_path}`);
      if (d.name) bits.push(`(${d.name})`);
      return bits.length ? `Index ref: ${bits.join(' ')}.` : 'Index reference.';
    }

    case 'index_bucket': {
      const indexName = parts.length >= 2 ? parts[parts.length - 2] : 'index';
      return `Index bucket: ${indexName} = ${name}. ${total} reference${total === 1 ? '' : 's'}.`;
    }

    case 'index_shelf':
      return `Index shelf: ${name}. ${total} group${total === 1 ? '' : 's'}.`;

    case 'collection':
      return `Collection: ${name}. ${total} record${total === 1 ? '' : 's'}.`;

    case 'record': {
      const d = (node && node.data) || {};
      const label = d.name || d.title || d.label || d.id || name;
      return `Record: ${label}.`;
    }

    case 'record_with_children':
      return `Record: ${name} (with ${total} child node${total === 1 ? '' : 's'}).`;

    case 'directory':
    default:
      return total
        ? `Directory: ${name} (${total} entr${total === 1 ? 'y' : 'ies'}).`
        : `Empty directory: ${name}.`;
  }
}

// Classify and summarize an EMBEDDED child — i.e. a row we fetched as part
// of listing children, but whose own children we did NOT enumerate.
//
// We only claim what we can see:
//   - child.data is an index_ref → kind: "index_ref", real summary.
//   - child.data is otherwise non-null → kind: "record", summary from fields.
//   - child.data is null → kind: "reference", no summary. Follow `self`.
//
// Crucially: we never claim "index_shelf" / "index_bucket" / "collection"
// for embedded children, because those require knowing about the child's
// own children. If a client wants that classification, it follows `self`
// and gets an authoritative response.
function classifyEmbeddedChild(childRow, childParts) {
  if (childParts[0] === '_meta' || childParts.includes('_meta')) {
    return { kind: 'system', item_kind: 'directory', display_as: 'detail', primary_link: 'self' };
  }
  if (childRow.data && childRow.data.kind === 'index_ref') {
    return { kind: 'index_ref', item_kind: 'ref', display_as: 'detail', primary_link: 'record' };
  }
  if (childRow.data != null) {
    return { kind: 'record', item_kind: 'field', display_as: 'detail', primary_link: 'self' };
  }
  return { kind: 'reference', item_kind: 'directory', display_as: 'list', primary_link: 'self' };
}

function summarizeEmbeddedChild(cls, childRow, childParts) {
  const name = childParts[childParts.length - 1];

  switch (cls.kind) {
    case 'index_ref': {
      const d = childRow.data || {};
      const bits = [];
      if (d.index_name && d.index_value) bits.push(`${d.index_name}=${d.index_value}`);
      if (d.record_path) bits.push(`→ ${d.record_path}`);
      if (d.name) bits.push(`(${d.name})`);
      return bits.length ? `Index ref: ${bits.join(' ')}.` : 'Index reference.';
    }

    case 'record': {
      const d = childRow.data || {};
      const label = d.name || d.title || d.label || d.id || name;
      return `Record: ${label}.`;
    }

    case 'system':
      return `System entry: ${name}. Follow self to explore.`;

    case 'reference':
    default:
      // No summary — we don't know. The client follows `self` to find out.
      // Returning `null` signals "no claim"; the builder omits the field.
      return null;
  }
}

async function buildNodeDoc(origin, dp, { page = 1, perPage = DEFAULT_PAGE_SIZE, order = 'key_asc' } = {}) {
  const parts = dp2parts(dp);
  const node = parts.length ? await mRead(dp) : null;
  const childrenPage = await mChildren(dp || '', {
    limit: perPage,
    offset: (page - 1) * perPage,
    order
  }).catch(() => ({ total: 0, rows: [] }));

  const cls = classifyNode(node, childrenPage, parts);
  const summary = summarizeNode(cls, node, childrenPage, parts);

  // --- Links ---------------------------------------------------------------
  const links = {
    self: stateHref(origin, dp || ''),
    stream: streamHref(origin, dp || ''),
    changes_since: apiHref(origin, dp || '', 'changes-since')
  };

  if (parts.length > 1) {
    links.parent = stateHref(origin, parts.slice(0, -1).join('.'));
  } else if (parts.length === 1) {
    links.parent = `${origin}/`;
  }

  // Named child links — convenient for hand-exploration.
  for (const child of childrenPage.rows) {
    const childDp = parts.length ? `${dp}.${child.name}` : child.name;
    links[child.name] = stateHref(origin, childDp);
  }

  // Pagination
  const total = childrenPage.total || 0;
  const numPages = Math.max(1, Math.ceil(total / perPage));
  const safePage = clamp(page, 1, numPages);
  if (safePage > 1) {
    links.prev = stateHref(origin, dp || '', { page: safePage - 1, per_page: perPage, order });
  }
  if (safePage < numPages) {
    links.next = stateHref(origin, dp || '', { page: safePage + 1, per_page: perPage, order });
  }

  // For index_refs, surface the canonical record at the top level.
  if (cls.kind === 'index_ref' && node && node.data && node.data.record_dot) {
    links.record = stateHref(origin, String(node.data.record_dot));
  }

  // --- Embedded children --------------------------------------------------
  //
  // Each embedded child is classified from data we saw, and nothing more.
  // No sub-queries, no speculative counts, no "0 groups" lies.
  const embedded = {};
  for (const child of childrenPage.rows) {
    const childDp = parts.length ? `${dp}.${child.name}` : child.name;
    const childParts = parts.length ? [...parts, child.name] : [child.name];
    const childCls = classifyEmbeddedChild(child, childParts);
    const childSummary = summarizeEmbeddedChild(childCls, child, childParts);

    const childState = {
      kind: childCls.kind,
      path: childDp,
      commit_seq: child.commit_seq,
      hints: {
        item_kind: childCls.item_kind,
        display_as: childCls.display_as,
        primary_link: childCls.primary_link
      }
    };
    // Only attach a summary if we have one to make honestly.
    if (childSummary) childState.summary = childSummary;

    const childLinks = {
      self: stateHref(origin, childDp),
      stream: streamHref(origin, childDp)
    };
    if (child.data && child.data.record_dot) {
      childLinks.record = stateHref(origin, String(child.data.record_dot));
    }

    embedded[child.name] = {
      _state: childState,
      _links: childLinks,
      data: child.data
    };
  }

  // --- Actions -------------------------------------------------------------
  const actions = {};

  actions.subscribe = buildAction(
    'GET',
    streamHref(origin, dp || '', { scope: 'subtree' }),
    [{ name: 'scope', type: 'string', required: false, options: ['exact', 'subtree'] }],
    'Subscribe to live changes (Server-Sent Events). Delivers a cursor frame then delta frames.'
  );

  actions.list_children = buildAction(
    'GET',
    stateHref(origin, dp || ''),
    [
      { name: 'page', type: 'number', required: false },
      { name: 'per_page', type: 'number', required: false },
      { name: 'order', type: 'string', required: false, options: ['key_asc', 'key_desc', 'updated_desc', 'updated_asc'] }
    ],
    'Navigate children via pagination.'
  );

  if (parts.length) {
    actions.update = buildAction(
      'PUT', stateHref(origin, dp),
      [{ name: 'data', type: 'object', required: true }],
      'Replace this node\'s data payload.'
    );
    actions.delete = buildAction(
      'DELETE', stateHref(origin, dp),
      [],
      'Delete this node. Empty parent directories are auto-pruned.'
    );
  }

  if (cls.kind === 'index_ref' && node && node.data && node.data.record_dot) {
    actions.follow_record = buildAction(
      'GET', stateHref(origin, String(node.data.record_dot)),
      [],
      'Follow this reference to its canonical record.'
    );
  }

  // --- State doc -----------------------------------------------------------
  //
  // `data` is `node.data` verbatim (or null). We do NOT synthesize a second
  // representation of `_embedded` / `_links` under `data`. Hypermedia is the
  // engine of application state — the state is the links.
  return {
    _state: {
      kind: cls.kind,
      path: dp || '/',
      summary,
      hints: {
        item_kind: cls.item_kind,
        display_as: cls.display_as,
        primary_link: cls.primary_link,
        sort_by: cls.sort_by
      },
      commit_seq: node ? node.commit_seq : 0,
      children_total: total,
      children_page: safePage,
      children_per_page: perPage,
      children_num_pages: numPages,
      order
    },
    _links: links,
    _actions: actions,
    _embedded: embedded,
    data: node ? node.data : null
  };
}

async function buildSystemRootDoc(origin) {
  const roots = await mRoots();
  const fakeChildren = {
    total: roots.length,
    rows: roots.map(r => ({ name: r, data: null, updated_at: 0, commit_seq: 0 }))
  };
  const cls = classifyNode(null, fakeChildren, []);
  const summary = summarizeNode(cls, null, fakeChildren, []);

  const links = { self: `${origin}/` };
  const embedded = {};
  for (const r of roots) {
    links[r] = stateHref(origin, r);
    // System root children are always top-level roots. We saw their names
    // but not their contents — so embedded is a plain `reference` with no
    // summary. Client follows `self` to classify the root.
    embedded[r] = {
      _state: {
        kind: 'reference',
        path: r,
        hints: { item_kind: 'directory', display_as: 'list', primary_link: 'self' }
      },
      _links: {
        self: stateHref(origin, r),
        stream: streamHref(origin, r)
      },
      data: null
    };
  }

  const actions = {
    subscribe: buildAction(
      'GET', streamHref(origin, '', { scope: 'subtree' }),
      [{ name: 'scope', type: 'string', required: false, options: ['exact', 'subtree'] }],
      'Subscribe to changes across all roots.'
    ),
    list_roots: buildAction('GET', `${origin}/`, [], 'List all root namespaces.')
  };

  return {
    _state: {
      kind: cls.kind,
      path: '/',
      summary,
      hints: {
        item_kind: cls.item_kind,
        display_as: cls.display_as,
        primary_link: cls.primary_link,
        sort_by: cls.sort_by
      },
      roots_total: roots.length
    },
    _links: links,
    _actions: actions,
    _embedded: { roots: embedded },
    data: null
  };
}

// ---------------------------------------------------------------------------
// Live: delta stream protocol
//
// The client subscribes at a path and gets:
//
//   1. A cursor frame:
//      { _state: {kind: "cursor", path, commit_seq, scope},
//        _links: {snapshot, resync}, }
//
//   2. Delta frames (as the outbox advances past the cursor):
//      { _state: {kind: "delta", path, scope, from_seq, to_seq},
//        changes: [ {op, path, data, commit_seq, updated_at}, ... ] }
//
// No full-state rebuild per commit. No caching on the client.
// ---------------------------------------------------------------------------

const LIVE_SUBS = new Map();  // clientId -> { res, root, path, scope, cursor, heartbeat }
const WORKER_CURSORS = new Map();
for (const w of localWorkers) WORKER_CURSORS.set(w.id, 0);

function pathMatchesScope(subPath, changePath, scope) {
  // Paths are slash-joined. `subPath` may be "" (whole relay — not used), a
  // single root, or nested. `scope` is either "exact" or "subtree".
  if (scope === 'exact') return subPath === changePath;
  // subtree: changePath must equal subPath or start with subPath + "/".
  if (!subPath) return true;
  if (subPath === changePath) return true;
  return changePath.startsWith(subPath + '/');
}

async function fanoutLoop() {
  // Note on two cursors:
  //
  //   - row.id        — outbox autoincrement PK. Used for `/changes?after=`
  //                     pagination and `/trim-changes`. This is internal
  //                     plumbing; subscribers track it as `sub.outbox_id` so
  //                     we know what we've forwarded and what's safe to trim.
  //
  //   - commit_seq    — per-worker monotonic commit counter from meta. This
  //                     is what we expose to clients in delta events and
  //                     accept on `/api/changes-since?cursor=N` for HTTP
  //                     resync. It's the consumer-facing sequence.
  //
  // Conflating the two means trimming can drop outbox rows a subscriber
  // still needs. Keep them separate.
  if (!LIVE_ENABLED) return;

  for (const w of localWorkers) {
    const after = WORKER_CURSORS.get(w.id) || 0;
    let rr;
    try {
      rr = await wFetch(w.internalUrl, `/changes?after=${after}&limit=${CHANGE_POLL_LIMIT}`);
    } catch (_) { continue; }

    const rows = rr && Array.isArray(rr.rows) ? rr.rows : [];
    if (!rows.length) continue;

    const deliver = new Map();  // clientId -> array of {change, outbox_id}

    for (const row of rows) {
      WORKER_CURSORS.set(w.id, row.id);

      let payload = null;
      if (row.payload) {
        try { payload = JSON.parse(row.payload); } catch (_) {}
      }

      const changePath = row.path;

      for (const [cid, sub] of LIVE_SUBS) {
        if (sub.root !== row.root) continue;
        if (!pathMatchesScope(sub.path, changePath, sub.scope)) continue;
        if (row.id <= sub.outbox_id) continue;

        if (!deliver.has(cid)) deliver.set(cid, []);
        deliver.get(cid).push({
          outbox_id: row.id,
          change: {
            op: row.op_kind,
            path: slash2dp(changePath),
            data: payload ? payload.data : null,
            commit_seq: row.commit_seq,
            updated_at: row.updated_at
          }
        });
      }
    }

    for (const [cid, entries] of deliver) {
      const sub = LIVE_SUBS.get(cid);
      if (!sub) continue;

      for (let i = 0; i < entries.length; i += DELTA_FRAME_MAX_CHANGES) {
        const slice = entries.slice(i, i + DELTA_FRAME_MAX_CHANGES);
        const changes = slice.map(e => e.change);
        const fromSeq = sub.commit_cursor;
        const toSeq = changes[changes.length - 1].commit_seq;
        writeSSE(sub.res, {
          _state: {
            kind: 'delta',
            path: sub.path ? slash2dp(sub.path) : '',
            scope: sub.scope,
            from_seq: fromSeq,
            to_seq: toSeq,
            count: changes.length
          },
          _links: {
            resync: apiHref(sub.origin, sub.path ? slash2dp(sub.path) : '', 'changes-since', { cursor: toSeq })
          },
          changes
        });
        sub.outbox_id = slice[slice.length - 1].outbox_id;
        sub.commit_cursor = toSeq;
      }
    }

    // Trim this worker's outbox up to the minimum outbox_id that any active
    // subscription whose root lives on this worker has delivered through.
    // Subs for other workers' roots don't gate this worker's trim.
    const thisWorkerUrl = w.internalUrl;
    let minOutboxId = Infinity;
    for (const sub of LIVE_SUBS.values()) {
      // Re-derive which worker holds this sub's root. Cheap — parts is one
      // lookup and shardFor is a hash.
      if (shardFor(sub.root).internalUrl !== thisWorkerUrl) continue;
      if (sub.outbox_id < minOutboxId) minOutboxId = sub.outbox_id;
    }
    if (!isFinite(minOutboxId)) {
      // No active subscribers on this shard — safe to trim everything we
      // just forwarded.
      minOutboxId = rows[rows.length - 1].id;
    }
    if (minOutboxId > 0) {
      try { await wFetch(w.internalUrl, '/trim-changes', { method: 'POST', body: JSON.stringify({ through_id: minOutboxId }) }); }
      catch (_) {}
    }
  }
}

if (LIVE_ENABLED) {
  setInterval(() => {
    fanoutLoop().catch(err => log('LIVE', 'fanout:', err && err.message || err));
  }, CHANGE_POLL_INTERVAL_MS);
}

// ---------------------------------------------------------------------------
// Master HTTP surface
// ---------------------------------------------------------------------------

(async function startMaster() {
  try {
    await waitForLocalWorkersReady();

    let activeMasterBatches = 0;

    const originFor = req => {
      const proto = req.headers['x-forwarded-proto'] || 'http';
      const host = req.headers['x-forwarded-host'] || req.headers.host || `127.0.0.1:${PORT}`;
      return `${proto}://${host}`;
    };

    const server = http.createServer(async (req, res) => {
      res.setHeader('Access-Control-Allow-Origin', '*');
      res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS');
      res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
      if (req.method === 'OPTIONS') { res.writeHead(200); res.end(); return; }

      const u = new URL(req.url, originFor(req));
      const pn = u.pathname.replace(/\/+$/, '') || '/';

      try {
        if (req.method === 'GET' && pn === '/health') return J(res, { ok: true, role: 'master' });

        if (req.method === 'GET' && (pn === '/' || pn === '/api')) {
          return J(res, await buildSystemRootDoc(u.origin));
        }

        // Global batch endpoint.
        if (req.method === 'POST' && pn === '/api/batch') {
          if (activeMasterBatches >= MAX_MASTER_ACTIVE_BATCHES) return J(res, { error: 'relay busy' }, 429);
          let b;
          try { b = JSON.parse(await readBody(req, MAX_HTTP_BODY_BYTES)); }
          catch (err) { return J(res, { error: err.message || 'bad json' }, err.statusCode || 400); }
          const ops = b.ops || b;
          if (!Array.isArray(ops)) return J(res, { error: 'ops must be array' }, 400);
          activeMasterBatches += 1;
          try {
            const r = await mBatch(ops);
            return J(res, { ok: true, count: r.count, commit_seq: r.commit_seq });
          } finally { activeMasterBatches -= 1; }
        }

        // Per-root batch: `/geo/api/batch`.
        const rootBatch = pn.match(/^\/([^/]+)\/api\/batch$/);
        if (req.method === 'POST' && rootBatch) {
          if (activeMasterBatches >= MAX_MASTER_ACTIVE_BATCHES) return J(res, { error: 'relay busy' }, 429);
          let b;
          try { b = JSON.parse(await readBody(req, MAX_HTTP_BODY_BYTES)); }
          catch (err) { return J(res, { error: err.message || 'bad json' }, err.statusCode || 400); }
          const ops = b.ops || b;
          if (!Array.isArray(ops)) return J(res, { error: 'ops must be array' }, 400);
          activeMasterBatches += 1;
          try {
            const r = await mBatch(ops);
            return J(res, { ok: true, count: r.count, commit_seq: r.commit_seq });
          } finally { activeMasterBatches -= 1; }
        }

        // Clear a root.
        const rootClear = pn.match(/^\/([^/]+)\/api\/clear$/);
        if (req.method === 'POST' && rootClear) {
          const root = decodeURIComponent(rootClear[1]);
          await Promise.all(workerUrls().map(u => wFetch(u, `/clear?root=${encodeURIComponent(root)}`, { method: 'POST' }).catch(() => null)));
          return J(res, { ok: true });
        }

        // Changes-since endpoint: HTTP resync for clients.
        // Path: `/<dp>/api/changes-since?cursor=N&limit=K`
        const changesSince = pn.match(/^\/(.+)\/api\/changes-since$/);
        if (req.method === 'GET' && changesSince) {
          const dp = normalizeDotPath(decodeURIComponent(changesSince[1]));
          const parts = dp2parts(dp);
          if (!parts.length) return J(res, { error: 'missing path' }, 400);
          const root = parts[0];
          const subPath = parts2slash(parts);
          const cursor = Math.max(0, parseInt(u.searchParams.get('cursor') || '0', 10) || 0);
          const limit = clamp(parseIntPositive(u.searchParams.get('limit'), CHANGE_POLL_LIMIT), 1, 5000);

          const rows = await mChangesSince(root, cursor, limit);
          const filtered = [];
          for (const row of rows) {
            if (!pathMatchesScope(subPath, row.path, 'subtree')) continue;
            let payload = null;
            if (row.payload) { try { payload = JSON.parse(row.payload); } catch (_) {} }
            filtered.push({
              op: row.op_kind,
              path: slash2dp(row.path),
              data: payload ? payload.data : null,
              commit_seq: row.commit_seq,
              updated_at: row.updated_at
            });
          }
          const nextCursor = filtered.length ? filtered[filtered.length - 1].commit_seq : cursor;

          return J(res, {
            _state: { kind: 'change_page', path: dp, cursor, next_cursor: nextCursor, count: filtered.length },
            _links: {
              self: apiHref(u.origin, dp, 'changes-since', { cursor }),
              next: filtered.length ? apiHref(u.origin, dp, 'changes-since', { cursor: nextCursor }) : null,
              stream: streamHref(u.origin, dp)
            },
            changes: filtered
          });
        }

        // Children-page endpoint: `/<dp>/api/children?page=&per_page=&order=`.
        const childrenApi = pn.match(/^\/(.+)\/api\/children$/);
        if (req.method === 'GET' && childrenApi) {
          const dp = normalizeDotPath(decodeURIComponent(childrenApi[1]));
          const page = clamp(parseIntPositive(u.searchParams.get('page'), 1), 1, 1000000);
          const perPage = clamp(parseIntPositive(u.searchParams.get('per_page'), DEFAULT_PAGE_SIZE), 1, MAX_PAGE_SIZE);
          const order = u.searchParams.get('order') || 'key_asc';
          return J(res, await buildNodeDoc(u.origin, dp, { page, perPage, order }));
        }

        // SSE stream endpoint. Handles both `?stream=true` on a path and the
        // legacy `.stream` suffix. Scope param chooses exact vs. subtree.
        const streamRoute = pn.match(/^\/(.+)\.stream$/);
        const wantsStream =
          (req.method === 'GET' && streamRoute) ||
          (req.method === 'GET' && pn !== '/' && parseBool(u.searchParams.get('stream')));

        if (wantsStream) {
          if (!LIVE_ENABLED) return J(res, { error: 'live disabled' }, 503);

          const raw = streamRoute ? decodeURIComponent(streamRoute[1]) : decodeURIComponent(pn.slice(1));
          const dp = normalizeDotPath(raw);
          if (!dp) return J(res, { error: 'missing path' }, 400);

          const parts = dp2parts(dp);
          const root = parts[0];
          const subPath = parts2slash(parts);
          const scope = u.searchParams.get('scope') === 'exact' ? 'exact' : 'subtree';

          res.writeHead(200, {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*'
          });
          res.write(':ok\n\n');

          // Initial cursor frame. The client uses `_links.snapshot` to fetch
          // the starting state at its own pace (and page through it).
          const health = await wFetch(shardFor(dp).internalUrl, '/health').catch(() => ({ commit_seq: 0 }));
          const initialCursor = health && health.commit_seq ? health.commit_seq : 0;

          writeSSE(res, {
            _state: { kind: 'cursor', path: dp, scope, commit_seq: initialCursor },
            _links: {
              self: streamHref(u.origin, dp, { scope }),
              snapshot: stateHref(u.origin, dp),
              resync: apiHref(u.origin, dp, 'changes-since', { cursor: initialCursor }),
              children: apiHref(u.origin, dp, 'children')
            }
          });

          const clientId = randomId();
          // Two cursors per subscription (see comment in fanoutLoop):
          //   outbox_id     — internal outbox row id; used to gate delivery
          //                   and to trim the outbox safely.
          //   commit_cursor — client-facing commit_seq; used as the
          //                   from_seq on delta frames and as the cursor in
          //                   resync URLs.
          const sub = {
            res,
            origin: u.origin,
            root,
            path: subPath,
            scope,
            outbox_id: 0,
            commit_cursor: initialCursor,
            heartbeat: null
          };
          LIVE_SUBS.set(clientId, sub);

          sub.heartbeat = setInterval(() => {
            try { res.write(':hb\n\n'); } catch (_) {}
          }, 15000);

          req.on('close', () => {
            clearInterval(sub.heartbeat);
            LIVE_SUBS.delete(clientId);
            try { res.end(); } catch (_) {}
          });
          return;
        }

        // Default: resource state doc for `/<dp>`.
        const dp = pn.length > 1 ? normalizeDotPath(decodeURIComponent(pn.slice(1))) : '';

        if (req.method === 'GET') {
          const page = clamp(parseIntPositive(u.searchParams.get('page'), 1), 1, 1000000);
          const perPage = clamp(parseIntPositive(u.searchParams.get('per_page'), DEFAULT_PAGE_SIZE), 1, MAX_PAGE_SIZE);
          const order = u.searchParams.get('order') || 'key_asc';
          return J(res, await buildNodeDoc(u.origin, dp, { page, perPage, order }));
        }

        if (req.method === 'PUT' && dp) {
          if (activeMasterBatches >= MAX_MASTER_ACTIVE_BATCHES) return J(res, { error: 'relay busy' }, 429);
          let payload;
          try { payload = JSON.parse(await readBody(req, MAX_HTTP_BODY_BYTES)); }
          catch (err) { return J(res, { error: err.message || 'bad json' }, err.statusCode || 400); }
          activeMasterBatches += 1;
          try {
            const r = await mBatch([{ path: dp, data: payload }]);
            return J(res, { ok: true, count: r.count, commit_seq: r.commit_seq });
          } finally { activeMasterBatches -= 1; }
        }

        if (req.method === 'DELETE' && dp) {
          if (activeMasterBatches >= MAX_MASTER_ACTIVE_BATCHES) return J(res, { error: 'relay busy' }, 429);
          activeMasterBatches += 1;
          try {
            const r = await mBatch([{ path: dp, delete: true }]);
            return J(res, { ok: true, count: r.count, commit_seq: r.commit_seq });
          } finally { activeMasterBatches -= 1; }
        }

        return J(res, { error: 'not found' }, 404);
      } catch (err) {
        log('MASTER', 'request failed:', err && err.message ? err.message : err);
        return J(res, { error: err && err.message ? err.message : 'internal_error' }, err && err.statusCode ? err.statusCode : 500);
      }
    });

    server.listen(PORT, BIND, () => log('MASTER', `http://${BIND}:${PORT}`));

    process.on('SIGINT', () => {
      try { server.close(); } catch (_) {}
      for (const w of localWorkers) { try { w.process.kill('SIGTERM'); } catch (_) {} }
      process.exit(0);
    });
    process.on('unhandledRejection', err => log('MASTER', 'unhandledRejection:', err && err.message ? err.message : err));
  } catch (err) {
    console.error(err);
    process.exit(1);
  }
})();