// relay.js
'use strict';

/**
 * Hypergraph Relay
 *
 * Design intent:
 * - The API is the contract.
 * - HTML is only a human projection of that contract.
 * - A dumb agent can start at "/" and navigate/query/mutate by reading:
 *   _state, _links, _actions, _embedded, data.
 * - The hypergraph does not know or care about domain content.
 * - Search is a shortcut action over the graph, not the application itself.
 */

const http = require('http');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const Database = require('better-sqlite3');

/* -------------------------------------------------------------------------- */
/* Configuration                                                              */
/* -------------------------------------------------------------------------- */

const PORT = parseInt(process.env.PORT || '8765', 10);
const BIND = process.env.HYPER_BIND_HOST || '0.0.0.0';
const RDIR = process.env.HYPER_DATA_DIR || path.join(process.cwd(), '.hyper-data');

const DEFAULT_PAGE_SIZE = parseInt(process.env.HYPER_PAGE_SIZE || '50', 10);
const MAX_PAGE_SIZE = parseInt(process.env.HYPER_MAX_PAGE_SIZE || '250', 10);
const MAX_HTTP_BODY_BYTES = parseInt(process.env.HYPER_MAX_HTTP_BODY_BYTES || '8000000', 10);
const MAX_RESPONSE_BYTES = parseInt(process.env.HYPER_MAX_RESPONSE_BYTES || '16777216', 10);

const BATCH_MAX_OPS = parseInt(process.env.HYPER_BATCH_MAX_OPS || '2000', 10);
const BATCH_MAX_BYTES = parseInt(process.env.HYPER_BATCH_MAX_BYTES || '4000000', 10);

const CHANGE_POLL_LIMIT = parseInt(process.env.HYPER_CHANGE_POLL_LIMIT || '1024', 10);
const CHANGE_POLL_INTERVAL_MS = parseInt(process.env.HYPER_CHANGE_POLL_INTERVAL_MS || '150', 10);
const LIVE_ENABLED = !parseBool(process.env.HYPER_DISABLE_LIVE, false);

const SQLITE_SYNCHRONOUS = String(process.env.HYPER_SQLITE_SYNCHRONOUS || 'normal').trim().toLowerCase();

const GUN_META = new Set(['_', '#', '>']);
const LIVE_SUBS = new Map();

/* -------------------------------------------------------------------------- */
/* Small Utilities                                                            */
/* -------------------------------------------------------------------------- */

function log(tag, ...args) {
  console.log(new Date().toISOString(), `[${tag}]`, ...args);
}

function isObj(value) {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function parseBool(value, fallback = false) {
  if (value == null) return fallback;
  const s = String(value).trim().toLowerCase();
  if (!s) return fallback;
  return s === '1' || s === 'true' || s === 'yes' || s === 'on';
}

function parseIntPositive(value, fallback) {
  const n = parseInt(String(value ?? ''), 10);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

function clamp(n, lo, hi) {
  return Math.max(lo, Math.min(hi, n));
}

function dp2parts(dp) {
  return String(dp || '').split('.').filter(Boolean);
}

function parts2dp(parts) {
  return (parts || []).join('.');
}

function parts2slash(parts) {
  return (parts || []).join('/');
}

function slash2dp(slashPath) {
  return parts2dp(String(slashPath || '').split('/').filter(Boolean));
}

function normalizeDotPath(value) {
  return String(value || '').trim().replace(/^\/+|\/+$/g, '').replace(/\//g, '.');
}

function parentOf(dp) {
  const parts = dp2parts(dp);
  if (parts.length <= 1) return '';
  return parts2dp(parts.slice(0, -1));
}

function qAsArray(value) {
  if (value == null) return [];
  return Array.isArray(value) ? value : [value];
}

function qString(value) {
  if (value === true) return 'true';
  if (value === false) return 'false';
  return String(value ?? '').trim();
}

function qTokenize(value) {
  return String(value || '')
    .toLowerCase()
    .split(/[^a-z0-9]+/g)
    .map(x => x.trim())
    .filter(Boolean);
}

function qNumericValue(value) {
  const s = String(value || '').trim();
  if (!/^-?\d+(\.\d+)?$/.test(s)) return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

function qNumberBounds(n) {
  const eps = Math.max(Math.abs(n) * 1e-9, 1e-9);
  return [n - eps, n + eps];
}

function parseMeasureParts(raw) {
  return String(raw || '')
    .split(/[,:]/g)
    .map(x => x.trim())
    .filter(Boolean);
}

function parseNumberUnit(raw) {
  const s = String(raw || '').trim().toLowerCase();
  const m = s.match(/^(-?\d+(?:\.\d+)?)(km|mi|m)?$/);
  if (!m) return null;

  const value = Number(m[1]);
  if (!Number.isFinite(value)) return null;

  if (m[2] === 'mi') return value * 1.609344;
  if (m[2] === 'm') return value / 1000;
  return value;
}

function parseScope(raw) {
  const s = String(raw || 'direct').trim().toLowerCase();
  if (!s || s === 'none' || s === 'direct') return { hops: 0 };
  if (s === 'refs' || s === 'graph') return { hops: 1 };

  const m = s.match(/^(refs|graph):(\d+)$/);
  if (!m) return { hops: 0 };

  return { hops: clamp(parseInt(m[2], 10) || 0, 0, 5) };
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function randomId() {
  return crypto.randomBytes(8).toString('hex');
}

function originFor(req) {
  const proto = req.headers['x-forwarded-proto'] || 'http';
  const host = req.headers['x-forwarded-host'] || req.headers.host || `127.0.0.1:${PORT}`;
  return `${proto}://${host}`;
}

/* -------------------------------------------------------------------------- */
/* HTTP Helpers                                                               */
/* -------------------------------------------------------------------------- */

function safeStringify(obj) {
  try {
    const body = JSON.stringify(obj);
    if (body && body.length > MAX_RESPONSE_BYTES) {
      return { error: new Error(`response too large (${body.length} > ${MAX_RESPONSE_BYTES})`) };
    }
    return { body };
  } catch (err) {
    return { error: err };
  }
}

function J(res, obj, status = 200) {
  if (res.headersSent) return;

  const { body, error } = safeStringify(obj);

  if (error) {
    res.writeHead(500, {
      'Content-Type': 'application/json;charset=utf-8',
      'Access-Control-Allow-Origin': '*',
    });
    res.end(JSON.stringify({
      error: 'response_too_large',
      message: error.message || 'serialization failed',
    }));
    return;
  }

  res.writeHead(status, {
    'Content-Type': 'application/json;charset=utf-8',
    'Access-Control-Allow-Origin': '*',
  });
  res.end(body);
}

function H(res, html, status = 200) {
  if (res.headersSent) return;

  res.writeHead(status, {
    'Content-Type': 'text/html;charset=utf-8',
    'Access-Control-Allow-Origin': '*',
  });
  res.end(html);
}

function wantsHtml(req, url) {
  if (url.searchParams.get('format') === 'json') return false;
  if (url.searchParams.get('format') === 'html') return true;

  const accept = String(req.headers.accept || '');
  return accept.includes('text/html') && !accept.includes('application/json');
}

function respondDoc(req, res, url, doc, status = 200) {
  if (wantsHtml(req, url)) return H(res, renderDocHtml(doc), status);
  return J(res, doc, status);
}

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

function writeSSE(res, payload) {
  const { body, error } = safeStringify(payload);
  try {
    res.write(`data: ${error ? JSON.stringify({ error: 'frame_too_large' }) : body}\n\n`);
  } catch (_) {}
}

/* -------------------------------------------------------------------------- */
/* Hypermedia Link Helpers                                                    */
/* -------------------------------------------------------------------------- */

function stateHref(origin, dp, query = {}) {
  const url = new URL(`${origin}/${encodeURIComponent(dp || '')}`);
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '' || value === false) continue;
    url.searchParams.set(key, String(value));
  }
  return url.toString();
}

function apiHref(origin, dp, op, query = {}) {
  const url = new URL(`${origin}/${encodeURIComponent(dp || '')}/api/${op}`);
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '' || value === false) continue;
    url.searchParams.set(key, String(value));
  }
  return url.toString();
}

function streamHref(origin, dp, query = {}) {
  const url = new URL(`${origin}/${encodeURIComponent(dp || '')}`);
  url.searchParams.set('stream', 'true');
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '' || value === false) continue;
    url.searchParams.set(key, String(value));
  }
  return url.toString();
}

function buildAction(method, href, fields = [], title = '', hints = {}) {
  return { method, href, fields, title, hints };
}

