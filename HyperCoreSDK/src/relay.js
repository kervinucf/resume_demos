const Gun = require('gun');
const http = require('http');
const invertedIndex = Object.create(null);
const docIndex = Object.create(null);
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

const graph = {};
const tokens = {};
const localMsgIds = new Set();
const sseClients = new Set();
let nextEventId = 1;

function isObject(v) {
  return !!v && typeof v === 'object' && !Array.isArray(v);
}

function getNode(dp) {
  return graph[dp] || null;
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

function deleteNode(dp) {
  delete graph[dp];
  removeFromIndex(dp);
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
    for (const [k, v] of Object.entries(value)) {
      const next = key ? key + '.' + k : k;
      addSearchEntry(entries, next, v);
    }
  }
}

function collectSearchEntries(dp, node) {
  const entries = [];
  entries.push({ key: '_path', text: dp.replace(/\./g, ' ') });

  for (const [k, v] of Object.entries(node || {})) {
    if (k === '_' || k === '#' || k === '>') continue;

    // Skip giant blobs by default.
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
    for (const term of tokenize(entry.text)) {
      terms.add(term);
    }
  }

  docIndex[dp] = { terms, entries };

  for (const term of terms) {
    if (!invertedIndex[term]) invertedIndex[term] = new Set();
    invertedIndex[term].add(dp);
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
    if (dp === rootDp || dp.startsWith(rootDp + '.')) actual.add(dp);
  }

  const nodes = Object.create(null);

  function ensure(path) {
    if (!nodes[path]) {
      nodes[path] = {
        path,
        type: actual.has(path) ? 'node' : 'branch',
        children: [],
      };
    } else if (actual.has(path)) {
      nodes[path].type = 'node';
    }
    return nodes[path];
  }

  ensure(rootDp);

  for (const dp of Array.from(actual)) {
    const rootParts = rootDp.split('.');
    const parts = dp.split('.');

    for (let i = rootParts.length; i <= parts.length; i++) {
      const sub = parts.slice(0, i).join('.');
      if (!(sub === rootDp || sub.startsWith(rootDp + '.'))) continue;
      ensure(sub);
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
  return origin + '/' + encodeURIComponent(dp) + suffix;
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

  if (node && node.file) {
    links.download = pathURL(origin, dp, '.download');
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
    if (fieldPath.length === 1 && fieldPath[0] === 'file') {
      return { type: 'json', data: fileSummary(val) };
    }
    return { type: typeof val === 'object' ? 'json' : 'raw', data: val };
  }

  if (childrenOf(dp).length > 0) return { type: 'node', data: buildResponse(dp, origin) };
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
  const root = parts[0];
  const scenePath = parts.length > 1 ? parts.slice(1).join('/') : '__root__';
  return { root, scenePath };
}

function soulToDotPath(soul) {
  const parts = soul.split('/');
  if (!(parts.length >= 3 && parts[1] === 'scene')) return null;
  const root = parts[0];
  const scenePath = parts.slice(2).join('/');
  if (scenePath === '__root__') return root;
  return root + '.' + scenePath.replace(/\//g, '.');
}

function isGunObject(v) {
  return !!v && typeof v === 'object' && !Array.isArray(v);
}

function buildGunNode(put, soul, obj, state) {
  const node = { _: { '#': soul, '>': {} } };

  for (const [k, v] of Object.entries(obj || {})) {
    if (k === '_' || k === '#' || k === '>') continue;

    if (isGunObject(v)) {
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

  const id = 'p_' + Math.random().toString(36).slice(2, 11);
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

setInterval(function() {
  for (const client of Array.from(sseClients)) {
    try { client.res.write(': ping\n\n'); }
    catch (_) {
      try { client.res.end(); } catch (_) {}
      sseClients.delete(client);
    }
  }
}, 25000);

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
      const dp = soulToDotPath(soul);
      if (!dp) continue;
      const nd = msg.put[soul] || {};
      const clean = {};
      let sawField = false;
      for (const [k, v] of Object.entries(nd)) {
        if (k === '_' || k === '#' || k === '>') continue;
        sawField = true;
        clean[k] = v;
      }
      if (!sawField) continue;
      mergeNode(dp, clean);
      touched.add(dp);
    }

    for (const dp of touched) notifyImpacted(dp);
    this.to.next(msg);
  });
}

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
  live.root=root;
  window.$gun=gun;window.$scene=scene;window.$root=root;window.$bucket=bucket;
  window.$peers=ALL_PEERS.map(function(u){return u.endsWith('/gun')?u.slice(0,-4):u;});
  window.action=function(p){return fetch('/'+bucket+'/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p||{})}).catch(function(e){console.warn('[action]',e);});};
  var CF=['html','css','js','link','json','data','meta','links','actions','layer','fixed','portal'];
  function has(d){return CF.some(function(f){return d[f]!==undefined&&d[f]!==null})}
  function cleared(d){return !d||!has(d)}
  function kid(k){return 'frag-'+String(k).replace(/[^\\w~-]/g,'_')}
  function par(k){if(k.includes('~'))return k.split('~')[0]||null;var p=k.split('/').filter(Boolean);return p.length<=1?null:p.slice(0,-1).join('/');}
  function host4(k){return live[k]||document.getElementById(kid(k))||null}
  function rootS(e,l){e.style.position='fixed';e.style.inset='0';e.style.zIndex=String(l);e.style.pointerEvents=l<=0?'none':'auto'}
  function childS(e,l){e.style.position='relative';e.style.flex='0 0 auto';e.style.minWidth='0';e.style.minHeight='';e.style.width='';e.style.height='';e.style.top='';e.style.left='';e.style.inset='';e.style.zIndex=String(l);e.style.pointerEvents=l<=0?'none':'auto'}
  function ensure(key,res,dat){if(live[key])return live[key];var h=document.createElement('div');h.id=kid(key);h.dataset.key=key;var layer=Number(res&&res.layer||dat&&dat.layer||0)||0;var pk=par(key);var isR=key.includes('~');var wRoot=!!(res&&res.fixed||dat&&dat.fixed||res&&res.portal||dat&&dat.portal);var ph=pk?host4(pk):null;if(ph&&!wRoot){childS(h,layer);var mp;if(isR){var row=ph.querySelector('[data-row]');if(!row){row=document.createElement('div');row.dataset.row='';row.style.display='flex';row.style.flex='1';row.style.width='100%';row.style.height='100%';row.style.minHeight='0';ph.appendChild(row)}mp=row}else{mp=ph.querySelector('[data-children]')||ph}mp.appendChild(h);}else{rootS(h,layer);root.appendChild(h)}live[key]=h;return h;}
  function restyle(h,key,res,dat){var layer=Number(res&&res.layer||dat&&dat.layer||0)||0;var pk=par(key);var wRoot=!!(res&&res.fixed||dat&&dat.fixed||res&&res.portal||dat&&dat.portal);var ph=pk?host4(pk):null;if(!ph||wRoot){rootS(h,layer);if(h.parentElement!==root)root.appendChild(h);return}childS(h,layer);var isR=key.includes('~');var mp;if(isR){var row=ph.querySelector('[data-row]');if(!row){row=document.createElement('div');row.dataset.row='';row.style.display='flex';row.style.flex='1';row.style.width='100%';row.style.height='100%';row.style.minHeight='0';ph.appendChild(row)}mp=row}else{mp=ph.querySelector('[data-children]')||ph}if(h.parentElement!==mp)mp.appendChild(h);}
  function prune(){var rows=root.querySelectorAll('[data-row]');for(var i=0;i<rows.length;i++)if(!rows[i].children.length)rows[i].remove()}
  function cleanup(key){if(key==='root')return;var a=key+'/',b=key+'~';for(var k of Object.keys(live)){if(k==='root')continue;if(k===key||k.startsWith(a)||k.startsWith(b)){if(live[k])live[k].remove();delete live[k];var c=document.getElementById('css-'+k);if(c)c.remove();delete jsH[k]}}prune();}
  function qh(s){var h=0;for(var i=0;i<s.length;i++)h=((h<<5)-h+s.charCodeAt(i))|0;return h}
  function bind(host,res){var data=(res&&res.data&&typeof res.data==='object')?res.data:{};var nodes=host.querySelectorAll('[data-bind-text],[data-bind-html],[data-bind-style]');for(var i=0;i<nodes.length;i++){var el=nodes[i];if(el.dataset.bindText&&data[el.dataset.bindText]!==undefined)el.textContent=data[el.dataset.bindText];if(el.dataset.bindHtml&&data[el.dataset.bindHtml]!==undefined)el.innerHTML=data[el.dataset.bindHtml];if(el.dataset.bindStyle){var pairs=el.dataset.bindStyle.split(';');for(var j=0;j<pairs.length;j++){var pp=pairs[j].split(':');if(pp[0]&&pp[1]&&data[pp[1].trim()]!==undefined)el.style[pp[0].trim()]=data[pp[1].trim()]}}}}
  async function renderR(dat,key,res){if(!res)return;if(res.css!=null){var s=document.getElementById('css-'+key);if(!s){s=document.createElement('style');s.id='css-'+key;document.head.appendChild(s)}s.textContent=res.css}if(res.html!=null){var h=ensure(key,res,dat);restyle(h,key,res,dat);h.innerHTML=res.html;bind(h,res)}if(res.js!=null){var hh=qh(res.js);if(jsH[key]!==hh){jsH[key]=hh;try{await new AF(res.js)()}catch(e){console.error('['+key+']',e)}}}}
  async function render(dat,key){if(!dat||key==='_')return;if(cleared(dat)){cleanup(key);return}await renderR(dat,key,dat);}
  scene.map().on(function(d,k){render(d,k)});
  (async function(){try{var r=await fetch('/'+bucket+'/api/snapshot',{cache:'no-store'});var snap=await r.json();if(!snap||typeof snap!=='object')return;var keys=Object.keys(snap).sort(function(a,b){return a.replace(/~/g,'/').split('/').length-b.replace(/~/g,'/').split('/').length});var w=new Set(keys);for(var i=0;i<keys.length;i++)await render(snap[keys[i]],keys[i]);for(var k of Object.keys(live)){if(k==='root')continue;if(!w.has(k))cleanup(k)}}catch(e){console.warn('[bootstrap]',e)}})();
})();
<\/script></body></html>`;
}

function resolveStreamTarget(dp) {
  if (getNode(dp) || childrenOf(dp).length > 0) return { nodeDp: dp, fieldPath: [] };

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
  const isBranch = !getNode(target.nodeDp) && childrenOf(target.nodeDp).length > 0;

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>${dp}</title>
<script src="https://cdn.jsdelivr.net/npm/gun/gun.js"><\/script>
<style>html,body,#app{margin:0;padding:0;width:100%;height:100%}body{font-family:system-ui,sans-serif}pre{margin:0;padding:12px;white-space:pre-wrap;word-break:break-word}</style>
</head>
<body>
<div id="app"></div>
<script>
(function(){
  var ALL_PEERS=${peersJSON};
  var local=location.origin+'/gun';
  if(ALL_PEERS.indexOf(local)===-1)ALL_PEERS.unshift(local);
  var gun=Gun({peers:ALL_PEERS});
  console.log('[STREAM PEERS]', ALL_PEERS);
setInterval(function(){
  console.log('[STREAM WS CHECK]', ALL_PEERS);
}, 5000);
  var app=document.getElementById('app');
  var nodeDp=${JSON.stringify(target.nodeDp)};
  var fieldPath=${JSON.stringify(target.fieldPath || [])};
  var bindDp=${JSON.stringify(bindDp)};
  var isBranch=${JSON.stringify(isBranch)};
  var nodeRoot=${JSON.stringify(nodeMap.root)};
  var nodeScenePath=${JSON.stringify(nodeMap.scenePath)};
  var bindRoot=${JSON.stringify(bindMap ? bindMap.root : null)};
  var bindScenePath=${JSON.stringify(bindMap ? bindMap.scenePath : null)};
  var currentNode={};
  var currentData={};
  var branchSnap={};
  function clean(node){if(!node||typeof node!=='object')return {};var out={};Object.keys(node).forEach(function(k){if(k==='_'||k==='#'||k==='>')return;if(node[k]!==null&&node[k]!==undefined)out[k]=node[k];});return out;}
  function dig(v,path,start){for(var i=(start||0);i<path.length;i++){if(v==null)return undefined;v=v[path[i]];}return v;}
  function bindData(root,data){data=data||{};var nodes=root.querySelectorAll('[data-bind-text],[data-bind-html],[data-bind-style]');for(var i=0;i<nodes.length;i++){var el=nodes[i];if(el.dataset.bindText&&data[el.dataset.bindText]!==undefined)el.textContent=data[el.dataset.bindText];if(el.dataset.bindHtml&&data[el.dataset.bindHtml]!==undefined)el.innerHTML=data[el.dataset.bindHtml];if(el.dataset.bindStyle){var pairs=el.dataset.bindStyle.split(';');for(var j=0;j<pairs.length;j++){var pp=pairs[j].split(':');if(pp[0]&&pp[1]&&data[pp[1].trim()]!==undefined)el.style[pp[0].trim()]=data[pp[1].trim()]}}}}
  function renderValue(v){if(v==null){app.textContent='';return;}if(typeof v==='string'){app.textContent=v;return;}app.innerHTML='<pre>'+JSON.stringify(v,null,2)+'</pre>';}
    function render(){
    if(isBranch){
      renderValue(branchSnap);
      return;
    }

    if(currentNode.html != null){
      app.innerHTML = String(currentNode.html);
      bindData(app, currentData || currentNode.data || {});
      return;
    }

    if(currentNode.data !== undefined){
      renderValue(currentNode.data);
      return;
    }

    renderValue(currentNode);
  }
    var currentValue;

  function chainFor(root, scenePath, segments){
    var c = gun.get(root).get('scene').get(scenePath);
    for (var i = 0; i < segments.length; i++) {
      c = c.get(segments[i]);
    }
    return c;
  }

  if(isBranch){
    gun.get(nodeRoot).get('scene').map().on(function(d,k){
      if(!k || k === '_') return;
      var dp = (k === '__root__') ? nodeRoot : (nodeRoot + '.' + String(k).replace(/\\//g,'.'));
      if(!(dp === nodeDp || dp.indexOf(nodeDp + '.') === 0)) return;
      var cleaned = clean(d);
      if(cleaned && Object.keys(cleaned).length) branchSnap[dp] = cleaned;
      else delete branchSnap[dp];
      renderValue(branchSnap);
    });
    return;
  }

  // Exact field stream: subscribe directly to the nested Gun chain.
  if(fieldPath.length){
    chainFor(nodeRoot, nodeScenePath, fieldPath).on(function(v){
      if(fieldPath[0] === 'html'){
        app.innerHTML = v == null ? '' : String(v);
        return;
      }

      if(v == null){
        app.textContent = '';
        return;
      }

      if(typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean'){
        app.textContent = String(v);
        return;
      }

      app.innerHTML = '<pre>' + JSON.stringify(v, null, 2) + '</pre>';
    });
    return;
  }
  // Whole-node stream.
  gun.get(nodeRoot).get('scene').get(nodeScenePath).on(function(node){
    currentNode = clean(node);
    render();
  });

  // Separate binding source for html views.
  if(bindRoot && bindScenePath){
    gun.get(bindRoot).get('scene').get(bindScenePath).get('data').on(function(data){
      currentData = clean(data);
      render();
    });
  }
  })();
<\/script>
</body>
</html>`;
}

function snapshotForRoot(rootName) {
  const snap = {};
  const pfx = rootName + '.';
  for (const [dp, data] of Object.entries(graph)) {
    if (!dp.startsWith(pfx)) continue;
    snap[dp.slice(pfx.length).replace(/\./g, '/')] = nodeForRead(data);
  }
  return snap;
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

function checkAuth(dp, req) {
  const tok = tokens[dp.split('.')[0]];
  if (!tok) return true;
  return (req.headers['authorization'] || '') === 'Bearer ' + tok;
}

function stripSuffix(path, suffix) {
  return path.endsWith(suffix) ? path.slice(0, -suffix.length) : null;
}

const server = http.createServer(async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
  if (req.method === 'OPTIONS') { res.writeHead(200); res.end(); return; }

  const parsed = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const pathname = parsed.pathname.replace(/\/+$/, '') || '/';

  if (pathname === '/gun') return;

  if (pathname === '/') {
    const roots = Array.from(new Set(Object.keys(graph).map(k => k.split('.')[0]))).sort();
    return sendJson(res, {
      relay: true,
      buckets: roots,
      _links: Object.fromEntries(roots.map(r => [r, pathURL(parsed.origin, r)])),
    });
  }

  const raw = decodeURIComponent(pathname.slice(1));
  const treePath = stripSuffix(raw, '.tree');
  if (treePath && req.method === 'GET') {
    return sendJson(res, {
      _path: treePath + '.tree',
      tree: buildTree(treePath),
    });
  }

  const searchPath = stripSuffix(raw, '.search');
  if (searchPath && req.method === 'GET') {
    const q = parsed.searchParams.get('q') || '';
    const limit = Math.min(parseInt(parsed.searchParams.get('limit') || '50', 10) || 50, 200);
    const results = searchSubtree(searchPath, q, limit);

    return sendJson(res, {
      _path: searchPath + '.search',
      query: q,
      count: results.length,
      results,
    });
  }
  const streamPath = stripSuffix(raw, '.stream') || stripSuffix(raw, '._connect');
  if (streamPath && req.method === 'GET') {
    res.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Access-Control-Allow-Origin': '*',
    });
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
    emitResolved(client, 'snapshot');
    req.on('close', function(){ sseClients.delete(client); });
    req.on('aborted', function(){ sseClients.delete(client); });
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
      if (!checkAuth(root, req)) return sendJson(res, { error: 'unauthorized' }, 401);
      for (const dp of descendantsOf(root)) { gunNullOut(dp); deleteNode(dp); }
      notifyImpacted(root);
      return sendJson(res, { ok: true });
    }
    if (req.method === 'POST' && op === 'auth') {
      try {
        const body = JSON.parse(await readBody(req));
        if (!body.token) delete tokens[root]; else tokens[root] = body.token;
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
      if (!checkAuth(dp, req)) return sendJson(res, { error: 'unauthorized' }, 401);
      try {
        const body = JSON.parse(await readBody(req));
        mergeNode(dp, body);
        gunPut(dp, graph[dp] || body);
        notifyImpacted(dp);
        return sendJson(res, { ok: true, path: dp });
      } catch (e) {
        return sendJson(res, { error: e.message }, 400);
      }
    }
    if (req.method === 'DELETE') {
      if (!checkAuth(dp, req)) return sendJson(res, { error: 'unauthorized' }, 401);
      for (const d of descendantsOf(dp)) { gunNullOut(d); deleteNode(d); }
      notifyImpacted(dp);
      return sendJson(res, { ok: true });
    }
  }

  const actionMatch = raw.match(/^([^./]+)\/action$/);
  if (actionMatch && req.method === 'POST') {
    try {
      const payload = JSON.parse(await readBody(req));
      const key = actionMatch[1] + '.inbox.' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
      mergeNode(key, { data: payload });
      gunPut(key, graph[key] || { data: payload });
      notifyImpacted(key);
      return sendJson(res, { ok: true, key });
    } catch (e) {
      return sendJson(res, { error: e.message }, 400);
    }
  }

  const dotPath = raw;

  if (!dotPath.includes('.') && !dotPath.includes('/') && req.method === 'GET') {
    const accept = req.headers['accept'] || '';
    if (accept.includes('text/html') && !parsed.searchParams.has('json')) {
      res.writeHead(200, {
        'Content-Type': 'text/html; charset=utf-8',
        'Access-Control-Allow-Origin': '*',
      });
      return res.end(shellHTML(dotPath));
    }
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
    if (!checkAuth(dotPath, req)) return sendJson(res, { error: 'unauthorized' }, 401);
    try {
      const body = JSON.parse(await readBody(req));
      mergeNode(dotPath, body);
      gunPut(dotPath, graph[dotPath] || body);
      notifyImpacted(dotPath);
      return sendJson(res, { ok: true, _path: dotPath });
    } catch (e) {
      return sendJson(res, { error: e.message }, 400);
    }
  }

  if (req.method === 'DELETE') {
    if (!checkAuth(dotPath, req)) return sendJson(res, { error: 'unauthorized' }, 401);
    const removed = descendantsOf(dotPath);
    for (const dp of removed) { gunNullOut(dp); deleteNode(dp); }
    notifyImpacted(dotPath);
    return sendJson(res, { ok: true, removed: removed.length });
  }

  if (req.method === 'POST') {
    try {
      const payload = JSON.parse(await readBody(req));
      const root = dotPath.split('.')[0];
      const key = root + '.inbox.' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
      mergeNode(key, { data: payload });
      gunPut(key, graph[key] || { data: payload });
      notifyImpacted(key);
      return sendJson(res, { ok: true, key });
    } catch (e) {
      return sendJson(res, { error: e.message }, 400);
    }
  }

  res.writeHead(404);
  res.end('Not found');
});

const gun = Gun({ peers: PEERS, web: server, radisk: true });

initGunSync();

server.listen(PORT, BIND, () => {
  console.log(`HyperRelay: http://localhost:${PORT}`);
  if (PEERS.length) console.log('Peering:', PEERS.join(', '));
});