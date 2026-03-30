const Gun = require('gun');
require('gun/sea');
const http = require('http');
const path = require('path');

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
const RADISK_DIR = process.env.HYPER_DATA_DIR || path.join(process.cwd(), '.hyper-data');

let PEERS;
try { PEERS = JSON.parse(process.env.HYPER_PEERS || '[]'); } catch (_) { PEERS = []; }

// ─── In-memory read cache ─────────────────────────────────────────────
// This is NOT the source of truth. Gun+radisk is.
// The cache exists so HTTP GET can respond fast without async Gun chains.
const graph = Object.create(null);
const localMsgIds = new Set();
const seenPutIds = new Map();
let gun;

// ─── CONTRACT KEY REGISTRY ────────────────────────────────────────────
const CONTRACT_KEYS = new Set([
  'manifest', 'schema', 'links', 'actions', 'events',
  'html', 'css', 'js', 'trust'
]);

const RESERVED_DATA_KEYS = new Set([
  'html', 'css', 'js', 'trust'
]);

function log(tag, ...args) {
  console.log(new Date().toISOString(), `[${tag}]`, ...args);
}

function isObject(v) {
  return !!v && typeof v === 'object' && !Array.isArray(v);
}

// ─── GRAPH CACHE OPERATIONS ───────────────────────────────────────────
// These only touch the in-memory cache. Gun is written separately.