function hrefForMaybeDotPath(origin, value) {
  const s = String(value || '').trim();
  if (!s) return '';
  if (/^https?:\/\//i.test(s)) return s;
  if (s.includes('.') && !s.includes('/')) return stateHref(origin, s);
  return s;
}

function promoteDataLinks(origin, links, data) {
  if (!isObj(data)) return;

  for (const bucket of [data._links, data.links]) {
    if (!isObj(bucket)) continue;

    for (const [rel, href] of Object.entries(bucket)) {
      if (!rel || !href || links[rel]) continue;
      links[rel] = hrefForMaybeDotPath(origin, href);
    }
  }
}

/* -------------------------------------------------------------------------- */
/* Persistence                                                                */
/* -------------------------------------------------------------------------- */

fs.mkdirSync(RDIR, { recursive: true });
const SQLITE_DIR = path.join(RDIR, 'sqlite');
fs.mkdirSync(SQLITE_DIR, { recursive: true });

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

db.exec(`
  CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER,
    root TEXT NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    data TEXT,
    updated_at INTEGER NOT NULL,
    commit_seq INTEGER NOT NULL
  );

  CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_parent_name ON nodes(parent_id, name);
  CREATE INDEX IF NOT EXISTS idx_nodes_root_path ON nodes(root, path);
  CREATE INDEX IF NOT EXISTS idx_nodes_parent_sort ON nodes(parent_id, name);
  CREATE INDEX IF NOT EXISTS idx_nodes_parent_updated ON nodes(parent_id, updated_at DESC, name DESC);

  CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    int_value INTEGER,
    text_value TEXT
  );

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
  CREATE INDEX IF NOT EXISTS idx_outbox_root_id ON outbox(root, id);

  CREATE TABLE IF NOT EXISTS q_entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    display TEXT,
    updated_at INTEGER NOT NULL,
    commit_seq INTEGER NOT NULL
  );

  CREATE INDEX IF NOT EXISTS idx_q_entities_type ON q_entities(entity_type, updated_at DESC, entity_id);
  CREATE INDEX IF NOT EXISTS idx_q_entities_updated ON q_entities(updated_at DESC, entity_id);
  CREATE INDEX IF NOT EXISTS idx_q_entities_path ON q_entities(canonical_path, entity_id);

  CREATE TABLE IF NOT EXISTS q_facets (
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    PRIMARY KEY(name, value, entity_id)
  );

  CREATE INDEX IF NOT EXISTS idx_q_facets_entity ON q_facets(entity_id, name, value);

  CREATE TABLE IF NOT EXISTS q_numbers (
    name TEXT NOT NULL,
    value REAL NOT NULL,
    entity_id TEXT NOT NULL,
    PRIMARY KEY(name, entity_id)
  );

  CREATE INDEX IF NOT EXISTS idx_q_numbers_lookup ON q_numbers(name, value, entity_id);

  CREATE TABLE IF NOT EXISTS q_times (
    name TEXT NOT NULL,
    value_ms INTEGER NOT NULL,
    entity_id TEXT NOT NULL,
    PRIMARY KEY(name, entity_id)
  );

  CREATE INDEX IF NOT EXISTS idx_q_times_lookup ON q_times(name, value_ms, entity_id);

  CREATE TABLE IF NOT EXISTS q_refs (
    rel TEXT NOT NULL,
    target_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    PRIMARY KEY(rel, target_id, entity_id)
  );

  CREATE INDEX IF NOT EXISTS idx_q_refs_entity ON q_refs(entity_id, rel, target_id);
  CREATE INDEX IF NOT EXISTS idx_q_refs_target ON q_refs(target_id, entity_id, rel);

  CREATE TABLE IF NOT EXISTS q_tokens (
    token TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    PRIMARY KEY(token, entity_id)
  );

  CREATE INDEX IF NOT EXISTS idx_q_tokens_entity ON q_tokens(entity_id, token);

  CREATE TABLE IF NOT EXISTS q_cells (
    scheme TEXT NOT NULL,
    value TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    PRIMARY KEY(scheme, value, entity_id)
  );

  CREATE INDEX IF NOT EXISTS idx_q_cells_entity ON q_cells(entity_id, scheme, value);

  INSERT INTO meta(key, int_value) VALUES ('commit_seq', 0) ON CONFLICT(key) DO NOTHING;
`);

const stInsertNode = db.prepare(`INSERT INTO nodes(parent_id, root, name, path, data, updated_at, commit_seq) VALUES (?, ?, ?, ?, ?, ?, ?)`);
const stUpdateData = db.prepare(`UPDATE nodes SET data = ?, updated_at = ?, commit_seq = ? WHERE id = ?`);
const stDeleteById = db.prepare(`DELETE FROM nodes WHERE id = ?`);
const stCountChildren = db.prepare(`SELECT COUNT(*) AS c FROM nodes WHERE parent_id = ?`);
const stNextCommitSeq = db.prepare(`
  INSERT INTO meta(key, int_value) VALUES ('commit_seq', 1)
  ON CONFLICT(key) DO UPDATE SET int_value = int_value + 1
  RETURNING int_value
`);
const stInsertOutbox = db.prepare(`INSERT INTO outbox(root, path, op_kind, commit_seq, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?)`);
const stOutboxTrim = db.prepare(`DELETE FROM outbox WHERE id <= ?`);
const stFindByParentNameW = db.prepare(`SELECT id, data FROM nodes WHERE parent_id IS ? AND name = ?`);
const stNodeMeta = db.prepare(`SELECT id, parent_id, name, path, data, updated_at, commit_seq FROM nodes WHERE id = ?`);

const stFindByPath = dbRead.prepare(`SELECT id, parent_id, data, updated_at, commit_seq FROM nodes WHERE root = ? AND path = ?`);
const stRoots = dbRead.prepare(`SELECT name FROM nodes WHERE parent_id IS NULL ORDER BY name ASC`);
const stCountChildrenRead = dbRead.prepare(`SELECT COUNT(*) AS c FROM nodes WHERE parent_id = ?`);
const stMetaGetInt = dbRead.prepare(`SELECT int_value FROM meta WHERE key = ?`);
const stOutboxAfter = dbRead.prepare(`SELECT id, root, path, op_kind, commit_seq, updated_at, payload FROM outbox WHERE id > ? ORDER BY id ASC LIMIT ?`);
const stOutboxAfterForRoot = dbRead.prepare(`SELECT id, root, path, op_kind, commit_seq, updated_at, payload FROM outbox WHERE id > ? AND root = ? ORDER BY id ASC LIMIT ?`);

const stChildren = {
  key_asc: dbRead.prepare(`SELECT name, data, updated_at, commit_seq FROM nodes WHERE parent_id = ? ORDER BY name ASC LIMIT ? OFFSET ?`),
  key_desc: dbRead.prepare(`SELECT name, data, updated_at, commit_seq FROM nodes WHERE parent_id = ? ORDER BY name DESC LIMIT ? OFFSET ?`),
  updated_desc: dbRead.prepare(`SELECT name, data, updated_at, commit_seq FROM nodes WHERE parent_id = ? ORDER BY updated_at DESC, name DESC LIMIT ? OFFSET ?`),
  updated_asc: dbRead.prepare(`SELECT name, data, updated_at, commit_seq FROM nodes WHERE parent_id = ? ORDER BY updated_at ASC, name ASC LIMIT ? OFFSET ?`),
};

const stRootPage = dbRead.prepare(`SELECT name, data, updated_at, commit_seq FROM nodes WHERE parent_id IS NULL ORDER BY name ASC LIMIT ? OFFSET ?`);
const stRootCount = dbRead.prepare(`SELECT COUNT(*) AS c FROM nodes WHERE parent_id IS NULL`);

/* -------------------------------------------------------------------------- */
/* Query Index Statements                                                     */
/* -------------------------------------------------------------------------- */

const stQUpsertEntity = db.prepare(`
  INSERT INTO q_entities(entity_id, entity_type, canonical_path, display, updated_at, commit_seq)
  VALUES (?, ?, ?, ?, ?, ?)
  ON CONFLICT(entity_id) DO UPDATE SET
    entity_type = excluded.entity_type,
    canonical_path = excluded.canonical_path,
    display = excluded.display,
    updated_at = excluded.updated_at,
    commit_seq = excluded.commit_seq
`);

const stQDeleteEntity = db.prepare(`DELETE FROM q_entities WHERE entity_id = ?`);
const stQDeleteFacets = db.prepare(`DELETE FROM q_facets WHERE entity_id = ?`);
const stQDeleteNumbers = db.prepare(`DELETE FROM q_numbers WHERE entity_id = ?`);
const stQDeleteTimes = db.prepare(`DELETE FROM q_times WHERE entity_id = ?`);
const stQDeleteRefs = db.prepare(`DELETE FROM q_refs WHERE entity_id = ?`);
const stQDeleteTokens = db.prepare(`DELETE FROM q_tokens WHERE entity_id = ?`);
const stQDeleteCells = db.prepare(`DELETE FROM q_cells WHERE entity_id = ?`);
const stQEntityIdsForPathW = db.prepare(`SELECT entity_id FROM q_entities WHERE entity_id = ? OR canonical_path = ?`);

const stQInsertFacet = db.prepare(`INSERT OR IGNORE INTO q_facets(name, value, entity_id) VALUES (?, ?, ?)`);
const stQInsertNumber = db.prepare(`INSERT OR REPLACE INTO q_numbers(name, value, entity_id) VALUES (?, ?, ?)`);
const stQInsertTime = db.prepare(`INSERT OR REPLACE INTO q_times(name, value_ms, entity_id) VALUES (?, ?, ?)`);
const stQInsertRef = db.prepare(`INSERT OR IGNORE INTO q_refs(rel, target_id, entity_id) VALUES (?, ?, ?)`);
const stQInsertToken = db.prepare(`INSERT OR IGNORE INTO q_tokens(token, entity_id) VALUES (?, ?)`);
const stQInsertCell = db.prepare(`INSERT OR IGNORE INTO q_cells(scheme, value, entity_id) VALUES (?, ?, ?)`);

const stQEntityById = dbRead.prepare(`SELECT entity_id, entity_type, canonical_path, display, updated_at, commit_seq FROM q_entities WHERE entity_id = ?`);
const stQEntityExactOrPath = dbRead.prepare(`SELECT entity_id, entity_type, canonical_path, display, updated_at, commit_seq FROM q_entities WHERE entity_id = ? OR canonical_path = ? ORDER BY updated_at DESC LIMIT 1`);
const stQBase = dbRead.prepare(`SELECT entity_id FROM q_entities WHERE (? IS NULL OR entity_type = ?) ORDER BY updated_at DESC, entity_id ASC LIMIT ?`);
const stQAll = dbRead.prepare(`SELECT entity_id FROM q_entities WHERE (? IS NULL OR entity_type = ?) ORDER BY updated_at DESC, entity_id ASC`);
const stQFacet = dbRead.prepare(`SELECT entity_id FROM q_facets WHERE name = ? AND value = ?`);
const stQToken = dbRead.prepare(`SELECT entity_id FROM q_tokens WHERE token = ?`);
const stQTokenPrefix = dbRead.prepare(`SELECT entity_id FROM q_tokens WHERE token LIKE ?`);
const stQHasRef = dbRead.prepare(`SELECT entity_id FROM q_refs WHERE rel = ?`);
const stQRef = dbRead.prepare(`SELECT entity_id FROM q_refs WHERE rel = ? AND target_id = ?`);
const stQRefsToTarget = dbRead.prepare(`SELECT entity_id, rel, target_id FROM q_refs WHERE target_id = ?`);
const stQCell = dbRead.prepare(`SELECT entity_id FROM q_cells WHERE scheme = ? AND value = ?`);
const stQTimeGte = dbRead.prepare(`SELECT entity_id FROM q_times WHERE name = ? AND value_ms >= ?`);
const stQTimeLte = dbRead.prepare(`SELECT entity_id FROM q_times WHERE name = ? AND value_ms <= ?`);
const stQNumberGte = dbRead.prepare(`SELECT entity_id FROM q_numbers WHERE name = ? AND value >= ?`);
const stQNumberLte = dbRead.prepare(`SELECT entity_id FROM q_numbers WHERE name = ? AND value <= ?`);
const stQNumberBetweenAny = dbRead.prepare(`SELECT entity_id FROM q_numbers WHERE value >= ? AND value <= ?`);
const stQNumberBetweenNamed = dbRead.prepare(`SELECT entity_id FROM q_numbers WHERE name = ? AND value >= ? AND value <= ?`);
const stQEntityLoose = dbRead.prepare(`SELECT entity_id FROM q_entities WHERE entity_id LIKE ? OR entity_type LIKE ? OR canonical_path LIKE ? OR display LIKE ?`);
const stQFacetLoose = dbRead.prepare(`SELECT entity_id FROM q_facets WHERE name LIKE ? OR value LIKE ?`);
const stQRefLoose = dbRead.prepare(`SELECT entity_id FROM q_refs WHERE rel LIKE ? OR target_id LIKE ?`);
const stQCellLoose = dbRead.prepare(`SELECT entity_id FROM q_cells WHERE scheme LIKE ? OR value LIKE ?`);
const stQFacetsForEntity = dbRead.prepare(`SELECT name, value FROM q_facets WHERE entity_id = ? ORDER BY name ASC, value ASC`);
const stQRefsForEntity = dbRead.prepare(`SELECT rel, target_id FROM q_refs WHERE entity_id = ? ORDER BY rel ASC, target_id ASC`);
const stQNumbersForEntity = dbRead.prepare(`SELECT name, value FROM q_numbers WHERE entity_id = ? ORDER BY name ASC`);
const stQTimesForEntity = dbRead.prepare(`SELECT name, value_ms FROM q_times WHERE entity_id = ? ORDER BY name ASC`);
const stQCellsForEntity = dbRead.prepare(`SELECT scheme, value FROM q_cells WHERE entity_id = ? ORDER BY scheme ASC, value ASC`);

/* -------------------------------------------------------------------------- */
/* Index Projection                                                           */
/* -------------------------------------------------------------------------- */

function qClearEntity(entityId) {
  stQDeleteFacets.run(entityId);
  stQDeleteNumbers.run(entityId);
  stQDeleteTimes.run(entityId);
  stQDeleteRefs.run(entityId);
  stQDeleteTokens.run(entityId);
  stQDeleteCells.run(entityId);
  stQDeleteEntity.run(entityId);
}

function qLooksLikeDotPath(value) {
  const s = String(value || '').trim();
  if (!s || s.length > 512) return false;
  if (/^https?:\/\//i.test(s)) return false;
  if (s.includes('/') && !s.includes('.')) return false;
  return /^[A-Za-z0-9_:@-]+(\.[A-Za-z0-9_:@-]+)+$/.test(s);
}

function qNormalizeRefTarget(value) {
  const s = String(value || '').trim();
  if (!s) return '';
  if (/^https?:\/\//i.test(s)) return s;
  return normalizeDotPath(s);
}

function qLooksTimeName(name) {
  const n = String(name || '').toLowerCase();
  return n === 'time' ||
    n === 'date' ||
    n.endsWith('_at') ||
    n.endsWith('_time') ||
    n.endsWith('_date') ||
    n.includes('timestamp');
}

function qDateMs(value) {
  if (value == null || value === '') return null;

  if (typeof value === 'number' && Number.isFinite(value)) {
    if (Math.abs(value) > 100000000000) return Math.trunc(value);
    if (Math.abs(value) > 1000000000) return Math.trunc(value * 1000);
    return null;
  }

  const s = String(value).trim();
  if (!s) return null;
  if (/^-?\d+(?:\.\d+)?$/.test(s)) return qDateMs(Number(s));

  const parsed = Date.parse(s);
  return Number.isFinite(parsed) ? parsed : null;
}

function qAddEncoded(set, a, b) {
  const left = qString(a);
  const right = qString(b);
  if (left && right) set.add(`${left}\u0000${right}`);
}

function qFlattenGeneric(value, prefix, out, depth = 0, seen = new Set()) {
  if (value == null || depth > 4) return;

  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    out.push([prefix || 'value', value]);
    return;
  }

  if (Array.isArray(value)) {
    for (const item of value.slice(0, 100)) qFlattenGeneric(item, prefix, out, depth + 1, seen);
    return;
  }

  if (!isObj(value) || seen.has(value)) return;
  seen.add(value);

  for (const [key, child] of Object.entries(value)) {
    if (GUN_META.has(key) || key === 'html' || key === 'css' || key === 'js') continue;
    const next = prefix ? `${prefix}.${key}` : key;
    qFlattenGeneric(child, next, out, depth + 1, seen);
  }
}

function qProject(parts, cleanPayload, updatedAt, commitSeq) {
  if (cleanPayload === undefined) return;

  const pathId = parts2dp(parts);

  const explicit = isObj(cleanPayload) && isObj(cleanPayload.query)
    ? cleanPayload.query
    : isObj(cleanPayload) && isObj(cleanPayload.search)
      ? cleanPayload.search
      : null;

  const data = isObj(cleanPayload) && isObj(cleanPayload.data) ? cleanPayload.data : cleanPayload;
  const dataObj = isObj(data) ? data : {};

  const canonicalPath = String((explicit && explicit.canonical_path) || pathId);
  const entityId = String((explicit && explicit.entity_id) || canonicalPath);
  const entityType = String(
    (explicit && explicit.entity_type) ||
    dataObj.kind ||
    dataObj.type ||
    dataObj.model ||
    (isObj(cleanPayload) && (cleanPayload.kind || cleanPayload.type || cleanPayload.model)) ||
    'node'
  );

  const display =
    (explicit && explicit.display) ||
    dataObj.name ||
    dataObj.title ||
    dataObj.label ||
    dataObj.display ||
    dataObj.display_name ||
    dataObj.id ||
    (isObj(cleanPayload) && (cleanPayload.name || cleanPayload.title || cleanPayload.label || cleanPayload.display)) ||
    parts[parts.length - 1] ||
    entityId;

  for (const row of stQEntityIdsForPathW.all(pathId, pathId)) qClearEntity(row.entity_id);
  qClearEntity(entityId);
  stQUpsertEntity.run(entityId, entityType, canonicalPath, display == null ? null : String(display), updatedAt, commitSeq);

  const facets = new Set();
  const numbers = new Map();
  const times = new Map();
  const refs = new Set();
  const tokens = new Set();
  const cells = new Set();

  const addToken = value => {
    for (const token of qTokenize(value)) tokens.add(token);
  };

  const addFacet = (name, value) => qAddEncoded(facets, name, value);

  const addNumber = (name, value) => {
    const n = Number(value);
    const k = qString(name);
    if (k && Number.isFinite(n)) numbers.set(k, n);
  };

  const addTime = (name, value) => {
    const ms = qDateMs(value);
    const k = qString(name);
    if (k && Number.isFinite(ms)) times.set(k, Math.trunc(ms));
  };

  const addRef = (rel, target) => {
    const clean = qNormalizeRefTarget(target);
    if (clean) qAddEncoded(refs, rel, clean);
  };

  const addCell = (scheme, value) => qAddEncoded(cells, scheme, value);

  addToken(entityId);
  addToken(canonicalPath);
  addToken(entityType);
  addToken(display);
  addCell('path', canonicalPath);
  addCell('entity_id', entityId);
  addFacet('entity_type', entityType);
  if (parts.length) addFacet('root', parts[0]);

  if (explicit) {
    if (isObj(explicit.facets)) {
      for (const [name, rawValues] of Object.entries(explicit.facets)) {
        for (const rawValue of qAsArray(rawValues)) addFacet(name, rawValue);
      }
    }

    if (isObj(explicit.numbers)) {
      for (const [name, rawValue] of Object.entries(explicit.numbers)) addNumber(name, rawValue);
    }

    if (isObj(explicit.times)) {
      for (const [name, rawValue] of Object.entries(explicit.times)) addTime(name, rawValue);
    }

    if (isObj(explicit.refs)) {
      for (const [rel, rawTargets] of Object.entries(explicit.refs)) {
        for (const rawTarget of qAsArray(rawTargets)) addRef(rel, rawTarget);
      }
    }

    for (const rawToken of qAsArray(explicit.tokens)) addToken(rawToken);
    if (explicit.text != null) addToken(explicit.text);

    if (isObj(explicit.cells)) {
      for (const [scheme, rawValues] of Object.entries(explicit.cells)) {
        for (const rawValue of qAsArray(rawValues)) addCell(scheme, rawValue);
      }
    }
  }

  const flat = [];
  qFlattenGeneric(data, '', flat);

  if (isObj(cleanPayload)) {
    qFlattenGeneric(cleanPayload.body, 'body', flat);
    qFlattenGeneric(cleanPayload.dates, 'dates', flat);
    qFlattenGeneric(cleanPayload.links, 'links', flat);
    qFlattenGeneric(cleanPayload._links, '_links', flat);
  }

  for (const [name, value] of flat) {
    addToken(name);
    addToken(value);
    addCell(name, value);

    if (typeof value === 'number' && Number.isFinite(value)) {
      if (qLooksTimeName(name)) addTime(name, value);
      else addNumber(name, value);
      continue;
    }

    if (typeof value === 'boolean') {
      addFacet(name, value);
      continue;
    }

    const s = qString(value);
    if (!s) continue;

    if (s.length <= 256) addFacet(name, s);
    if (qLooksTimeName(name)) addTime(name, s);
    if (qLooksLikeDotPath(s)) addRef(name, s);
  }

  const linkBuckets = [];

  if (isObj(cleanPayload)) {
    if (isObj(cleanPayload.links)) linkBuckets.push(cleanPayload.links);
    if (isObj(cleanPayload._links)) linkBuckets.push(cleanPayload._links);
  }

  if (isObj(data)) {
    if (isObj(data.links)) linkBuckets.push(data.links);
    if (isObj(data._links)) linkBuckets.push(data._links);
  }

  for (const links of linkBuckets) {
    for (const [rel, raw] of Object.entries(links)) {
      for (const target of qAsArray(raw)) {
        addRef(rel, target);
        addCell(`link.${rel}`, target);
        addToken(rel);
        addToken(target);
      }
    }
  }

  for (const key of ['target', 'record', 'record_dot', 'source', 'parent']) {
    if (isObj(data) && data[key]) addRef(key, data[key]);
    if (isObj(cleanPayload) && cleanPayload[key]) addRef(key, cleanPayload[key]);
  }

  for (const encoded of facets) {
    const [name, value] = encoded.split('\u0000');
    if (name && value) stQInsertFacet.run(name, value, entityId);
  }

  for (const [name, value] of numbers.entries()) stQInsertNumber.run(name, value, entityId);
  for (const [name, value] of times.entries()) stQInsertTime.run(name, value, entityId);

  for (const encoded of refs) {
    const [rel, target] = encoded.split('\u0000');
    if (rel && target) stQInsertRef.run(rel, target, entityId);
  }

  for (const token of tokens) stQInsertToken.run(token, entityId);

  for (const encoded of cells) {
    const [scheme, value] = encoded.split('\u0000');
    if (scheme && value) stQInsertCell.run(scheme, value, entityId);
  }

  if (isObj(cleanPayload)) delete cleanPayload.query;
}

function qDeleteByParts(parts) {
  const pathId = parts2dp(parts);
  const ids = new Set([pathId]);

  for (const row of stQEntityIdsForPathW.all(pathId, pathId)) ids.add(row.entity_id);
  for (const id of ids) qClearEntity(id);
}

/* -------------------------------------------------------------------------- */
/* Node Persistence Helpers                                                   */
/* -------------------------------------------------------------------------- */

function findChildIdW(parentId, name) {
  const row = stFindByParentNameW.get(parentId, name);
  return row ? row.id : null;
}

function ensurePath(parts, data, updatedAt, commitSeq) {
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
    } else {
      if (isTerminal) {
        stUpdateData.run(
          data !== undefined ? JSON.stringify(data) : null,
          updatedAt,
          commitSeq,
          existingId
        );
      }
      currentId = existingId;
    }

    parentId = currentId;
  }

  return currentId;
}

