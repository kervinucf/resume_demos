/**
 * Scene Relay — serves the shell, syncs the graph.
 *
 * Key addition: GET /{bucket}/ now returns the full shell HTML with
 * the correct Gun peer list injected. Browsers on any machine get
 * the right peers automatically — no manual URL fiddling.
 *
 * Env vars:
 *   PORT               (default 8765)
 *   HYPER_BIND_HOST    (default 0.0.0.0)
 *   HYPER_PEERS        (JSON array of Gun peer URLs)
 *   HYPER_MACHINE_ID
 *   HYPER_MACHINE_NAME
 *   HYPER_DISCOVERY
 */

const Gun = require('gun');
require('gun/sea');
const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = parseInt(process.env.PORT || '8765', 10);
const BIND = process.env.HYPER_BIND_HOST || '0.0.0.0';

// Peers the Python client computed (includes LAN IPs, mDNS results, etc.)
let PEERS;
try {
  PEERS = JSON.parse(process.env.HYPER_PEERS || '[]');
} catch (_) {
  PEERS = [];
}

const buckets = {};
const tokens = {};

const CONTENT_FIELDS = [
  'html', 'css', 'js', 'link', 'json', 'data', 'meta', 'links', 'actions',
  'layer', 'fixed', 'portal'
];

const ALL_FIELDS = [
  ...CONTENT_FIELDS,
  'lat', 'lng', 'altitude', 'duration', 'remove'
];

function getBucket(name) {
  if (!buckets[name]) buckets[name] = { snapshot: {}, subscribed: false };
  return buckets[name];
}

function hasContent(data) {
  return CONTENT_FIELDS.some(f => data[f] !== undefined && data[f] !== null);
}

function cleanNodeData(data) {
  const clean = {};
  for (const k of Object.keys(data || {})) {
    if (k === '_' || k === '#' || k === '>') continue;
    if (data[k] !== null) clean[k] = data[k];
  }
  delete clean.remove;
  return clean;
}

function subscribe(name) {
  const b = getBucket(name);
  if (b.subscribed) return;
  b.subscribed = true;

  gun.get(name).get('scene').map().on((data, key) => {
    if (!data || key === '_') return;

    if (!hasContent(data)) {
      delete b.snapshot[key];
      return;
    }

    const clean = cleanNodeData(data);
    if (Object.keys(clean).length > 0) b.snapshot[key] = clean;
    else delete b.snapshot[key];
  });
}

function updateSnapshot(b, key, data) {
  if (!hasContent(data)) return;
  const existing = b.snapshot[key] || {};
  const merged = { ...existing };

  for (const [k, v] of Object.entries(data)) {
    if (v !== null && v !== undefined) merged[k] = v;
  }

  delete merged.remove;

  if (Object.keys(merged).length > 0) b.snapshot[key] = merged;
  else delete b.snapshot[key];
}

function deleteSnapshotPath(b, key) {
  delete b.snapshot[key];
  const prefix = key + '/';
  for (const k of Object.keys(b.snapshot)) {
    if (k.startsWith(prefix)) delete b.snapshot[k];
  }
}

function nullOut(bucketName, key) {
  const node = gun.get(bucketName).get('scene').get(key);
  const tombstone = {};
  for (const f of ALL_FIELDS) tombstone[f] = null;
  node.put(tombstone);
}

function nullOutDescendants(bucketName, snapshot, key) {
  nullOut(bucketName, key);
  const prefix = key + '/';
  for (const k of Object.keys(snapshot)) {
    if (k.startsWith(prefix)) nullOut(bucketName, k);
  }
}

function checkAuth(bkt, req) {
  const tok = tokens[bkt];
  if (!tok) return true;
  return (req.headers['authorization'] || '') === 'Bearer ' + tok;
}

function sendJson(res, obj, status = 200) {
  if (res.headersSent) return;
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(obj));
}

function readBody(req) {
  return new Promise(resolve => {
    let d = '';
    req.on('data', c => d += c);
    req.on('end', () => resolve(d));
  });
}

// ------------------------------------------------------------------
// Shell HTML — built once at startup with the correct peers
// ------------------------------------------------------------------

function buildPeersForBrowser(req) {
  // Start with the peers the Python client gave us
  const peers = [...PEERS];

  // Also add origin-relative /gun so the browser always peers with
  // whatever relay it's currently looking at
  // (handled client-side via location.origin)

  return peers;
}

