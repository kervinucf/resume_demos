const Gun = require('gun');
const http = require('http');

if (Gun.SEA && Gun.SEA.opt) {
  Gun.SEA.opt.stringify = JSON.stringify;
  Gun.SEA.opt.parse = JSON.parse;
}
if (typeof global !== 'undefined' && global.YSON) {
  global.YSON.stringify = JSON.stringify;
  global.YSON.parse = JSON.parse;
}

const PORT = parseInt(process.env.PORT || '8765', 10);
const BIND = process.env.HYPER_BIND_HOST || '0.0.0.0';
let PEERS;
try { PEERS = JSON.parse(process.env.HYPER_PEERS || '[]'); } catch (_) { PEERS = []; }

const ADMIN_TOKEN = process.env.HYPER_ADMIN_TOKEN || '';
const ALLOW_SELF_RESTART = process.env.HYPER_ALLOW_SELF_RESTART === '1';
const runtimeConfig = {
  port: PORT,
  bind: BIND,
  peers: PEERS.slice(),
  startedAt: Date.now(),
  restartPending: false,
};
const graph = {};
const tokens = {};
const localMsgIds = new Set();
const sseClients = new Set();
const invertedIndex = Object.create(null);
const docIndex = Object.create(null);
const rootVersions = Object.create(null);

let nextEventId = 1;
let gun;

function log(tag, ...args) {
  console.log(new Date().toISOString(), `[${tag}]`, ...args);
}

function j(x) {
  try { return JSON.stringify(x); }
  catch (_) { return String(x); }
}

function isObject(v) {
  return !!v && typeof v === 'object' && !Array.isArray(v);
}

function isInternalPath(dp) {
  return dp === '_sys' || dp.startsWith('_sys.');
}

function visibleRoots() {
  return Array.from(
    new Set(
      Object.keys(graph)
        .filter(k => !isInternalPath(k))
        .map(k => k.split('.')[0])
    )
  ).sort();
}

function getNode(dp) {
  return graph[dp] || null;
}

function parentOf(dp) {
  const parts = String(dp || '').split('.');
  if (parts.length <= 1) return null;
  return parts.slice(0, -1).join('.');
}

function ancestorPaths(dp) {
  const out = [];
  let cur = dp;
  while (cur) {
    out.push(cur);
    cur = parentOf(cur);
  }
  return out;
}

function childrenOf(dp) {
  const out = new Set();
  const pfx = dp ? dp + '.' : '';
  for (const key of Object.keys(graph)) {
    if (!pfx) {
      out.add(key.split('.')[0]);
      continue;
    }
    if (!key.startsWith(pfx)) continue;
    const rest = key.slice(pfx.length);
    const head = rest.split('.')[0];
    if (head) out.add(dp + '.' + head);
  }
  return Array.from(out).sort();
}

function descendantsOf(dp) {
  const pfx = dp + '.';
  return Object.keys(graph).filter(k => k === dp || k.startsWith(pfx));
}

function mergeData(existing, incoming) {
  const out = isObject(existing) ? { ...existing } : {};
  for (const [k, v] of Object.entries(incoming || {})) {
    if (v === null || v === undefined) delete out[k];
    else out[k] = v;
  }
  return out;
}

function tokenize(text) {
  return String(text || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim()
    .split(/\s+/)
    .filter(Boolean);
}

function addSearchEntry(entries, key, value) {
  if (value == null) return;

  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    entries.push({ key, text: String(value) });
    return;
  }

  if (Array.isArray(value)) {
    const parts = [];
    for (const item of value) {
      if (item == null) continue;
      if (typeof item === 'string' || typeof item === 'number' || typeof item === 'boolean') {
        parts.push(String(item));
      } else if (isObject(item)) {
        parts.push(JSON.stringify(item));
      }
    }
    if (parts.length) entries.push({ key, text: parts.join(' ') });
    return;
  }

  if (isObject(value)) {
    for (const [k, v] of Object.entries(value)) addSearchEntry(entries, key ? key + '.' + k : k, v);
  }
}

function collectSearchEntries(dp, node) {
  const entries = [{ key: '_path', text: dp.replace(/\./g, ' ') }];
  for (const [k, v] of Object.entries(node || {})) {
    if (k === '_' || k === '#' || k === '>') continue;
    if (k === 'html' || k === 'css' || k === 'js') continue;
    if (k === 'file') {
      const meta = fileSummary(v);
      if (meta) {
        addSearchEntry(entries, 'file.name', meta.name);
        addSearchEntry(entries, 'file.type', meta.type);
      }
      continue;
    }
    addSearchEntry(entries, k, v);
  }
  return entries;
}

function removeFromIndex(dp) {
  const prev = docIndex[dp];
  if (!prev) return;
  for (const term of prev.terms) {
    const bucket = invertedIndex[term];
    if (!bucket) continue;
    bucket.delete(dp);
    if (bucket.size === 0) delete invertedIndex[term];
  }
  delete docIndex[dp];
}

function reindexPath(dp, node) {
  removeFromIndex(dp);
  if (!node) return;

  const entries = collectSearchEntries(dp, node);
  const terms = new Set();
  for (const entry of entries) {
    for (const term of tokenize(entry.text)) terms.add(term);
  }

  docIndex[dp] = { terms, entries };

  for (const term of terms) {
    if (!invertedIndex[term]) invertedIndex[term] = new Set();
    invertedIndex[term].add(dp);
  }
}

function deleteNode(dp) {
  delete graph[dp];
  removeFromIndex(dp);
}

function mergeNode(dp, incoming) {
  const ex = graph[dp] || {};
  for (const [k, v] of Object.entries(incoming || {})) {
    if (k === '_' || k === '#' || k === '>') continue;

    if (k === 'data' && isObject(v)) {
      const merged = mergeData(ex.data, v);
      if (Object.keys(merged).length === 0) delete ex.data;
      else ex.data = merged;
      continue;
    }

    if (v === null || v === undefined) delete ex[k];
    else ex[k] = v;
  }

  if (Object.keys(ex).length === 0) {
    delete graph[dp];
    removeFromIndex(dp);
    return null;
  }

  graph[dp] = ex;
  reindexPath(dp, ex);
  return ex;
}

function fileSummary(file) {
  if (file == null) return null;

  if (typeof file === 'string') {
    return {
      name: 'download.txt',
      type: 'text/plain; charset=utf-8',
      size: Buffer.byteLength(file),
      encoding: 'utf8',
    };
  }

  const data = file.data == null ? '' : String(file.data);
  const encoding = file.encoding || 'base64';
  let size = 0;
  try {
    size = encoding === 'base64' ? Buffer.from(data, 'base64').length : Buffer.byteLength(data);
  } catch (_) {
    size = Buffer.byteLength(data);
  }

  return {
    name: file.name || 'download.bin',
    type: file.type || 'application/octet-stream',
    size,
    encoding,
  };
}

function fileBuffer(file) {
  if (file == null) return Buffer.alloc(0);
  if (typeof file === 'string') return Buffer.from(file, 'utf8');

  const data = file.data == null ? '' : String(file.data);
  const encoding = file.encoding || 'base64';
  try {
    return encoding === 'base64' ? Buffer.from(data, 'base64') : Buffer.from(data, 'utf8');
  } catch (_) {
    return Buffer.from(data, 'utf8');
  }
}

function intersectPostingSets(sets) {
  if (!sets.length) return new Set();
  const ordered = sets.slice().sort((a, b) => a.size - b.size);
  const out = new Set(ordered[0]);
  for (let i = 1; i < ordered.length; i++) {
    for (const v of Array.from(out)) {
      if (!ordered[i].has(v)) out.delete(v);
    }
  }
  return out;
}

function searchSubtree(rootDp, query, limit = 50) {
  const raw = String(query || '').trim();
  const terms = tokenize(raw);
  if (!terms.length) return [];

  const postingSets = [];
  for (const term of terms) {
    const bucket = invertedIndex[term];
    if (!bucket) return [];
    postingSets.push(bucket);
  }

  const candidates = intersectPostingSets(postingSets);
  const results = [];

  for (const dp of candidates) {
    if (!(dp === rootDp || dp.startsWith(rootDp + '.'))) continue;
    const doc = docIndex[dp];
    if (!doc) continue;

    let score = 0;
    let match = '_path';
    let excerpt = dp;

    for (const entry of doc.entries) {
      const hay = String(entry.text || '').toLowerCase();
      let matched = false;
      if (hay.includes(raw.toLowerCase())) matched = true;
      else if (terms.some(t => hay.includes(t))) matched = true;

      if (matched) {
        score += 1;
        if (match === '_path') {
          match = entry.key;
          excerpt = String(entry.text).slice(0, 160);
        }
      }
    }

    results.push({ path: dp, score, match, excerpt });
  }

  results.sort((a, b) => b.score - a.score || a.path.localeCompare(b.path));
  return results.slice(0, limit);
}

function buildTree(rootDp) {
  const actual = new Set();

  for (const dp of Object.keys(graph)) {
    if (isInternalPath(dp)) continue;
    if (dp === rootDp || dp.startsWith(rootDp + '.')) actual.add(dp);
  }

  const nodes = Object.create(null);

  function ensure(path) {
    if (!nodes[path]) nodes[path] = { path, type: actual.has(path) ? 'node' : 'branch', children: [] };
    else if (actual.has(path)) nodes[path].type = 'node';
    return nodes[path];
  }

  ensure(rootDp);

  for (const dp of Array.from(actual)) {
    const rootParts = rootDp.split('.');
    const parts = dp.split('.');
    for (let i = rootParts.length; i <= parts.length; i++) {
      const sub = parts.slice(0, i).join('.');
      if (sub === rootDp || sub.startsWith(rootDp + '.')) ensure(sub);
    }
  }

  const attached = new Set();
  for (const path of Object.keys(nodes).sort()) {
    if (path === rootDp) continue;
    const parent = parentOf(path);
    if (!parent || !nodes[parent]) continue;
    const key = parent + '->' + path;
    if (attached.has(key)) continue;
    attached.add(key);
    nodes[parent].children.push(nodes[path]);
  }

  function sortNode(node) {
    node.children.sort((a, b) => a.path.localeCompare(b.path));
    for (const child of node.children) sortNode(child);
    return node;
  }

  return sortNode(nodes[rootDp]);
}

