/**
 * Scene Relay with EXHAUSTIVE DIAGNOSTICS
 *
 * Diagnostics added:
 * - Server: logs every WebSocket upgrade attempt
 * - Server: logs Gun peer connections
 * - Shell HTML: patches WebSocket BEFORE Gun loads
 * - Shell HTML: monitors gun.on('in') immediately after Gun creation
 * - Explorer: hooks into existing Gun instance
 */

const Gun = require('gun');
const http = require('http');

const PORT = parseInt(process.env.PORT || '8765', 10);
const BIND = process.env.HYPER_BIND_HOST || '0.0.0.0';

let PEERS;
try { PEERS = JSON.parse(process.env.HYPER_PEERS || '[]'); } catch (_) { PEERS = []; }

const buckets = {};
const tokens = {};

const CONTENT_FIELDS = [
  'html', 'css', 'js', 'link', 'json', 'data', 'meta', 'links', 'actions',
  'layer', 'fixed', 'portal'
];
const ALL_FIELDS = [...CONTENT_FIELDS, 'lat', 'lng', 'altitude', 'duration', 'remove'];

function getBucket(n) {
  if (!buckets[n]) buckets[n] = { snapshot: {}, subscribed: false };
  return buckets[n];
}

function hasContent(d) { return CONTENT_FIELDS.some(f => d[f] !== undefined && d[f] !== null); }

function cleanNodeData(d) {
  const c = {};
  for (const k of Object.keys(d || {})) { if (k === '_' || k === '#' || k === '>') continue; if (d[k] !== null) c[k] = d[k]; }
  delete c.remove;
  return c;
}

function subscribe(name) {
  const b = getBucket(name);
  if (b.subscribed) return;
  b.subscribed = true;
  gun.get(name).get('scene').map().on((data, key) => {
    if (!data || key === '_') return;
    const clean = cleanNodeData(data);
    if (Object.keys(clean).length > 0) b.snapshot[key] = clean;
    else delete b.snapshot[key];
  });
}

function updateSnapshot(b, key, data) {
  const merged = { ...(b.snapshot[key] || {}) };
  for (const [k, v] of Object.entries(data)) {
    if (v !== null && v !== undefined) merged[k] = v;
  }
  delete merged.remove;
  if (Object.keys(merged).length > 0) b.snapshot[key] = merged;
  else delete b.snapshot[key];
}

function deleteSnapshotPath(b, key) {
  delete b.snapshot[key];
  const pfx = key + '/';
  for (const k of Object.keys(b.snapshot)) { if (k.startsWith(pfx)) delete b.snapshot[k]; }
}

function nullOut(bkt, key) {
  const tomb = {};
  for (const f of ALL_FIELDS) tomb[f] = null;
  gunPut(bkt, key, tomb);
}

function nullOutDesc(bkt, snap, key) {
  nullOut(bkt, key);
  const pfx = key + '/';
  for (const k of Object.keys(snap)) { if (k.startsWith(pfx)) nullOut(bkt, k); }
}

function gunPut(bkt, scenePath, data) {
  var soul = bkt + '/scene/' + scenePath;
  var parentSoul = bkt + '/scene';
  var state = Gun.state();

  var node = { '_': { '#': soul, '>': {} } };
  for (var k in data) {
    if (k === '_') continue;
    node[k] = data[k];
    node['_']['>'][k] = state;
  }

  var parentNode = { '_': { '#': parentSoul, '>': {} } };
  parentNode[scenePath] = { '#': soul };
  parentNode['_']['>'][scenePath] = state;

  var bktNode = { '_': { '#': bkt, '>': {} } };
  bktNode['scene'] = { '#': parentSoul };
  bktNode['_']['>']['scene'] = state;

  var put = {};
  put[soul] = node;
  put[parentSoul] = parentNode;
  put[bkt] = bktNode;

  var msg = {
    put: put,
    '#': 'inject_' + Math.random().toString(36).slice(2, 11)
  };

  gun._.on('in', msg);
  gun._.on('out', msg);
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
  return new Promise(r => { let d = ''; req.on('data', c => d += c); req.on('end', () => r(d)); });
}

// ------------------------------------------------------------------
// Shell HTML — WebSocket monkey-patch BEFORE Gun loads
// ------------------------------------------------------------------