function deleteNodeByPath(parts) {
  if (!parts.length) return false;

  let parentId = null;
  let nodeId = null;

  for (const name of parts) {
    const row = stFindByParentNameW.get(parentId, name);
    if (!row) return false;
    nodeId = row.id;
    parentId = row.id;
  }

  stDeleteById.run(nodeId);

  for (let i = parts.length - 2; i >= 0; i -= 1) {
    let pId = null;
    let aId = null;

    for (let j = 0; j <= i; j += 1) {
      const row = stFindByParentNameW.get(pId, parts[j]);
      if (!row) {
        aId = null;
        break;
      }
      aId = row.id;
      pId = row.id;
    }

    if (aId == null) break;

    const meta = stNodeMeta.get(aId);
    if (!meta) break;

    const childCount = stCountChildren.get(aId).c;
    const hasData = meta.data != null;

    if (childCount === 0 && !hasData) stDeleteById.run(aId);
    else break;
  }

  return true;
}

function dbGetByParts(parts) {
  if (!parts.length) return null;

  const row = stFindByPath.get(parts[0], parts2slash(parts));
  if (!row) return null;

  return {
    id: row.id,
    parent_id: row.parent_id,
    data: row.data ? JSON.parse(row.data) : null,
    updated_at: row.updated_at,
    commit_seq: row.commit_seq,
  };
}

function dbResolvePathId(parts) {
  if (!parts.length) return null;
  const row = stFindByPath.get(parts[0], parts2slash(parts));
  return row ? row.id : null;
}

function dbChildrenPage(parentId, limit, offset, order) {
  const stmt = stChildren[order] || stChildren.key_asc;
  const rows = [];

  for (const row of stmt.iterate(parentId, limit, offset)) {
    rows.push({
      name: row.name,
      data: row.data ? JSON.parse(row.data) : null,
      updated_at: row.updated_at,
      commit_seq: row.commit_seq,
    });
  }

  return rows;
}

function buildChildrenPage(dp, { page = 1, perPage = DEFAULT_PAGE_SIZE, order = 'key_asc' } = {}) {
  const parts = dp2parts(dp);
  const limit = clamp(parseIntPositive(perPage, DEFAULT_PAGE_SIZE), 1, MAX_PAGE_SIZE);
  const offset = Math.max(0, (Math.max(1, page) - 1) * limit);

  if (!parts.length) {
    const rows = [];

    for (const row of stRootPage.iterate(limit, offset)) {
      rows.push({
        name: row.name,
        data: row.data ? JSON.parse(row.data) : null,
        updated_at: row.updated_at,
        commit_seq: row.commit_seq,
      });
    }

    return { total: stRootCount.get().c, rows };
  }

  const parentId = dbResolvePathId(parts);
  if (parentId == null) return { total: 0, rows: [] };

  return {
    total: stCountChildrenRead.get(parentId).c,
    rows: dbChildrenPage(parentId, limit, offset, order),
  };
}

/* -------------------------------------------------------------------------- */
/* Query Engine                                                               */
/* -------------------------------------------------------------------------- */

function qRowsToIds(rows) {
  return rows.map(row => row.entity_id);
}

function qIntersect(a, b) {
  const right = new Set(b);
  return a.filter(x => right.has(x));
}

function qParsePairs(values) {
  const out = [];

  for (const raw of qAsArray(values)) {
    const s = String(raw);
    const idx = s.indexOf(':');
    if (idx <= 0) continue;
    out.push([s.slice(0, idx), s.slice(idx + 1)]);
  }

  return out;
}

function qParseTriples(values) {
  const out = [];

  for (const raw of qAsArray(values)) {
    const parts = parseMeasureParts(raw);
    if (parts.length >= 3) out.push(parts);
  }

  return out;
}

function qIncludeSet(params) {
  const out = new Set();

  for (const raw of qAsArray(params.include)) {
    for (const part of String(raw).split(',')) {
      const clean = part.trim().toLowerCase();
      if (clean) out.add(clean);
    }
  }

  return out;
}

function qHydrateItem(row, include, matchedBy, scores) {
  const item = {
    entity_id: row.entity_id,
    entity_type: row.entity_type,
    canonical_path: row.canonical_path,
    display: row.display,
    updated_at: row.updated_at,
    commit_seq: row.commit_seq,
  };

  if (scores && scores.has(row.entity_id)) item.score = scores.get(row.entity_id);
  if (include.has('facets')) item.facets = stQFacetsForEntity.all(row.entity_id);
  if (include.has('refs')) item.refs = stQRefsForEntity.all(row.entity_id);
  if (include.has('numbers')) item.numbers = stQNumbersForEntity.all(row.entity_id);
  if (include.has('times')) item.times = stQTimesForEntity.all(row.entity_id);
  if (include.has('cells')) item.cells = stQCellsForEntity.all(row.entity_id);
  if (matchedBy && matchedBy.has(row.entity_id)) item.matched_by = matchedBy.get(row.entity_id);

  return item;
}

function qNumberMap(entityId) {
  const out = {};
  for (const row of stQNumbersForEntity.all(entityId)) out[row.name] = Number(row.value);
  return out;
}

function haversineKm(lat1, lon1, lat2, lon2) {
  const r = 6371.0088;
  const toRad = x => x * Math.PI / 180;
  const p1 = toRad(lat1);
  const p2 = toRad(lat2);
  const dp = toRad(lat2 - lat1);
  const dl = toRad(lon2 - lon1);
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * r * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function qRadiusDistance(mode, xName, yName, x, y, cx, cy) {
  const lowerX = String(xName).toLowerCase();
  const lowerY = String(yName).toLowerCase();
  const useGeo = mode === 'geo' || (mode === 'auto' && lowerX === 'lat' && (lowerY === 'lon' || lowerY === 'lng' || lowerY === 'longitude'));
  return useGeo ? haversineKm(cx, cy, x, y) : Math.sqrt((x - cx) ** 2 + (y - cy) ** 2);
}

function qApplyBbox(ids, bboxValues, matchedBy) {
  let out = ids;

  for (const raw of qAsArray(bboxValues)) {
    const parts = parseMeasureParts(raw);
    if (parts.length < 6) continue;

    const [xName, yName, minXRaw, minYRaw, maxXRaw, maxYRaw] = parts;
    const minX = Number(minXRaw);
    const minY = Number(minYRaw);
    const maxX = Number(maxXRaw);
    const maxY = Number(maxYRaw);

    if (![minX, minY, maxX, maxY].every(Number.isFinite)) continue;

    const xIds = qIntersect(
      qRowsToIds(stQNumberGte.all(xName, Math.min(minX, maxX))),
      qRowsToIds(stQNumberLte.all(xName, Math.max(minX, maxX)))
    );

    const yIds = qIntersect(
      qRowsToIds(stQNumberGte.all(yName, Math.min(minY, maxY))),
      qRowsToIds(stQNumberLte.all(yName, Math.max(minY, maxY)))
    );

    const direct = qIntersect(xIds, yIds);

    for (const id of direct) {
      if (!matchedBy.has(id)) {
        matchedBy.set(id, {
          kind: 'measure_direct',
          measure: 'bbox',
          path: [id],
          rels: [],
        });
      }
    }

    out = qIntersect(out, direct);
  }

  return out;
}

function qApplyRadius(ids, radiusValues, radiusMode, matchedBy) {
  let out = ids;
  const mode = String(radiusMode || 'auto').toLowerCase();

  for (const raw of qAsArray(radiusValues)) {
    const parts = parseMeasureParts(raw);
    if (parts.length < 5) continue;

    const [xName, yName, cxRaw, cyRaw, rRaw] = parts;
    const cx = Number(cxRaw);
    const cy = Number(cyRaw);
    const radius = parseNumberUnit(rRaw);

    if (![cx, cy, radius].every(Number.isFinite)) continue;

    const keep = [];

    for (const id of out) {
      const nums = qNumberMap(id);
      const x = Number(nums[xName]);
      const y = Number(nums[yName]);

      if (!Number.isFinite(x) || !Number.isFinite(y)) continue;

      const dist = qRadiusDistance(mode, xName, yName, x, y, cx, cy);

      if (dist <= radius) {
        keep.push(id);

        if (!matchedBy.has(id)) {
          matchedBy.set(id, {
            kind: 'measure_direct',
            measure: 'radius',
            path: [id],
            rels: [],
            distance: dist,
          });
        }
      }
    }

    out = keep;
  }

  return out;
}

function qExpandMeasuredMatches(directIds, scope, matchedBy) {
  const parsed = parseScope(scope);
  if (!parsed.hops) return directIds;

  const result = new Set(directIds);
  let frontier = Array.from(directIds);

  for (let hop = 1; hop <= parsed.hops; hop += 1) {
    const next = [];

    for (const target of frontier) {
      const row = stQEntityById.get(target);
      const keys = Array.from(new Set([target, row && row.canonical_path].filter(Boolean)));

      for (const key of keys) {
        for (const edge of stQRefsToTarget.all(key)) {
          if (result.has(edge.entity_id)) continue;

          result.add(edge.entity_id);
          next.push(edge.entity_id);

          const prior = matchedBy.get(target) || {
            kind: 'measure_direct',
            measure: 'related',
            path: [target],
            rels: [],
          };

          matchedBy.set(edge.entity_id, {
            kind: 'measure_ref',
            measure: prior.measure,
            path: [edge.entity_id, ...prior.path],
            rels: [edge.rel, ...(prior.rels || [])],
            hops: hop,
            distance: prior.distance,
          });
        }
      }
    }

    frontier = next;
    if (!frontier.length) break;
  }

  return Array.from(result);
}

function qPathFragmentsForPath(pathValue) {
  const parts = dp2parts(pathValue);
  const out = new Set();

  if (parts.length) out.add(parts[0]);
  if (parts.length >= 2) out.add(parts.slice(0, 2).join('.'));
  if (parts.length >= 3) out.add(parts.slice(0, 3).join('.'));
  if (parts.length >= 4) out.add(parts.slice(0, 4).join('.'));

  return Array.from(out);
}

function qBuildFilterSummary(ids, maxIds = 300) {
  const selected = ids.slice(0, maxIds);
  const types = new Map();
  const facets = new Map();
  const refs = new Map();
  const measures = new Map();
  const times = new Map();
  const paths = new Map();

  for (const id of selected) {
    const row = stQEntityById.get(id);

    if (row && row.entity_type) types.set(row.entity_type, (types.get(row.entity_type) || 0) + 1);

    if (row && row.canonical_path) {
      for (const fragment of qPathFragmentsForPath(row.canonical_path)) {
        paths.set(fragment, (paths.get(fragment) || 0) + 1);
      }
    }

    for (const f of stQFacetsForEntity.all(id)) {
      if (!facets.has(f.name)) facets.set(f.name, new Map());
      const values = facets.get(f.name);
      values.set(f.value, (values.get(f.value) || 0) + 1);
    }

    for (const r of stQRefsForEntity.all(id)) refs.set(r.rel, (refs.get(r.rel) || 0) + 1);
    for (const n of stQNumbersForEntity.all(id)) measures.set(n.name, (measures.get(n.name) || 0) + 1);
    for (const t of stQTimesForEntity.all(id)) times.set(t.name, (times.get(t.name) || 0) + 1);
  }

  const sortCounts = (entries, limit = 20) => {
    return Array.from(entries)
      .map(([value, count]) => ({ value, count }))
      .sort((a, b) => b.count - a.count || String(a.value).localeCompare(String(b.value)))
      .slice(0, limit);
  };

  const facetObj = {};
  for (const [name, values] of Array.from(facets.entries()).slice(0, 10)) {
    facetObj[name] = sortCounts(values.entries(), 10);
  }

  return {
    sampled: selected.length,
    types: sortCounts(types.entries(), 15),
    paths: sortCounts(paths.entries(), 30),
    facets: facetObj,
    refs: Array.from(refs.entries())
      .map(([rel, count]) => ({ rel, count }))
      .sort((a, b) => b.count - a.count || String(a.rel).localeCompare(String(b.rel)))
      .slice(0, 20),
    measures: Array.from(measures.entries())
      .map(([name, count]) => ({ name, count }))
      .sort((a, b) => b.count - a.count || String(a.name).localeCompare(String(b.name)))
      .slice(0, 20),
    times: Array.from(times.entries())
      .map(([name, count]) => ({ name, count }))
      .sort((a, b) => b.count - a.count || String(a.name).localeCompare(String(b.name)))
      .slice(0, 20),
  };
}

function qAddScore(scores, id, points) {
  scores.set(id, (scores.get(id) || 0) + points);
}

function qSetMatch(matchedBy, id, reason) {
  if (!matchedBy.has(id)) {
    matchedBy.set(id, {
      kind: 'search',
      terms: [],
      path: [id],
      rels: [],
    });
  }

  const m = matchedBy.get(id);
  if (reason && !m.terms.includes(reason)) m.terms.push(reason);
}

function qSearchTermMatches(rawTerm, scores, matchedBy) {
  const term = String(rawTerm || '').trim();
  if (!term) return [];

  const lower = term.toLowerCase();
  const like = `%${term}%`;
  const lowerLike = `%${lower}%`;
  const prefix = `${lower}%`;
  const numeric = qNumericValue(term);
  const hits = new Set();

  const add = (rows, points, reason) => {
    for (const id of qRowsToIds(rows)) {
      hits.add(id);
      qAddScore(scores, id, points);
      qSetMatch(matchedBy, id, reason);
    }
  };

  add(stQToken.all(lower), 40, `token:${lower}`);

  if (lower.length >= 3) add(stQTokenPrefix.all(prefix), 18, `token_prefix:${lower}`);

  add(stQEntityLoose.all(like, like, like, like), 30, `entity:${term}`);
  add(stQFacetLoose.all(like, like), 22, `facet:${term}`);
  add(stQRefLoose.all(like, like), 20, `ref:${term}`);
  add(stQCellLoose.all(like, like), 16, `cell:${term}`);

  if (lower !== term) {
    add(stQEntityLoose.all(lowerLike, lowerLike, lowerLike, lowerLike), 24, `entity:${lower}`);
    add(stQFacetLoose.all(lowerLike, lowerLike), 18, `facet:${lower}`);
    add(stQRefLoose.all(lowerLike, lowerLike), 16, `ref:${lower}`);
    add(stQCellLoose.all(lowerLike, lowerLike), 12, `cell:${lower}`);
  }

  if (numeric !== null) {
    const [lo, hi] = qNumberBounds(numeric);
    add(stQNumberBetweenAny.all(lo, hi), 28, `number:${term}`);
  }

  return Array.from(hits);
}

function qSplitSearchText(raw) {
  const s = String(raw || '').trim();
  if (!s) return [];

  const parts = [];
  const re = /"([^"]+)"|'([^']+)'|(\S+)/g;
  let m;

  while ((m = re.exec(s))) {
    const token = (m[1] || m[2] || m[3] || '').trim();
    if (token) parts.push(token);
  }

  return parts;
}