function ensureRootVersion(root) {
  if (!rootVersions[root]) rootVersions[root] = { version: 0, tree_version: 0 };
  return rootVersions[root];
}

function nodeForRead(node) {
  if (!node) return null;
  const out = {};
  for (const [k, v] of Object.entries(node)) {
    if (k === 'file' && v != null) out.file = fileSummary(v);
    else out[k] = v;
  }
  return out;
}

function pathURL(origin, dp, suffix = '') {
  return (origin || '') + '/' + encodeURIComponent(dp) + suffix;
}

function buildResponse(dp, origin) {
  const node = nodeForRead(getNode(dp));
  const links = {
    self: pathURL(origin, dp),
    stream: pathURL(origin, dp, '.stream'),
    events: pathURL(origin, dp, '.events'),
  };

  const parent = parentOf(dp);
  if (parent) links.parent = pathURL(origin, parent);

  for (const child of childrenOf(dp).filter(c => !isInternalPath(c))) {
    links[child.split('.').pop()] = pathURL(origin, child);
  }

  if (node && node.file) links.download = pathURL(origin, dp, '.download');
  return { _path: dp, _links: links, ...(node || {}) };
}

function resolve(dp, origin) {
  const exact = getNode(dp);
  if (exact) return { type: 'node', data: buildResponse(dp, origin) };

  const parts = dp.split('.');
  for (let i = parts.length - 1; i >= 1; i--) {
    const nodeDp = parts.slice(0, i).join('.');
    const fieldPath = parts.slice(i);
    const node = getNode(nodeDp);
    if (!node) continue;

    let val = node;
    for (const key of fieldPath) {
      if (val && typeof val === 'object') val = val[key];
      else { val = undefined; break; }
    }

    if (val === undefined) continue;
    if (fieldPath.length === 1 && fieldPath[0] === 'file') return { type: 'json', data: fileSummary(val) };
    return { type: typeof val === 'object' ? 'json' : 'raw', data: val };
  }

  if (childrenOf(dp).filter(c => !isInternalPath(c)).length > 0) {
    return { type: 'node', data: buildResponse(dp, origin) };
  }

  return null;
}

function resolveDownload(dp) {
  const exact = getNode(dp);
  if (exact && exact.file != null) {
    const meta = fileSummary(exact.file);
    return {
      body: fileBuffer(exact.file),
      type: meta.type || 'application/octet-stream',
      name: meta.name || 'download.bin',
    };
  }

  const parts = dp.split('.');
  for (let i = parts.length; i >= 1; i--) {
    const nodeDp = parts.slice(0, i).join('.');
    const fieldPath = parts.slice(i);
    const node = getNode(nodeDp);
    if (!node) continue;

    let val = node;
    for (const key of fieldPath) {
      if (val && typeof val === 'object') val = val[key];
      else { val = undefined; break; }
    }
    if (val === undefined) continue;

    if (typeof val === 'string') {
      return {
        body: Buffer.from(val, 'utf8'),
        type: val.trim().startsWith('<') ? 'text/html; charset=utf-8' : 'text/plain; charset=utf-8',
        name: 'download.txt',
      };
    }

    return {
      body: Buffer.from(JSON.stringify(val, null, 2), 'utf8'),
      type: 'application/json; charset=utf-8',
      name: 'download.json',
    };
  }

  return null;
}

function nearestDataNode(dp) {
  let cur = dp;
  while (cur) {
    const node = getNode(cur);
    if (node && isObject(node.data)) return cur;
    cur = parentOf(cur);
  }
  return null;
}

function dotToScenePath(dp) {
  const parts = dp.split('.');
  return { root: parts[0], scenePath: parts.length > 1 ? parts.slice(1).join('/') : '__root__' };
}