function shellHTML(bucket) {
  const peersJSON = JSON.stringify(PEERS);

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>${bucket}</title>
<style>html,body,#scene{margin:0;padding:0;width:100vw;height:100vh;overflow:hidden;background:#000}#scene{position:relative}</style>

<!-- DIAGNOSTIC: Patch WebSocket BEFORE Gun.js loads -->
<script>
(function(){
  var _WS = window.WebSocket;
  var _wc = 0;
  window._wsLog = [];
  window._wsInstances = [];

  window.WebSocket = function(url, protocols) {
    var id = ++_wc;
    var entry = {id:id, url:url, state:'CONNECTING', msgs:0};
    window._wsLog.push('[WS#'+id+'] NEW: '+url);
    console.log('[WS#'+id+'] NEW:', url);

    var ws = protocols ? new _WS(url, protocols) : new _WS(url);
    window._wsInstances.push({id:id, ws:ws, url:url});

    ws.addEventListener('open', function(){
      entry.state = 'OPEN';
      window._wsLog.push('[WS#'+id+'] OPEN');
      console.log('[WS#'+id+'] OPEN');
    });
    ws.addEventListener('close', function(e){
      entry.state = 'CLOSED';
      window._wsLog.push('[WS#'+id+'] CLOSED code='+e.code+' reason='+e.reason);
      console.log('[WS#'+id+'] CLOSED code='+e.code+' reason='+e.reason);
    });
    ws.addEventListener('error', function(e){
      entry.state = 'ERROR';
      window._wsLog.push('[WS#'+id+'] ERROR');
      console.log('[WS#'+id+'] ERROR (check browser network tab for details)');
    });
    ws.addEventListener('message', function(e){
      entry.msgs++;
      if(entry.msgs <= 3 || entry.msgs % 500 === 0){
        window._wsLog.push('[WS#'+id+'] MSG#'+entry.msgs+' len='+String(e.data).length);
        console.log('[WS#'+id+'] MSG#'+entry.msgs+' len='+String(e.data).length, String(e.data).slice(0,150));
      }
    });

    return ws;
  };
  window.WebSocket.prototype = _WS.prototype;
  window.WebSocket.CONNECTING = 0;
  window.WebSocket.OPEN = 1;
  window.WebSocket.CLOSING = 2;
  window.WebSocket.CLOSED = 3;

  console.log('[DIAG] WebSocket patched BEFORE Gun loads');
})();
</script>

<script src="https://cdn.jsdelivr.net/npm/gun/gun.js"><\/script>
</head>
<body>
<div id="scene"></div>
<script>
(function(){
  var ALL_PEERS=${peersJSON};
  var o=location.origin+'/gun';
  if(ALL_PEERS.indexOf(o)===-1) ALL_PEERS.unshift(o);
  var bucket=${JSON.stringify(bucket)};

  console.log('[SHELL] Creating Gun with peers:', ALL_PEERS);
  var gun=Gun({peers:ALL_PEERS});

  // DIAGNOSTIC: Monitor Gun inbound immediately
  var gunInCount = 0;
  gun.on('in', function(msg){
    gunInCount++;
    if(msg && msg.put && (gunInCount <= 10 || gunInCount % 100 === 0)){
      console.log('[SHELL gun.on(in)] #'+gunInCount+' souls:', Object.keys(msg.put).slice(0,3));
    }
    this.to.next(msg);
  });

  var mapCount = 0;
  var scene=gun.get(bucket).get('scene');
  scene.map().on(function(d,k){
    mapCount++;
    if(mapCount <= 10 || mapCount % 100 === 0){
      console.log('[SHELL map().on] #'+mapCount+' key='+k);
    }
    render(d,k);
  });

  // Export for explorer
  window.$gun = gun;
  window.$scene = scene;
  window.$gunInCount = function(){ return gunInCount; };
  window.$mapCount = function(){ return mapCount; };

  var root=document.getElementById('scene');
  var live={};
  var AF=Object.getPrototypeOf(async function(){}).constructor;
  var jsH={};
  live["root"]=root;

window.$root = root;
window.$bucket = bucket;
window.$peers = ALL_PEERS.map(function (u) {
  u = String(u || "");
  return u.endsWith("/gun") ? u.slice(0, -4) : u;
});

  window.action = function(payload) {
    return fetch('/' + bucket + '/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {})
    }).catch(function(e) { console.warn('[action]', e); });
  };

  var CF=['html','css','js','link','json','data','meta','links','actions','layer','fixed','portal'];
  function has(d){return CF.some(function(f){return d[f]!==undefined&&d[f]!==null})}
  function cleared(d){return !d||!has(d)}
  function kid(k){return 'frag-'+String(k).replace(/[^\\w~-]/g,'_')}
  function par(k){
    if(k.includes('~'))return k.split('~')[0]||null;
    var p=k.split('/').filter(Boolean);
    return p.length<=1?null:p.slice(0,-1).join('/');
  }
  function host4(k){return live[k]||document.getElementById(kid(k))||null}
  function rootS(e,l){e.style.position='fixed';e.style.inset='0';e.style.zIndex=String(l);e.style.pointerEvents=l<=0?'none':'auto'}
  function childS(e,l){e.style.position='relative';e.style.flex='0 0 auto';e.style.minWidth='0';e.style.minHeight='';e.style.width='';e.style.height='';e.style.top='';e.style.left='';e.style.inset='';e.style.zIndex=String(l);e.style.pointerEvents=l<=0?'none':'auto'}

  function ensure(key,res,dat){
    if(live[key])return live[key];
    var h=document.createElement('div');h.id=kid(key);h.dataset.key=key;
    var layer=Number(res&&res.layer||dat&&dat.layer||0)||0;
    var pk=par(key);var isR=key.includes('~');
    var wRoot=!!(res&&res.fixed||dat&&dat.fixed||res&&res.portal||dat&&dat.portal);
    var ph=pk?host4(pk):null;
    if(ph&&!wRoot){
      childS(h,layer);
      var mp;
      if(isR){var row=ph.querySelector('[data-row]');if(!row){row=document.createElement('div');row.dataset.row='';row.style.display='flex';row.style.flex='1';row.style.width='100%';row.style.height='100%';row.style.minHeight='0';ph.appendChild(row)}mp=row}
      else{mp=ph.querySelector('[data-children]')||ph}
      mp.appendChild(h);
    }else{rootS(h,layer);root.appendChild(h)}
    live[key]=h;return h;
  }

  function restyle(h,key,res,dat){
    var layer=Number(res&&res.layer||dat&&dat.layer||0)||0;
    var pk=par(key);var wRoot=!!(res&&res.fixed||dat&&dat.fixed||res&&res.portal||dat&&dat.portal);
    var ph=pk?host4(pk):null;
    if(!ph||wRoot){rootS(h,layer);if(h.parentElement!==root)root.appendChild(h);return}
    childS(h,layer);
    var isR=key.includes('~');var mp;
    if(isR){var row=ph.querySelector('[data-row]');if(!row){row=document.createElement('div');row.dataset.row='';row.style.display='flex';row.style.flex='1';row.style.width='100%';row.style.height='100%';row.style.minHeight='0';ph.appendChild(row)}mp=row}
    else{mp=ph.querySelector('[data-children]')||ph}
    if(h.parentElement!==mp)mp.appendChild(h);
  }

  function prune(){var rows=root.querySelectorAll('[data-row]');for(var i=0;i<rows.length;i++)if(!rows[i].children.length)rows[i].remove()}

  function cleanup(key){
    if(key==='root')return;
    var a=key+'/',b=key+'~';
    for(var k of Object.keys(live)){if(k==='root')continue;if(k===key||k.startsWith(a)||k.startsWith(b)){if(live[k])live[k].remove();delete live[k];var c=document.getElementById('css-'+k);if(c)c.remove();delete jsH[k]}}
    prune();
  }

  function qh(s){var h=0;for(var i=0;i<s.length;i++)h=((h<<5)-h+s.charCodeAt(i))|0;return h}

  function cleanN(d){if(!d)return null;var o={};for(var k in d){if(k==="_"||k==="#"||k===">")continue;if(d[k]!==null)o[k]=d[k]}return Object.keys(o).length?o:null}

  function bind(host,res){
    var nodes=host.querySelectorAll('[data-bind-text],[data-bind-html],[data-bind-style]');
    for(var i=0;i<nodes.length;i++){
      var el=nodes[i];
      if(el.dataset.bindText&&res[el.dataset.bindText]!==undefined)el.textContent=res[el.dataset.bindText];
      if(el.dataset.bindHtml&&res[el.dataset.bindHtml]!==undefined)el.innerHTML=res[el.dataset.bindHtml];
      if(el.dataset.bindStyle){
        var pairs=el.dataset.bindStyle.split(';');
        for(var j=0;j<pairs.length;j++){var pp=pairs[j].split(':');if(pp[0]&&pp[1]&&res[pp[1].trim()]!==undefined)el.style[pp[0].trim()]=res[pp[1].trim()]}
      }
    }
  }

  async function renderR(dat,key,res){
    if(!res)return;
    if(res.css!=null){var s=document.getElementById('css-'+key);if(!s){s=document.createElement('style');s.id='css-'+key;document.head.appendChild(s)}s.textContent=res.css}
    if(res.html!=null){var h=ensure(key,res,dat);restyle(h,key,res,dat);if(!h._m){h.innerHTML=res.html;h._m=true}bind(h,res)}
    if(res.js!=null){var hh=qh(res.js);if(jsH[key]!==hh){jsH[key]=hh;try{await new AF(res.js)()}catch(e){console.error('['+key+']',e)}}}
  }

  async function render(dat,key){
    if(!dat||key==='_')return;
    if(cleared(dat)){cleanup(key);return}
    await renderR(dat,key,dat);
  }

  // Bootstrap: one-time snapshot fetch
  async function bootstrap(){
    try{
      var r=await fetch('/'+bucket+'/api/snapshot',{cache:'no-store'});
      var snap=await r.json();
      if(!snap||typeof snap!=='object')return;
      var keys=Object.keys(snap).sort(function(a,b){return a.replace(/~/g,'/').split('/').length-b.replace(/~/g,'/').split('/').length});
      var w=new Set(keys);
      for(var i=0;i<keys.length;i++)await render(snap[keys[i]],keys[i]);
      for(var k of Object.keys(live)){if(k==='root')continue;if(!w.has(k))cleanup(k)}
    }catch(e){console.warn('[bootstrap]',e)}
  }

  bootstrap();

  // DIAGNOSTIC: periodic status check
  setInterval(function(){
    var wsInfo = (window._wsInstances||[]).map(function(w){
      return 'WS#'+w.id+'('+w.url.slice(-20)+'):'+['CONNECTING','OPEN','CLOSING','CLOSED'][w.ws.readyState];
    });
    console.log('[SHELL STATUS] gunIn='+gunInCount+' mapOn='+mapCount+' ws=['+wsInfo.join(', ')+']');
  }, 5000);
})();
<\/script>
</body>
</html>`;
}

// ------------------------------------------------------------------
// HTTP Server
// ------------------------------------------------------------------

const server = http.createServer(async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
  if (req.method === 'OPTIONS') { res.writeHead(200); res.end(); return; }

  const parsed = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const segs = parsed.pathname.split('/').filter(Boolean);

  if (segs[0] === 'gun') return;
  if (segs.length === 0) return sendJson(res, { relay: true, buckets: Object.keys(buckets) });

  const bkt = segs[0];
  const action = segs[1] || '';
  const rest = segs.slice(2).join('/');

  subscribe(bkt);
  const b = getBucket(bkt);

  // Shell
  if (req.method === 'GET' && (action === '' || action === 'index.html')) {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    return res.end(shellHTML(bkt));
  }

  // Action
  if (action === 'action' && req.method === 'POST') {
    try {
      const raw = await readBody(req);
      const payload = JSON.parse(raw);
      const key = 'inbox/' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
      const node = { data: JSON.stringify(payload) };
      b.snapshot[key] = node;
      gunPut(bkt, key, node);
      return sendJson(res, { ok: true, key });
    } catch (e) {
      return sendJson(res, { error: e.message }, 400);
    }
  }

  // Scene CRUD
  if (action === 'scene' && rest) {
    if (req.method === 'PUT') {
      if (!checkAuth(bkt, req)) return sendJson(res, { error: 'unauthorized' }, 401);
      try {
        const data = JSON.parse(await readBody(req));
        updateSnapshot(b, rest, data);
        gunPut(bkt, rest, data);
        return sendJson(res, { ok: true, path: rest });
      } catch (e) { return sendJson(res, { error: e.message }, 400); }
    }
    if (req.method === 'DELETE') {
      if (!checkAuth(bkt, req)) return sendJson(res, { error: 'unauthorized' }, 401);
      nullOutDesc(bkt, b.snapshot, rest);
      deleteSnapshotPath(b, rest);
      return sendJson(res, { ok: true, path: rest });
    }
    if (req.method === 'GET') { return sendJson(res, b.snapshot[rest] || null); }
  }

  // Frag
  if (action === 'frag' && rest) {
    if (req.method === 'GET') {
      res.writeHead(200, { 'Content-Type': 'text/html' });
      return res.end(b.snapshot[rest] ? (b.snapshot[rest].html || '') : '');
    }
    if (req.method === 'POST') {
      if (!checkAuth(bkt, req)) return sendJson(res, { error: 'unauthorized' }, 401);
      const raw = await readBody(req);
      const ct = req.headers['content-type'] || '';
      let html = raw;
      if (ct.includes('json')) { try { html = JSON.parse(raw).html || raw; } catch(e) {} }
      updateSnapshot(b, rest, { html });
      gunPut(bkt, rest, { html });
      res.writeHead(200, { 'Content-Type': 'text/html' });
      return res.end(html);
    }
  }

  // API
  if (action === 'api') {
    if (req.method === 'GET' && rest === 'keys') return sendJson(res, Object.keys(b.snapshot));
    if (req.method === 'GET' && rest === 'snapshot') return sendJson(res, b.snapshot);
    if (req.method === 'GET' && rest === 'stats') return sendJson(res, { fragments: Object.keys(b.snapshot).length, auth: !!tokens[bkt] });
    if (req.method === 'POST' && rest === 'clear') {
      if (!checkAuth(bkt, req)) return sendJson(res, { error: 'unauthorized' }, 401);
      for (const key of Object.keys(b.snapshot)) nullOut(bkt, key);
      b.snapshot = {};
      return sendJson(res, { ok: true });
    }
    if (req.method === 'POST' && rest === 'auth') {
      try {
        const { token } = JSON.parse(await readBody(req));
        if (!token) delete tokens[bkt]; else tokens[bkt] = token;
        return sendJson(res, { ok: true });
      } catch (e) { return sendJson(res, { error: 'need {token}' }, 400); }
    }
  }

  if (!res.headersSent) { res.writeHead(404); res.end('Not found'); }
});

// ------------------------------------------------------------------
// DIAGNOSTIC: Log WebSocket upgrade events on the server
// ------------------------------------------------------------------
server.on('upgrade', function(req, socket, head) {
  console.log('[SERVER WS UPGRADE] url=' + req.url + ' origin=' + (req.headers.origin || 'none') + ' from=' + socket.remoteAddress);
});

const gun = Gun({ peers: PEERS, web: server, radisk: true });

// Monitor outbound
gun.on('out', function(msg) {
  if (msg.put) {
    console.log('[RELAY MESH OUT] Broadcasting souls:', Object.keys(msg.put).slice(0, 3).join(', '));
  }
  this.to.next(msg);
});

// DIAGNOSTIC: Monitor inbound on server
gun.on('in', function(msg) {
  if (msg.put) {
    console.log('[RELAY MESH IN] Received souls:', Object.keys(msg.put).slice(0, 3).join(', '));
  }
  this.to.next(msg);
});

// DIAGNOSTIC: Log Gun's internal peer state periodically
setInterval(function() {
  const peers = gun._.opt.peers || {};
  const peerInfo = Object.keys(peers).map(function(url) {
    const p = peers[url];
    const wire = p && p.wire;
    const state = wire ? (wire.readyState !== undefined ? wire.readyState : 'unknown') : 'no-wire';
    return url.slice(-30) + ':' + state;
  });
  console.log('[RELAY PEERS] ' + peerInfo.join(' | '));
}, 10000);

server.listen(PORT, BIND, () => {
  console.log(`Relay: http://localhost:${PORT}`);
  if (PEERS.length) console.log('Peering: ' + PEERS.join(', '));
});