function qApplyInlineSearchToken(ids, token) {
  const s = String(token || '').trim();
  if (!s) return ids;

  const facet = s.match(/^facet:([^:]+):(.+)$/i) || s.match(/^f:([^:]+):(.+)$/i);
  if (facet) return qIntersect(ids, qRowsToIds(stQFacet.all(facet[1], facet[2])));

  const ref = s.match(/^ref:([^:]+):(.+)$/i);
  if (ref) return qIntersect(ids, qRowsToIds(stQRef.all(ref[1], qNormalizeRefTarget(ref[2]))));

  const hasRef = s.match(/^has_ref:(.+)$/i) || s.match(/^rel:(.+)$/i);
  if (hasRef) return qIntersect(ids, qRowsToIds(stQHasRef.all(hasRef[1])));

  const cell = s.match(/^cell:([^:]+):(.+)$/i) || s.match(/^c:([^:]+):(.+)$/i);
  if (cell) return qIntersect(ids, qRowsToIds(stQCell.all(cell[1], cell[2])));

  const type = s.match(/^(type|kind|model):(.+)$/i);
  if (type) {
    const like = `%${type[2]}%`;
    const matches = new Set(qRowsToIds(stQEntityLoose.all(like, like, like, like)));
    return ids.filter(id => matches.has(id));
  }

  const pathToken = s.match(/^(path|id|display|name|title|label):(.+)$/i);
  if (pathToken) {
    const like = `%${pathToken[2]}%`;
    const matches = new Set(qRowsToIds(stQEntityLoose.all(like, like, like, like)));
    return ids.filter(id => matches.has(id));
  }

  const numCmp =
    s.match(/^number:([^:<>=]+)(<=|>=|=|:)(-?\d+(?:\.\d+)?)$/i) ||
    s.match(/^([^:<>=]+)(<=|>=)(-?\d+(?:\.\d+)?)$/i);

  if (numCmp) {
    const name = numCmp[1];
    const op = numCmp[2];
    const value = Number(numCmp[3]);

    if (Number.isFinite(value)) {
      if (op === '<=') return qIntersect(ids, qRowsToIds(stQNumberLte.all(name, value)));
      if (op === '>=') return qIntersect(ids, qRowsToIds(stQNumberGte.all(name, value)));
      const [lo, hi] = qNumberBounds(value);
      return qIntersect(ids, qRowsToIds(stQNumberBetweenNamed.all(name, lo, hi)));
    }
  }

  return null;
}

function qResolveEntityRow(idOrPath) {
  const key = String(idOrPath || '').trim();
  if (!key) return null;
  return stQEntityExactOrPath.get(key, key) || null;
}

function qExpandGraphMatches(seedIds, scope, dir, matchedBy, scores) {
  const parsed = parseScope(scope);
  if (!parsed.hops) return seedIds;

  const direction = String(dir || 'both').toLowerCase();
  const allowOut = direction !== 'in';
  const allowIn = direction !== 'out';
  const result = new Set(seedIds);
  let frontier = Array.from(seedIds);

  for (let hop = 1; hop <= parsed.hops; hop += 1) {
    const next = [];

    for (const id of frontier) {
      if (allowOut) {
        for (const edge of stQRefsForEntity.all(id)) {
          const row = qResolveEntityRow(edge.target_id);
          if (!row) continue;

          const target = row.entity_id;
          if (result.has(target)) continue;

          result.add(target);
          next.push(target);
          qAddScore(scores, target, Math.max(1, 12 - hop));

          matchedBy.set(target, {
            kind: 'graph_ref_out',
            path: [id, target],
            rels: [edge.rel],
            hops: hop,
          });
        }
      }

      if (allowIn) {
        const row = stQEntityById.get(id);
        const keys = Array.from(new Set([id, row && row.canonical_path].filter(Boolean)));

        for (const key of keys) {
          for (const edge of stQRefsToTarget.all(key)) {
            const source = edge.entity_id;
            if (result.has(source)) continue;

            result.add(source);
            next.push(source);
            qAddScore(scores, source, Math.max(1, 12 - hop));

            matchedBy.set(source, {
              kind: 'graph_ref_in',
              path: [source, id],
              rels: [edge.rel],
              hops: hop,
            });
          }
        }
      }
    }

    frontier = next;
    if (!frontier.length) break;
  }

  return Array.from(result);
}

function qPathFragmentsFromParams(params) {
  const values = [
    ...qAsArray(params.exclude_path_fragment),
    ...qAsArray(params.exclude_path),
    ...qAsArray(params.not_path),
  ];

  const fragments = [];

  for (const raw of values) {
    for (const part of String(raw || '').split(',')) {
      const clean = part.trim().toLowerCase();
      if (clean) fragments.push(clean);
    }
  }

  return Array.from(new Set(fragments));
}

function qApplyPathExclusions(ids, params) {
  const fragments = qPathFragmentsFromParams(params);
  if (!fragments.length) return ids;

  return ids.filter(id => {
    const row = stQEntityById.get(id);
    if (!row) return false;

    const haystack = [
      row.entity_id,
      row.canonical_path,
      row.display,
      row.entity_type,
    ].filter(Boolean).join(' ').toLowerCase();

    return !fragments.some(fragment => haystack.includes(fragment));
  });
}

function qSortIds(ids, sort, scores) {
  const rows = new Map();

  for (const id of ids) {
    const row = stQEntityById.get(id);
    if (row) rows.set(id, row);
  }

  const order = String(sort || 'score').toLowerCase();

  return ids.filter(id => rows.has(id)).sort((a, b) => {
    const ra = rows.get(a);
    const rb = rows.get(b);

    if (order === 'updated_desc') {
      return (rb.updated_at || 0) - (ra.updated_at || 0) ||
        String(ra.entity_id).localeCompare(String(rb.entity_id));
    }

    if (order === 'updated_asc') {
      return (ra.updated_at || 0) - (rb.updated_at || 0) ||
        String(ra.entity_id).localeCompare(String(rb.entity_id));
    }

    if (order === 'path_asc') {
      return String(ra.canonical_path || ra.entity_id).localeCompare(String(rb.canonical_path || rb.entity_id));
    }

    if (order === 'type_asc') {
      return String(ra.entity_type || '').localeCompare(String(rb.entity_type || '')) ||
        String(ra.canonical_path || ra.entity_id).localeCompare(String(rb.canonical_path || rb.entity_id));
    }

    return (scores.get(b) || 0) - (scores.get(a) || 0) ||
      (rb.updated_at || 0) - (ra.updated_at || 0) ||
      String(ra.entity_id).localeCompare(String(rb.entity_id));
  });
}

function qRunEntityQuery(params) {
  const limit = clamp(parseIntPositive(params.limit, 50), 1, MAX_PAGE_SIZE);
  const offset = Math.max(0, parseInt(params.offset || '0', 10) || 0);
  const entityType = params.type || null;
  const include = qIncludeSet(params);
  const matchedBy = new Map();
  const scores = new Map();
  const matchMode = String(params.match || 'all').toLowerCase() === 'any' ? 'any' : 'all';

  const hasMeasureIntent =
    qAsArray(params.number_gte).length ||
    qAsArray(params.number_lte).length ||
    qAsArray(params.number_between).length ||
    qAsArray(params.bbox).length ||
    qAsArray(params.radius).length;

  const hasSearchIntent = qAsArray(params.q).some(x => String(x || '').trim());
  const hasGraphExpansion = parseScope(params.graph_scope).hops > 0;
  const seedLimit = hasMeasureIntent || hasGraphExpansion || hasSearchIntent
    ? 1000000
    : Math.max((limit + offset) * 100, 1000);

  let ids = qRowsToIds(
    hasMeasureIntent || hasGraphExpansion || hasSearchIntent
      ? stQAll.all(entityType, entityType)
      : stQBase.all(entityType, entityType, seedLimit)
  );

  for (const id of ids) qAddScore(scores, id, 1);

  for (const [name, value] of qParsePairs(params.facet)) {
    ids = qIntersect(ids, qRowsToIds(stQFacet.all(name, value)));
  }

  for (const raw of qAsArray(params.q)) {
    const terms = qSplitSearchText(raw);
    if (!terms.length) continue;

    if (matchMode === 'any') {
      const union = new Set();

      for (const term of terms) {
        const inline = qApplyInlineSearchToken(ids, term);
        const matches = inline === null ? qSearchTermMatches(term, scores, matchedBy) : inline;
        for (const id of matches) union.add(id);
      }

      ids = ids.filter(id => union.has(id));
      continue;
    }

    for (const term of terms) {
      const inline = qApplyInlineSearchToken(ids, term);
      if (inline !== null) {
        ids = inline;
      } else {
        const matches = new Set(qSearchTermMatches(term, scores, matchedBy));
        ids = ids.filter(id => matches.has(id));
      }
    }
  }

  for (const rel of qAsArray(params.has_ref)) {
    if (rel) ids = qIntersect(ids, qRowsToIds(stQHasRef.all(String(rel))));
  }

  for (const [rel, target] of qParsePairs(params.ref)) {
    ids = qIntersect(ids, qRowsToIds(stQRef.all(rel, qNormalizeRefTarget(target))));
  }

  for (const [scheme, value] of qParsePairs(params.cell)) {
    ids = qIntersect(ids, qRowsToIds(stQCell.all(scheme, value)));
  }

  for (const [name, valueRaw] of qParsePairs(params.time_gte)) {
    const value = qDateMs(valueRaw);
    if (Number.isFinite(value)) ids = qIntersect(ids, qRowsToIds(stQTimeGte.all(name, Math.trunc(value))));
  }

  for (const [name, valueRaw] of qParsePairs(params.time_lte)) {
    const value = qDateMs(valueRaw);
    if (Number.isFinite(value)) ids = qIntersect(ids, qRowsToIds(stQTimeLte.all(name, Math.trunc(value))));
  }

  for (const [name, valueRaw] of qParsePairs(params.number_gte)) {
    const value = Number(valueRaw);
    if (Number.isFinite(value)) ids = qIntersect(ids, qRowsToIds(stQNumberGte.all(name, value)));
  }

  for (const [name, valueRaw] of qParsePairs(params.number_lte)) {
    const value = Number(valueRaw);
    if (Number.isFinite(value)) ids = qIntersect(ids, qRowsToIds(stQNumberLte.all(name, value)));
  }

  for (const triple of qParseTriples(params.number_between)) {
    const [name, loRaw, hiRaw] = triple;
    const lo = Number(loRaw);
    const hi = Number(hiRaw);

    if (Number.isFinite(lo) && Number.isFinite(hi)) {
      ids = qIntersect(
        ids,
        qRowsToIds(stQNumberBetweenNamed.all(name, Math.min(lo, hi), Math.max(lo, hi)))
      );
    }
  }

  const preMeasureIds = ids.slice();

  ids = qApplyBbox(ids, params.bbox, matchedBy);
  ids = qApplyRadius(ids, params.radius, params.radius_mode || 'auto', matchedBy);

  if ((qAsArray(params.bbox).length || qAsArray(params.radius).length) && String(params.measure_scope || 'direct') !== 'direct') {
    const expandedSet = new Set(qExpandMeasuredMatches(ids.slice(), params.measure_scope || 'direct', matchedBy));
    const preMeasureSet = new Set(preMeasureIds);
    ids = Array.from(expandedSet).filter(id => preMeasureSet.has(id));
  }

  ids = qApplyPathExclusions(ids, params);

  if (hasGraphExpansion) {
    ids = qExpandGraphMatches(ids, params.graph_scope || 'none', params.graph_dir || 'both', matchedBy, scores);
    ids = qApplyPathExclusions(ids, params);
  }

  ids = qSortIds(ids, params.sort || 'score', scores);

  const items = [];
  for (const id of ids.slice(offset, offset + limit)) {
    const row = stQEntityById.get(id);
    if (row) items.push(qHydrateItem(row, include, matchedBy, scores));
  }

  return {
    total: ids.length,
    offset,
    limit,
    items,
    filters: qBuildFilterSummary(ids),
    query: {
      match: matchMode,
      sort: params.sort || 'score',
      graph_scope: params.graph_scope || 'none',
      graph_dir: params.graph_dir || 'both',
      exclude_path_fragment: qPathFragmentsFromParams(params),
    },
  };
}

/* -------------------------------------------------------------------------- */
/* Query Param Canonicalization                                                */
/* -------------------------------------------------------------------------- */

function queryParamsFromUrl(url) {
  return {
    type: url.searchParams.get('type') || null,
    limit: url.searchParams.get('limit') || '50',
    offset: url.searchParams.get('offset') || '0',
    facet: url.searchParams.getAll('facet'),
    q: url.searchParams.getAll('q'),
    has_ref: url.searchParams.getAll('has_ref'),
    ref: url.searchParams.getAll('ref'),
    cell: url.searchParams.getAll('cell'),
    time_gte: url.searchParams.getAll('time_gte'),
    time_lte: url.searchParams.getAll('time_lte'),
    number_gte: url.searchParams.getAll('number_gte'),
    number_lte: url.searchParams.getAll('number_lte'),
    number_between: url.searchParams.getAll('number_between'),
    bbox: url.searchParams.getAll('bbox'),
    radius: url.searchParams.getAll('radius'),
    radius_mode: url.searchParams.get('radius_mode') || 'auto',
    measure_scope: url.searchParams.get('measure_scope') || 'direct',
    graph_scope: url.searchParams.get('graph_scope') || 'none',
    graph_dir: url.searchParams.get('graph_dir') || 'both',
    sort: url.searchParams.get('sort') || 'score',
    match: url.searchParams.get('match') || 'all',
    include: url.searchParams.getAll('include'),
    exclude_path_fragment: [
      ...url.searchParams.getAll('exclude_path_fragment'),
      ...url.searchParams.getAll('exclude_path'),
      ...url.searchParams.getAll('not_path'),
    ],
  };
}

