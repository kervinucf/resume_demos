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

const graph = Object.create(null);
const localMsgIds = new Set();
const seenPutIds = new Map();
const sseClients = new Set();
let nextEventId = 1;
let gun;

function log(tag, ...args) {
  console.log(new Date().toISOString(), `[${tag}]`, ...args);
}

function isObject(v) {
  return !!v && typeof v === 'object' && !Array.isArray(v);
}

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

function deleteNode(dp) {
  delete graph[dp];
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
    return null;
  }

  graph[dp] = ex;
  return ex;
}

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
    stream: pathURL(origin, dp, '.stream'),
    events: pathURL(origin, dp, '.events'),
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
  log('GUN-PUT', dp, JSON.stringify(data));
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
    mergeNode(dp, clean);
    notifyImpacted(dp);
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
  if (v.endsWith('.events')) v = v.slice(0, -7);
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
  if (raw.endsWith('.events')) raw = raw.slice(0, -7);

  const parts = raw.split('.').filter(Boolean);
  if (!parts.length) {
    return { path: raw, nodeDp: raw, fieldPath: [] };
  }

  const root = parts[0];
  const rest = parts.slice(1);

  // In this graph model, these are field namespaces on a node,
  // not additional scene segments.
  const FIELD_NAMES = new Set([
    'data',
    'html',
    'css',
    'js',
    'fixed',
    'layer'
  ]);

  let fieldStart = -1;
  for (let i = 0; i < rest.length; i++) {
    if (FIELD_NAMES.has(rest[i])) {
      fieldStart = i;
      break;
    }
  }

  const nodeSegs = fieldStart === -1 ? rest : rest.slice(0, fieldStart);
  const fieldPath = fieldStart === -1 ? [] : rest.slice(fieldStart);

  return {
    path: raw,
    nodeDp: [root].concat(nodeSegs).join('.'),
    fieldPath
  };
}
function relayMeta(origin) {
  const roots = Array.from(new Set(Object.keys(graph).map(k => k.split('.')[0]))).sort();
  return {
    relay: true,
    roots,
    bind: BIND,
    port: PORT,
    peers: PEERS.slice(),
    _links: Object.fromEntries(roots.map(r => [r, pathURL(origin, r)])),
  };
}

