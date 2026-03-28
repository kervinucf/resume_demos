const Gun = require('gun');
const http = require('http');

// Neutralize SEA's YSON — crashes on HTML/JS with curly braces
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

// =========================================================================
// GRAPH STORE
// =========================================================================
const graph = {};
const tokens = {};
const localMsgIds = new Set();

function getNode(dp) { return graph[dp] || null; }

function mergeNode(dp, data) {
  const ex = graph[dp] || {};
  for (const [k, v] of Object.entries(data || {})) {
    if (k === '_' || k === '#' || k === '>') continue;
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

function deleteNode(dp) { delete graph[dp]; }

function childrenOf(dp) {
  const pfx = dp ? dp + '.' : '';
  const kids = new Set();
  for (const key of Object.keys(graph)) {
    if (!pfx) {
      kids.add(key.split('.')[0]);
      continue;
    }
    if (key.startsWith(pfx)) {
      const rest = key.slice(pfx.length);
      const head = rest.split('.')[0];
      if (head) kids.add(dp + '.' + head);
    }
  }
  return Array.from(kids).sort();
}

function descendantsOf(dp) {
  const pfx = dp + '.';
  return Object.keys(graph).filter(k => k === dp || k.startsWith(pfx));
}

// =========================================================================
// HATEOAS RESPONSE
// =========================================================================
function buildResponse(dp, origin) {
  const node = getNode(dp);
  const children = childrenOf(dp);
  const links = { self: origin + '/' + dp };

  const parts = dp.split('.');
  if (parts.length > 1) links.parent = origin + '/' + parts.slice(0, -1).join('.');

  for (const child of children) links[child.split('.').pop()] = origin + '/' + child;
  if (node) for (const k of Object.keys(node)) links[k] = origin + '/' + dp + '.' + k;

  return { _path: dp, _links: links, ...(node || {}) };
}

function resolve(dp, origin) {
  const node = getNode(dp);
  if (node) return { type: 'node', data: buildResponse(dp, origin) };

  const parts = dp.split('.');
  for (let i = parts.length - 1; i >= 1; i--) {
    const np = parts.slice(0, i).join('.');
    const fp = parts.slice(i);
    const n = getNode(np);
    if (!n) continue;

    let val = n;
    for (const f of fp) {
      if (val && typeof val === 'object') val = val[f];
      else { val = undefined; break; }
    }
    if (val !== undefined) return { type: typeof val === 'object' ? 'json' : 'raw', data: val };
  }

  const kids = childrenOf(dp);
  if (kids.length > 0) return { type: 'node', data: buildResponse(dp, origin) };

  return null;
}

// =========================================================================
// GUN INTEGRATION
// =========================================================================
function dotToScenePath(dp) {
  const parts = dp.split('.');
  return { root: parts[0], scenePath: parts.slice(1).join('/') };
}

function gunPut(dp, data) {
  const { root, scenePath } = dotToScenePath(dp);
  if (!scenePath) return null;

  const soul = root + '/scene/' + scenePath;
  const parentSoul = root + '/scene';
  const state = Gun.state();

  const node = { '_': { '#': soul, '>': {} } };
  for (const k in data) {
    if (k === '_' || k === '#' || k === '>') continue;
    node[k] = data[k];
    node['_']['>'][k] = state;
  }

  const parentNode = { '_': { '#': parentSoul, '>': {} } };
  parentNode[scenePath] = { '#': soul };
  parentNode['_']['>'][scenePath] = state;

  const rootNode = { '_': { '#': root, '>': {} } };
  rootNode.scene = { '#': parentSoul };
  rootNode['_']['>'].scene = state;

  const put = {};
  put[soul] = node;
  put[parentSoul] = parentNode;
  put[root] = rootNode;

  const id = 'p_' + Math.random().toString(36).slice(2, 11);
  const msg = { put, '#': id };
  localMsgIds.add(id);
  gun._.on('in', msg);
  gun._.on('out', msg);
  return id;
}

function gunNullOut(dp) {
  const n = getNode(dp);
  if (!n) return null;
  const tomb = {};
  for (const k of Object.keys(n)) tomb[k] = null;
  return gunPut(dp, tomb);
}

function initGunSync() {
  gun.on('in', function(msg) {
    if (!msg || !msg.put) {
      this.to.next(msg);
      return;
    }

    if (msg['#'] && localMsgIds.has(msg['#'])) {
      localMsgIds.delete(msg['#']);
      this.to.next(msg);
      return;
    }

    const touched = new Set();
    for (const soul of Object.keys(msg.put)) {
      const parts = soul.split('/');
      if (!(parts.length >= 3 && parts[1] === 'scene')) continue;

      const dp = parts[0] + '.' + parts.slice(2).join('.');
      const nd = msg.put[soul] || {};
      const clean = {};
      let sawField = false;
      for (const k of Object.keys(nd)) {
        if (k === '_' || k === '#' || k === '>') continue;
        sawField = true;
        clean[k] = nd[k];
      }

      if (!sawField) continue;
      mergeNode(dp, clean);
      touched.add(dp);
    }

    for (const dp of touched) notifyImpacted(dp);
    this.to.next(msg);
  });
}

// =========================================================================
// LIVE RESOURCES: browser ._connect + Python ._events
// =========================================================================
const sseClients = new Set();
let nextEventId = 1;

function stripSuffix(dp, suffix) {
  return dp.endsWith(suffix) ? dp.slice(0, -suffix.length) : null;
}

function impactsPath(mutatedDp, subPath) {
  return mutatedDp === subPath ||
    mutatedDp.startsWith(subPath + '.') ||
    subPath.startsWith(mutatedDp + '.');
}

function sendSSE(res, eventName, payload) {
  const id = nextEventId++;
  res.write('id: ' + id + '\n');
  res.write('event: ' + eventName + '\n');
  const body = JSON.stringify(payload);
  for (const line of body.split('\n')) res.write('data: ' + line + '\n');
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

setInterval(function() {
  for (const client of Array.from(sseClients)) {
    try { client.res.write(': ping\n\n'); }
    catch (_) {
      try { client.res.end(); } catch (_) {}
      sseClients.delete(client);
    }
  }
}, 25000);

function resolveConnectTarget(dp) {
  if (getNode(dp)) return { kind: 'node', nodeDp: dp, fieldPath: [] };
  if (childrenOf(dp).length > 0) return { kind: 'branch', nodeDp: dp, fieldPath: [] };

  const parts = dp.split('.');
  for (let i = parts.length - 1; i >= 1; i--) {
    const nodeDp = parts.slice(0, i).join('.');
    const fieldPath = parts.slice(i);
    if (getNode(nodeDp)) return { kind: 'field', nodeDp, fieldPath };
  }

  if (parts.length >= 2) {
    return {
      kind: 'field',
      nodeDp: parts.slice(0, -1).join('.'),
      fieldPath: [parts[parts.length - 1]],
    };
  }

  return { kind: 'branch', nodeDp: dp, fieldPath: [] };
}

function connectHTML(dp) {
  const peersJSON = JSON.stringify(PEERS);
  const target = resolveConnectTarget(dp);
  const root = target.nodeDp.split('.')[0];
  const branchScenePath = target.nodeDp.split('.').slice(1).join('/');
  const scenePath = target.nodeDp.split('.').slice(1).join('/');
  const fieldPath = target.fieldPath || [];
  const mode =
    target.kind === 'branch' ? 'json' :
    fieldPath[0] === 'html' ? 'html' :
    fieldPath[0] === 'css' ? 'css' :
    fieldPath.length === 0 ? 'json' : 'text';

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
pre{margin:0;white-space:pre-wrap;word-break:break-word}
</style>
</head>
<body>
<div id="app"></div>
<script>
(function(){
  var ALL_PEERS=${peersJSON};
  var local=location.origin+'/gun';
  if(ALL_PEERS.indexOf(local)===-1) ALL_PEERS.unshift(local);

  var gun=Gun({peers:ALL_PEERS});
  var root=${JSON.stringify(root)};
  var scenePath=${JSON.stringify(scenePath)};
  var branchScenePath=${JSON.stringify(branchScenePath)};
  var fieldPath=${JSON.stringify(fieldPath)};
  var targetKind=${JSON.stringify(target.kind)};
  var mode=${JSON.stringify(mode)};
  var app=document.getElementById('app');
  var branchSnap={};

  function clean(node){
    if(!node||typeof node!=='object') return null;
    var out={};
    Object.keys(node).forEach(function(k){
      if(k==='_'||k==='#'||k==='>') return;
      if(node[k] !== null && node[k] !== undefined) out[k]=node[k];
    });
    return out;
  }

  function dig(v,path){
    for(var i=1;i<path.length;i++){
      if(v==null) return undefined;
      v=v[path[i]];
    }
    return v;
  }

  function render(v){
    if(mode==='html'){
      app.innerHTML = v == null ? '' : String(v);
      return;
    }
    if(mode==='css'){
      app.innerHTML = '<style>' + (v == null ? '' : String(v)) + '</style>';
      return;
    }
    if(v && typeof v === 'object'){
      app.innerHTML = '<pre>' + JSON.stringify(v, null, 2) + '</pre>';
      return;
    }
    app.textContent = v == null ? '' : String(v);
  }

  if(targetKind==='branch'){
    gun.get(root).get('scene').map().on(function(d,k){
      if(!k||k==='_') return;
      if(branchScenePath && !(k===branchScenePath || k.indexOf(branchScenePath + '/')===0)) return;
      var cleaned = clean(d);
      if(cleaned && Object.keys(cleaned).length) branchSnap[k.replace(/\\//g,'.')] = cleaned;
      else delete branchSnap[k.replace(/\\//g,'.')];
      render(branchSnap);
    });
    return;
  }

  var chain = gun.get(root).get('scene').get(scenePath);

  if(fieldPath.length===0){
    chain.on(function(node){ render(clean(node) || {}); });
    return;
  }

  chain.get(fieldPath[0]).on(function(v){ render(dig(v, fieldPath)); });
})();
<\/script>
</body>
</html>`;
}

// =========================================================================
// BACKWARDS-COMPAT: snapshot for shell bootstrap
// =========================================================================
function snapshotForRoot(rootName) {
  const snap = {};
  const pfx = rootName + '.';
  for (const [dp, data] of Object.entries(graph)) {
    if (dp.startsWith(pfx)) snap[dp.slice(pfx.length).replace(/\./g, '/')] = data;
  }
  return snap;
}

// =========================================================================
// HTTP HELPERS
// =========================================================================
function sendJson(res, obj, status = 200) {
  if (res.headersSent) return;
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
  });
  res.end(JSON.stringify(obj, null, 2));
}

function sendRaw(res, val) {
  if (res.headersSent) return;
  const str = String(val);
  const ct = str.trim().startsWith('<') ? 'text/html' : 'text/plain';
  res.writeHead(200, {
    'Content-Type': ct + '; charset=utf-8',
    'Access-Control-Allow-Origin': '*',
  });
  res.end(str);
}

function readBody(req) {
  return new Promise(r => {
    let d = '';
    req.on('data', c => d += c);
    req.on('end', () => r(d));
  });
}

function checkAuth(dp, req) {
  const tok = tokens[dp.split('.')[0]];
  if (!tok) return true;
  return (req.headers['authorization'] || '') === 'Bearer ' + tok;
}

// =========================================================================
// SHELL HTML
// =========================================================================
function shellHTML(rootName) {
  const peersJSON = JSON.stringify(PEERS);
  return `<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>${rootName}</title>
<script src="https://cdn.jsdelivr.net/npm/gun/gun.js"><\/script>
<style>html,body,#scene{margin:0;padding:0;width:100vw;height:100vh;overflow:hidden;background:#000}#scene{position:relative}</style>
</head><body><div id="scene"></div>
<script>
(function(){
  var ALL_PEERS=${peersJSON},o=location.origin+'/gun';
  if(ALL_PEERS.indexOf(o)===-1)ALL_PEERS.unshift(o);
  var bucket=${JSON.stringify(rootName)};
  var gun=Gun({peers:ALL_PEERS}),scene=gun.get(bucket).get('scene');
  var root=document.getElementById('scene'),live={},AF=Object.getPrototypeOf(async function(){}).constructor,jsH={};
  live["root"]=root;
  window.$gun=gun;window.$scene=scene;window.$root=root;window.$bucket=bucket;
  window.$peers=ALL_PEERS.map(function(u){return u.endsWith("/gun")?u.slice(0,-4):u;});
  window.action=function(p){return fetch('/'+bucket+'/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p||{})}).catch(function(e){console.warn('[action]',e);});};
  var CF=['html','css','js','link','json','data','meta','links','actions','layer','fixed','portal'];
  function has(d){return CF.some(function(f){return d[f]!==undefined&&d[f]!==null})}
  function cleared(d){return !d||!has(d)}
  function kid(k){return'frag-'+String(k).replace(/[^\\w~-]/g,'_')}
  function par(k){if(k.includes('~'))return k.split('~')[0]||null;var p=k.split('/').filter(Boolean);return p.length<=1?null:p.slice(0,-1).join('/');}
  function host4(k){return live[k]||document.getElementById(kid(k))||null}
  function rootS(e,l){e.style.position='fixed';e.style.inset='0';e.style.zIndex=String(l);e.style.pointerEvents=l<=0?'none':'auto'}
  function childS(e,l){e.style.position='relative';e.style.flex='0 0 auto';e.style.minWidth='0';e.style.minHeight='';e.style.width='';e.style.height='';e.style.top='';e.style.left='';e.style.inset='';e.style.zIndex=String(l);e.style.pointerEvents=l<=0?'none':'auto'}
  function ensure(key,res,dat){if(live[key])return live[key];var h=document.createElement('div');h.id=kid(key);h.dataset.key=key;var layer=Number(res&&res.layer||dat&&dat.layer||0)||0;var pk=par(key);var isR=key.includes('~');var wRoot=!!(res&&res.fixed||dat&&dat.fixed||res&&res.portal||dat&&dat.portal);var ph=pk?host4(pk):null;if(ph&&!wRoot){childS(h,layer);var mp;if(isR){var row=ph.querySelector('[data-row]');if(!row){row=document.createElement('div');row.dataset.row='';row.style.display='flex';row.style.flex='1';row.style.width='100%';row.style.height='100%';row.style.minHeight='0';ph.appendChild(row)}mp=row}else{mp=ph.querySelector('[data-children]')||ph}mp.appendChild(h);}else{rootS(h,layer);root.appendChild(h)}live[key]=h;return h;}
  function restyle(h,key,res,dat){var layer=Number(res&&res.layer||dat&&dat.layer||0)||0;var pk=par(key);var wRoot=!!(res&&res.fixed||dat&&dat.fixed||res&&res.portal||dat&&dat.portal);var ph=pk?host4(pk):null;if(!ph||wRoot){rootS(h,layer);if(h.parentElement!==root)root.appendChild(h);return}childS(h,layer);var isR=key.includes('~');var mp;if(isR){var row=ph.querySelector('[data-row]');if(!row){row=document.createElement('div');row.dataset.row='';row.style.display='flex';row.style.flex='1';row.style.width='100%';row.style.height='100%';row.style.minHeight='0';ph.appendChild(row)}mp=row}else{mp=ph.querySelector('[data-children]')||ph}if(h.parentElement!==mp)mp.appendChild(h);}
  function prune(){var rows=root.querySelectorAll('[data-row]');for(var i=0;i<rows.length;i++)if(!rows[i].children.length)rows[i].remove()}
  function cleanup(key){if(key==='root')return;var a=key+'/',b=key+'~';for(var k of Object.keys(live)){if(k==='root')continue;if(k===key||k.startsWith(a)||k.startsWith(b)){if(live[k])live[k].remove();delete live[k];var c=document.getElementById('css-'+k);if(c)c.remove();delete jsH[k]}}prune();}
  function qh(s){var h=0;for(var i=0;i<s.length;i++)h=((h<<5)-h+s.charCodeAt(i))|0;return h}
  function bind(host,res){var nodes=host.querySelectorAll('[data-bind-text],[data-bind-html],[data-bind-style]');for(var i=0;i<nodes.length;i++){var el=nodes[i];if(el.dataset.bindText&&res[el.dataset.bindText]!==undefined)el.textContent=res[el.dataset.bindText];if(el.dataset.bindHtml&&res[el.dataset.bindHtml]!==undefined)el.innerHTML=res[el.dataset.bindHtml];if(el.dataset.bindStyle){var pairs=el.dataset.bindStyle.split(';');for(var j=0;j<pairs.length;j++){var pp=pairs[j].split(':');if(pp[0]&&pp[1]&&res[pp[1].trim()]!==undefined)el.style[pp[0].trim()]=res[pp[1].trim()]}}}}
  async function renderR(dat,key,res){if(!res)return;if(res.css!=null){var s=document.getElementById('css-'+key);if(!s){s=document.createElement('style');s.id='css-'+key;document.head.appendChild(s)}s.textContent=res.css}if(res.html!=null){var h=ensure(key,res,dat);restyle(h,key,res,dat);if(!h._m){h.innerHTML=res.html;h._m=true}bind(h,res)}if(res.js!=null){var hh=qh(res.js);if(jsH[key]!==hh){jsH[key]=hh;try{await new AF(res.js)()}catch(e){console.error('['+key+']',e)}}}}
  async function render(dat,key){if(!dat||key==='_')return;if(cleared(dat)){cleanup(key);return}await renderR(dat,key,dat);}
  scene.map().on(function(d,k){render(d,k)});
  (async function(){try{var r=await fetch('/'+bucket+'/api/snapshot',{cache:'no-store'});var snap=await r.json();if(!snap||typeof snap!=='object')return;var keys=Object.keys(snap).sort(function(a,b){return a.replace(/~/g,'/').split('/').length-b.replace(/~/g,'/').split('/').length});var w=new Set(keys);for(var i=0;i<keys.length;i++)await render(snap[keys[i]],keys[i]);for(var k of Object.keys(live)){if(k==='root')continue;if(!w.has(k))cleanup(k)}}catch(e){console.warn('[bootstrap]',e)}})();
})();
<\/script></body></html>`;
}

// =========================================================================
// HTTP ROUTER
// =========================================================================
const server = http.createServer(async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
  if (req.method === 'OPTIONS') { res.writeHead(200); res.end(); return; }

  const parsed = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const pathname = parsed.pathname.replace(/\/+$/, '') || '/';

  if (pathname === '/gun') return;

  if (pathname === '/') {
    const roots = new Set();
    for (const k of Object.keys(graph)) roots.add(k.split('.')[0]);
    const buckets = Array.from(roots).sort();
    return sendJson(res, {
      relay: true,
      buckets,
      _links: Object.fromEntries(buckets.map(r => [r, parsed.origin + '/' + r])),
    });
  }

  const raw = decodeURIComponent(pathname.slice(1));

  const liveConnectPath = stripSuffix(raw, '._connect');
  if (liveConnectPath && req.method === 'GET') {
    res.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Access-Control-Allow-Origin': '*',
    });
    return res.end(connectHTML(liveConnectPath));
  }

  const liveEventsPath = stripSuffix(raw, '._events');
  if (liveEventsPath && req.method === 'GET') {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      'Connection': 'keep-alive',
      'Access-Control-Allow-Origin': '*',
      'X-Accel-Buffering': 'no',
    });
    const client = { path: liveEventsPath, origin: parsed.origin, res };
    sseClients.add(client);
    emitResolved(client, 'snapshot');
    req.on('close', function() { sseClients.delete(client); });
    req.on('aborted', function() { sseClients.delete(client); });
    return;
  }

  const apiMatch = raw.match(/^([^./]+)\/api\/(\w+)$/);
  if (apiMatch) {
    const root = apiMatch[1];
    const op = apiMatch[2];

    if (req.method === 'GET' && op === 'snapshot') return sendJson(res, snapshotForRoot(root));
    if (req.method === 'GET' && op === 'keys') return sendJson(res, Object.keys(snapshotForRoot(root)));
    if (req.method === 'GET' && op === 'stats') {
      return sendJson(res, { fragments: Object.keys(snapshotForRoot(root)).length, auth: !!tokens[root] });
    }
    if (req.method === 'POST' && op === 'clear') {
      if (!checkAuth(root, req)) return sendJson(res, { error: 'unauthorized' }, 401);
      for (const d of descendantsOf(root)) {
        gunNullOut(d);
        deleteNode(d);
      }
      notifyImpacted(root);
      return sendJson(res, { ok: true });
    }
    if (req.method === 'POST' && op === 'auth') {
      try {
        const { token } = JSON.parse(await readBody(req));
        if (!token) delete tokens[root];
        else tokens[root] = token;
        return sendJson(res, { ok: true });
      } catch (_) {
        return sendJson(res, { error: 'need {token}' }, 400);
      }
    }
  }

  const sceneMatch = raw.match(/^([^./]+)\/scene\/(.+)$/);
  if (sceneMatch) {
    const dp = sceneMatch[1] + '.' + sceneMatch[2].replace(/\//g, '.');
    if (req.method === 'PUT') {
      if (!checkAuth(dp, req)) return sendJson(res, { error: 'unauthorized' }, 401);
      try {
        const d = JSON.parse(await readBody(req));
        mergeNode(dp, d);
        gunPut(dp, graph[dp] || d);
        notifyImpacted(dp);
        return sendJson(res, { ok: true, path: dp });
      } catch (e) {
        return sendJson(res, { error: e.message }, 400);
      }
    }
    if (req.method === 'GET') return sendJson(res, getNode(dp));
    if (req.method === 'DELETE') {
      if (!checkAuth(dp, req)) return sendJson(res, { error: 'unauthorized' }, 401);
      for (const d of descendantsOf(dp)) {
        gunNullOut(d);
        deleteNode(d);
      }
      notifyImpacted(dp);
      return sendJson(res, { ok: true });
    }
  }

  const actionMatch = raw.match(/^([^./]+)\/action$/);
  if (actionMatch && req.method === 'POST') {
    try {
      const p = JSON.parse(await readBody(req));
      const k = actionMatch[1] + '.inbox.' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
      mergeNode(k, { data: JSON.stringify(p) });
      gunPut(k, graph[k] || { data: JSON.stringify(p) });
      notifyImpacted(k);
      return sendJson(res, { ok: true, key: k });
    } catch (e) {
      return sendJson(res, { error: e.message }, 400);
    }
  }

  const dotPath = raw;

  if (!dotPath.includes('.') && !dotPath.includes('/') && req.method === 'GET') {
    const accept = req.headers['accept'] || '';
    if (accept.includes('text/html') && !parsed.searchParams.has('json')) {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      return res.end(shellHTML(dotPath));
    }
    return sendJson(res, buildResponse(dotPath, parsed.origin));
  }

  if (req.method === 'GET') {
    const origin = parsed.origin;
    const also = parsed.searchParams.getAll('also');
    if (also.length > 0) {
      const result = {};
      for (const p of [dotPath, ...also]) {
        const r = resolve(p, origin);
        result[p] = r ? r.data : null;
      }
      return sendJson(res, result);
    }

    const r = resolve(dotPath, origin);
    if (r) {
      if (r.type === 'raw') return sendRaw(res, r.data);
      return sendJson(res, r.data);
    }
    return sendJson(res, { error: 'not found', _path: dotPath, _links: { self: origin + '/' + dotPath } }, 404);
  }

  if (req.method === 'PUT') {
    if (!checkAuth(dotPath, req)) return sendJson(res, { error: 'unauthorized' }, 401);
    try {
      const d = JSON.parse(await readBody(req));
      mergeNode(dotPath, d);
      gunPut(dotPath, graph[dotPath] || d);
      notifyImpacted(dotPath);
      return sendJson(res, { ok: true, _path: dotPath });
    } catch (e) {
      return sendJson(res, { error: e.message }, 400);
    }
  }

  if (req.method === 'DELETE') {
    if (!checkAuth(dotPath, req)) return sendJson(res, { error: 'unauthorized' }, 401);
    const rm = descendantsOf(dotPath);
    for (const d of rm) {
      gunNullOut(d);
      deleteNode(d);
    }
    notifyImpacted(dotPath);
    return sendJson(res, { ok: true, removed: rm.length });
  }

  if (req.method === 'POST') {
    try {
      const p = JSON.parse(await readBody(req));
      const root = dotPath.split('.')[0];
      const k = root + '.inbox.' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
      mergeNode(k, { data: JSON.stringify(p) });
      gunPut(k, graph[k] || { data: JSON.stringify(p) });
      notifyImpacted(k);
      return sendJson(res, { ok: true, key: k });
    } catch (e) {
      return sendJson(res, { error: e.message }, 400);
    }
  }

  if (!res.headersSent) {
    res.writeHead(404);
    res.end('Not found');
  }
});

const gun = Gun({ peers: PEERS, web: server, radisk: true });
initGunSync();

server.listen(PORT, BIND, () => {
  console.log(`HyperRelay: http://localhost:${PORT}`);
  if (PEERS.length) console.log('Peering:', PEERS.join(', '));
});