function normalizeDerivedParams(params) {
  const out = new URLSearchParams(params);

  const availablePathFragments = out.getAll('available_path_fragment');
  const includedPathFragments = new Set(out.getAll('include_path_fragment'));

  if (availablePathFragments.length) {
    out.delete('exclude_path_fragment');
    out.delete('exclude_path');
    out.delete('not_path');

    for (const fragment of availablePathFragments) {
      const clean = String(fragment || '').trim();
      if (!clean) continue;
      if (!includedPathFragments.has(clean)) out.append('exclude_path_fragment', clean);
    }

    out.delete('available_path_fragment');
    out.delete('include_path_fragment');
  }

  const dateName = out.get('date_name') || out.get('time_name');
  const dateFrom = out.get('date_from') || out.get('time_from');
  const dateTo = out.get('date_to') || out.get('time_to');

  if (dateName && dateFrom) out.append('time_gte', `${dateName}:${dateFrom}`);
  if (dateName && dateTo) out.append('time_lte', `${dateName}:${dateTo}`);

  const measureName = out.get('measure_name');
  const measureMin = out.get('measure_min');
  const measureMax = out.get('measure_max');

  if (measureName && measureMin !== null && measureMax !== null && measureMin !== '' && measureMax !== '') {
    out.append('number_between', `${measureName}:${measureMin}:${measureMax}`);
  }

  const bx = out.get('bbox_x');
  const by = out.get('bbox_y');
  const bminx = out.get('bbox_min_x');
  const bminy = out.get('bbox_min_y');
  const bmaxx = out.get('bbox_max_x');
  const bmaxy = out.get('bbox_max_y');

  if (bx && by && bminx && bminy && bmaxx && bmaxy) {
    out.append('bbox', `${bx}:${by}:${bminx}:${bminy}:${bmaxx}:${bmaxy}`);
  }

  const rx = out.get('radius_x');
  const ry = out.get('radius_y');
  const rcx = out.get('radius_cx');
  const rcy = out.get('radius_cy');
  const rr = out.get('radius_r');

  if (rx && ry && rcx && rcy && rr) {
    out.append('radius', `${rx}:${ry}:${rcx}:${rcy}:${rr}`);
  }

  const nearbyXField = out.get('nearby_x') || 'lat';
  const nearbyYField = out.get('nearby_y') || 'lon';
  const nearbyLat = out.get('nearby_cx') || out.get('nearby_lat');
  const nearbyLon = out.get('nearby_cy') || out.get('nearby_lon') || out.get('nearby_lng') || out.get('nearby_longitude');
  const nearbyRadius = out.get('nearby_radius');

  if (nearbyLat && nearbyLon && nearbyRadius) {
    out.append('radius', `${nearbyXField}:${nearbyYField}:${nearbyLat}:${nearbyLon}:${nearbyRadius}`);
    if (!out.get('radius_mode')) out.set('radius_mode', 'geo');
  }

  for (const key of [
    'date_name',
    'date_from',
    'date_to',
    'time_name',
    'time_from',
    'time_to',
    'measure_name',
    'measure_min',
    'measure_max',
    'bbox_x',
    'bbox_y',
    'bbox_min_x',
    'bbox_min_y',
    'bbox_max_x',
    'bbox_max_y',
    'radius_x',
    'radius_y',
    'radius_cx',
    'radius_cy',
    'radius_r',
    'nearby_x',
    'nearby_y',
    'nearby_cx',
    'nearby_cy',
    'nearby_lat',
    'nearby_lon',
    'nearby_lng',
    'nearby_longitude',
    'nearby_radius',
  ]) {
    out.delete(key);
  }

  return out;
}

function canonicalSearchParams(params) {
  const normalized = normalizeDerivedParams(params);
  const out = new URLSearchParams();

  const appendAll = key => {
    for (const value of normalized.getAll(key)) {
      if (value !== null && value !== undefined && String(value).trim() !== '') out.append(key, value);
    }
  };

  appendAll('q');
  appendAll('type');
  appendAll('facet');
  appendAll('has_ref');
  appendAll('ref');
  appendAll('cell');
  appendAll('time_gte');
  appendAll('time_lte');
  appendAll('number_gte');
  appendAll('number_lte');
  appendAll('number_between');
  appendAll('bbox');
  appendAll('radius');
  appendAll('exclude_path_fragment');

  const radiusMode = normalized.get('radius_mode');
  if (normalized.getAll('radius').length && radiusMode && radiusMode !== 'auto') out.set('radius_mode', radiusMode);

  const measureScope = normalized.get('measure_scope');
  if (measureScope && measureScope !== 'direct') out.set('measure_scope', measureScope);

  const graphScope = normalized.get('graph_scope');
  if (graphScope && graphScope !== 'none') out.set('graph_scope', graphScope);

  const graphDir = normalized.get('graph_dir');
  if (graphScope && graphScope !== 'none' && graphDir && graphDir !== 'both') out.set('graph_dir', graphDir);

  const match = normalized.get('match');
  if (match && match !== 'all') out.set('match', match);

  const sort = normalized.get('sort');
  if (sort && sort !== 'score') out.set('sort', sort);

  const view = normalized.get('view');
  if (view && view !== 'cards') out.set('view', view);

  const limit = normalized.get('limit');
  if (limit && limit !== '25') out.set('limit', limit);

  const offset = normalized.get('offset');
  if (offset && offset !== '0') out.set('offset', offset);

  const include = normalized.get('include');
  if (include && include !== 'facets,refs,numbers,times,cells') out.set('include', include);

  return out;
}

function currentSearchParams(url) {
  const params = new URLSearchParams(url.searchParams);
  if (!params.get('include')) params.set('include', 'facets,refs,numbers,times,cells');
  if (!params.get('limit')) params.set('limit', '25');
  if (!params.get('view')) params.set('view', 'cards');
  if (!params.get('measure_scope')) params.set('measure_scope', 'direct');
  if (!params.get('graph_scope')) params.set('graph_scope', 'none');
  if (!params.get('graph_dir')) params.set('graph_dir', 'both');
  if (!params.get('sort')) params.set('sort', 'score');
  if (!params.get('match')) params.set('match', 'all');
  return normalizeDerivedParams(params);
}

function queryParamsFromParams(params) {
  return queryParamsFromUrl({ searchParams: params });
}

function searchHref(pathname, params, patch = {}) {
  const next = new URLSearchParams(params);

  for (const [key, value] of Object.entries(patch)) {
    next.delete(key);

    if (Array.isArray(value)) {
      for (const item of value) {
        if (item !== null && item !== undefined && String(item).trim() !== '') next.append(key, String(item));
      }
    } else if (value !== null && value !== undefined && String(value).trim() !== '') {
      next.set(key, String(value));
    }
  }

  const canonical = canonicalSearchParams(next);
  const qs = canonical.toString();
  return qs ? `${pathname}?${qs}` : pathname;
}

function withoutFilterHref(pathname, params, key, value) {
  const next = new URLSearchParams(params);

  const kept = [];
  for (const v of next.getAll(key)) {
    if (v !== value) kept.push(v);
  }

  next.delete(key);
  for (const v of kept) next.append(key, v);

  return searchHref(pathname, next);
}

function selectedValue(params, ...names) {
  for (const name of names) {
    const value = params.get(name);
    if (value) return value;
  }
  return '';
}

function hiddenSearchInputs(params, skip = []) {
  const skipSet = new Set(skip.concat([
    'measure_name',
    'measure_min',
    'measure_max',
    'bbox_x',
    'bbox_y',
    'bbox_min_x',
    'bbox_min_y',
    'bbox_max_x',
    'bbox_max_y',
    'radius_x',
    'radius_y',
    'radius_cx',
    'radius_cy',
    'radius_r',
    'date_name',
    'date_from',
    'date_to',
    'time_name',
    'time_from',
    'time_to',
    'nearby_x',
    'nearby_y',
    'nearby_cx',
    'nearby_cy',
    'nearby_lat',
    'nearby_lon',
    'nearby_lng',
    'nearby_longitude',
    'nearby_radius',
    'available_path_fragment',
    'include_path_fragment',
  ]));

  const rows = [];

  for (const [key, value] of params.entries()) {
    if (!skipSet.has(key)) {
      rows.push(`<input type="hidden" name="${escapeHtml(key)}" value="${escapeHtml(value)}">`);
    }
  }

  return rows.join('');
}

function renderSelect(name, rows, selected, placeholder, valueKey = 'name') {
  if (!rows || !rows.length) {
    return `<input name="${escapeHtml(name)}" value="${escapeHtml(selected || '')}" placeholder="${escapeHtml(placeholder || '')}">`;
  }

  return `
    <select name="${escapeHtml(name)}">
      <option value="">${escapeHtml(placeholder || 'choose')}</option>
      ${rows.map(row => {
        const value = row[valueKey] || row.value || row.name;
        return `<option value="${escapeHtml(value)}" ${String(selected || '') === String(value) ? 'selected' : ''}>${escapeHtml(value)}${row.count != null ? ` (${escapeHtml(row.count)})` : ''}</option>`;
      }).join('')}
    </select>
  `;
}

function qExcludedPathFragmentsFromSearchParams(params) {
  const out = [];

  for (const key of ['exclude_path_fragment', 'exclude_path', 'not_path']) {
    for (const raw of params.getAll(key)) {
      for (const part of String(raw || '').split(',')) {
        const clean = part.trim().toLowerCase();
        if (clean) out.push(clean);
      }
    }
  }

  return new Set(out);
}

/* -------------------------------------------------------------------------- */
/* HTML Renderer                                                              */
/* -------------------------------------------------------------------------- */

function renderSearchForm(params) {
  return `
    <form method="get" action="/search/ui">
      <input type="hidden" name="include" value="${escapeHtml(params.get('include') || 'facets,refs,numbers,times,cells')}">

      <fieldset>
        <legend>Search</legend>

        <label>Intent</label>
        <input name="q" value="${escapeHtml(params.get('q') || '')}" placeholder="Search words, path, kind, property, number, relationship">

        <label>Type</label>
        <input name="type" value="${escapeHtml(params.get('type') || '')}" placeholder="optional entity_type">

        <label>Exclude path fragments</label>
        <input name="exclude_path_fragment" value="${escapeHtml(params.get('exclude_path_fragment') || '')}" placeholder="_meta, archive, debug, refs">

        <label>Date field</label>
        <input name="date_name" value="${escapeHtml(selectedValue(params, 'date_name', 'time_name'))}" placeholder="start_time, created_at, updated_at, fetched_at">

        <label>From</label>
        <input name="date_from" value="${escapeHtml(selectedValue(params, 'date_from', 'time_from'))}" placeholder="2026-04-01">

        <label>To</label>
        <input name="date_to" value="${escapeHtml(selectedValue(params, 'date_to', 'time_to'))}" placeholder="2026-04-30">

        <details>
          <summary>Nearby</summary>

          <label>Latitude field</label>
          <input name="nearby_x" value="${escapeHtml(selectedValue(params, 'nearby_x') || 'lat')}" placeholder="lat">

          <label>Longitude field</label>
          <input name="nearby_y" value="${escapeHtml(selectedValue(params, 'nearby_y') || 'lon')}" placeholder="lon">

          <label>Latitude</label>
          <input name="nearby_cx" value="${escapeHtml(selectedValue(params, 'nearby_cx', 'nearby_lat'))}" placeholder="41.8781">

          <label>Longitude</label>
          <input name="nearby_cy" value="${escapeHtml(selectedValue(params, 'nearby_cy', 'nearby_lng', 'nearby_lon'))}" placeholder="-87.6298">

          <label>Radius</label>
          <input name="nearby_radius" value="${escapeHtml(params.get('nearby_radius') || '')}" placeholder="10km or 25mi">

          <input type="hidden" name="radius_mode" value="geo">
        </details>

        <details>
          <summary>Advanced</summary>

          <label>Match</label>
          <select name="match">
            <option value="all" ${params.get('match') === 'all' ? 'selected' : ''}>all terms</option>
            <option value="any" ${params.get('match') === 'any' ? 'selected' : ''}>any term</option>
          </select>

          <label>Sort</label>
          <select name="sort">
            <option value="score" ${params.get('sort') === 'score' ? 'selected' : ''}>score</option>
            <option value="updated_desc" ${params.get('sort') === 'updated_desc' ? 'selected' : ''}>updated desc</option>
            <option value="updated_asc" ${params.get('sort') === 'updated_asc' ? 'selected' : ''}>updated asc</option>
            <option value="path_asc" ${params.get('sort') === 'path_asc' ? 'selected' : ''}>path asc</option>
            <option value="type_asc" ${params.get('sort') === 'type_asc' ? 'selected' : ''}>type asc</option>
          </select>

          <label>Limit</label>
          <input name="limit" value="${escapeHtml(params.get('limit') || '25')}" inputmode="numeric">

          <label>View</label>
          <select name="view">
            <option value="cards" ${params.get('view') === 'cards' ? 'selected' : ''}>cards</option>
            <option value="tree" ${params.get('view') === 'tree' ? 'selected' : ''}>path tree</option>
          </select>

          <label>Graph scope</label>
          <select name="graph_scope">
            <option value="none" ${params.get('graph_scope') === 'none' ? 'selected' : ''}>none</option>
            <option value="refs:1" ${params.get('graph_scope') === 'refs:1' ? 'selected' : ''}>1 hop</option>
            <option value="refs:2" ${params.get('graph_scope') === 'refs:2' ? 'selected' : ''}>2 hops</option>
            <option value="refs:3" ${params.get('graph_scope') === 'refs:3' ? 'selected' : ''}>3 hops</option>
          </select>

          <label>Graph direction</label>
          <select name="graph_dir">
            <option value="both" ${params.get('graph_dir') === 'both' ? 'selected' : ''}>both</option>
            <option value="out" ${params.get('graph_dir') === 'out' ? 'selected' : ''}>outgoing refs</option>
            <option value="in" ${params.get('graph_dir') === 'in' ? 'selected' : ''}>incoming refs</option>
          </select>
        </details>

        <p>
          <button type="submit">Search / generate URL</button>
          <a href="/search/ui">Reset</a>
        </p>
      </fieldset>
    </form>
  `;
}

function renderQueryState(params) {
  const visibleKeys = [
    'q',
    'type',
    'time_gte',
    'time_lte',
    'radius',
    'exclude_path_fragment',
    'facet',
    'has_ref',
    'ref',
    'cell',
    'number_between',
    'graph_scope',
  ];

  const chips = [];

  for (const key of visibleKeys) {
    for (const value of params.getAll(key)) {
      if (!value || value === 'none') continue;
      const removeHref = withoutFilterHref('/search/ui', params, key, value);
      chips.push(`<a href="${escapeHtml(removeHref)}">${escapeHtml(key)}=${escapeHtml(value)} x</a>`);
    }
  }

  return chips.length ? `<p>${chips.join(' | ')}</p>` : '';
}