function browserPutHelperJS() {
  return `
function hyperScenePut(gun, root, sceneKey, payload){
  return new Promise(function(resolve, reject){
    gun.get(root).get('scene').get(sceneKey).put(payload, function(ack){
      if(ack && ack.err){ reject(new Error(ack.err)); return; }
      resolve(ack || {ok:true});
    });
  });
}
`;
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

  var gun = Gun({ peers: peers });
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

  function log(tag, value){
    try { console.log('[STREAM FIX]', tag, value); } catch (_) {}
  }

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
    Object.keys(src).forEach(function(k){
      dst[k] = src[k];
    });
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
    if (v == null) {
      app.textContent = '';
      return;
    }
    if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
      app.textContent = String(v);
      return;
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
    } catch (e) {
      console.error('[STREAM NODE JS FAIL]', e);
    }
  }

  function render(){
    if (fieldPath.length) {
      if (fieldPath[0] === 'data') {
        renderValue(dig(currentData, fieldPath, 1));
        return;
      }
      renderValue(dig(currentNode, fieldPath, 0));
      return;
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
      renderValue(buildContext(currentNode.data));
      return;
    }

    renderValue(currentNode);
  }

  function subscribeBinding(name, info){
    var parts = String(info.nodeDp || '').split('.');
    var root = parts[0];
    var scenePath = parts.length > 1 ? parts.slice(1).join('/') : '__root__';
    var fp = info.fieldPath || [];

    log('binding:init', { name:name, info:info });

    if (!fp.length) {
      var state = { node: {}, data: {} };

      function emitWhole(){
        var whole = {};
        mergeInto(whole, clean(state.node) || {});
        if (isObject(state.data)) whole.data = clean(state.data) || {};
        boundCtx[name] = whole;
        log('binding:whole', { name:name, value:whole });
        render();
      }

      gun.get(root).get('scene').get(scenePath).on(function(node){
        state.node = node;
        emitWhole();
      });

      gun.get(root).get('scene').get(scenePath).get('data').on(function(d){
        state.data = d;
        emitWhole();
      });

      return;
    }

    if (fp[0] === 'data') {
      gun.get(root).get('scene').get(scenePath).get('data').on(function(d){
        var base = clean(d) || {};
        var out = fp.length > 1 ? dig(base, fp, 1) : base;
        if (out === undefined) return;
        boundCtx[name] = out;
        log('binding:data', { name:name, value:out });
        render();
      });
      return;
    }

    if (fp.length === 1) {
      chainFor(root, scenePath, fp).on(function(v){
        if (v === undefined) return;
        boundCtx[name] = v;
        log('binding:leaf', { name:name, value:v });
        render();
      });
      return;
    }

    gun.get(root).get('scene').get(scenePath).on(function(node){
      var cleaned = clean(node) || {};
      var out = dig(cleaned, fp, 0);
      if (out === undefined) return;
      boundCtx[name] = out;
      log('binding:nested', { name:name, value:out });
      render();
    });
  }

  window.action = function(payload){
    var key = 'inbox/' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
    return new Promise(function(resolve, reject){
      gun.get(nodeRoot).get('scene').get(key).put({
        data: JSON.stringify(payload || {})
      }, function(ack){
        if (ack && ack.err) {
          reject(new Error(ack.err));
          return;
        }
        resolve(ack || { ok: true });
      });
    });
  };

  gun.get(nodeRoot).get('scene').get(nodeScenePath).on(function(node){
    currentNode = clean(node) || {};
    log('node:update', currentNode);
    render();
  });

  if (bindRoot && bindScenePath) {
    gun.get(bindRoot).get('scene').get(bindScenePath).get('data').on(function(d){
      currentData = clean(d) || {};
      log('data:update', currentData);
      render();
    });
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
  res.writeHead(200, {
    'Content-Type': type,
    'Access-Control-Allow-Origin': '*',
  });
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
  mergeNode(path, payload);
  gunPut(path, graph[path] || payload);
  notifyImpacted(path);
  return { ok: true, existed_before: existed };
}

function serverDelete(path) {
  const removed = descendantsOf(path);
  for (const dp of removed) {
    gunNullOut(dp);
    deleteNode(dp);
  }
  notifyImpacted(path);
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
    deleteNode(dp);
  }
  notifyImpacted(rootName);
  return { ok: true, cleared: paths.length };
}

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

  const apiMatch = pathname.match(/^\/([^/]+)\/api\/(snapshot|clear|keys)$/);
  if (apiMatch) {
    const root = decodeURIComponent(apiMatch[1]);
    const op = apiMatch[2];
    if (op === 'snapshot' && req.method === 'GET') return sendJson(res, snapshotForRoot(root));
    if (op === 'clear' && req.method === 'POST') return sendJson(res, clearRoot(root));
    if (op === 'keys' && req.method === 'GET') return sendJson(res, Object.keys(snapshotForRoot(root)).sort());
  }

  const raw = decodeURIComponent(pathname.slice(1));

  const treePath = stripSuffix(raw, '.tree');
  if (treePath && req.method === 'GET') {
    return sendJson(res, { _path: treePath + '.tree', tree: { path: treePath, children: childrenOf(treePath) } });
  }

  const eventsPath = stripSuffix(raw, '.events');
  if (eventsPath && req.method === 'GET') {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      'Access-Control-Allow-Origin': '*',
    });
    const client = { res, path: eventsPath, origin: parsed.origin };
    sseClients.add(client);
    emitResolved(client, 'init');
    req.on('close', () => sseClients.delete(client));
    return;
  }

  const streamPath = stripSuffix(raw, '.stream') || stripSuffix(raw, '._connect');
  if (streamPath && req.method === 'GET') {
    const intent = parseIntentQuery(parsed.searchParams);
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Access-Control-Allow-Origin': '*' });
    return res.end(connectHTML(streamPath, intent));
  }

  if (req.method === 'GET') {
    const resolved = resolve(raw, parsed.origin);
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

gun = Gun({ web: server, peers: PEERS });
initGunSync();

server.listen(PORT, BIND, () => {
  log('HTTP', 'listening on http://' + BIND + ':' + PORT);
});