function parentOf(dp) {
  const parts = String(dp || '').split('.');
  if (parts.length <= 1) return null;
  return parts.slice(0, -1).join('.');
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

function getNode(dp) {
  return graph[dp] || null;
}

function deleteNodeCache(dp) {
  delete graph[dp];
}

function mergeCacheNode(dp, incoming) {
  const ex = graph[dp] || {};
  for (const [k, v] of Object.entries(incoming || {})) {
    if (k === '_' || k === '#' || k === '>') continue;

    if (k === 'data' && isObject(v)) {
      const sanitized = {};
      for (const [dk, dv] of Object.entries(v)) {
        if (RESERVED_DATA_KEYS.has(dk)) {
          log('GUARD', `rejected reserved key "${dk}" inside data at ${dp}`);
          continue;
        }
        sanitized[dk] = dv;
      }
      const merged = mergeData(ex.data, sanitized);
      if (Object.keys(merged).length === 0) delete ex.data;
      else ex.data = merged;
      continue;
    }

    if (v === null || v === undefined) delete ex[k];
    else ex[k] = v;
  }

  if (Object.keys(ex).length === 0) {
    delete graph[dp];
    return null;
  }

  graph[dp] = ex;
  return ex;
}

// ─── CONTRACT VALIDATION ──────────────────────────────────────────────

function validateContract(dp, node) {
  const warnings = [];
  if (!node) return warnings;

  if (node.manifest) {
    if (!node.manifest.name) warnings.push(`[${dp}] manifest missing "name"`);
    if (!node.manifest.version) warnings.push(`[${dp}] manifest missing "version"`);
  }

  if (node.schema) {
    for (const ns of ['public', 'secure', 'local']) {
      if (node.schema[ns] && !isObject(node.schema[ns])) {
        warnings.push(`[${dp}] schema.${ns} is not an object`);
      }
    }
  }

  if (isObject(node.actions)) {
    for (const [name, spec] of Object.entries(node.actions)) {
      if (!isObject(spec)) warnings.push(`[${dp}] action "${name}" has no spec object`);
    }
  }

  if (isObject(node.events)) {
    for (const [name, spec] of Object.entries(node.events)) {
      if (!isObject(spec)) warnings.push(`[${dp}] event "${name}" has no spec object`);
    }
  }

  for (const w of warnings) log('CONTRACT', w);
  return warnings;
}

// ─── NODE RESPONSE BUILDING ──────────────────────────────────────────

function nodeForRead(node) {
  if (!node) return null;
  const out = {};
  for (const [k, v] of Object.entries(node)) out[k] = v;
  return out;
}

function pathURL(origin, dp, suffix = '') {
  return (origin || '') + '/' + encodeURIComponent(dp) + suffix;
}

function buildResponse(dp, origin) {
  const node = nodeForRead(getNode(dp));
  const links = {
    self: pathURL(origin, dp),
    stream: pathURL(origin, dp, '.stream')
  };

  const parent = parentOf(dp);
  if (parent) links.parent = pathURL(origin, parent);

  for (const child of childrenOf(dp)) {
    links[child.split('.').pop()] = pathURL(origin, child);
  }

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
    return { type: typeof val === 'object' ? 'json' : 'raw', data: val };
  }

  if (childrenOf(dp).length > 0) {
    return { type: 'node', data: buildResponse(dp, origin) };
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

// ─── GUN INTEGRATION ──────────────────────────────────────────────────

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
  gun._.on('in', msg);
  gun._.on('out', msg);
  return id;
}

function gunNullOut(dp) {
  const node = getNode(dp);
  if (!node) return null;
  const tomb = {};
  for (const k of Object.keys(node)) tomb[k] = null;
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

function rememberPut(id) {
  if (!id) return;
  const now = Date.now();
  seenPutIds.set(id, now);
  if (seenPutIds.size > 5000) {
    const cutoff = now - 60000;
    for (const [k, t] of seenPutIds) {
      if (t < cutoff) seenPutIds.delete(k);
    }
  }
}

function alreadySeenPut(id) {
  return !!id && seenPutIds.has(id);
}

function ingestPutMessage(msg) {
  if (!msg || !msg.put) return;
  const msgId = msg['#'] || null;
  if (alreadySeenPut(msgId)) return;
  rememberPut(msgId);

  for (const soul of Object.keys(msg.put || {})) {
    const dp = soulToDotPath(soul);
    if (!dp) continue;
    const clean = cleanGunNode(msg.put[soul], msg.put);
    if (!clean || !Object.keys(clean).length) continue;
    // Only update the read cache — Gun already has the data
    mergeCacheNode(dp, clean);
  }
}

function initGunSync() {
  gun.on('in', function (msg) {
    if (msg && msg.put) ingestPutMessage(msg);
    this.to.next(msg);
  });
  gun.on('out', function (msg) {
    if (msg && msg.put) ingestPutMessage(msg);
    this.to.next(msg);
  });
}

// ─── CACHE WARM-UP FROM RADISK ────────────────────────────────────────
// The cache fills naturally as data flows through ingestPutMessage
// from Gun's wire protocol. For HTTP reads that arrive before the
// cache is warm, resolveAsync falls back to a direct Gun read.

function resolveAsync(dp) {
  return new Promise((resolve) => {
    const { root, scenePath } = dotToScenePath(dp);
    const timeout = setTimeout(() => resolve(null), 2000);

    gun.get(root).get('scene').get(scenePath).once((data) => {
      clearTimeout(timeout);
      if (!data) return resolve(null);

      const cleaned = {};
      for (const [k, v] of Object.entries(data)) {
        if (k === '_' || k === '#' || k === '>') continue;
        if (v !== null && v !== undefined) cleaned[k] = v;
      }

      if (Object.keys(cleaned).length) {
        mergeCacheNode(dp, cleaned);
        resolve(cleaned);
      } else {
        resolve(null);
      }
    });
  });
}

// ─── QUERY PARSING ────────────────────────────────────────────────────

function parseMaybeJSON(value) {
  if (value === '') return '';
  try { return JSON.parse(value); }
  catch (_) { return value; }
}

function normalizeBindPath(raw) {
  let v = String(raw || '').trim();
  if (!v) return '';
  if (v.startsWith('$')) v = v.slice(1);
  if (v.startsWith('/')) v = v.slice(1).replace(/\//g, '.');
  if (v.endsWith('.stream')) v = v.slice(0, -7);
  return v;
}

function parseIntentQuery(searchParams) {
  const literal = {};
  const bindings = {};
  for (const [key, raw] of searchParams.entries()) {
    if (raw != null && String(raw).startsWith('$')) {
      bindings[key] = normalizeBindPath(raw);
    } else {
      literal[key] = parseMaybeJSON(raw);
    }
  }
  return { literal, bindings };
}

function resolveTargetInfo(rawPath) {
  let raw = String(rawPath || '');
  raw = raw.replace(/^[$/]+/, '');
  const q = raw.indexOf('?');
  if (q !== -1) raw = raw.slice(0, q);
  const h = raw.indexOf('#');
  if (h !== -1) raw = raw.slice(0, h);
  if (raw.endsWith('.stream')) raw = raw.slice(0, -7);

  const parts = raw.split('.').filter(Boolean);
  if (!parts.length) return { path: raw, nodeDp: raw, fieldPath: [] };

  const root = parts[0];
  const rest = parts.slice(1);
  const FIELD_NAMES = new Set(['data', 'html', 'css', 'js', 'fixed', 'layer']);

  let fieldStart = -1;
  for (let i = 0; i < rest.length; i++) {
    if (FIELD_NAMES.has(rest[i])) { fieldStart = i; break; }
  }

  const nodeSegs = fieldStart === -1 ? rest : rest.slice(0, fieldStart);
  const fieldPath = fieldStart === -1 ? [] : rest.slice(fieldStart);

  return {
    path: raw,
    nodeDp: [root].concat(nodeSegs).join('.'),
    fieldPath
  };
}

// ─── HTTP HELPERS ─────────────────────────────────────────────────────

function relayMeta(origin) {
  const roots = Array.from(new Set(Object.keys(graph).map(k => k.split('.')[0]))).sort();
  return {
    relay: true,
    roots,
    bind: BIND,
    port: PORT,
    peers: PEERS.slice(),
    persistence: 'radisk',
    dataDir: RADISK_DIR,
    _links: Object.fromEntries(roots.map(r => [r, pathURL(origin, r)])),
  };
}

function connectHTML(dp, intent) {
  const target = resolveTargetInfo(dp);
  const bindDp = nearestDataNode(target.nodeDp);
  const nodeMap = dotToScenePath(target.nodeDp);
  const bindMap = bindDp ? dotToScenePath(bindDp) : null;
  const bindingTargets = Object.fromEntries(
    Object.entries(intent.bindings || {}).map(([k, v]) => [k, resolveTargetInfo(v)])
  );
  const peersJSON = JSON.stringify(PEERS);

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
  var peers = ${peersJSON};
  var local = location.origin + '/gun';
  if (peers.indexOf(local) === -1) peers.unshift(local);

  var gun;
  try {
    if (window.parent && window.parent.$gun) {
      gun = window.parent.$gun;
    } else {
      gun = Gun({ peers: peers });
      window.$gun = gun;
    }
  } catch(e) {
    gun = Gun({ peers: peers });
    window.$gun = gun;
  }

  var app = document.getElementById('app');
  var AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;

  var nodeDp = ${JSON.stringify(target.nodeDp)};
  var fieldPath = ${JSON.stringify(target.fieldPath || [])};
  var nodeRoot = ${JSON.stringify(nodeMap.root)};
  var nodeScenePath = ${JSON.stringify(nodeMap.scenePath)};
  var bindRoot = ${JSON.stringify(bindMap ? bindMap.root : null)};
  var bindScenePath = ${JSON.stringify(bindMap ? bindMap.scenePath : null)};
  var literalCtx = ${JSON.stringify(intent.literal || {})};
  var bindingTargets = ${JSON.stringify(bindingTargets)};

  var currentNode = {};
  var currentData = {};
  var boundCtx = {};

  function isObject(v){
    return !!v && typeof v === 'object' && !Array.isArray(v);
  }

  function clean(v){
    if (v === undefined) return undefined;
    if (v === null) return null;
    if (typeof v !== 'object') return v;
    var out = {};
    Object.keys(v).forEach(function(k){
      if (k === '_' || k === '#' || k === '>') return;
      if (v[k] !== undefined && v[k] !== null) out[k] = v[k];
    });
    return out;
  }

  function mergeInto(dst, src){
    if (!src || typeof src !== 'object') return;
    Object.keys(src).forEach(function(k){ dst[k] = src[k]; });
  }

  function dig(v, path, start){
    for (var i = start || 0; i < path.length; i++) {
      if (v == null) return undefined;
      v = v[path[i]];
    }
    return v;
  }

  function bindData(root, data){
    data = data || {};
    var nodes = root.querySelectorAll('[data-bind-text],[data-bind-html],[data-bind-style]');
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (el.dataset.bindText) {
        var tv = dig(data, String(el.dataset.bindText).split('.'), 0);
        if (tv !== undefined) el.textContent = String(tv);
      }
      if (el.dataset.bindHtml) {
        var hv = dig(data, String(el.dataset.bindHtml).split('.'), 0);
        if (hv !== undefined) el.innerHTML = String(hv);
      }
      if (el.dataset.bindStyle) {
        var pairs = el.dataset.bindStyle.split(';');
        for (var j = 0; j < pairs.length; j++) {
          var pp = pairs[j].split(':');
          if (!pp[0] || !pp[1]) continue;
          var sv = dig(data, String(pp[1].trim()).split('.'), 0);
          if (sv !== undefined) el.style[pp[0].trim()] = sv;
        }
      }
    }
  }

  function renderValue(v){
    if (v == null) { app.textContent = ''; return; }
    if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
      app.textContent = String(v); return;
    }
    app.innerHTML = '<pre>' + JSON.stringify(v, null, 2) + '</pre>';
  }

  function chainFor(root, scenePath, segments){
    var c = gun.get(root).get('scene').get(scenePath);
    for (var i = 0; i < segments.length; i++) c = c.get(segments[i]);
    return c;
  }

  function buildContext(extra){
    var out = {};
    mergeInto(out, currentNode && isObject(currentNode.data) ? currentNode.data : null);
    mergeInto(out, currentData && isObject(currentData) ? currentData : null);
    mergeInto(out, literalCtx && isObject(literalCtx) ? literalCtx : null);
    mergeInto(out, boundCtx && isObject(boundCtx) ? boundCtx : null);
    mergeInto(out, extra && isObject(extra) ? extra : null);
    return out;
  }

  function setHyperContext(ctx){
    window.hyper_context = {
      params: ctx || {},
      literal: literalCtx || {},
      bindings: bindingTargets || {},
      node: nodeDp
    };
    window.hyperContext = window.hyper_context;
  }

  function runNodeJS(ctx){
    if (!currentNode || currentNode.js == null) return;
    try {
      setHyperContext(ctx);
      new AsyncFunction(String(currentNode.js))();
    } catch (e) { console.error('[STREAM NODE JS FAIL]', e); }
  }

  function render(){
    if (fieldPath.length) {
      if (fieldPath[0] === 'data') { renderValue(dig(currentData, fieldPath, 1)); return; }
      renderValue(dig(currentNode, fieldPath, 0)); return;
    }
    if (currentNode && currentNode.html != null) {
      app.innerHTML = String(currentNode.html);
      var ctx = buildContext();
      setHyperContext(ctx);
      bindData(app, ctx);
      runNodeJS(ctx);
      bindData(app, buildContext());
      return;
    }
    if (currentNode && currentNode.data !== undefined) {
      renderValue(buildContext(currentNode.data)); return;
    }
    renderValue(currentNode);
  }

  function subscribeBinding(name, info){
    var parts = String(info.nodeDp || '').split('.');
    var root = parts[0];
    var scenePath = parts.length > 1 ? parts.slice(1).join('/') : '__root__';
    var fp = info.fieldPath || [];

    if (!fp.length) {
      var state = { node: {}, data: {} };
      function emitWhole(){
        var whole = {};
        mergeInto(whole, clean(state.node) || {});
        if (isObject(state.data)) whole.data = clean(state.data) || {};
        boundCtx[name] = whole;
        render();
      }
      gun.get(root).get('scene').get(scenePath).on(function(node){ state.node = node; emitWhole(); });
      gun.get(root).get('scene').get(scenePath).get('data').on(function(d){ state.data = d; emitWhole(); });
      return;
    }

    if (fp[0] === 'data') {
      gun.get(root).get('scene').get(scenePath).get('data').on(function(d){
        var base = clean(d) || {};
        var out = fp.length > 1 ? dig(base, fp, 1) : base;
        if (out === undefined) return;
        boundCtx[name] = out;
        render();
      });
      return;
    }

    if (fp.length === 1) {
      chainFor(root, scenePath, fp).on(function(v){
        if (v === undefined) return;
        boundCtx[name] = v;
        render();
      });
      return;
    }

    gun.get(root).get('scene').get(scenePath).on(function(node){
      var cleaned = clean(node) || {};
      var out = dig(cleaned, fp, 0);
      if (out === undefined) return;
      boundCtx[name] = out;
      render();
    });
  }

  window.action = function(payload){
    var key = 'inbox/' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
    return new Promise(function(resolve, reject){
      gun.get(nodeRoot).get('scene').get(key).put({
        data: JSON.stringify(payload || {})
      }, function(ack){
        if (ack && ack.err) { reject(new Error(ack.err)); return; }
        resolve(ack || { ok: true });
      });
    });
  };

  gun.get(nodeRoot).get('scene').get(nodeScenePath).on(function(node){
    currentNode = clean(node) || {};
    render();
  });

  // When fieldPath targets data (e.g. data.temp), always subscribe to
  // the node's own data child. When bindRoot/bindScenePath are set
  // (from nearestDataNode), also subscribe to that for bound data.
  // This ensures field-level streams work even if the relay cache
  // wasn't warm when the HTML was generated.
  if (fieldPath.length && fieldPath[0] === 'data') {
    gun.get(nodeRoot).get('scene').get(nodeScenePath).get('data').on(function(d){
      currentData = clean(d) || {};
      render();
    });
  }

  if (bindRoot && bindScenePath) {
    var isSameNode = (bindRoot === nodeRoot && bindScenePath === nodeScenePath);
    if (!isSameNode) {
      gun.get(bindRoot).get('scene').get(bindScenePath).get('data').on(function(d){
        currentData = clean(d) || {};
        render();
      });
    }
  }
  }

  Object.keys(bindingTargets).forEach(function(name){
    subscribeBinding(name, bindingTargets[name]);
  });

  setHyperContext(buildContext());
})();
<\/script>
</body>
</html>`;
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
  res.writeHead(200, { 'Content-Type': type, 'Access-Control-Allow-Origin': '*' });
  res.end(str);
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

function serverWrite(path, payload) {
  const existed = !!getNode(path);
  const warnings = validateContract(path, payload);

  // Update read cache
  mergeCacheNode(path, payload);

  // Write to Gun — radisk persists automatically
  gunPut(path, graph[path] || payload);

  return {
    ok: true,
    existed_before: existed,
    contract_warnings: warnings.length ? warnings : undefined,
  };
}

function serverDelete(path) {
  const removed = descendantsOf(path);
  for (const dp of removed) {
    gunNullOut(dp);
    deleteNodeCache(dp);
  }
  return { ok: true, removed: removed.length };
}

function snapshotForRoot(rootName) {
  const snap = {};
  const pfx = rootName + '.';
  for (const [dp, data] of Object.entries(graph)) {
    if (!(dp === rootName || dp.startsWith(pfx))) continue;
    if (dp === rootName) continue;
    snap[dp.slice(pfx.length).replace(/\./g, '/')] = nodeForRead(data);
  }
  return snap;
}

function clearRoot(rootName) {
  const paths = descendantsOf(rootName);
  for (const dp of paths) {
    gunNullOut(dp);
    deleteNodeCache(dp);
  }
  return { ok: true, cleared: paths.length };
}

// ─── HTTP SERVER ──────────────────────────────────────────────────────

const server = http.createServer(async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
  if (req.method === 'OPTIONS') { res.writeHead(200); res.end(); return; }

  const parsed = new URL(req.url, 'http://' + (req.headers.host || 'localhost'));
  const pathname = parsed.pathname.replace(/\/+$/, '') || '/';
  const wantsJSON = parsed.searchParams.has('json') || (req.headers.accept || '').includes('application/json');

  if (pathname === '/gun') return;

  if (pathname === '/') {
    if (wantsJSON) return sendJson(res, relayMeta(parsed.origin));
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Access-Control-Allow-Origin': '*' });
    return res.end('<pre>' + JSON.stringify(relayMeta(parsed.origin), null, 2) + '</pre>');
  }

  const apiMatch = pathname.match(/^\/([^/]+)\/api\/(snapshot|clear|keys|validate)$/);
  if (apiMatch) {
    const root = decodeURIComponent(apiMatch[1]);
    const op = apiMatch[2];
    if (op === 'snapshot' && req.method === 'GET') return sendJson(res, snapshotForRoot(root));
    if (op === 'clear' && req.method === 'POST') return sendJson(res, clearRoot(root));
    if (op === 'keys' && req.method === 'GET') return sendJson(res, Object.keys(snapshotForRoot(root)).sort());
    if (op === 'validate' && req.method === 'GET') {
      const allWarnings = [];
      for (const dp of descendantsOf(root)) {
        const node = getNode(dp);
        if (node) allWarnings.push(...validateContract(dp, node));
      }
      return sendJson(res, {
        ok: allWarnings.length === 0,
        warnings: allWarnings,
        checked: descendantsOf(root).length,
      });
    }
  }

  // Persistence status
  if (pathname === '/api/persist/status' && req.method === 'GET') {
    return sendJson(res, {
      engine: 'radisk',
      dataDir: RADISK_DIR,
      cachedNodes: Object.keys(graph).length,
    });
  }

  const raw = decodeURIComponent(pathname.slice(1));

  const treePath = stripSuffix(raw, '.tree');
  if (treePath && req.method === 'GET') {
    return sendJson(res, { _path: treePath + '.tree', tree: { path: treePath, children: childrenOf(treePath) } });
  }

  const streamPath = stripSuffix(raw, '.stream') || stripSuffix(raw, '._connect');
  if (streamPath && req.method === 'GET') {
    const intent = parseIntentQuery(parsed.searchParams);
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Access-Control-Allow-Origin': '*' });
    return res.end(connectHTML(streamPath, intent));
  }

  if (req.method === 'GET') {
    // Try cache first, fall back to async Gun/radisk read
    let resolved = resolve(raw, parsed.origin);
    if (!resolved) {
      const asyncResult = await resolveAsync(raw);
      if (asyncResult) {
        resolved = resolve(raw, parsed.origin);
      }
    }
    if (!resolved) return sendJson(res, { error: 'not found', path: raw }, 404);
    if (wantsJSON || resolved.type === 'node' || resolved.type === 'json') return sendJson(res, resolved.data);
    return sendRaw(res, resolved.data);
  }

  if (req.method === 'PUT') {
    const body = await readBody(req);
    let payload = {};
    try { payload = body ? JSON.parse(body) : {}; }
    catch (_) { return sendJson(res, { error: 'invalid json' }, 400); }
    return sendJson(res, serverWrite(raw, payload));
  }

  if (req.method === 'DELETE') {
    return sendJson(res, serverDelete(raw));
  }

  return sendJson(res, { error: 'method not allowed' }, 405);
});

// ─── BOOT ─────────────────────────────────────────────────────────────
// Gun with radisk: file option tells Gun where to persist on disk.
// On restart, radisk loads persisted data and feeds it through the
// wire protocol, which populates the in-memory cache via ingestPutMessage.
gun = Gun({
  web: server,
  peers: PEERS,
  file: RADISK_DIR,
  radisk: true,
});

initGunSync();
log('CACHE', 'cache will warm from radisk + Gun traffic');

server.listen(PORT, BIND, () => {
  log('HTTP', 'listening on http://' + BIND + ':' + PORT);
  log('PERSIST', 'radisk data dir:', RADISK_DIR);
});