function buildShellHTML(bucket, req) {
  // Figure out what Gun peer URLs the browser should use.
  // We always include the request's own origin so it works
  // whether you access via IP, hostname, or .local
  const peers = buildPeersForBrowser(req);
  const peersJSON = JSON.stringify(peers);

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>${bucket}</title>
  <style>
    html, body, #scene { margin:0; padding:0; width:100vw; height:100vh; overflow:hidden; background:#000; }
    #scene { position:relative; }
  </style>
</head>
<body>
<div id="scene"></div>
<script src="/gun/gun.js"></script>
<script src="/gun/sea.js"></script>
<script>
(function(){
  'use strict';

  // Peers from the Python client (LAN IPs, mDNS, etc.)
  // Plus the current origin so it always works regardless of how you got here
  var CONFIGURED_PEERS = ${peersJSON};
  var originPeer = location.origin + '/gun';
  var allPeers = [originPeer];
  for (var i = 0; i < CONFIGURED_PEERS.length; i++) {
    if (CONFIGURED_PEERS[i] !== originPeer) allPeers.push(CONFIGURED_PEERS[i]);
  }

  var bucket = ${JSON.stringify(bucket)};

  window.$gun = Gun({ peers: allPeers });
  window.$root = window.$gun.get(bucket);
  window.$scene = window.$root.get('scene');
  window.$bucket = bucket;

  var root = document.getElementById('scene');
  var live = {};
  var AF = Object.getPrototypeOf(async function(){}).constructor;
  var jsHashes = {};

  live["root"] = root;

  var CONTENT_FIELDS = [
    'html','css','js','link','json','data','meta','links','actions',
    'layer','fixed','portal'
  ];

  function hasContent(data) {
    return CONTENT_FIELDS.some(function(f){ return data[f] !== undefined && data[f] !== null; });
  }

  function isCleared(data) {
    if (!data) return true;
    return !hasContent(data);
  }

  function keyToId(key) {
    return 'frag-' + String(key).replace(/[^\\w~-]/g, '_');
  }

  function parentKeyOf(key) {
    if (key.includes("~")) return key.split("~")[0] || null;
    var parts = key.split("/").filter(Boolean);
    if (parts.length <= 1) return null;
    return parts.slice(0, -1).join("/");
  }

  function hostForKey(key) {
    return live[key] || document.getElementById(keyToId(key)) || null;
  }

  function rootMountStyle(el, layer) {
    el.style.position = 'fixed';
    el.style.inset = '0';
    el.style.zIndex = String(layer);
    el.style.pointerEvents = layer <= 0 ? 'none' : 'auto';
  }

  function childMountStyle(el, layer) {
    el.style.position = 'relative';
    el.style.flex = '1';
    el.style.minWidth = '0';
    el.style.minHeight = '0';
    el.style.width = '';
    el.style.height = '';
    el.style.top = '';
    el.style.left = '';
    el.style.inset = '';
    el.style.zIndex = String(layer);
    el.style.pointerEvents = layer <= 0 ? 'none' : 'auto';
  }

  function ensureHost(key, resolved, data) {
    if (live[key]) return live[key];

    var host = document.createElement('div');
    host.id = keyToId(key);
    host.dataset.key = key;
    host.dataset.owner = key;

    var layer = Number(resolved && resolved.layer || data && data.layer || 0) || 0;
    var parentKey = parentKeyOf(key);
    var isRow = key.includes("~");
    var wantsRoot = !!(resolved && resolved.fixed || data && data.fixed || resolved && resolved.portal || data && data.portal);
    var parentHost = parentKey ? hostForKey(parentKey) : null;

    if (parentHost && !wantsRoot) {
      childMountStyle(host, layer);
      var mountPoint;
      if (isRow) {
        var row = parentHost.querySelector("[data-row]");
        if (!row) {
          row = document.createElement("div");
          row.dataset.row = "";
          row.style.display = "flex";
          row.style.flex = "1";
          row.style.width = "100%";
          row.style.height = "100%";
          row.style.minHeight = "0";
          parentHost.appendChild(row);
        }
        mountPoint = row;
      } else {
        mountPoint = parentHost.querySelector('[data-children]') || parentHost;
      }
      mountPoint.appendChild(host);
    } else {
      rootMountStyle(host, layer);
      root.appendChild(host);
    }

    live[key] = host;
    return host;
  }

  function restyleHost(host, key, resolved, data) {
    var layer = Number(resolved && resolved.layer || data && data.layer || 0) || 0;
    var parentKey = parentKeyOf(key);
    var wantsRoot = !!(resolved && resolved.fixed || data && data.fixed || resolved && resolved.portal || data && data.portal);
    var parentHost = parentKey ? hostForKey(parentKey) : null;
    var shouldBeInRoot = !parentHost || wantsRoot;

    if (shouldBeInRoot) {
      rootMountStyle(host, layer);
      if (host.parentElement !== root) root.appendChild(host);
      return;
    }

    childMountStyle(host, layer);
    var isRow = key.includes("~");
    var mountPoint;
    if (isRow) {
      var row = parentHost.querySelector("[data-row]");
      if (!row) {
        row = document.createElement("div");
        row.dataset.row = "";
        row.style.display = "flex";
        row.style.flex = "1";
        row.style.width = "100%";
        row.style.height = "100%";
        row.style.minHeight = "0";
        parentHost.appendChild(row);
      }
      mountPoint = row;
    } else {
      mountPoint = parentHost.querySelector('[data-children]') || parentHost;
    }
    if (host.parentElement !== mountPoint) mountPoint.appendChild(host);
  }

  function pruneEmptyRows() {
    var rows = root.querySelectorAll("[data-row]");
    for (var i = 0; i < rows.length; i++) {
      if (!rows[i].children.length) rows[i].remove();
    }
  }

  function cleanup(key) {
    if (key === "root") return;
    var prefixA = key + '/';
    var prefixB = key + '~';
    for (var k of Object.keys(live)) {
      if (k === "root") continue;
      if (k === key || k.startsWith(prefixA) || k.startsWith(prefixB)) {
        if (live[k]) live[k].remove();
        delete live[k];
        var css = document.getElementById('css-' + k);
        if (css) css.remove();
        delete jsHashes[k];
      }
    }
    pruneEmptyRows();
  }

  function quickHash(str) {
    var h = 0;
    for (var i = 0; i < str.length; i++) {
      h = ((h << 5) - h + str.charCodeAt(i)) | 0;
    }
    return h;
  }

  function parseMaybeJSON(v) {
    if (typeof v !== 'string') return v;
    var t = v.trim();
    if (!t) return v;
    if ((t.startsWith('{') && t.endsWith('}')) || (t.startsWith('[') && t.endsWith(']'))) {
      try { return JSON.parse(t); } catch(_) {}
    }
    return v;
  }

  function applyBindings(host, resolved) {
    var nodes = host.querySelectorAll("[data-bind-text],[data-bind-html],[data-bind-style]");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var textKey = el.dataset.bindText;
      var htmlKey = el.dataset.bindHtml;
      var styleSpec = el.dataset.bindStyle;

      if (textKey && resolved[textKey] !== undefined) el.textContent = resolved[textKey];
      if (htmlKey && resolved[htmlKey] !== undefined) el.innerHTML = resolved[htmlKey];
      if (styleSpec) {
        var pairs = styleSpec.split(";").map(function(x){ return x.trim(); }).filter(Boolean);
        for (var j = 0; j < pairs.length; j++) {
          var parts = pairs[j].split(":").map(function(x){ return x.trim(); });
          if (parts[0] && parts[1] && resolved[parts[1]] !== undefined) {
            el.style[parts[0]] = resolved[parts[1]];
          }
        }
      }
    }
  }

  async function renderResolved(data, key, resolved) {
    if (!resolved) return;

    if (resolved.css !== undefined && resolved.css !== null) {
      var s = document.getElementById('css-' + key);
      if (!s) {
        s = document.createElement('style');
        s.id = 'css-' + key;
        document.head.appendChild(s);
      }
      s.textContent = resolved.css;
    }

    if (resolved.html !== undefined && resolved.html !== null) {
      var host = ensureHost(key, resolved, data);
      restyleHost(host, key, resolved, data);
      if (!host._mounted) {
        host.innerHTML = resolved.html;
        host._mounted = true;
      }
      applyBindings(host, resolved);
    }

    if (resolved.js !== undefined && resolved.js !== null) {
      var h = quickHash(resolved.js);
      if (jsHashes[key] !== h) {
        jsHashes[key] = h;
        try { await new AF(resolved.js)(); } catch(e) { console.error('[' + key + ']', e); }
      }
    }
  }

  async function render(data, key) {
    if (!data || key === '_') return;
    if (isCleared(data)) { cleanup(key); return; }
    await renderResolved(data, key, data);
  }

  window.$scene.map().on(function(data, key){ render(data, key); });

  async function syncFromSnapshot() {
    try {
      var resp = await fetch('/' + bucket + '/api/snapshot', {cache:'no-store'});
      var snap = await resp.json();
      if (!snap || typeof snap !== 'object') return;

      var keys = Object.keys(snap).sort(function(a,b){
        return a.replace(/~/g,'/').split('/').length - b.replace(/~/g,'/').split('/').length;
      });
      var wanted = new Set(keys);

      for (var i = 0; i < keys.length; i++) {
        await render(snap[keys[i]], keys[i]);
      }

      for (var k of Object.keys(live)) {
        if (k === "root") continue;
        if (!wanted.has(k)) cleanup(k);
      }
    } catch(e) {
      console.warn('[snapshot sync] failed:', e);
    }
  }

  (async function bootstrap(){
    await syncFromSnapshot();
    setInterval(syncFromSnapshot, 250);
  })();
})();
</script>
</body>
</html>`;
}

// ------------------------------------------------------------------
// HTTP server
// ------------------------------------------------------------------

const server = http.createServer(async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');

  if (req.method === 'OPTIONS') {
    res.writeHead(200);
    res.end();
    return;
  }

  const parsed = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const segs = parsed.pathname.split('/').filter(Boolean);

  // Gun handles its own route
  if (segs[0] === 'gun') return;

  // Root — relay info
  if (segs.length === 0) return sendJson(res, { relay: true, buckets: Object.keys(buckets) });

  const bkt = segs[0];
  const action = segs[1] || '';
  const rest = segs.slice(2).join('/');

  subscribe(bkt);
  const b = getBucket(bkt);

  // GET /{bucket}/ or /{bucket}/index.html — serve the shell with peers injected
  if (req.method === 'GET' && (action === '' || action === 'index.html')) {
    const html = buildShellHTML(bkt, req);
    res.writeHead(200, { 'Content-Type': 'text/html' });
    return res.end(html);
  }

  // Scene CRUD
  if (action === 'scene' && rest) {
    if (req.method === 'PUT') {
      if (!checkAuth(bkt, req)) return sendJson(res, { error: 'unauthorized' }, 401);
      try {
        const data = JSON.parse(await readBody(req));
        gun.get(bkt).get('scene').get(rest).put(data);
        updateSnapshot(b, rest, data);
        return sendJson(res, { ok: true, path: rest });
      } catch (e) {
        return sendJson(res, { error: e.message }, 400);
      }
    }

    if (req.method === 'DELETE') {
      if (!checkAuth(bkt, req)) return sendJson(res, { error: 'unauthorized' }, 401);
      nullOutDescendants(bkt, b.snapshot, rest);
      deleteSnapshotPath(b, rest);
      return sendJson(res, { ok: true, path: rest });
    }

    if (req.method === 'GET') {
      return sendJson(res, b.snapshot[rest] || null);
    }
  }

  // Fragment shorthand
  if (action === 'frag' && rest) {
    if (req.method === 'GET') {
      const html = b.snapshot[rest] ? (b.snapshot[rest].html || '') : '';
      res.writeHead(200, { 'Content-Type': 'text/html' });
      return res.end(html);
    }
    if (req.method === 'POST') {
      if (!checkAuth(bkt, req)) return sendJson(res, { error: 'unauthorized' }, 401);
      const raw = await readBody(req);
      const ct = req.headers['content-type'] || '';
      let html = raw;
      if (ct.includes('json')) {
        try { html = JSON.parse(raw).html || raw; } catch(e) {}
      }
      gun.get(bkt).get('scene').get(rest).put({ html });
      updateSnapshot(b, rest, { html });
      res.writeHead(200, { 'Content-Type': 'text/html' });
      return res.end(html);
    }
  }

  // API endpoints
  if (action === 'api') {
    if (req.method === 'GET' && rest === 'keys') {
      return sendJson(res, Object.keys(b.snapshot));
    }
    if (req.method === 'GET' && rest === 'snapshot') {
      return sendJson(res, b.snapshot);
    }
    if (req.method === 'GET' && rest === 'stats') {
      return sendJson(res, { fragments: Object.keys(b.snapshot).length, auth: !!tokens[bkt] });
    }
    if (req.method === 'POST' && rest === 'clear') {
      if (!checkAuth(bkt, req)) return sendJson(res, { error: 'unauthorized' }, 401);
      const keys = Object.keys(b.snapshot);
      for (const key of keys) nullOut(bkt, key);
      b.snapshot = {};
      return sendJson(res, { ok: true, cleared: keys.length });
    }
    if (req.method === 'POST' && rest === 'auth') {
      try {
        const { token } = JSON.parse(await readBody(req));
        if (!token) delete tokens[bkt];
        else tokens[bkt] = token;
        return sendJson(res, { ok: true, auth: !!tokens[bkt] });
      } catch (e) {
        return sendJson(res, { error: 'need {token}' }, 400);
      }
    }
  }

  if (!res.headersSent) {
    res.writeHead(404);
    res.end('Not found');
  }
});

const gun = Gun({ web: server });

server.listen(PORT, BIND, () => {
  console.log(`Relay: http://localhost:${PORT}`);
});