function renderFilterChip(params, key, value, label, count) {
  const href = searchHref('/search/ui', params, { [key]: value });
  return `<a href="${escapeHtml(href)}">${escapeHtml(label)}${count != null ? ` (${escapeHtml(count)})` : ''}</a>`;
}

function renderPrimaryRefinements(params, result) {
  const filters = result.filters || {};
  const timeRows = filters.times || [];
  const measureRows = filters.measures || [];
  const excluded = qExcludedPathFragmentsFromSearchParams(params);

  return `
    <section>
      <h2>Refine</h2>

      <details open>
        <summary>Date range</summary>
        <form method="get" action="/search/ui">
          ${hiddenSearchInputs(params, ['time_gte', 'time_lte', 'date_name', 'date_from', 'date_to'])}

          <label>Date field</label>
          ${renderSelect('date_name', timeRows, selectedValue(params, 'date_name', 'time_name'), 'choose field')}

          <label>From</label>
          <input name="date_from" value="${escapeHtml(selectedValue(params, 'date_from', 'time_from'))}" placeholder="2026-04-01">

          <label>To</label>
          <input name="date_to" value="${escapeHtml(selectedValue(params, 'date_to', 'time_to'))}" placeholder="2026-04-30">

          <button type="submit">Apply date</button>
        </form>
      </details>

      <details>
        <summary>Nearby</summary>
        <form method="get" action="/search/ui">
          ${hiddenSearchInputs(params, ['radius', 'radius_mode', 'nearby_x', 'nearby_y', 'nearby_cx', 'nearby_cy', 'nearby_radius'])}

          <label>Latitude field</label>
          ${renderSelect('nearby_x', measureRows, selectedValue(params, 'nearby_x') || 'lat', 'lat')}

          <label>Longitude field</label>
          ${renderSelect('nearby_y', measureRows, selectedValue(params, 'nearby_y') || 'lon', 'lon')}

          <label>Latitude</label>
          <input name="nearby_cx" value="${escapeHtml(selectedValue(params, 'nearby_cx', 'nearby_lat'))}" placeholder="41.8781">

          <label>Longitude</label>
          <input name="nearby_cy" value="${escapeHtml(selectedValue(params, 'nearby_cy', 'nearby_lng', 'nearby_lon'))}" placeholder="-87.6298">

          <label>Radius</label>
          <input name="nearby_radius" value="${escapeHtml(params.get('nearby_radius') || '')}" placeholder="10km or 25mi">

          <input type="hidden" name="radius_mode" value="geo">
          <button type="submit">Apply nearby</button>
        </form>
      </details>

      ${renderPathOptions(params, result, excluded)}
      ${renderCompactBuckets(params, result)}
    </section>
  `;
}

function renderPathOptions(params, result, excluded) {
  const rows = ((result.filters && result.filters.paths) || []).slice(0, 24);
  if (!rows.length) return '';

  return `
    <details>
      <summary>Included paths</summary>
      <form method="get" action="/search/ui">
        ${hiddenSearchInputs(params, ['exclude_path_fragment', 'exclude_path', 'not_path', 'available_path_fragment', 'include_path_fragment'])}
        <p>Untick a path group to exclude it.</p>
        ${rows.map(row => {
          const value = String(row.value || '');
          const key = value.toLowerCase();
          const checked = excluded.has(key) ? '' : 'checked';
          return `
            <div>
              <input type="hidden" name="available_path_fragment" value="${escapeHtml(value)}">
              <label>
                <input type="checkbox" name="include_path_fragment" value="${escapeHtml(value)}" ${checked}>
                ${escapeHtml(value)} (${escapeHtml(row.count)})
              </label>
            </div>
          `;
        }).join('')}
        <button type="submit">Apply paths</button>
      </form>
    </details>
  `;
}

function renderCompactBuckets(params, result) {
  const filters = result.filters || {};
  const typeRows = filters.types || [];
  const refRows = filters.refs || [];
  const facetGroups = filters.facets || {};

  const typeHtml = typeRows.length
    ? typeRows.slice(0, 10).map(row => renderFilterChip(params, 'type', row.value, row.value, row.count)).join(' | ')
    : 'No type refinements.';

  const refHtml = refRows.length
    ? refRows.slice(0, 10).map(row => renderFilterChip(params, 'has_ref', row.rel, row.rel, row.count)).join(' | ')
    : 'No relationship refinements.';

  const facetHtml = Object.entries(facetGroups).slice(0, 6).map(([name, values]) => {
    return `<p><strong>${escapeHtml(name)}</strong>: ${(values || []).slice(0, 8).map(row => renderFilterChip(params, 'facet', `${name}:${row.value}`, row.value, row.count)).join(' | ')}</p>`;
  }).join('');

  return `
    <details>
      <summary>Types</summary>
      <p>${typeHtml}</p>
    </details>

    <details>
      <summary>Relationships</summary>
      <p>${refHtml}</p>
    </details>

    <details>
      <summary>Top properties</summary>
      ${facetHtml || '<p>No property refinements.</p>'}
    </details>
  `;
}

function renderMatchExplanation(origin, item) {
  const match = item.matched_by;
  if (!match || !Array.isArray(match.path) || !match.path.length) return '';

  const parts = [];

  for (let i = 0; i < match.path.length; i += 1) {
    const p = match.path[i];

    if (i > 0) parts.push(` --${escapeHtml((match.rels && match.rels[i - 1]) || 'ref')}-> `);
    parts.push(`<a href="${escapeHtml(stateHref(origin, p))}"><code>${escapeHtml(p)}</code></a>`);
  }

  const distance = typeof match.distance === 'number'
    ? ` distance=${escapeHtml(match.distance.toFixed(3))}`
    : '';

  return `<p>Matched through ${escapeHtml(match.kind || 'search')}: ${parts.join('')}${distance}</p>`;
}

function renderResultCard(origin, params, item) {
  const canonicalPath = item.canonical_path || item.entity_id || '';
  const display = item.display || item.entity_id || canonicalPath;
  const recordHref = canonicalPath ? stateHref(origin, canonicalPath) : '';
  const jsonHref = recordHref ? `${recordHref}${recordHref.includes('?') ? '&' : '?'}format=json` : '';
  const sameTypeHref = item.entity_type ? searchHref('/search/ui', params, { type: item.entity_type }) : '';

  const numbers = (item.numbers || [])
    .slice(0, 4)
    .map(n => `${escapeHtml(n.name)}=${escapeHtml(n.value)}`)
    .join(' | ');

  const times = (item.times || [])
    .slice(0, 3)
    .map(t => `${escapeHtml(t.name)}=${escapeHtml(new Date(Number(t.value_ms)).toISOString())}`)
    .join(' | ');

  const refs = (item.refs || [])
    .slice(0, 3)
    .map(r => `${escapeHtml(r.rel)} -> ${escapeHtml(r.target_id)}`)
    .join(' | ');

  return `
    <article>
      <h3>${recordHref ? `<a href="${escapeHtml(recordHref)}">${escapeHtml(display)}</a>` : escapeHtml(display)}</h3>
      <p>${escapeHtml(item.entity_type || 'entity')}${item.updated_at ? ` | ${escapeHtml(new Date(Number(item.updated_at)).toISOString())}` : ''}${item.score != null ? ` | score ${escapeHtml(item.score)}` : ''}</p>
      <p><code>${escapeHtml(canonicalPath)}</code></p>
      ${renderMatchExplanation(origin, item)}
      ${times ? `<p>${times}</p>` : ''}
      ${numbers ? `<p>${numbers}</p>` : ''}
      ${refs ? `<p>${refs}</p>` : ''}
      <p>
        ${recordHref ? `<a href="${escapeHtml(recordHref)}">Open</a>` : ''}
        ${jsonHref ? ` | <a href="${escapeHtml(jsonHref)}">JSON</a>` : ''}
        ${sameTypeHref ? ` | <a href="${escapeHtml(sameTypeHref)}">same type</a>` : ''}
      </p>
      <details>
        <summary>Details</summary>
        <pre>${escapeHtml(JSON.stringify(item, null, 2))}</pre>
      </details>
    </article>
  `;
}

function treeInsert(root, parts, item) {
  let cur = root;

  for (const part of parts) {
    if (!cur.children.has(part)) {
      cur.children.set(part, {
        name: part,
        children: new Map(),
        items: [],
      });
    }

    cur = cur.children.get(part);
  }

  cur.items.push(item);
}

function renderTreeNode(origin, node, prefixPath = '') {
  const rows = [];

  for (const child of Array.from(node.children.values()).sort((a, b) => a.name.localeCompare(b.name))) {
    const childPath = prefixPath ? `${prefixPath}.${child.name}` : child.name;
    const labels = child.items.map(item => {
      return `${item.entity_type ? ` [${escapeHtml(item.entity_type)}]` : ''}${item.display ? ` - ${escapeHtml(item.display)}` : ''}`;
    }).join(' ');

    rows.push(`<li><a href="${escapeHtml(stateHref(origin, childPath))}">${escapeHtml(child.name)}</a>${labels}${child.children.size ? `<ul>${renderTreeNode(origin, child, childPath)}</ul>` : ''}</li>`);
  }

  return rows.join('');
}

function renderTreeView(origin, items) {
  const root = { name: '', children: new Map(), items: [] };

  for (const item of items) {
    const parts = dp2parts(item.canonical_path || item.entity_id || '');
    if (parts.length) treeInsert(root, parts, item);
  }

  const html = renderTreeNode(origin, root);
  return html ? `<ul>${html}</ul>` : '<p>No paths to show.</p>';
}

function renderSearchPaging(params, result) {
  const limit = clamp(parseIntPositive(params.get('limit'), 25), 1, MAX_PAGE_SIZE);
  const offset = Math.max(0, parseInt(params.get('offset') || '0', 10) || 0);
  const total = Number(result.total || 0);
  const rows = [];

  if (offset > 0) rows.push(`<a href="${escapeHtml(searchHref('/search/ui', params, { offset: Math.max(0, offset - limit) }))}">Prev</a>`);
  if (offset + limit < total) rows.push(`<a href="${escapeHtml(searchHref('/search/ui', params, { offset: offset + limit }))}">Next</a>`);

  return rows.length ? `<nav>${rows.join(' | ')}</nav>` : '';
}

function renderSearchResultsHtml(origin, url) {
  const params = currentSearchParams(url);
  const result = qRunEntityQuery(queryParamsFromParams(params));
  const items = result.items || [];

  const resultHref = `${origin}${searchHref('/search/ui', params)}`;
  const rawJsonHref = `${origin}${searchHref('/query/entities', params)}`;
  const apiJsonHref = `${origin}${searchHref('/api/query/entities', params)}`;

  const body = (params.get('view') || 'cards') === 'tree'
    ? renderTreeView(origin, items)
    : (items.length
      ? items.map(item => renderResultCard(origin, params, item)).join('')
      : '<article><h3>No results</h3><p>Try a broader search or remove a filter.</p></article>');

  return `
    <main>
      ${renderQueryState(params)}

      <p>
        <strong>${escapeHtml(result.total ?? items.length)}</strong> results
        | <a href="${escapeHtml(apiJsonHref)}">JSON</a>
        | <a href="${escapeHtml(rawJsonHref)}">raw JSON</a>
      </p>

      <details>
        <summary>Shareable URL</summary>
        <p><a href="${escapeHtml(resultHref)}"><code>${escapeHtml(resultHref)}</code></a></p>
      </details>

      ${renderPrimaryRefinements(params, result)}

      <hr>

      ${renderSearchPaging(params, result)}
      ${body}
    </main>
  `;
}