function soulToDotPath(soul) {
  const parts = soul.split('/');
  if (!(parts.length >= 3 && parts[1] === 'scene')) return null;
  const root = parts[0];
  const scenePath = parts.slice(2).join('/');
  if (scenePath === '__root__') return root;
  return root + '.' + scenePath.replace(/\//g, '.');
}

function buildGunNode(put, soul, obj, state) {
  const node = { _: { '#': soul, '>': {} } };

  for (const [k, v] of Object.entries(obj || {})) {
    if (k === '_' || k === '#' || k === '>') continue;

    if (isObject(v)) {
      const childSoul = soul + '/' + k;
      node[k] = { '#': childSoul };
      node._['>'][k] = state;
      buildGunNode(put, childSoul, v, state);
      continue;
    }

    node[k] = v;
    node._['>'][k] = state;
  }

  put[soul] = node;
}

function gunPut(dp, data) {
  const { root, scenePath } = dotToScenePath(dp);
  const soul = root + '/scene/' + scenePath;
  const parentSoul = root + '/scene';
  const state = Gun.state();
  const put = {};

  buildGunNode(put, soul, data, state);

  const parentNode = { _: { '#': parentSoul, '>': {} } };
  parentNode[scenePath] = { '#': soul };
  parentNode._['>'][scenePath] = state;

  const rootNode = { _: { '#': root, '>': {} } };
  rootNode.scene = { '#': parentSoul };
  rootNode._['>'].scene = state;

  put[parentSoul] = parentNode;
  put[root] = rootNode;

  const id = 'srv_' + Math.random().toString(36).slice(2, 11);
  const msg = { put, '#': id };
  localMsgIds.add(id);
  log('GUN-PUT', dp, j(data));
  gun._.on('in', msg);
  gun._.on('out', msg);
  return id;
}

function gunNullOut(dp) {
  const node = getNode(dp);
  if (!node) return null;
  const tomb = {};
  for (const k of Object.keys(node)) tomb[k] = null;
  log('GUN-TOMB', dp, Object.keys(tomb));
  return gunPut(dp, tomb);
}

function materializeGunValue(val, put) {
  if (!isObject(val)) return val;
  const keys = Object.keys(val);
  if (keys.length === 1 && keys[0] === '#' && put[val['#']]) {
    return cleanGunNode(put[val['#']], put);
  }
  const out = {};
  for (const [k, v] of Object.entries(val)) {
    if (k === '_' || k === '#' || k === '>') continue;
    out[k] = materializeGunValue(v, put);
  }
  return out;
}

function cleanGunNode(node, put) {
  if (!node || typeof node !== 'object') return node;
  const out = {};
  for (const [k, v] of Object.entries(node)) {
    if (k === '_' || k === '#' || k === '>') continue;
    if (v === null || v === undefined) continue;
    out[k] = materializeGunValue(v, put);
  }
  return out;
}

function impactsPath(mutatedDp, subPath) {
  return mutatedDp === subPath || mutatedDp.startsWith(subPath + '.') || subPath.startsWith(mutatedDp + '.');
}

function sendSSE(res, eventName, payload) {
  const id = nextEventId++;
  res.write('id: ' + id + '\n');
  res.write('event: ' + eventName + '\n');
  const text = JSON.stringify(payload);
  for (const line of text.split('\n')) res.write('data: ' + line + '\n');
  res.write('\n');
}

function emitResolved(client, eventName = 'update') {
  const resolved = resolve(client.path, client.origin);
  if (!resolved) {
    sendSSE(client.res, eventName, { path: client.path, kind: 'missing', data: null });
    return;
  }
  sendSSE(client.res, eventName, { path: client.path, kind: resolved.type, data: resolved.data });
}

function notifyImpacted(mutatedDp) {
  log('SSE-NOTIFY', mutatedDp, 'clients', sseClients.size);
  for (const client of Array.from(sseClients)) {
    if (!impactsPath(mutatedDp, client.path)) continue;
    try {
      emitResolved(client, 'update');
    } catch (_) {
      try { client.res.end(); } catch (_) {}
      sseClients.delete(client);
    }
  }
}

setInterval(function () {
  for (const client of Array.from(sseClients)) {
    try { client.res.write(': ping\n\n'); }
    catch (_) {
      try { client.res.end(); } catch (_) {}
      sseClients.delete(client);
    }
  }
}, 25000);

function publishInternalJSON(dp, obj) {
  mergeNode(dp, { json: JSON.stringify(obj), t: Date.now() });
  gunPut(dp, graph[dp] || {});
}

function deleteInternal(dp) {
  if (!getNode(dp)) return;
  gunNullOut(dp);
  deleteNode(dp);
}

function adminStateObject() {
  return {
    ok: true,
    current: { bind: BIND, port: PORT },
    desired: { bind: BIND, port: PORT },
    peers: PEERS.slice(),
    restart_pending: ALLOW_SELF_RESTART ? false : false,
    uptime_ms: Date.now() - runtimeConfig.startedAt,
  };
}

function syncRootsMetadata() {
  publishInternalJSON('_sys.roots', visibleRoots());
}

function syncAdminState() {
  publishInternalJSON('_sys.admin.state', adminStateObject());
}

function syncViewNode(dp) {
  if (isInternalPath(dp)) return;
  const node = getNode(dp);
  const kids = childrenOf(dp).filter(c => !isInternalPath(c));
  if (!node && !kids.length) {
    deleteInternal('_sys.view.' + dp);
    return;
  }
  publishInternalJSON('_sys.view.' + dp, buildResponse(dp, ''));
}

function syncTreeNode(dp) {
  if (isInternalPath(dp)) return;
  const node = getNode(dp);
  const kids = childrenOf(dp).filter(c => !isInternalPath(c));
  if (!node && !kids.length) {
    deleteInternal('_sys.tree.' + dp);
    return;
  }
  publishInternalJSON('_sys.tree.' + dp, {
    path: dp,
    type: node ? 'node' : 'branch',
    children: kids,
  });
}

function bumpRootMeta(root, treeChanged) {
  if (!root) return;
  const meta = ensureRootVersion(root);
  meta.version += 1;
  if (treeChanged) meta.tree_version += 1;
  log('META', root, j(meta));
  publishInternalJSON('_sys.meta.' + root, { root, version: meta.version, tree_version: meta.tree_version });
}

function syncMetadataForPaths(paths, opts = {}) {
  const toSync = new Set();
  const rootsTouched = new Set();

  for (const dp of paths) {
    if (!dp || isInternalPath(dp)) continue;
    rootsTouched.add(dp.split('.')[0]);
    for (const p of ancestorPaths(dp)) toSync.add(p);
  }

  for (const dp of Array.from(toSync).sort()) {
    syncViewNode(dp);
    syncTreeNode(dp);
  }

  for (const root of rootsTouched) {
    bumpRootMeta(root, !!opts.treeChanged);
  }

  syncRootsMetadata();
  syncAdminState();
}

function hasGraphAccess(root, token) {
  if (ADMIN_TOKEN && token === ADMIN_TOKEN) return true;
  const tok = tokens[root];
  if (!tok) return true;
  return token === tok;
}

function hasAdminAccess(token) {
  if (!ADMIN_TOKEN) return true;
  return token === ADMIN_TOKEN;
}

function cleanUserPayload(payload) {
  const out = { ...(payload || {}) };
  delete out._path;
  delete out._links;
  return out;
}

function processSearchRequest(dp, node) {
  if (!dp.startsWith('_sys.query.request.')) return false;
  const outDp = dp.replace('_sys.query.request.', '_sys.query.result.');
  if (getNode(outDp)) return true;

  const root = String(node.root || '').trim();
  const q = String(node.q || '').trim();
  const limit = Math.min(parseInt(node.limit || '50', 10) || 50, 200);
  const results = root && q ? searchSubtree(root, q, limit) : [];
  log('QUERY', root, q, 'count', results.length);

  publishInternalJSON(outDp, {
    ok: true,
    root,
    query: q,
    count: results.length,
    results,
    request_ts: node.ts || Date.now(),
  });
  return true;
}

function processWriteRequest(dp, node) {
  if (!dp.startsWith('_sys.write.request.')) return false;
  const outDp = dp.replace('_sys.write.request.', '_sys.write.result.');
  if (getNode(outDp)) return true;

  const token = String(node.token || '');
  const path = String(node.path || '').trim();
  const op = String(node.op || '').trim();

  log('WRITE-REQ', dp, 'op', op, 'path', path);

  if (!path || isInternalPath(path)) {
    publishInternalJSON(outDp, { ok: false, error: 'invalid path' });
    return true;
  }

  const root = path.split('.')[0];
  if (!hasGraphAccess(root, token)) {
    publishInternalJSON(outDp, { ok: false, error: 'unauthorized' });
    return true;
  }

  if (op === 'delete') {
    const removed = descendantsOf(path);
    for (const d of removed) { gunNullOut(d); deleteNode(d); }
    notifyImpacted(path);
    syncMetadataForPaths([path, parentOf(path) || path, ...removed], { treeChanged: removed.length > 0 });
    publishInternalJSON(outDp, { ok: true, op, path, removed: removed.length });
    return true;
  }

  if (op === 'put') {
    let payload;
    try { payload = JSON.parse(String(node.payload_json || '{}')); }
    catch (_) {
      publishInternalJSON(outDp, { ok: false, error: 'invalid payload_json' });
      return true;
    }
    payload = cleanUserPayload(payload);
    const existed = !!getNode(path);
    mergeNode(path, payload);
    gunPut(path, graph[path] || payload);
    notifyImpacted(path);
    syncMetadataForPaths([path, parentOf(path) || path], { treeChanged: !existed && !!getNode(path) });
    publishInternalJSON(outDp, { ok: true, op, path });
    return true;
  }

  publishInternalJSON(outDp, { ok: false, error: 'unknown op' });
  return true;
}

function processAdminRequest(dp, node) {
  if (!dp.startsWith('_sys.admin.request.')) return false;
  const outDp = dp.replace('_sys.admin.request.', '_sys.admin.result.');
  if (getNode(outDp)) return true;

  const token = String(node.token || '');
  if (!hasAdminAccess(token)) {
    publishInternalJSON(outDp, { ok: false, error: 'unauthorized' });
    return true;
  }

  const op = String(node.op || '').trim();
  log('ADMIN-REQ', dp, 'op', op);

  if (op === 'restart') {
    if (!ALLOW_SELF_RESTART) {
      publishInternalJSON(outDp, {
        ok: false,
        error: 'self restart disabled; run under a supervisor or set HYPER_ALLOW_SELF_RESTART=1',
      });
      return true;
    }
    publishInternalJSON(outDp, { ok: true, restarting: true });
    setTimeout(() => process.exit(0), 150);
    return true;
  }

  if (op === 'set_peers') {
    let peers;
    try { peers = JSON.parse(String(node.peers_json || '[]')); }
    catch (_) {
      publishInternalJSON(outDp, { ok: false, error: 'invalid peers_json' });
      return true;
    }
    PEERS = Array.isArray(peers) ? peers.map(String) : [];
    publishInternalJSON(outDp, { ok: true, peers: PEERS.slice() });
    return true;
  }

  publishInternalJSON(outDp, { ok: false, error: 'unknown admin op' });
  return true;
}

function processInternalRequest(dp, node) {
  if (!isInternalPath(dp)) return false;

  if (
    dp === '_sys.roots' ||
    dp === '_sys.admin.state' ||
    dp.startsWith('_sys.meta.') ||
    dp.startsWith('_sys.view.') ||
    dp.startsWith('_sys.tree.') ||
    dp.startsWith('_sys.query.result.') ||
    dp.startsWith('_sys.write.result.') ||
    dp.startsWith('_sys.admin.result.')
  ) return true;

  if (dp.startsWith('_sys.query.request.')) return processSearchRequest(dp, node);
  if (dp.startsWith('_sys.write.request.')) return processWriteRequest(dp, node);
  if (dp.startsWith('_sys.admin.request.')) return processAdminRequest(dp, node);

  return true;
}
const seenPutIds = new Map();

function rememberPut(id) {
  if (!id) return false;
  const now = Date.now();
  seenPutIds.set(id, now);

  if (seenPutIds.size > 5000) {
    const cutoff = now - 60_000;
    for (const [k, t] of seenPutIds) {
      if (t < cutoff) seenPutIds.delete(k);
    }
  }
  return true;
}

function alreadySeenPut(id) {
  if (!id) return false;
  return seenPutIds.has(id);
}

function ingestPutMessage(msg, source) {
  if (!msg || !msg.put) return;

  const msgId = msg['#'] || null;
  if (msgId && alreadySeenPut(msgId)) return;
  if (msgId) rememberPut(msgId);

  const souls = Object.keys(msg.put || {});
  log('INGEST-' + source.toUpperCase(), 'id=', msgId, 'souls=', souls);

  const normalTouched = [];
  const treeRoots = new Set();

  for (const soul of souls) {
    const dp = soulToDotPath(soul);
    if (!dp) continue;

    const existed = !!getNode(dp);
    const clean = cleanGunNode(msg.put[soul], msg.put);
    if (!clean || !Object.keys(clean).length) continue;

    log('MERGE-' + source.toUpperCase(), dp, clean);

    mergeNode(dp, clean);

    if (isInternalPath(dp)) {
      processInternalRequest(dp, getNode(dp) || {});
    } else {
      normalTouched.push(dp);
      if (!existed && !!getNode(dp)) treeRoots.add(dp.split('.')[0]);
      notifyImpacted(dp);
    }
  }

  if (normalTouched.length) {
    syncMetadataForPaths(normalTouched, { treeChanged: treeRoots.size > 0 });
  }
}
function initGunSync() {
  gun.on('in', function (msg) {
    if (msg && msg.put) {
      log('GUN-IN', 'id=', msg['#'], 'souls=', Object.keys(msg.put));
      ingestPutMessage(msg, 'in');
    }
    this.to.next(msg);
  });

  gun.on('out', function (msg) {
    if (msg && msg.put) {
      log('GUN-OUT', 'id=', msg['#'], 'souls=', Object.keys(msg.put));
      ingestPutMessage(msg, 'out');
    }
    this.to.next(msg);
  });
}
function snapshotForRoot(rootName) {
  const snap = {};
  const pfx = rootName + '.';
  for (const [dp, data] of Object.entries(graph)) {
    if (!dp.startsWith(pfx)) continue;
    if (isInternalPath(dp)) continue;
    snap[dp.slice(pfx.length).replace(/\./g, '/')] = nodeForRead(data);
  }
  return snap;
}

function relayMeta(origin) {
  const roots = visibleRoots();
  return {
    relay: true,
    buckets: roots,
    roots,
    bind: BIND,
    port: PORT,
    peers: PEERS.slice(),
    uptime_ms: Date.now() - runtimeConfig.startedAt,
    capabilities: {
      view: true,
      edit: true,
      admin: !ADMIN_TOKEN,
      admin_configured: !!ADMIN_TOKEN,
    },
    _links: Object.fromEntries(roots.map(r => [r, pathURL(origin, r)])),
  };
}

function checkBearer(req, token, parsed) {
  if (!token) return true;
  const auth = req.headers['authorization'] || '';
  if (auth === 'Bearer ' + token) return true;
  if (parsed && parsed.searchParams && parsed.searchParams.get('token') === token) return true;
  return false;
}

function checkAuth(dp, req, parsed) {
  const tok = tokens[dp.split('.')[0]];
  if (!tok) return true;
  return checkBearer(req, tok, parsed) || (!!ADMIN_TOKEN && checkBearer(req, ADMIN_TOKEN, parsed));
}

function checkAdmin(req, parsed) {
  return checkBearer(req, ADMIN_TOKEN, parsed);
}

function sendJson(res, obj, status = 200) {
  if (res.headersSent) return;
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Access-Control-Allow-Origin': '*',
  });
  res.end(JSON.stringify(obj, null, 2));
}

