/**
 * Scene Relay — dumb pipe between writers and viewers.
 *
 * Snapshot is updated both by GunDB's async map().on() callback
 * AND directly in the PUT handler so REST reads are immediately
 * consistent after writes.
 */

const Gun = require('gun');
require('gun/sea');
const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = process.env.PORT || 8765;

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

const SHELL_DIR = path.join(__dirname, 'utils');

function serveFile(res, filepath, mime) {
  if (res.headersSent) return;
  try {
    const data = fs.readFileSync(filepath);
    res.writeHead(200, { 'Content-Type': mime });
    res.end(data);
  } catch (e) {
    res.writeHead(404);
    res.end('Not found');
  }
}

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

  if (segs[0] === 'gun') return;
  if (segs.length === 0) return sendJson(res, { relay: true, buckets: Object.keys(buckets) });

  if (segs[0] === 'inspect') {
    if (req.method === 'GET') {
      return serveFile(res, path.join(SHELL_DIR, 'index.html'), 'text/html');
    }
    res.writeHead(404);
    res.end('Not found');
    return;
  }

  const bkt = segs[0];
  const action = segs[1] || '';
  const rest = segs.slice(2).join('/');

  subscribe(bkt);
  const b = getBucket(bkt);

  if (req.method === 'GET' && (action === '' || action === 'index.html')) {
    return serveFile(res, path.join(SHELL_DIR, 'index.html'), 'text/html');
  }

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
        try {
          html = JSON.parse(raw).html || raw;
        } catch (e) {}
      }

      gun.get(bkt).get('scene').get(rest).put({ html });
      updateSnapshot(b, rest, { html });

      res.writeHead(200, { 'Content-Type': 'text/html' });
      return res.end(html);
    }
  }

if (action === 'scene' && rest) {
  if (req.method === 'PUT') {
    if (!checkAuth(bkt, req)) return sendJson(res, { error: 'unauthorized' }, 401);

    try {
      const data = JSON.parse(await readBody(req));

      // store flat by full path string so scene.map().on() stays realtime
      gun.get(bkt).get('scene').get(rest).put(data);

      // snapshot also uses the full path
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
  if (action === 'api') {
    if (req.method === 'GET' && rest === 'keys') {
      return sendJson(res, Object.keys(b.snapshot));
    }

    if (req.method === 'GET' && rest === 'snapshot') {
      return sendJson(res, b.snapshot);
    }

    if (req.method === 'GET' && rest === 'stats') {
      return sendJson(res, {
        fragments: Object.keys(b.snapshot).length,
        auth: !!tokens[bkt]
      });
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
server.listen(PORT, () => console.log(`Relay: http://localhost:${PORT}`));