function renderSearchPage(origin, url) {
  const params = currentSearchParams(url);

  const hasIntent =
    params.get('q') ||
    params.get('type') ||
    params.get('facet') ||
    params.get('ref') ||
    params.get('has_ref') ||
    params.get('number_between') ||
    params.get('number_gte') ||
    params.get('number_lte') ||
    params.get('bbox') ||
    params.get('radius') ||
    params.get('time_gte') ||
    params.get('time_lte') ||
    params.get('exclude_path_fragment') ||
    params.get('exclude_path') ||
    params.get('not_path') ||
    (params.get('graph_scope') && params.get('graph_scope') !== 'none');

  const initial = hasIntent
    ? renderSearchResultsHtml(origin, url)
    : '<main><h2>Start with intent</h2><p>Search first. Filters appear only after there is something meaningful to refine.</p></main>';

  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Hypergraph Search</title>
</head>
<body>
  <h1>Hypergraph Search</h1>
  <p>Search, refine, navigate. Details stay collapsed unless requested.</p>
  ${renderSearchForm(params)}
  ${initial}
</body>
</html>`;
}

function renderDocHtml(doc) {
  const state = isObj(doc && doc._state) ? doc._state : {};
  const links = isObj(doc && doc._links) ? doc._links : {};
  const actions = isObj(doc && doc._actions) ? doc._actions : {};
  const embedded = isObj(doc && doc._embedded) ? doc._embedded : {};

  const linkRows = Object.entries(links).map(([rel, href]) => {
    return `<tr><td><code>${escapeHtml(rel)}</code></td><td>${href ? `<a href="${escapeHtml(href)}">${escapeHtml(href)}</a>` : ''}</td></tr>`;
  }).join('');

  const actionRows = Object.entries(actions).map(([name, action]) => {
    return `<tr><td><code>${escapeHtml(name)}</code></td><td><code>${escapeHtml(isObj(action) ? action.method : '')}</code></td><td>${isObj(action) && action.href ? `<a href="${escapeHtml(action.href)}">${escapeHtml(action.href)}</a>` : ''}</td><td>${escapeHtml(isObj(action) ? action.title : '')}</td></tr>`;
  }).join('');

  const embeddedRows = Object.entries(embedded.children || embedded).map(([name, child]) => {
    const childState = isObj(child && child._state) ? child._state : {};
    const childLinks = isObj(child && child._links) ? child._links : {};
    const href = childLinks.self || childLinks.target || childLinks.record || '';
    return `<tr><td>${href ? `<a href="${escapeHtml(href)}">${escapeHtml(name)}</a>` : escapeHtml(name)}</td><td><code>${escapeHtml(childState.kind || '')}</code></td><td>${escapeHtml(childState.summary || '')}</td></tr>`;
  }).join('');

  const relatedHref = state.path && state.path !== '/'
    ? `/search/ui?q=${encodeURIComponent(state.path)}&limit=25&include=facets,refs,numbers,times,cells`
    : '/search/ui';

  return `<!doctype html>
<html>
<head><meta charset="utf-8"><title>${escapeHtml(state.path || '/')}</title></head>
<body>
  <h1>${escapeHtml(state.path || '/')}</h1>
  <p>${escapeHtml(state.summary || '')}</p>
  <nav><a href="?format=json">JSON</a> | <a href="${escapeHtml(relatedHref)}">Search from here</a></nav>
  <p>kind: ${escapeHtml(state.kind || '')} | children: ${escapeHtml(state.children_total ?? state.roots_total ?? 0)} | commit_seq: ${escapeHtml(state.commit_seq ?? 0)}</p>

  <h2>Embedded</h2>
  <table>
    <thead><tr><th>name</th><th>kind</th><th>summary</th></tr></thead>
    <tbody>${embeddedRows || '<tr><td colspan="3">No embedded resources.</td></tr>'}</tbody>
  </table>

  <details>
    <summary>Links</summary>
    <table><thead><tr><th>rel</th><th>href</th></tr></thead><tbody>${linkRows || '<tr><td colspan="2">No links.</td></tr>'}</tbody></table>
  </details>

  <details>
    <summary>Actions</summary>
    <table><thead><tr><th>name</th><th>method</th><th>href</th><th>title</th></tr></thead><tbody>${actionRows || '<tr><td colspan="4">No actions.</td></tr>'}</tbody></table>
  </details>

  <details>
    <summary>Data</summary>
    <pre>${escapeHtml(JSON.stringify(doc.data, null, 2))}</pre>
  </details>
</body>
</html>`;
}

/* -------------------------------------------------------------------------- */
/* Hypermedia Documents                                                       */
/* -------------------------------------------------------------------------- */

function searchAction(origin, href = `${origin}/api/query/entities`) {
  return buildAction('GET', href, [
    { name: 'q', type: 'string', required: false, hint: 'Words, paths, ids, labels, values, or inline filters like type:sports_team.' },
    { name: 'type', type: 'string', required: false, hint: 'Entity type filter.' },
    { name: 'facet', type: 'string', required: false, repeatable: true, hint: 'name:value' },
    { name: 'has_ref', type: 'string', required: false, repeatable: true, hint: 'Relationship name that must exist.' },
    { name: 'ref', type: 'string', required: false, repeatable: true, hint: 'rel:target' },
    { name: 'cell', type: 'string', required: false, repeatable: true, hint: 'scheme:value' },
    { name: 'time_gte', type: 'string', required: false, repeatable: true, hint: 'field:date' },
    { name: 'time_lte', type: 'string', required: false, repeatable: true, hint: 'field:date' },
    { name: 'number_gte', type: 'string', required: false, repeatable: true, hint: 'field:number' },
    { name: 'number_lte', type: 'string', required: false, repeatable: true, hint: 'field:number' },
    { name: 'number_between', type: 'string', required: false, repeatable: true, hint: 'field:min:max' },
    { name: 'bbox', type: 'string', required: false, repeatable: true, hint: 'xField:yField:minX:minY:maxX:maxY' },
    { name: 'radius', type: 'string', required: false, repeatable: true, hint: 'xField:yField:centerX:centerY:distance' },
    { name: 'radius_mode', type: 'string', required: false, options: ['auto', 'geo', 'euclidean'] },
    { name: 'exclude_path_fragment', type: 'string', required: false, repeatable: true },
    { name: 'graph_scope', type: 'string', required: false, options: ['none', 'refs:1', 'refs:2', 'refs:3', 'refs:4', 'refs:5'] },
    { name: 'graph_dir', type: 'string', required: false, options: ['both', 'out', 'in'] },
    { name: 'limit', type: 'number', required: false },
    { name: 'offset', type: 'number', required: false },
  ], 'Search indexed hypergraph entities.', {
    protocol: 'hypermedia',
    canonical_url_uses: ['time_gte', 'time_lte', 'radius', 'exclude_path_fragment'],
    ui_aliases: ['date_name/date_from/date_to', 'nearby_x/nearby_y/nearby_cx/nearby_cy/nearby_radius'],
  });
}

function classifyNode(node, children, parts) {
  const hasData = !!(node && node.data != null);
  const hasChildren = (children && children.total) > 0;

  if (parts.length === 0) {
    return { kind: 'system_root', item_kind: 'root', display_as: 'list', primary_link: 'self', sort_by: 'name' };
  }

  const d = hasData && isObj(node.data) ? node.data : {};

  if (parts[0] === '_meta' || parts.includes('_meta')) {
    return { kind: 'system', item_kind: hasChildren ? 'directory' : 'record', display_as: 'detail', primary_link: 'self', sort_by: 'name' };
  }

  if (d.kind === 'index_ref') {
    return { kind: 'index_ref', item_kind: 'ref', display_as: 'detail', primary_link: 'record', sort_by: 'name' };
  }

  if (d.kind === 'ref' || d.target || d.record_dot) {
    return { kind: d.kind || 'ref', item_kind: 'ref', display_as: 'detail', primary_link: d.record_dot ? 'record' : 'target', sort_by: 'name' };
  }

  if (hasData && hasChildren) {
    return { kind: d.kind || d.type || d.model || 'record_with_children', item_kind: 'mixed', display_as: 'detail', primary_link: 'self', sort_by: 'name' };
  }

  if (hasData) {
    return { kind: d.kind || d.type || d.model || 'record', item_kind: 'field', display_as: 'detail', primary_link: 'self', sort_by: 'name' };
  }

  if (hasChildren) {
    return { kind: 'directory', item_kind: 'directory', display_as: 'list', primary_link: 'self', sort_by: 'name' };
  }

  return { kind: 'directory', item_kind: 'directory', display_as: 'list', primary_link: 'self', sort_by: 'name' };
}

function summarizeNode(cls, node, children, parts) {
  const total = (children && children.total) || 0;
  const name = parts.length ? parts[parts.length - 1] : '/';
  const d = node && isObj(node.data) ? node.data : {};

  if (cls.kind === 'system_root') return `Hypergraph root. ${total} root namespace${total === 1 ? '' : 's'}.`;
  if (cls.item_kind === 'ref') return `Reference: ${d.name || d.rel || name}${d.target ? ` -> ${d.target}` : ''}${d.record_dot ? ` -> ${d.record_dot}` : ''}.`;
  if (cls.kind === 'record' || cls.kind === 'record_with_children') return `Record: ${d.name || d.title || d.display_name || d.label || d.id || name}.`;

  return total
    ? `Directory: ${name} (${total} entr${total === 1 ? 'y' : 'ies'}).`
    : `Node: ${name}.`;
}

function childDoc(origin, childDp, row) {
  const parts = dp2parts(childDp);
  const node = { data: row.data, updated_at: row.updated_at, commit_seq: row.commit_seq };
  const cls = classifyNode(node, { total: 0, rows: [] }, parts);

  const links = {
    self: stateHref(origin, childDp),
    stream: streamHref(origin, childDp),
    search: `${origin}/search/ui?q=${encodeURIComponent(childDp)}&limit=25&include=facets,refs,numbers,times,cells`,
  };

  if (row.data) {
    promoteDataLinks(origin, links, row.data);
    if (row.data.target && !links.target) links.target = hrefForMaybeDotPath(origin, row.data.target);
    if (row.data.record_dot && !links.record) links.record = stateHref(origin, String(row.data.record_dot));
  }

  return {
    _state: {
      kind: cls.kind,
      path: childDp,
      summary: summarizeNode(cls, node, { total: 0 }, parts),
      commit_seq: row.commit_seq,
      hints: {
        item_kind: cls.item_kind,
        display_as: cls.display_as,
        primary_link: cls.primary_link,
      },
    },
    _links: links,
    _actions: {},
    _embedded: {},
    data: row.data,
  };
}

function buildNodeDoc(origin, dp, { page = 1, perPage = DEFAULT_PAGE_SIZE, order = 'key_asc' } = {}) {
  const parts = dp2parts(dp);
  const node = parts.length ? dbGetByParts(parts) : null;
  const childrenPage = buildChildrenPage(dp || '', { page, perPage, order });
  const cls = classifyNode(node, childrenPage, parts);
  const total = childrenPage.total || 0;
  const safePerPage = clamp(parseIntPositive(perPage, DEFAULT_PAGE_SIZE), 1, MAX_PAGE_SIZE);
  const numPages = Math.max(1, Math.ceil(total / safePerPage));
  const safePage = clamp(parseIntPositive(page, 1), 1, numPages);

  const links = {
    self: stateHref(origin, dp || '', { page: safePage, per_page: safePerPage, order }),
    stream: streamHref(origin, dp || ''),
    changes_since: apiHref(origin, dp || '', 'changes-since'),
    children: apiHref(origin, dp || '', 'children', { page: safePage, per_page: safePerPage, order }),
    search: `${origin}/search/ui?q=${encodeURIComponent(dp || '')}&limit=25&include=facets,refs,numbers,times,cells`,
  };

  if (node && node.data) {
    promoteDataLinks(origin, links, node.data);
    if (node.data.target && !links.target) links.target = hrefForMaybeDotPath(origin, node.data.target);
    if (node.data.record_dot && !links.record) links.record = stateHref(origin, String(node.data.record_dot));
  }

  const parent = parentOf(dp || '');
  if (parts.length > 1) links.parent = stateHref(origin, parent);
  else if (parts.length === 1) links.parent = `${origin}/`;

  if (safePage > 1) links.prev = stateHref(origin, dp || '', { page: safePage - 1, per_page: safePerPage, order });
  if (safePage < numPages) links.next = stateHref(origin, dp || '', { page: safePage + 1, per_page: safePerPage, order });

  const embeddedChildren = {};

  for (const child of childrenPage.rows) {
    const childDp = parts.length ? `${dp}.${child.name}` : child.name;
    links[child.name] = stateHref(origin, childDp);
    embeddedChildren[child.name] = childDoc(origin, childDp, child);
  }

  const actions = {
    subscribe: buildAction('GET', streamHref(origin, dp || ''), [{ name: 'scope', type: 'string', required: false, options: ['exact', 'subtree'] }], 'Subscribe to live changes.'),
    list_children: buildAction('GET', apiHref(origin, dp || '', 'children'), [
      { name: 'page', type: 'number', required: false },
      { name: 'per_page', type: 'number', required: false },
      { name: 'order', type: 'string', required: false, options: ['key_asc', 'key_desc', 'updated_desc', 'updated_asc'] },
    ], 'Navigate children via pagination.'),
    search: searchAction(origin, `${origin}/api/query/entities?q=${encodeURIComponent(dp || '')}`),
  };

  if (parts.length) {
    actions.update = buildAction('PUT', stateHref(origin, dp), [{ name: 'data', type: 'object', required: true }], "Replace this node's data payload.");
    actions.delete = buildAction('DELETE', stateHref(origin, dp), [], 'Delete this node. Empty parent directories are auto-pruned.');
  }

  return {
    _state: {
      kind: cls.kind,
      path: dp || '/',
      summary: summarizeNode(cls, node, childrenPage, parts),
      protocol: {
        agent_instruction: 'Read _state. Follow _links. Use _actions. Inspect _embedded. Treat data as opaque unless an action explains how to query it.',
      },
      hints: {
        item_kind: cls.item_kind,
        display_as: cls.display_as,
        primary_link: cls.primary_link,
        sort_by: cls.sort_by,
      },
      commit_seq: node ? node.commit_seq : 0,
      children_total: total,
      children_page: safePage,
      children_per_page: safePerPage,
      children_num_pages: numPages,
      order,
    },
    _links: links,
    _actions: actions,
    _embedded: { children: embeddedChildren },
    data: node ? node.data : null,
  };
}

function dbRoots() {
  const out = [];
  for (const row of stRoots.iterate()) out.push(row.name);
  return out;
}

function buildSystemRootDoc(origin) {
  const roots = dbRoots();

  const links = {
    self: `${origin}/`,
    query: `${origin}/api/query`,
    search_ui: `${origin}/search/ui`,
  };

  const embedded = {};

  for (const root of roots) {
    links[root] = stateHref(origin, root);
    embedded[root] = {
      _state: {
        kind: 'root_namespace',
        path: root,
        summary: `Root namespace ${root}.`,
      },
      _links: {
        self: stateHref(origin, root),
        stream: streamHref(origin, root),
        search: `${origin}/search/ui?q=${encodeURIComponent(root)}&limit=25&include=facets,refs,numbers,times,cells`,
      },
      _actions: {
        search_from_here: searchAction(origin, `${origin}/api/query/entities?q=${encodeURIComponent(root)}`),
      },
      data: null,
    };
  }

  return {
    _state: {
      kind: 'system_root',
      path: '/',
      summary: `Root of the hypergraph. ${roots.length} namespace${roots.length === 1 ? '' : 's'} available.`,
      protocol: {
        agent_instruction: 'Start here. Follow _links to namespaces, use _actions.search to query, inspect _embedded for nearby resources, and treat data as opaque.',
      },
      roots_total: roots.length,
    },
    _links: links,
    _actions: {
      search: searchAction(origin),
      subscribe: buildAction('GET', streamHref(origin, '', { scope: 'subtree' }), [{ name: 'scope', type: 'string', required: false, options: ['exact', 'subtree'] }], 'Subscribe to changes across all roots.'),
      list_roots: buildAction('GET', `${origin}/`, [], 'List all root namespaces.'),
    },
    _embedded: { roots: embedded },
    data: null,
  };
}

function buildQueryEntitiesDoc(origin, url) {
  const result = qRunEntityQuery(queryParamsFromUrl(url));

  const links = {
    self: `${origin}${searchHref('/api/query/entities', url.searchParams)}`,
    ui: `${origin}${searchHref('/search/ui', url.searchParams)}`,
    raw: `${origin}${searchHref('/query/entities', url.searchParams)}`,
  };

  const pageLimit = result.limit || 50;
  const pageOffset = result.offset || 0;

  if (pageOffset > 0) {
    const prevParams = new URLSearchParams(url.searchParams);
    prevParams.set('offset', String(Math.max(0, pageOffset - pageLimit)));
    links.prev = `${origin}${searchHref('/api/query/entities', prevParams)}`;
  }

  if (pageOffset + pageLimit < result.total) {
    const nextParams = new URLSearchParams(url.searchParams);
    nextParams.set('offset', String(pageOffset + pageLimit));
    links.next = `${origin}${searchHref('/api/query/entities', nextParams)}`;
  }

  const embedded = {};

  for (const item of result.items) {
    embedded[item.entity_id] = {
      _state: {
        kind: 'query_result_item',
        path: item.canonical_path,
        summary: item.display ? `Result: ${item.display}.` : 'Query result.',
        commit_seq: item.commit_seq,
      },
      _links: {
        record: stateHref(origin, item.canonical_path),
        self: stateHref(origin, item.canonical_path),
        search_related: `${origin}${searchHref('/api/query/entities', new URLSearchParams({ q: item.canonical_path, graph_scope: 'refs:1' }))}`,
      },
      data: item,
    };
  }

  return {
    _state: {
      kind: 'query_result',
      path: 'query.entities',
      summary: `${result.total} result${result.total === 1 ? '' : 's'}.`,
      protocol: {
        agent_instruction: 'Use _embedded results as navigable resources. Use _links.next for pagination. Use _actions.refine to narrow or expand the result set.',
      },
      children_total: result.total,
      children_offset: result.offset,
      children_limit: result.limit,
    },
    _links: links,
    _actions: {
      refine: searchAction(origin),
    },
    _embedded: embedded,
    data: {
      total: result.total,
      offset: result.offset,
      limit: result.limit,
      filters: result.filters,
      query: result.query,
    },
  };
}

function buildQueryHomeDoc(origin) {
  return {
    _state: {
      kind: 'query_home',
      path: 'query',
      summary: 'Query/search affordances for the hypergraph.',
      protocol: {
        agent_instruction: 'Use _actions.search. Fields describe the query grammar. The HTML search UI is only a human shortcut composer.',
      },
    },
    _links: {
      self: `${origin}/api/query`,
      entities: `${origin}/api/query/entities`,
      search: `${origin}/api/search`,
      raw_search: `${origin}/search`,
      ui: `${origin}/search/ui`,
      root: `${origin}/`,
    },
    _actions: {
      search: searchAction(origin),
    },
    data: null,
  };
}

/* -------------------------------------------------------------------------- */
/* Write Transactions                                                         */
/* -------------------------------------------------------------------------- */

function sanitizeForWrite(payload) {
  if (!isObj(payload)) return payload;

  const out = {};

  for (const [key, value] of Object.entries(payload)) {
    if (GUN_META.has(key) || value === undefined) continue;

    if (key === 'data' && isObj(value)) {
      const cleanData = {};
      for (const [dk, dv] of Object.entries(value)) {
        if (!GUN_META.has(dk) && dv !== undefined) cleanData[dk] = dv;
      }
      out.data = cleanData;
    } else {
      out[key] = value;
    }
  }

  return out;
}

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
        qDeleteByParts(parts);
        stInsertOutbox.run(parts[0], parts2slash(parts), 'del', seq, now, null);
      }
    } else {
      const clean = sanitizeForWrite(op.data || {});
      qProject(parts, clean, now, seq);
      ensurePath(parts, clean, now, seq);
      stInsertOutbox.run(parts[0], parts2slash(parts), 'put', seq, now, JSON.stringify({ data: clean, updated_at: now, commit_seq: seq }));
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

  for (const chunk of chunkOps(ops, BATCH_MAX_OPS, BATCH_MAX_BYTES)) {
    const result = txApplyOps(chunk);
    count += result.count;
    if (result.commit_seq > commitSeq) commitSeq = result.commit_seq;
  }

  return { count, commit_seq: commitSeq };
}

/* -------------------------------------------------------------------------- */
/* Live Changes                                                               */
/* -------------------------------------------------------------------------- */

function pathMatchesScope(subPath, changePath, scope) {
  if (scope === 'exact') return subPath === changePath;
  if (!subPath) return true;
  if (subPath === changePath) return true;
  return changePath.startsWith(subPath + '/');
}

let liveCursor = 0;

async function fanoutLoop() {
  if (!LIVE_ENABLED) return;

  const rows = stOutboxAfter.all(liveCursor, CHANGE_POLL_LIMIT);
  if (!rows.length) return;

  const deliver = new Map();

  for (const row of rows) {
    liveCursor = row.id;
    let payload = null;

    if (row.payload) {
      try {
        payload = JSON.parse(row.payload);
      } catch (_) {}
    }

    for (const [cid, sub] of LIVE_SUBS) {
      if (sub.root && sub.root !== row.root) continue;
      if (!pathMatchesScope(sub.path, row.path, sub.scope)) continue;
      if (row.id <= sub.outbox_id) continue;

      if (!deliver.has(cid)) deliver.set(cid, []);
      deliver.get(cid).push({
        outbox_id: row.id,
        change: {
          op: row.op_kind,
          path: slash2dp(row.path),
          data: payload ? payload.data : null,
          commit_seq: row.commit_seq,
          updated_at: row.updated_at,
        },
      });
    }
  }

  for (const [cid, entries] of deliver) {
    const sub = LIVE_SUBS.get(cid);
    if (!sub) continue;

    const changes = entries.map(entry => entry.change);
    const toSeq = changes[changes.length - 1].commit_seq;

    writeSSE(sub.res, {
      _state: {
        kind: 'delta',
        path: sub.path ? slash2dp(sub.path) : '',
        scope: sub.scope,
        from_seq: sub.commit_cursor,
        to_seq: toSeq,
        count: changes.length,
      },
      _links: {
        resync: apiHref(sub.origin, sub.path ? slash2dp(sub.path) : '', 'changes-since', { cursor: toSeq }),
      },
      changes,
    });

    sub.outbox_id = entries[entries.length - 1].outbox_id;
    sub.commit_cursor = toSeq;
  }

  let minOutboxId = Infinity;
  for (const sub of LIVE_SUBS.values()) {
    if (sub.outbox_id < minOutboxId) minOutboxId = sub.outbox_id;
  }

  if (!isFinite(minOutboxId)) minOutboxId = rows[rows.length - 1].id;
  if (minOutboxId > 0) stOutboxTrim.run(minOutboxId);
}

if (LIVE_ENABLED) {
  setInterval(() => {
    Promise.resolve(fanoutLoop()).catch(err => log('LIVE', 'fanout:', err && err.message || err));
  }, CHANGE_POLL_INTERVAL_MS);
}

/* -------------------------------------------------------------------------- */
/* HTTP Router                                                                */
/* -------------------------------------------------------------------------- */

let activeBatches = 0;

const server = http.createServer(async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') {
    res.writeHead(200);
    res.end();
    return;
  }

  const url = new URL(req.url, originFor(req));
  const pn = url.pathname.replace(/\/+$/, '') || '/';

  try {
    if (req.method === 'GET' && pn === '/health') {
      return J(res, {
        ok: true,
        role: 'relay',
        commit_seq: ((stMetaGetInt.get('commit_seq') || {}).int_value || 0),
      });
    }

    if (req.method === 'GET' && (pn === '/' || pn === '/api')) {
      return respondDoc(req, res, url, buildSystemRootDoc(url.origin));
    }

    if (req.method === 'GET' && pn === '/api/query') {
      return respondDoc(req, res, url, buildQueryHomeDoc(url.origin));
    }

    if (req.method === 'GET' && pn === '/api/query/entities') {
      return respondDoc(req, res, url, buildQueryEntitiesDoc(url.origin, url));
    }

    if (req.method === 'GET' && pn === '/api/search') {
      return respondDoc(req, res, url, buildQueryEntitiesDoc(url.origin, url));
    }

    if (req.method === 'GET' && pn === '/query/entities') {
      return J(res, qRunEntityQuery(queryParamsFromUrl(url)));
    }

    if (req.method === 'GET' && pn === '/search') {
      return J(res, qRunEntityQuery(queryParamsFromUrl(url)));
    }

    if (req.method === 'GET' && pn === '/search/ui') {
      return H(res, renderSearchPage(url.origin, url));
    }

    if (req.method === 'GET' && pn === '/search/results') {
      return H(res, renderSearchResultsHtml(url.origin, url));
    }

    if (req.method === 'POST' && pn === '/api/batch') {
      if (activeBatches >= 8) return J(res, { error: 'relay busy' }, 429);

      let body;
      try {
        body = JSON.parse(await readBody(req, MAX_HTTP_BODY_BYTES));
      } catch (err) {
        return J(res, { error: err.message || 'bad json' }, err.statusCode || 400);
      }

      const ops = body.ops || body;
      if (!Array.isArray(ops)) return J(res, { error: 'ops must be array' }, 400);

      activeBatches += 1;
      try {
        const result = applyMany(ops);
        return J(res, { ok: true, count: result.count, commit_seq: result.commit_seq });
      } finally {
        activeBatches -= 1;
      }
    }

    const rootBatch = pn.match(/^\/([^/]+)\/api\/batch$/);
    if (req.method === 'POST' && rootBatch) {
      if (activeBatches >= 8) return J(res, { error: 'relay busy' }, 429);

      let body;
      try {
        body = JSON.parse(await readBody(req, MAX_HTTP_BODY_BYTES));
      } catch (err) {
        return J(res, { error: err.message || 'bad json' }, err.statusCode || 400);
      }

      const ops = body.ops || body;
      if (!Array.isArray(ops)) return J(res, { error: 'ops must be array' }, 400);

      activeBatches += 1;
      try {
        const result = applyMany(ops);
        return J(res, { ok: true, count: result.count, commit_seq: result.commit_seq });
      } finally {
        activeBatches -= 1;
      }
    }

    const rootClear = pn.match(/^\/([^/]+)\/api\/clear$/);
    if (req.method === 'POST' && rootClear) {
      const root = decodeURIComponent(rootClear[1]);

      db.transaction(r => {
        db.prepare(`DELETE FROM nodes WHERE root = ?`).run(r);
        db.prepare(`DELETE FROM outbox WHERE root = ?`).run(r);

        const ids = db.prepare(`
          SELECT entity_id FROM q_entities
          WHERE canonical_path = ?
             OR canonical_path LIKE ?
             OR entity_id = ?
             OR entity_id LIKE ?
        `).all(r, `${r}.%`, r, `${r}.%`);

        for (const row of ids) qClearEntity(row.entity_id);
      })(root);

      return J(res, { ok: true });
    }

    const changesSince = pn.match(/^\/(.+)\/api\/changes-since$/);
    if (req.method === 'GET' && changesSince) {
      const dp = normalizeDotPath(decodeURIComponent(changesSince[1]));
      const parts = dp2parts(dp);

      if (!parts.length) return J(res, { error: 'missing path' }, 400);

      const root = parts[0];
      const subPath = parts2slash(parts);
      const cursor = Math.max(0, parseInt(url.searchParams.get('cursor') || '0', 10) || 0);
      const limit = clamp(parseIntPositive(url.searchParams.get('limit'), CHANGE_POLL_LIMIT), 1, 5000);
      const rows = stOutboxAfterForRoot.all(cursor, root, limit);
      const filtered = [];

      for (const row of rows) {
        if (!pathMatchesScope(subPath, row.path, 'subtree')) continue;

        let payload = null;
        if (row.payload) {
          try {
            payload = JSON.parse(row.payload);
          } catch (_) {}
        }

        filtered.push({
          op: row.op_kind,
          path: slash2dp(row.path),
          data: payload ? payload.data : null,
          commit_seq: row.commit_seq,
          updated_at: row.updated_at,
        });
      }

      const nextCursor = filtered.length ? filtered[filtered.length - 1].commit_seq : cursor;

      return respondDoc(req, res, url, {
        _state: {
          kind: 'change_page',
          path: dp,
          cursor,
          next_cursor: nextCursor,
          count: filtered.length,
        },
        _links: {
          self: apiHref(url.origin, dp, 'changes-since', { cursor }),
          next: filtered.length ? apiHref(url.origin, dp, 'changes-since', { cursor: nextCursor }) : null,
          stream: streamHref(url.origin, dp),
        },
        _embedded: {},
        _actions: {},
        changes: filtered,
        data: null,
      });
    }

    const childrenApi = pn.match(/^\/(.+)\/api\/children$/);
    if (req.method === 'GET' && childrenApi) {
      const dp = normalizeDotPath(decodeURIComponent(childrenApi[1]));
      const page = clamp(parseIntPositive(url.searchParams.get('page'), 1), 1, 1000000);
      const perPage = clamp(parseIntPositive(url.searchParams.get('per_page'), DEFAULT_PAGE_SIZE), 1, MAX_PAGE_SIZE);
      const order = url.searchParams.get('order') || 'key_asc';

      return respondDoc(req, res, url, buildNodeDoc(url.origin, dp, { page, perPage, order }));
    }

    const streamRoute = pn.match(/^\/(.+)\.stream$/);
    const wantsStream =
      (req.method === 'GET' && streamRoute) ||
      (req.method === 'GET' && pn !== '/' && parseBool(url.searchParams.get('stream')));

    if (wantsStream) {
      if (!LIVE_ENABLED) return J(res, { error: 'live disabled' }, 503);

      const raw = streamRoute ? decodeURIComponent(streamRoute[1]) : decodeURIComponent(pn.slice(1));
      const dp = normalizeDotPath(raw);

      if (!dp) return J(res, { error: 'missing path' }, 400);

      const parts = dp2parts(dp);
      const root = parts[0];
      const subPath = parts2slash(parts);
      const scope = url.searchParams.get('scope') === 'exact' ? 'exact' : 'subtree';
      const initialCursor = ((stMetaGetInt.get('commit_seq') || {}).int_value || 0);

      res.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
        'Access-Control-Allow-Origin': '*',
      });

      res.write(':ok\n\n');

      writeSSE(res, {
        _state: {
          kind: 'cursor',
          path: dp,
          scope,
          commit_seq: initialCursor,
        },
        _links: {
          self: streamHref(url.origin, dp, { scope }),
          snapshot: stateHref(url.origin, dp),
          resync: apiHref(url.origin, dp, 'changes-since', { cursor: initialCursor }),
          children: apiHref(url.origin, dp, 'children'),
        },
      });

      const clientId = randomId();

      LIVE_SUBS.set(clientId, {
        res,
        origin: url.origin,
        root,
        path: subPath,
        scope,
        outbox_id: liveCursor,
        commit_cursor: initialCursor,
      });

      req.on('close', () => {
        LIVE_SUBS.delete(clientId);
      });

      return;
    }

    if (req.method === 'PUT' && pn !== '/') {
      const dp = normalizeDotPath(decodeURIComponent(pn.slice(1)));

      let payload;
      try {
        payload = JSON.parse(await readBody(req, MAX_HTTP_BODY_BYTES));
      } catch (err) {
        return J(res, { error: err.message || 'bad json' }, err.statusCode || 400);
      }

      const result = applyMany([{ path: dp, data: payload }]);
      return J(res, { ok: true, count: result.count, commit_seq: result.commit_seq });
    }

    if (req.method === 'DELETE' && pn !== '/') {
      const dp = normalizeDotPath(decodeURIComponent(pn.slice(1)));
      const result = applyMany([{ path: dp, delete: true }]);
      return J(res, { ok: true, count: result.count, commit_seq: result.commit_seq });
    }

    if (req.method === 'GET' && pn !== '/') {
      const dp = normalizeDotPath(decodeURIComponent(pn.slice(1)));
      const page = clamp(parseIntPositive(url.searchParams.get('page'), 1), 1, 1000000);
      const perPage = clamp(parseIntPositive(url.searchParams.get('per_page'), DEFAULT_PAGE_SIZE), 1, MAX_PAGE_SIZE);
      const order = url.searchParams.get('order') || 'key_asc';

      return respondDoc(req, res, url, buildNodeDoc(url.origin, dp, { page, perPage, order }));
    }

    return J(res, { error: 'unknown' }, 404);
  } catch (err) {
    log('SERVER', 'request failed:', err && err.message ? err.message : err);
    return J(res, { error: err && err.message ? err.message : 'internal_error' }, err && err.statusCode ? err.statusCode : 500);
  }
});

/* -------------------------------------------------------------------------- */
/* Shutdown                                                                   */
/* -------------------------------------------------------------------------- */

server.on('error', err => {
  if (err && err.code === 'EADDRINUSE') {
    log('SERVER', `port ${PORT} already in use; another relay is probably running`);
    process.exit(0);
  }

  throw err;
});

process.on('exit', () => {
  try { db.close(); } catch (_) {}
  try { dbRead.close(); } catch (_) {}
});

process.on('SIGTERM', () => {
  try { db.close(); } catch (_) {}
  try { dbRead.close(); } catch (_) {}
  process.exit(0);
});

process.on('unhandledRejection', err => {
  log('SERVER', 'unhandledRejection:', err && err.message ? err.message : err);
});

server.listen(PORT, BIND, () => {
  log('SERVER', `http://${BIND}:${PORT}`, `sync=${SQLITE_SYNCHRONOUS}`);
});