function sendRaw(res, value) {
  if (res.headersSent) return;
  const str = String(value);
  const type = str.trim().startsWith('<') ? 'text/html; charset=utf-8' : 'text/plain; charset=utf-8';
  res.writeHead(200, {
    'Content-Type': type,
    'Access-Control-Allow-Origin': '*',
  });
  res.end(str);
}

function sendBuffer(res, body, type, name) {
  if (res.headersSent) return;
  res.writeHead(200, {
    'Content-Type': type || 'application/octet-stream',
    'Content-Length': body.length,
    'Content-Disposition': 'inline; filename="' + (name || 'download.bin') + '"',
    'Access-Control-Allow-Origin': '*',
  });
  res.end(body);
}

function readBody(req) {
  return new Promise(resolve => {
    let data = '';
    req.on('data', chunk => data += chunk);
    req.on('end', () => resolve(data));
  });
}

function stripSuffix(path, suffix) {
  return path.endsWith(suffix) ? path.slice(0, -suffix.length) : null;
}

function browserPutHelperJS() {
  return `
function hyperScenePut(gun, root, sceneKey, payload){
  console.log('[BROWSER PUT] root=', root, 'sceneKey=', sceneKey, 'payload=', payload);
  return new Promise(function(resolve, reject){
    gun.get(root).get('scene').get(sceneKey).put(payload, function(ack){
      console.log('[BROWSER PUT ACK]', sceneKey, ack);
      if(ack && ack.err){
        reject(new Error(ack.err));
        return;
      }
      resolve(ack || {ok:true});
    });
  });
}
`;
}

function resolveStreamTarget(dp) {
  if (getNode(dp) || childrenOf(dp).filter(c => !isInternalPath(c)).length > 0) {
    return { nodeDp: dp, fieldPath: [] };
  }

  const parts = dp.split('.');
  for (let i = parts.length - 1; i >= 1; i--) {
    const nodeDp = parts.slice(0, i).join('.');
    const fieldPath = parts.slice(i);
    if (getNode(nodeDp)) return { nodeDp, fieldPath };
  }

  return { nodeDp: dp, fieldPath: [] };
}

function connectHTML(dp) {
  const peersJSON = JSON.stringify(PEERS);
  const target = resolveStreamTarget(dp);
  const bindDp = nearestDataNode(target.nodeDp);
  const nodeMap = dotToScenePath(target.nodeDp);
  const bindMap = bindDp ? dotToScenePath(bindDp) : null;
  const isBranch = !getNode(target.nodeDp) && childrenOf(target.nodeDp).filter(c => !isInternalPath(c)).length > 0;
  const helperJS = browserPutHelperJS();

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>${dp}</title>
<script src="https://cdn.jsdelivr.net/npm/gun/gun.js"><\/script>
<style>
html,body,#app{margin:0;padding:0;width:100%;height:100%}
body{font-family:system-ui,sans-serif}
pre{margin:0;padding:12px;white-space:pre-wrap;word-break:break-word}
</style>
</head>
<body>
<div id="app"></div>
<script>
(function(){
  var ALL_PEERS=${peersJSON};
  var local=location.origin + '/gun';
  if(ALL_PEERS.indexOf(local)===-1) ALL_PEERS.unshift(local);

  var gun=Gun({peers:ALL_PEERS});
  var app=document.getElementById('app');
  var AF=Object.getPrototypeOf(async function(){}).constructor;

  var nodeDp=${JSON.stringify(target.nodeDp)};
  var fieldPath=${JSON.stringify(target.fieldPath || [])};
  var isBranch=${JSON.stringify(isBranch)};
  var nodeRoot=${JSON.stringify(nodeMap.root)};
  var nodeScenePath=${JSON.stringify(nodeMap.scenePath)};
  var bindRoot=${JSON.stringify(bindMap ? bindMap.root : null)};
  var bindScenePath=${JSON.stringify(bindMap ? bindMap.scenePath : null)};

  var currentNode={};
  var currentData={};
  var branchSnap={};
  var childSnap={};
  var childLive={root:app};
  var childJsH={};
  var childDataSnap={};
  var childDataOff={};

  ${helperJS}

  console.log('[STREAM BOOT]', {
    nodeDp: nodeDp,
    nodeRoot: nodeRoot,
    nodeScenePath: nodeScenePath,
    bindRoot: bindRoot,
    bindScenePath: bindScenePath,
    fieldPath: fieldPath,
    isBranch: isBranch,
    peers: ALL_PEERS
  });

  window.action=function(payload){
    var suffix=Date.now() + '_' + Math.random().toString(36).slice(2,7);
    var sceneKey='inbox/' + suffix;
    console.log('[STREAM ACTION]', payload, 'sceneKey=', sceneKey);
    return hyperScenePut(gun, nodeRoot, sceneKey, {
      data: JSON.stringify(payload || {})
    }).then(function(ack){
      console.log('[STREAM ACTION SENT]', sceneKey, ack);
      return {ok:true,key:nodeRoot + '.inbox.' + suffix};
    }).catch(function(err){
      console.error('[STREAM ACTION FAIL]', sceneKey, err);
      throw err;
    });
  };

  function clean(node){
    if(!node || typeof node!=='object') return {};
    var out={};
    Object.keys(node).forEach(function(k){
      if(k==='_'||k==='#'||k==='>') return;
      if(node[k]!==null && node[k]!==undefined) out[k]=node[k];
    });
    return out;
  }

  function bindData(root,data){
    data=data||{};
    var nodes=root.querySelectorAll('[data-bind-text],[data-bind-html],[data-bind-style]');
    for(var i=0;i<nodes.length;i++){
      var el=nodes[i];
      if(el.dataset.bindText && data[el.dataset.bindText]!==undefined) el.textContent=data[el.dataset.bindText];
      if(el.dataset.bindHtml && data[el.dataset.bindHtml]!==undefined) el.innerHTML=data[el.dataset.bindHtml];
      if(el.dataset.bindStyle){
        var pairs=el.dataset.bindStyle.split(';');
        for(var j=0;j<pairs.length;j++){
          var pp=pairs[j].split(':');
          if(pp[0] && pp[1] && data[pp[1].trim()]!==undefined){
            el.style[pp[0].trim()] = data[pp[1].trim()];
          }
        }
      }
    }
  }

  function dig(v,path,start){
    for(var i=(start||0);i<path.length;i++){
      if(v==null) return undefined;
      v=v[path[i]];
    }
    return v;
  }

  function renderValue(v){
    console.log('[STREAM RENDER VALUE]', v);
    if(v==null){ app.textContent=''; return; }
    if(typeof v==='string' || typeof v==='number' || typeof v==='boolean'){
      app.textContent=String(v);
      return;
    }
    app.innerHTML='<pre>' + JSON.stringify(v,null,2) + '</pre>';
  }

  function chainFor(root,scenePath,segments){
    var c=gun.get(root).get('scene').get(scenePath);
    for(var i=0;i<segments.length;i++) c=c.get(segments[i]);
    return c;
  }

  function qh(s){
    var h=0;
    for(var i=0;i<s.length;i++) h=((h<<5)-h+s.charCodeAt(i))|0;
    return h;
  }

  function hasRenderable(d){
    return d && (d.html!=null || d.css!=null || d.js!=null || d.data!=null);
  }

  function childKeyId(k){ return 'frag-' + String(k).replace(/[^\\w~-]/g,'_'); }
  function childCssId(k){ return 'css-child-' + String(k).replace(/[^\\w~-]/g,'_'); }

  function childParent(k){
    var parts=String(k).split('/').filter(Boolean);
    return parts.length<=1 ? null : parts.slice(0,-1).join('/');
  }

  function childHost(k){
    return childLive[k] || document.getElementById(childKeyId(k)) || null;
  }

  function rootChildrenHost(){
    return app.querySelector('[data-children]') || app;
  }

  function childRootStyle(el,layer){
    el.style.position='fixed';
    el.style.inset='0';
    el.style.zIndex=String(layer||0);
    el.style.pointerEvents=(layer||0)<=0?'none':'auto';
  }

  function childNormalStyle(el,layer){
    el.style.position='relative';
    el.style.flex='0 0 auto';
    el.style.minWidth='0';
    el.style.zIndex=String(layer||0);
    el.style.pointerEvents='auto';
  }

  function ensureChild(key,res,dat){
    if(childLive[key]) return childLive[key];

    var el=document.createElement('div');
    el.id=childKeyId(key);
    el.dataset.key=key;

    var layer=Number((res&&res.layer)||(dat&&dat.layer)||0)||0;
    var pk=childParent(key);
    var forceRoot=!!((res&&res.fixed)||(dat&&dat.fixed)||(res&&res.portal)||(dat&&dat.portal));
    var parent=pk?childHost(pk):rootChildrenHost();

    if(parent && !forceRoot){
      childNormalStyle(el,layer);
      (parent.querySelector('[data-children]') || parent).appendChild(el);
    } else {
      childRootStyle(el,layer);
      app.appendChild(el);
    }

    childLive[key]=el;
    console.log('[STREAM CHILD ENSURE]', key);
    return el;
  }

  function restyleChild(el,key,res,dat){
    var layer=Number((res&&res.layer)||(dat&&dat.layer)||0)||0;
    var pk=childParent(key);
    var forceRoot=!!((res&&res.fixed)||(dat&&dat.fixed)||(res&&res.portal)||(dat&&dat.portal));
    var parent=pk?childHost(pk):rootChildrenHost();

    if(parent && !forceRoot){
      childNormalStyle(el,layer);
      var mount=parent.querySelector('[data-children]')||parent;
      if(el.parentElement!==mount) mount.appendChild(el);
    } else {
      childRootStyle(el,layer);
      if(el.parentElement!==app) app.appendChild(el);
    }
  }

  function unsubscribeChildData(key){
    if(childDataOff[key]){
      try{ childDataOff[key].off(); }catch(_){}
      delete childDataOff[key];
    }
    delete childDataSnap[key];
  }

  function subscribeChildData(key){
    if(childDataOff[key]) return;
    console.log('[STREAM CHILD DATA SUB]', key);
    childDataOff[key] = gun.get(nodeRoot).get('scene').get(nodeScenePath + '/' + key).get('data').on(function(d){
      childDataSnap[key]=clean(d);
      console.log('[STREAM CHILD DATA]', key, childDataSnap[key]);
      var host=childLive[key];
      if(host) bindData(host, childDataSnap[key]);
    });
  }

  function removeChildBranch(key){
    var pfx=key + '/';
    Object.keys(childLive).forEach(function(k){
      if(k==='root') return;
      if(k===key || k.indexOf(pfx)===0){
        if(childLive[k]) childLive[k].remove();
        delete childLive[k];
        var css=document.getElementById(childCssId(k));
        if(css) css.remove();
        delete childJsH[k];
        unsubscribeChildData(k);
      }
    });
  }

  function renderChildNode(key,dat){
    console.log('[STREAM CHILD RENDER]', key, dat);
    if(!hasRenderable(dat)){ removeChildBranch(key); return; }

    if(dat.css!=null){
      var s=document.getElementById(childCssId(key));
      if(!s){
        s=document.createElement('style');
        s.id=childCssId(key);
        document.head.appendChild(s);
      }
      s.textContent=String(dat.css);
    }

    if(dat.html!=null){
      var el=ensureChild(key,dat,dat);
      restyleChild(el,key,dat,dat);
      el.innerHTML=String(dat.html);

      subscribeChildData(key);

      var bindSource =
        childDataSnap[key] ||
        ((dat.data && typeof dat.data==='object' && !dat.data['#']) ? dat.data : dat);

      bindData(el, bindSource);
    }

    if(dat.js!=null){
      var hh=qh(String(dat.js));
      if(childJsH[key]!==hh){
        childJsH[key]=hh;
        try{ new AF(String(dat.js))(); }catch(e){ console.error('[STREAM CHILD JS FAIL]', key, e); }
      }
    }
  }

  function rebuildChildren(){
    console.log('[STREAM REBUILD CHILDREN]', Object.keys(childSnap));
    Object.keys(childLive).forEach(function(k){
      if(k==='root') return;
      if(childLive[k]) childLive[k].remove();
    });

    Object.keys(childJsH).forEach(function(k){ delete childJsH[k]; });

    var styles=document.querySelectorAll('style[id^="css-child-"]');
    for(var i=0;i<styles.length;i++) styles[i].remove();

    childLive={root:app};

    Object.keys(childSnap)
      .sort(function(a,b){ return a.split('/').length - b.split('/').length; })
      .forEach(function(k){ renderChildNode(k, childSnap[k]); });
  }

  function runNodeJS(){
    if(!currentNode || currentNode.js == null) return;
    console.log('[STREAM NODE JS RUN]');
    try{ new AF(String(currentNode.js))(); }catch(e){ console.error('[STREAM NODE JS FAIL]', e); }
  }

  function render(){
    console.log('[STREAM RENDER]', {isBranch:isBranch, node:currentNode, data:currentData});
    if(isBranch){ renderValue(branchSnap); return; }

    if(currentNode.html!=null){
      app.innerHTML=String(currentNode.html);
      bindData(app, currentData || currentNode.data || {});
      runNodeJS();
      rebuildChildren();
      return;
    }

    if(currentNode.data!==undefined){
      renderValue(currentNode.data);
      return;
    }

    renderValue(currentNode);
  }

  if(isBranch){
    gun.get(nodeRoot).get('scene').map().on(function(d,k){
      if(!k || k==='_') return;
      var dp2=(k==='__root__')?nodeRoot:(nodeRoot+'.'+String(k).replace(/\\\//g,'.'));
      if(!(dp2===nodeDp || dp2.indexOf(nodeDp+'.')===0)) return;
      var cleaned=clean(d);
      if(cleaned && Object.keys(cleaned).length) branchSnap[dp2]=cleaned;
      else delete branchSnap[dp2];
      console.log('[STREAM BRANCH UPDATE]', dp2, cleaned);
      renderValue(branchSnap);
    });
    return;
  }

  gun.get(nodeRoot).get('scene').map().on(function(d,k){
    if(!k || k==='_' || k===nodeScenePath) return;
    if(k.indexOf(nodeScenePath + '/') !== 0) return;

    var localKey=k.slice(nodeScenePath.length + 1);
    var cleaned=clean(d);

    if(cleaned && Object.keys(cleaned).length) childSnap[localKey]=cleaned;
    else delete childSnap[localKey];

    console.log('[STREAM CHILD MAP]', localKey, cleaned);

    if(currentNode.html!=null) rebuildChildren();
  });

  if(fieldPath.length){
    if(fieldPath[0] === 'data'){
      gun.get(nodeRoot).get('scene').get(nodeScenePath).get('data').on(function(d){
        var v=dig(clean(d), fieldPath, 1);
        console.log('[STREAM FIELD DATA]', fieldPath, v);
        renderValue(v);
      });
      return;
    }

    if(fieldPath.length > 1){
      gun.get(nodeRoot).get('scene').get(nodeScenePath).on(function(node){
        currentNode=clean(node);
        var v=dig(currentNode, fieldPath, 0);
        console.log('[STREAM FIELD NODE]', fieldPath, v);
        if(fieldPath[0]==='html'){
          app.innerHTML=v==null?'':String(v);
          return;
        }
        renderValue(v);
      });
      return;
    }

    chainFor(nodeRoot,nodeScenePath,fieldPath).on(function(v){
      console.log('[STREAM FIELD SIMPLE]', fieldPath, v);
      if(fieldPath[0]==='html'){
        app.innerHTML=v==null?'':String(v);
        return;
      }
      renderValue(v);
    });
    return;
  }

  gun.get(nodeRoot).get('scene').get(nodeScenePath).on(function(node){
    currentNode=clean(node);
    console.log('[STREAM NODE UPDATE]', currentNode);
    render();
  });

  if(bindRoot && bindScenePath){
    gun.get(bindRoot).get('scene').get(bindScenePath).get('data').on(function(d){
      currentData=clean(d);
      console.log('[STREAM DATA UPDATE]', currentData);
      render();
    });
  }
})();
<\/script>
</body>
</html>`;
}

function inspectorHTML(origin) {
  const boot = {
    peers: PEERS.slice(),
    roots: visibleRoots(),
    meta: relayMeta(origin),
    admin: adminStateObject(),
  };
  const helperJS = browserPutHelperJS();

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>HyperRelay Inspector</title>
<script src="https://cdn.jsdelivr.net/npm/gun/gun.js"><\/script>
<style>
html,body{margin:0;padding:0;background:#fff;color:#111;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
*{box-sizing:border-box}
a{color:#2563eb;text-decoration:none}
.shell{display:grid;grid-template-columns:320px 1fr;min-height:100vh}
.side{border-right:1px solid #e5e7eb;padding:16px;overflow:auto}
.main{padding:18px 20px 40px;overflow:auto}
h1,h2,h3{margin:0 0 12px;font-weight:600;color:#111827}
h1{font-size:22px}
h2{font-size:16px;margin-top:22px}
h3{font-size:14px;margin-top:16px}
p,ul,pre{margin:0 0 14px}
.muted{color:#6b7280}
.crumbs{margin-bottom:14px}
.live{display:inline-block;width:7px;height:7px;border-radius:50%;background:#22c55e;margin-right:6px;vertical-align:middle}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;background:#eef2ff;color:#4338ca;font-size:12px}
.box{border:1px solid #e5e7eb;background:#fafafa;border-radius:8px;padding:12px;margin-bottom:14px}
pre{white-space:pre-wrap;word-break:break-word;padding:12px;border:1px solid #e5e7eb;background:#fafafa;border-radius:8px;overflow:auto}
ul.tree,ul.links,ul.results,ul.roots{list-style:none;padding-left:0;margin-left:0}
ul.tree ul{list-style:none;padding-left:16px;margin:4px 0 0;border-left:1px dashed #e5e7eb}
li.node{margin:3px 0}
.selected{background:#eff6ff;border-radius:6px;padding:2px 6px}
form.search,.rowform{display:flex;gap:8px;margin:10px 0 14px;flex-wrap:wrap}
input[type=text],textarea{width:100%;padding:8px 10px;border:1px solid #d1d5db;border-radius:8px;font:inherit}
textarea{min-height:140px}
button{border:1px solid #d1d5db;background:#fff;padding:8px 10px;border-radius:8px;font:inherit;cursor:pointer}
button:hover{background:#f9fafb}
iframe.preview{width:100%;min-height:280px;border:1px solid #e5e7eb;border-radius:8px;background:#fff}
.toggle{display:inline-flex;align-items:center;gap:6px;margin-left:6px;color:#374151;font-weight:500}
.toggle input{margin:0}
.navbtn{background:none;border:none;padding:0;color:#2563eb;cursor:pointer;font:inherit}
</style>
</head>
<body>
<div class="shell">
  <aside class="side" id="side"></aside>
  <main class="main" id="main"></main>
</div>
<script>
(function(){
  var BOOT=${JSON.stringify(boot)};
  var ALL_PEERS=(BOOT.peers||[]).slice();
  var local=location.origin + '/gun';
  if(ALL_PEERS.indexOf(local)===-1) ALL_PEERS.unshift(local);

  var gun=Gun({peers:ALL_PEERS});
  var side=document.getElementById('side');
  var main=document.getElementById('main');

  ${helperJS}

  var state={
    meta:BOOT.meta||null,
    roots:(BOOT.roots||[]).slice(),
    root:'',
    path:'',
    q:'',
    token:localStorage.getItem('hyper_admin_token')||'',
    tree:null,
    node:null,
    contextNode:null,
    contextPath:'',
    search:null,
    admin:BOOT.admin||null,
    nodeLive:localStorage.getItem('hyper_node_live')==='1',
    subs:[],
    metaSub:null,
    rootsSub:null,
    adminSub:null,
    clientId:'c_' + Math.random().toString(36).slice(2,10),
    previewKey:''
  };

  console.log('[INSPECTOR BOOT]', BOOT);

  function esc(x){
    return String(x==null?'':x)
      .replace(/&/g,'&amp;')
      .replace(/</g,'&lt;')
      .replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;');
  }

  function parseHash(){
    var q=new URLSearchParams(location.hash.replace(/^#/,''));
    return { root:q.get('root')||'', path:q.get('path')||'', q:q.get('q')||'' };
  }

  function setHash(next){
    var q=new URLSearchParams();
    if(next.root) q.set('root', next.root);
    if(next.path) q.set('path', next.path);
    if(next.q) q.set('q', next.q);
    location.hash='#' + q.toString();
  }

  function navHash(root,path,q){
    var s='#root=' + encodeURIComponent(root || '');
    if(path) s += '&path=' + encodeURIComponent(path);
    if(q) s += '&q=' + encodeURIComponent(q);
    return s;
  }

  function absPath(){
    if(!state.root) return '';
    return state.path ? state.root + '.' + state.path : state.root;
  }

  function parentPath(p){
    if(!p) return '';
    var i=p.lastIndexOf('.');
    return i===-1 ? '' : p.slice(0,i);
  }

  function relFromAbs(root,abs){
    if(!root) return abs||'';
    if(abs===root) return '';
    return abs.indexOf(root + '.')===0 ? abs.slice(root.length+1) : abs;
  }

  function encPath(abs){ return encodeURIComponent(abs); }

  function chainForPath(dp){
    var parts=String(dp||'').split('.');
    var root=parts[0];
    var scenePath=parts.length>1 ? parts.slice(1).join('/') : '__root__';
    return gun.get(root).get('scene').get(scenePath);
  }

  function parseJSONValue(v){
    if(v==null || v==='') return null;
    if(typeof v!=='string') return v;
    try { return JSON.parse(v); } catch (_) { return null; }
  }

  function readJSONOnce(dp){
    return new Promise(function(resolve){
      chainForPath(dp).get('json').once(function(v){
        var parsed=parseJSONValue(v);
        console.log('[INSPECTOR READ ONCE]', dp, parsed);
        resolve(parsed);
      });
    });
  }

  function subscribeJSON(dp, fn){
    console.log('[INSPECTOR SUB]', dp);
    var ev=chainForPath(dp).get('json').on(function(v){
      var parsed=parseJSONValue(v);
      if(parsed===null || parsed===undefined) return;
      console.log('[INSPECTOR SUB UPDATE]', dp, parsed);
      fn(parsed);
    });
    return function(){ try { ev.off(); } catch(_) {} };
  }

  function closeSubs(){
    for(var i=0;i<state.subs.length;i++){ try { state.subs[i](); } catch(_) {} }
    state.subs=[];
  }

  function closeMetaSub(){
    if(state.metaSub){
      try { state.metaSub(); } catch(_) {}
      state.metaSub=null;
    }
  }

  function patchPre(id, value){
    var el=document.getElementById(id);
    if(!el) return;
    el.textContent=JSON.stringify(value == null ? {} : value, null, 2);
  }

  function patchNodeBlocks(){
    patchPre('node-json', state.node || {});
    patchPre('context-json', state.contextNode || {});
    var edit=document.getElementById('edit-json');
    if(edit && document.activeElement !== edit){
      edit.value=JSON.stringify(state.node || {}, null, 2);
    }
    var ctx=document.getElementById('context-path');
    if(ctx) ctx.textContent=state.contextPath || '';
  }

  function loadContextFor(abs){
    if(state.node && state.node.data && typeof state.node.data === 'object'){
      state.contextPath=abs;
      state.contextNode=state.node;
      return Promise.resolve();
    }

    var cur=parentPath(abs);
    function step(){
      if(!cur){
        state.contextPath='';
        state.contextNode=null;
        return Promise.resolve();
      }
      return readJSONOnce('_sys.view.' + cur).then(function(obj){
        if(obj && obj.data && typeof obj.data === 'object'){
          state.contextPath=cur;
          state.contextNode=obj;
          return;
        }
        cur=parentPath(cur);
        return step();
      });
    }
    return step();
  }

  function loadTreeNode(dp){
    return readJSONOnce('_sys.tree.' + dp).then(function(node){
      if(!node) return null;
      var kids=Array.isArray(node.children) ? node.children.slice() : [];
      return Promise.all(kids.map(loadTreeNode)).then(function(children){
        node.children=children.filter(Boolean);
        return node;
      });
    });
  }

  function openNodeLiveSubs(){
    closeSubs();
    if(!state.nodeLive || !state.root) return;

    var abs=absPath() || state.root;
    state.subs.push(subscribeJSON('_sys.view.' + abs, function(obj){
      state.node=obj;
      patchNodeBlocks();
    }));

    if(state.contextPath && state.contextPath !== abs){
      state.subs.push(subscribeJSON('_sys.view.' + state.contextPath, function(obj){
        state.contextNode=obj;
        patchNodeBlocks();
      }));
    }
  }

  function openMetaSub(){
    closeMetaSub();
    if(!state.root) return;

    var lastMeta=null;
    state.metaSub=subscribeJSON('_sys.meta.' + state.root, function(meta){
      if(!meta) return;
      console.log('[INSPECTOR META]', meta);
      if(lastMeta && meta.tree_version !== lastMeta.tree_version){
        loadTreeNode(state.root).then(function(tree){
          state.tree=tree ? {tree:tree} : null;
          renderTreeOnly();
        });
      }
      lastMeta=meta;
    });
  }

  function refreshCurrent(){
    if(!state.root){
      render();
      return Promise.resolve();
    }

    var abs=absPath() || state.root;
    console.log('[INSPECTOR REFRESH]', abs);

    return Promise.all([
      loadTreeNode(state.root).then(function(tree){ state.tree=tree ? {tree:tree} : null; }),
      readJSONOnce('_sys.view.' + abs).then(function(obj){
        state.node=obj;
        return loadContextFor(abs);
      })
    ]).then(function(){
      if(state.q) return runSearch(state.q);
      state.search=null;
    }).then(function(){
      render();
      openNodeLiveSubs();
      openMetaSub();
    });
  }

  function mountPreview(force){
    var frame=document.getElementById('preview-frame');
    if(!frame) return;

    var abs=absPath() || state.root;
    var key='preview|' + abs;
    if(!force && state.previewKey === key) return;

    state.previewKey=key;
    frame.src='/' + encPath(abs) + '.stream';
    console.log('[INSPECTOR PREVIEW]', frame.src);
  }

  function normalizeNodeLinks(node){
    if(!node || !node.links) return [];
    var raw=node.links;
    if(typeof raw==='string'){
      try { raw=JSON.parse(raw); } catch (_) { raw=[]; }
    }
    return Array.isArray(raw) ? raw.filter(Boolean) : [];
  }

  function crumbs(){
    var out=['<button type="button" class="navbtn" data-nav-hash="#">relay</button>'];
    if(state.root){
      out.push(' / <button type="button" class="navbtn" data-nav-hash="' + navHash(state.root,'',state.q) + '">' + esc(state.root) + '</button>');
    }
    if(state.path){
      var parts=state.path.split('.');
      var acc=[];
      for(var i=0;i<parts.length;i++){
        acc.push(parts[i]);
        out.push(' / <button type="button" class="navbtn" data-nav-hash="' + navHash(state.root,acc.join('.'),state.q) + '">' + esc(parts[i]) + '</button>');
      }
    }
    return out.join('');
  }

  function renderTreeNode(node){
    if(!node) return '';
    var rel=relFromAbs(state.root, node.path);
    var selected=(rel===state.path)?' selected':'';
    var label=rel || state.root;
    var href=navHash(state.root, rel, state.q);
    var html='<li class="node"><button type="button" class="navbtn' + selected + '" data-nav-hash="' + href + '">' + esc(label) + '</button>';
    if(node.type) html+=' <span class="muted">(' + esc(node.type) + ')</span>';
    if(Array.isArray(node.children) && node.children.length){
      html+='<ul>';
      for(var i=0;i<node.children.length;i++) html+=renderTreeNode(node.children[i]);
      html+='</ul>';
    }
    html+='</li>';
    return html;
  }

  function renderTreeOnly(){
    var el=document.getElementById('tree-root');
    if(!el) return;
    if(state.tree && state.tree.tree){
      el.innerHTML='<ul class="tree">' + renderTreeNode(state.tree.tree) + '</ul>';
    } else {
      el.innerHTML='<p class="muted">No tree loaded.</p>';
    }
  }

  function renderSide(){
    var html='<h2>Relay</h2>';
    if(state.meta){
      html+='<div class="box">';
      html+='<div><strong>port</strong>: ' + esc(state.meta.port) + '</div>';
      html+='<div><strong>bind</strong>: ' + esc(state.meta.bind) + '</div>';
      html+='<div><strong>peers</strong>: ' + esc((state.meta.peers||[]).length) + '</div>';
      html+='</div>';
    }

    html+='<div class="rowform">';
    html+='<input id="token-input" type="text" placeholder="admin token" value="' + esc(state.token) + '"/>';
    html+='<button type="button" id="save-token">Save</button>';
    html+='</div>';

    html+='<h2>Roots</h2><ul class="roots">';
    if(!state.roots.length){
      html+='<li class="muted">No roots</li>';
    } else {
      for(var i=0;i<state.roots.length;i++){
        var r=state.roots[i];
        html+='<li><button type="button" class="navbtn" data-nav-hash="' + navHash(r,'','') + '">' + esc(r) + '</button></li>';
      }
    }
    html+='</ul>';

    if(state.root){
      html+='<h2>Tree</h2><div id="tree-root"></div>';
    }

    side.innerHTML=html;
    renderTreeOnly();

    var btn=document.getElementById('save-token');
    if(btn){
      btn.onclick=function(){
        var val=document.getElementById('token-input').value.trim();
        state.token=val;
        localStorage.setItem('hyper_admin_token', val);
        render();
      };
    }
  }

  function renderMain(){
    var html='<div class="crumbs">' + crumbs() + '</div>';
    html+='<div class="row"><h1><span class="live"></span>HyperRelay Inspector</h1><span class="pill">trace build</span></div>';

    if(!state.root){
      html+='<p class="muted">Choose a root from the left.</p>';
      if(state.meta) html+='<pre>' + esc(JSON.stringify(state.meta,null,2)) + '</pre>';
      main.innerHTML=html;
      return;
    }

    var abs=absPath() || state.root;

    html+='<div class="box">';
    html+='<div><strong>self</strong>: /' + esc(abs) + '</div>';
    html+='<div><strong>tree</strong>: /' + esc(abs) + '.tree</div>';
    html+='<div><strong>events</strong>: /' + esc(abs) + '.events <span class="muted">(python bridge)</span></div>';
    html+='<div><strong>stream</strong>: /' + esc(abs) + '.stream</div>';
    html+='</div>';

    html+='<div class="rowform"><button type="button" id="refresh-now">Refresh</button></div>';
    html+='<form class="search" id="search-form">';
    html+='<input id="search-input" type="text" placeholder="search this subtree" value="' + esc(state.q) + '"/>';
    html+='<button type="submit">Search</button>';
    html+='<button type="button" id="clear-search">Clear</button>';
    html+='</form>';

    if(state.search){
      html+='<h2>Search</h2>';
      if(state.search.results && state.search.results.length){
        html+='<ul class="results">';
        for(var i=0;i<state.search.results.length;i++){
          var it=state.search.results[i];
          var rel=relFromAbs(state.root, it.path);
          html+='<li>';
          html+='<button type="button" class="navbtn" data-nav-hash="' + navHash(state.root, rel, '') + '">' + esc(it.path) + '</button>';
          html+=' <span class="muted">[' + esc(it.match||'') + ']</span>';
          if(it.excerpt) html+='<div class="muted">' + esc(it.excerpt) + '</div>';
          html+='</li>';
        }
        html+='</ul>';
      } else {
        html+='<p class="muted">No matches.</p>';
      }
    }

    html+='<div class="row"><h2>Node</h2><label class="toggle"><input id="live-node-toggle" type="checkbox" ' + (state.nodeLive?'checked':'') + '/> node live</label></div>';

    if(!state.node){
      html+='<p class="muted">No node or branch metadata found.</p>';
    } else {
      html+='<pre id="node-json">' + esc(JSON.stringify(state.node,null,2)) + '</pre>';

      if(state.contextPath && state.contextPath !== abs && state.contextNode){
        html+='<div class="row"><h2>Context</h2><span class="muted" id="context-path">' + esc(state.contextPath) + '</span></div>';
        html+='<pre id="context-json">' + esc(JSON.stringify(state.contextNode,null,2)) + '</pre>';
      }

      var apiLinks=state.node._links||null;
      if(apiLinks&&typeof apiLinks==='object'){
        html+='<h2>API Links</h2><ul class="links">';
        Object.keys(apiLinks).forEach(function(k){
          html+='<li><strong>' + esc(k) + '</strong>: ' + esc(apiLinks[k]) + '</li>';
        });
        html+='</ul>';
      }

      var semanticLinks=normalizeNodeLinks(state.node);
      if(semanticLinks.length){
        html+='<h2>Links</h2><ul class="links">';
        for(var j=0;j<semanticLinks.length;j++){
          var link=semanticLinks[j];
          var target=link.path||'';
          var rel=relFromAbs(state.root, target);
          html+='<li><button type="button" class="navbtn" data-nav-hash="' + navHash(state.root, rel, '') + '">' + esc(link.label||link.rel||target) + '</button>' + (link.path ? ' <span class="muted">(' + esc(link.path) + ')</span>' : '') + '</li>';
        }
        html+='</ul>';
      }

      if(state.node.html!=null){
        html+='<h2>Preview</h2><iframe class="preview" id="preview-frame"></iframe>';
      }

      if(state.node.file && state.node._links && state.node._links.download){
        html+='<h2>File</h2><p>' + esc(state.node._links.download) + '</p>';
      }
    }

    html+='<h2>Edit Node</h2>';
    html+='<p class="muted">Submit raw node JSON through Gun write request for ' + esc(abs) + '</p>';
    html+='<textarea id="edit-json">' + esc(state.node ? JSON.stringify(state.node,null,2) : '{}') + '</textarea>';
    html+='<div class="rowform"><button type="button" id="save-node">Save Node</button><button type="button" id="delete-node">Delete Node</button></div>';

    html+='<h2>Admin</h2>';
    html+='<pre id="admin-json">' + esc(JSON.stringify(state.admin||{},null,2)) + '</pre>';

    main.innerHTML=html;
    mountPreview(true);

    var rn=document.getElementById('refresh-now');
    if(rn) rn.onclick=function(){ refreshCurrent(); };

    var lnt=document.getElementById('live-node-toggle');
    if(lnt){
      lnt.onchange=function(){
        state.nodeLive=!!lnt.checked;
        localStorage.setItem('hyper_node_live', state.nodeLive?'1':'0');
        openNodeLiveSubs();
      };
    }

    var sf=document.getElementById('search-form');
    if(sf){
      sf.onsubmit=function(e){
        e.preventDefault();
        var input=document.getElementById('search-input');
        setHash({root:state.root,path:state.path,q:input.value.trim()});
      };
    }

    var cs=document.getElementById('clear-search');
    if(cs){
      cs.onclick=function(){ setHash({root:state.root,path:state.path,q:''}); };
    }

    var sn=document.getElementById('save-node');
    if(sn){
      sn.onclick=function(){
        var raw=document.getElementById('edit-json').value;
        var parsed;
        try { parsed=JSON.parse(raw); } catch(err) { alert('Invalid JSON: ' + err.message); return; }
        delete parsed._path;
        delete parsed._links;
        sendWriteRequest('put', abs, parsed).then(function(result){
          console.log('[INSPECTOR SAVE RESULT]', result);
          if(!result.ok) alert(result.error || 'write failed');
          refreshCurrent();
        });
      };
    }

    var dn=document.getElementById('delete-node');
    if(dn){
      dn.onclick=function(){
        if(!confirm('Delete ' + abs + ' ?')) return;
        sendWriteRequest('delete', abs, null).then(function(result){
          console.log('[INSPECTOR DELETE RESULT]', result);
          if(!result.ok) alert(result.error || 'delete failed');
          refreshCurrent();
        });
      };
    }
  }

  function render(){
    renderSide();
    renderMain();
  }

  function requestOnce(base, payload){
    return new Promise(function(resolve, reject){
      var reqId='r_' + Math.random().toString(36).slice(2,10);
      var reqPath=base + '.request.' + state.clientId + '.' + reqId;
      var resPath=base + '.result.' + state.clientId + '.' + reqId;
      console.log('[INSPECTOR REQUEST]', reqPath, payload);
      var off=subscribeJSON(resPath, function(obj){
        console.log('[INSPECTOR RESULT]', resPath, obj);
        off();
        resolve(obj);
      });
      var parts=reqPath.split('.');
      var root=parts[0];
      var sceneKey=parts.slice(1).join('/');
      hyperScenePut(gun, root, sceneKey, payload).catch(function(err){
        off();
        reject(err);
      });
    });
  }

  function sendSearchRequest(root,q,limit){
    return requestOnce('_sys.query', { root:root, q:q, limit:limit||50, ts:Date.now() });
  }

  function sendWriteRequest(op,path,value){
    return requestOnce('_sys.write', {
      op:op,
      path:path,
      token:state.token||'',
      payload_json:value==null?'':JSON.stringify(value),
      ts:Date.now()
    });
  }

  function runSearch(q){
    var abs=absPath() || state.root;
    if(!q){ state.search=null; return Promise.resolve(); }
    return sendSearchRequest(abs, q, 50).then(function(result){ state.search=result; });
  }

  function route(force){
    closeMetaSub();
    closeSubs();

    var next=parseHash();
    var changed=next.root!==state.root || next.path!==state.path || next.q!==state.q;
    console.log('[INSPECTOR ROUTE]', next, 'changed=', changed, 'force=', force);

    if(!force && !changed && state.meta) return;

    state.root=next.root;
    state.path=next.path;
    state.q=next.q;

    if(!state.root && state.roots.length){
      setHash({root:state.roots[0], path:'', q:''});
      return;
    }

    refreshCurrent().catch(function(err){
      console.error('[INSPECTOR REFRESH FAIL]', err);
      render();
    });
  }

  function bootRootsSub(){
    if(state.rootsSub) return;
    state.rootsSub=subscribeJSON('_sys.roots', function(arr){
      console.log('[INSPECTOR ROOTS]', arr);
      if(!Array.isArray(arr)) return;
      state.roots=arr.slice();
      if(!state.root && state.roots.length){
        setHash({root:state.roots[0], path:'', q:''});
      }
    });
  }

  function bootAdminSub(){
    if(state.adminSub) return;
    state.adminSub=subscribeJSON('_sys.admin.state', function(obj){
      console.log('[INSPECTOR ADMIN]', obj);
      state.admin=obj;
      var adminPre=document.getElementById('admin-json');
      if(adminPre) patchPre('admin-json', state.admin || {});
    });
  }

  document.addEventListener('click', function(e){
    var btn=e.target.closest('[data-nav-hash]');
    if(!btn) return;
    e.preventDefault();
    var href=btn.getAttribute('data-nav-hash') || '#';
    console.log('[INSPECTOR NAV CLICK]', href);
    if(location.hash === href) route(true);
    else location.hash = href;
  }, true);

  window.addEventListener('hashchange', function(){ route(false); });

  bootRootsSub();
  bootAdminSub();

  var initial=parseHash();
  if(!initial.root && state.roots.length){
    setHash({root:state.roots[0], path:'', q:''});
  } else {
    route(true);
  }
})();
<\/script>
</body>
</html>`;
}

function serverWrite(path, payload) {
  log('SERVER-WRITE', path, payload);
  const existed = !!getNode(path);
  mergeNode(path, payload);
  gunPut(path, graph[path] || payload);
  notifyImpacted(path);
  syncMetadataForPaths([path, parentOf(path) || path], { treeChanged: !existed && !!getNode(path) });
}

function serverDelete(path) {
  const removed = descendantsOf(path);
  log('SERVER-DELETE', path, 'removed', removed);
  for (const dp of removed) {
    gunNullOut(dp);
    deleteNode(dp);
  }
  notifyImpacted(path);
  syncMetadataForPaths([path, parentOf(path) || path, ...removed], { treeChanged: removed.length > 0 });
  return removed.length;
}

const server = http.createServer(async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
  if (req.method === 'OPTIONS') { res.writeHead(200); res.end(); return; }

  const parsed = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const pathname = parsed.pathname.replace(/\/+$/, '') || '/';
  const wantsJSON = parsed.searchParams.has('json') || (req.headers.accept || '').includes('application/json');

  log('HTTP', req.method, pathname);

  if (pathname === '/gun') return;

  if (pathname === '/') {
    if (wantsJSON) return sendJson(res, relayMeta(parsed.origin));
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Access-Control-Allow-Origin': '*' });
    return res.end(inspectorHTML(parsed.origin));
  }

  if (pathname === '/_admin') {
    if (!checkAdmin(req, parsed)) return sendJson(res, { error: 'unauthorized' }, 401);
    return sendJson(res, adminStateObject());
  }

  const raw = decodeURIComponent(pathname.slice(1));

  const treePath = stripSuffix(raw, '.tree');
  if (treePath && req.method === 'GET') {
    return sendJson(res, { _path: treePath + '.tree', tree: buildTree(treePath) });
  }

  const searchPath = stripSuffix(raw, '.search');
  if (searchPath && req.method === 'GET') {
    const q = parsed.searchParams.get('q') || '';
    const limit = Math.min(parseInt(parsed.searchParams.get('limit') || '50', 10) || 50, 200);
    return sendJson(res, {
      _path: searchPath + '.search',
      query: q,
      count: searchSubtree(searchPath, q, limit).length,
      results: searchSubtree(searchPath, q, limit),
    });
  }

  const streamPath = stripSuffix(raw, '.stream') || stripSuffix(raw, '._connect');
  if (streamPath && req.method === 'GET') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Access-Control-Allow-Origin': '*' });
    return res.end(connectHTML(streamPath));
  }

  const eventsPath = stripSuffix(raw, '.events') || stripSuffix(raw, '._events');
  if (eventsPath && req.method === 'GET') {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      'Connection': 'keep-alive',
      'X-Accel-Buffering': 'no',
      'Access-Control-Allow-Origin': '*',
    });
    const client = { path: eventsPath, origin: parsed.origin, res };
    sseClients.add(client);
    log('SSE-OPEN', eventsPath);
    emitResolved(client, 'snapshot');
    req.on('close', function(){ sseClients.delete(client); log('SSE-CLOSE', eventsPath); });
    req.on('aborted', function(){ sseClients.delete(client); log('SSE-ABORT', eventsPath); });
    return;
  }

  const downloadPath = stripSuffix(raw, '.download');
  if (downloadPath && req.method === 'GET') {
    const dl = resolveDownload(downloadPath);
    if (!dl) return sendJson(res, { error: 'not found', _path: downloadPath }, 404);
    return sendBuffer(res, dl.body, dl.type, dl.name);
  }

  const apiMatch = raw.match(/^([^./]+)\/api\/(\w+)$/);
  if (apiMatch) {
    const root = apiMatch[1];
    const op = apiMatch[2];

    if (req.method === 'GET' && op === 'snapshot') return sendJson(res, snapshotForRoot(root));
    if (req.method === 'GET' && op === 'keys') return sendJson(res, Object.keys(snapshotForRoot(root)));

    if (req.method === 'POST' && op === 'clear') {
      if (!checkAuth(root, req, parsed)) return sendJson(res, { error: 'unauthorized' }, 401);
      const removed = descendantsOf(root);
      for (const dp of removed) { gunNullOut(dp); deleteNode(dp); }
      notifyImpacted(root);
      syncMetadataForPaths([root, ...removed], { treeChanged: removed.length > 0 });
      return sendJson(res, { ok: true });
    }

    if (req.method === 'POST' && op === 'auth') {
      try {
        const body = JSON.parse(await readBody(req));
        if (!body.token) delete tokens[root];
        else tokens[root] = body.token;
        return sendJson(res, { ok: true });
      } catch (_) {
        return sendJson(res, { error: 'need {token}' }, 400);
      }
    }
  }

  const sceneMatch = raw.match(/^([^./]+)\/scene\/(.+)$/);
  if (sceneMatch) {
    const dp = sceneMatch[1] + '.' + sceneMatch[2].replace(/\//g, '.');

    if (req.method === 'GET') return sendJson(res, nodeForRead(getNode(dp)));

    if (req.method === 'PUT') {
      if (!checkAuth(dp, req, parsed)) return sendJson(res, { error: 'unauthorized' }, 401);
      try {
        const body = JSON.parse(await readBody(req));
        serverWrite(dp, cleanUserPayload(body));
        return sendJson(res, { ok: true, path: dp });
      } catch (e) { return sendJson(res, { error: e.message }, 400); }
    }

    if (req.method === 'DELETE') {
      if (!checkAuth(dp, req, parsed)) return sendJson(res, { error: 'unauthorized' }, 401);
      serverDelete(dp);
      return sendJson(res, { ok: true });
    }
  }

  const actionMatch = raw.match(/^([^./]+)\/action$/);
  if (actionMatch && req.method === 'POST') {
    try {
      const payload = JSON.parse(await readBody(req));
      const key = actionMatch[1] + '.inbox.' + Date.now() + '_' + Math.random().toString(36).slice(2,7);
      log('HTTP-ACTION', key, payload);
      serverWrite(key, { data: JSON.stringify(payload) });
      return sendJson(res, { ok: true, key });
    } catch (e) { return sendJson(res, { error: e.message }, 400); }
  }

  const dotPath = raw;

  if (!dotPath.includes('.') && !dotPath.includes('/') && req.method === 'GET') {
    return sendJson(res, buildResponse(dotPath, parsed.origin));
  }

  if (req.method === 'GET') {
    const also = parsed.searchParams.getAll('also');
    if (also.length > 0) {
      const out = {};
      for (const p of [dotPath, ...also]) {
        const r = resolve(p, parsed.origin);
        out[p] = r ? r.data : null;
      }
      return sendJson(res, out);
    }

    const r = resolve(dotPath, parsed.origin);
    if (!r) {
      return sendJson(res, {
        error: 'not found',
        _path: dotPath,
        _links: {
          self: pathURL(parsed.origin, dotPath),
          stream: pathURL(parsed.origin, dotPath, '.stream'),
          events: pathURL(parsed.origin, dotPath, '.events'),
        },
      }, 404);
    }

    if (r.type === 'raw') return sendRaw(res, r.data);
    return sendJson(res, r.data);
  }

  if (req.method === 'PUT') {
    if (!checkAuth(dotPath, req, parsed)) return sendJson(res, { error: 'unauthorized' }, 401);
    try {
      const body = JSON.parse(await readBody(req));
      serverWrite(dotPath, cleanUserPayload(body));
      return sendJson(res, { ok: true, _path: dotPath });
    } catch (e) { return sendJson(res, { error: e.message }, 400); }
  }

  if (req.method === 'DELETE') {
    if (!checkAuth(dotPath, req, parsed)) return sendJson(res, { error: 'unauthorized' }, 401);
    const removed = serverDelete(dotPath);
    return sendJson(res, { ok: true, removed });
  }

  if (req.method === 'POST') {
    try {
      const payload = JSON.parse(await readBody(req));
      const root = dotPath.split('.')[0];
      const key = root + '.inbox.' + Date.now() + '_' + Math.random().toString(36).slice(2,7);
      serverWrite(key, { data: JSON.stringify(payload) });
      return sendJson(res, { ok: true, key });
    } catch (e) { return sendJson(res, { error: e.message }, 400); }
  }

  res.writeHead(404);
  res.end('Not found');
});

gun = Gun({ peers: PEERS, web: server, radisk: true });
initGunSync();

syncRootsMetadata();
syncAdminState();

server.listen(PORT, BIND, () => {
  log('START', 'HyperRelay http://localhost:' + PORT);
  if (PEERS.length) log('PEERS', PEERS.join(', '));
});