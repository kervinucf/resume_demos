#!/usr/bin/env python3
"""
Explorer — browse the hypergraph in real-time.

Now that the relay has require('gun/lib/ws'), the browser's Gun
WebSocket actually connects. gun.on('in') and scene.map().on()
fire normally. The explorer uses:

  1. gun.on('in') wire listener — catches all inbound data reliably
  2. scene.map().on() — standard Gun subscription as belt & suspenders
  3. One snapshot fetch at boot for initial state

No polling anywhere.
"""

import argparse
import time
from HyperCoreSDK.client import HyperClient

HTML = """
<style>
  #app {
    min-height: 100vh;
    box-sizing: border-box;
    padding: 18px 20px 40px;
    background: #fff;
    color: #000;
    font: 16px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  #app a:link { color: #2563eb; }
  #app a:visited { color: #7c3aed; }
  #app h1, #app h2, #app h3 { margin: 0 0 12px; font-weight: 600; }
  #app h1 { font-size: 22px; }
  #app h2 { font-size: 17px; margin-top: 20px; }
  #app p, #app ul, #app pre { margin: 0 0 14px; }
  #app ul { padding-left: 20px; }
  #app li { margin: 4px 0; }
  #app .crumbs { margin-bottom: 14px; }
  #app .muted { color: #555; }
  #app pre {
    white-space: pre-wrap;
    word-break: break-word;
    padding: 12px;
    border: 1px solid #ddd;
    background: #fafafa;
    overflow: auto;
  }
  #app .section { margin-top: 18px; }
  #app .live { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #22c55e; margin-right: 6px; }
</style>
<div id="app">loading...</div>
"""

JS = r"""
(function () {
  var app = document.getElementById("app");
  if (!app || app.dataset.on) return;
  app.dataset.on = "1";

  var renderQueued = false;
  var keysCache = {};
  var nodeWatchEv = null;
  var nodeWatchPath = "";
  var sceneSubs = {};

  var view = {
    peer: "", bucket: "", path: "",
    node: null, nodeLoaded: false, nodeSig: ""
  };

  function queueRender() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(function () { renderQueued = false; renderCurrentView(); });
  }

  function uniq(xs) {
    var out = [], seen = {};
    for (var i = 0; i < xs.length; i++) {
      var x = String(xs[i] || "").replace(/\/$/, "");
      if (!x || seen[x]) continue; seen[x] = 1; out.push(x);
    }
    return out;
  }

  function peers() {
    var xs = [];
    if (Array.isArray(window.$peers)) xs = xs.concat(window.$peers);
    xs.push(location.origin);
    return uniq(xs);
  }

  function esc(x) { return String(x == null ? "" : x).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
  function parts(p) { return String(p || "").split("/").filter(Boolean); }
  function join(ps) { return ps.filter(Boolean).join("/"); }
  function encPath(p) { return parts(p).map(encodeURIComponent).join("/"); }

  function parseHash() {
    var q = new URLSearchParams(location.hash.replace(/^#/, ""));
    return { peer: q.get("peer") || "", bucket: q.get("bucket") || "", path: q.get("path") || "" };
  }
  function makeHash(st) {
    var q = new URLSearchParams();
    if (st.peer) q.set("peer", st.peer);
    if (st.bucket) q.set("bucket", st.bucket);
    if (st.path) q.set("path", st.path);
    return "#" + q.toString();
  }
  function alink(l, st) { return '<a href="' + makeHash(st) + '">' + esc(l) + "</a>"; }

  async function getJSON(url) {
    var r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(url + " -> HTTP " + r.status);
    return r.json();
  }

  function parentPath(p) { var a = parts(p); if (!a.length) return ""; a.pop(); return join(a); }

  function crumbs(st) {
    var out = [alink("peers", {})];
    if (st.peer) out.push(" / " + alink(st.peer, { peer: st.peer }));
    if (st.bucket) out.push(" / " + alink(st.bucket, { peer: st.peer, bucket: st.bucket }));
    if (st.path) {
      var p = parts(st.path), acc = [];
      for (var i = 0; i < p.length; i++) { acc.push(p[i]); out.push(" / " + alink(p[i], { peer: st.peer, bucket: st.bucket, path: join(acc) })); }
    }
    return out.join("");
  }

  function exact(keys, path) { if (!path) return false; for (var i = 0; i < keys.length; i++) if (keys[i] === path) return true; return false; }

  function childrenOf(keys, prefix) {
    var out = {}, pref = prefix ? prefix + "/" : "";
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      if (prefix && k !== prefix && k.indexOf(pref) !== 0) continue;
      var rest = prefix ? (k === prefix ? "" : k.slice(pref.length)) : k;
      if (!rest) continue;
      var head = rest.split("/")[0];
      out[prefix ? prefix + "/" + head : head] = 1;
    }
    return Object.keys(out).sort();
  }

  function normalizeLinks(node, st) {
    if (!node || !node.links) return [];
    var raw = node.links;
    if (typeof raw === "string") { try { raw = JSON.parse(raw); } catch (_) { raw = []; } }
    if (!Array.isArray(raw)) return [];
    var out = [];
    for (var i = 0; i < raw.length; i++) {
      var it = raw[i];
      if (typeof it === "string") { out.push({ label: it, peer: st.peer, bucket: st.bucket, path: it }); continue; }
      if (!it || !it.path) continue;
      out.push({ label: it.label || it.rel || it.path, peer: it.peer || st.peer, bucket: it.bucket || st.bucket, path: it.path });
    }
    return out;
  }

  async function peerBuckets(peer) {
    try { var m = await getJSON(peer + "/"); return Array.isArray(m.buckets) ? m.buckets.slice().sort() : []; }
    catch (_) { return []; }
  }

  function sig(x) { try { return JSON.stringify(x); } catch (_) { return String(Math.random()); } }

  var CF = ["html","css","js","link","json","data","meta","links","actions","layer","fixed","portal"];
  function hasContent(d) { if (!d) return false; for (var i = 0; i < CF.length; i++) if (d[CF[i]] !== undefined && d[CF[i]] !== null) return true; return false; }

  function cleanNode(d) {
    if (!d) return null;
    var out = {};
    for (var k in d) { if (k === "_" || k === "#" || k === ">") continue; if (d[k] !== null) out[k] = d[k]; }
    return Object.keys(out).length ? out : null;
  }

  // ---------------------------------------------------------------
  // Wire-level listener — catches all Gun inbound data
  // ---------------------------------------------------------------
  function initWireListener() {
    if (!window.$gun || window.$gun._explorerWireHooked) return;
    window.$gun._explorerWireHooked = true;

    window.$gun.on('in', function (msg) {
      if (!msg || !msg.put) { this.to.next(msg); return; }

      var souls = Object.keys(msg.put);
      for (var i = 0; i < souls.length; i++) {
        var soul = souls[i];

        for (var bucket in sceneSubs) {
          var scenePrefix = bucket + "/scene/";

          if (soul.indexOf(scenePrefix) === 0) {
            var path = soul.slice(scenePrefix.length);
            if (!path) continue;

            var nodeData = msg.put[soul];
            if (!nodeData) continue;

            var clean = cleanNode(nodeData);
            if (!keysCache[bucket]) keysCache[bucket] = {};

            if (clean && hasContent(clean)) {
              var existing = keysCache[bucket][path];
              if (existing && typeof existing === "object") {
                for (var ck in clean) existing[ck] = clean[ck];
                for (var ck in existing) if (existing[ck] === null) delete existing[ck];
                if (!hasContent(existing)) delete keysCache[bucket][path];
              } else {
                keysCache[bucket][path] = clean;
              }
            } else {
              delete keysCache[bucket][path];
            }

            var st = parseHash();
            if (st.bucket === bucket && st.path === path) {
              var cached = keysCache[bucket][path] || null;
              var ns = sig(cached);
              if (ns !== view.nodeSig) { view.node = cached; view.nodeSig = ns; view.nodeLoaded = true; }
            }

            queueRender();
          }
        }
      }
      this.to.next(msg);
    });
  }

  // ---------------------------------------------------------------
  // Subscribe to a bucket
  // ---------------------------------------------------------------
  function ensureSceneSub(peer, bucket) {
    if (sceneSubs[bucket]) return;
    sceneSubs[bucket] = true;
    if (!keysCache[bucket]) keysCache[bucket] = {};

    initWireListener();

    // Standard Gun subscription
    window.$gun.get(bucket).get("scene").map().on(function (data, key) {
      if (!data || key === "_") return;
      if (!keysCache[bucket]) keysCache[bucket] = {};
      var clean = cleanNode(data);
      if (clean && hasContent(clean)) keysCache[bucket][key] = clean;
      else delete keysCache[bucket][key];

      var st = parseHash();
      if (st.bucket === bucket && st.path === key) {
        var ns = sig(clean);
        if (ns !== view.nodeSig) { view.node = clean; view.nodeSig = ns; view.nodeLoaded = true; }
      }
      queueRender();
    });

    // One-time snapshot for initial data
    getJSON(peer + "/" + encodeURIComponent(bucket) + "/api/snapshot")
      .then(function (snap) {
        if (!snap || typeof snap !== "object") return;
        for (var key in snap) {
          var clean = cleanNode(snap[key]);
          if (clean && hasContent(clean)) keysCache[bucket][key] = clean;
        }
        queueRender();
      })
      .catch(function () {});
  }

  function getKeys(bucket) { var ks = keysCache[bucket]; return ks ? Object.keys(ks).sort() : []; }

  function stopNodeWatch() {
    if (nodeWatchEv) { try { nodeWatchEv.off(); } catch (_) {} }
    nodeWatchEv = null; nodeWatchPath = "";
  }

  function watchNode(peer, bucket, path) {
    if (nodeWatchPath === bucket + "|" + path) return;
    stopNodeWatch();
    if (!path) { view.node = null; view.nodeLoaded = true; return; }

    nodeWatchPath = bucket + "|" + path;
    view.node = null; view.nodeLoaded = false; view.nodeSig = "";

    if (keysCache[bucket] && keysCache[bucket][path]) {
      view.node = keysCache[bucket][path];
      view.nodeSig = sig(view.node);
      view.nodeLoaded = true;
    }

    var targetSoul = bucket + "/scene/" + path;
    window.$gun.get(targetSoul).on(function (data, key, msg, ev) {
      nodeWatchEv = ev;
      var st = parseHash();
      if (st.path !== path || st.bucket !== bucket) return;
      var clean = cleanNode(data);
      var ns = sig(clean);
      if (ns !== view.nodeSig) {
        view.node = clean; view.nodeSig = ns; view.nodeLoaded = true;
        if (keysCache[bucket]) {
          if (clean && hasContent(clean)) keysCache[bucket][path] = clean;
          else delete keysCache[bucket][path];
        }
        queueRender();
      }
    });

    if (!view.nodeLoaded) {
      getJSON(peer + "/" + encodeURIComponent(bucket) + "/scene/" + encPath(path))
        .then(function (node) {
          var st = parseHash();
          if (st.path !== path || st.bucket !== bucket) return;
          if (view.nodeLoaded) return;
          view.node = cleanNode(node); view.nodeSig = sig(view.node); view.nodeLoaded = true;
          queueRender();
        })
        .catch(function () { if (!view.nodeLoaded) { view.node = null; view.nodeLoaded = true; queueRender(); } });
    }
  }

  // ---------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------
  async function renderHome() {
    var xs = peers(), currentBucket = window.$bucket || "";
    var rows = await Promise.all(xs.map(async function (peer) { return { peer: peer, buckets: await peerBuckets(peer) }; }));
    var html = "<div class='crumbs'>" + crumbs({}) + "</div><h1>Peers</h1>";
    if (currentBucket) html += "<p class='muted'>Current bucket: <strong>" + esc(currentBucket) + "</strong></p>";
    html += "<ul>";
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      html += "<li>" + alink(row.peer, { peer: row.peer });
      if (currentBucket && row.buckets.indexOf(currentBucket) !== -1) html += " - " + alink(currentBucket, { peer: row.peer, bucket: currentBucket });
      else if (row.buckets.length) html += " <span class='muted'>(" + row.buckets.length + " buckets)</span>";
      else html += " <span class='muted'>(unreachable or empty)</span>";
      html += "</li>";
    }
    html += "</ul>"; app.innerHTML = html;
  }

  async function renderBuckets(st) {
    var buckets = await peerBuckets(st.peer);
    var html = "<div class='crumbs'>" + crumbs(st) + "</div><h1>" + esc(st.peer) + "</h1><ul>";
    for (var i = 0; i < buckets.length; i++) html += "<li>" + alink(buckets[i], { peer: st.peer, bucket: buckets[i] }) + "</li>";
    html += "</ul>"; app.innerHTML = html;
  }

  function renderCurrentView() {
    var st = { peer: view.peer, bucket: view.bucket, path: view.path };
    if (!st.peer) { renderHome(); return; }
    if (!st.bucket) { renderBuckets(st); return; }

    var keys = getKeys(st.bucket), path = st.path || "";
    var kids = childrenOf(keys, path), isNode = exact(keys, path);

    var html = "<div class='crumbs'>" + crumbs(st) + "</div>";
    html += "<p>" + alink("..", { peer: st.peer, bucket: st.bucket, path: parentPath(path) }) + "</p>";
    html += "<h1><span class='live'></span>" + esc(path || "/") + "</h1>";

    if (!keys.length) { html += "<p class='muted'>Loading...</p>"; app.innerHTML = html; return; }

    if (kids.length) {
      html += "<div class='section'><h2>Children</h2><ul>";
      for (var i = 0; i < kids.length; i++) html += "<li>" + alink(parts(kids[i]).slice(-1)[0], { peer: st.peer, bucket: st.bucket, path: kids[i] }) + "</li>";
      html += "</ul></div>";
    }

    if (!isNode) { if (!path) { app.innerHTML = html; return; } html += "<p class='muted'>No node at this path.</p>"; app.innerHTML = html; return; }
    if (!view.nodeLoaded) { html += "<p class='muted'>Loading node...</p>"; app.innerHTML = html; return; }
    if (!view.node) { html += "<p class='muted'>Node is empty or missing.</p>"; app.innerHTML = html; return; }

    var node = view.node;
    html += "<div class='section'><h2>Node</h2><pre>" + esc(JSON.stringify(node, null, 2)) + "</pre></div>";

    var links = normalizeLinks(node, st);
    if (links.length) {
      html += "<div class='section'><h2>Links</h2><ul>";
      for (var j = 0; j < links.length; j++) { var it = links[j]; html += "<li>" + alink(it.label, { peer: it.peer, bucket: it.bucket, path: it.path }) + "</li>"; }
      html += "</ul></div>";
    }

    if (node.html) html += "<div class='section'><h2>Rendered HTML</h2><div data-rendered-html>" + String(node.html) + "</div></div>";
    app.innerHTML = html;
  }

  // ---------------------------------------------------------------
  // Navigation
  // ---------------------------------------------------------------
  app.addEventListener("click", function (e) {
    var el = e.target.closest("[data-rendered-html] a"); if (!el) return;
    var st = parseHash(), href = String(el.getAttribute("href") || "").trim();
    if (href && href !== "#") {
      if (href.charAt(0) === "#") { e.preventDefault(); location.hash = href; return; }
      if (!/^(https?:|mailto:|tel:|javascript:)/i.test(href)) { e.preventDefault(); location.hash = makeHash({ peer: st.peer, bucket: st.bucket, path: href.replace(/^\/+/, "") }); return; }
      return;
    }
    var path = String(el.getAttribute("data-path") || el.textContent || "").trim();
    if (!path) return;
    e.preventDefault(); location.hash = makeHash({ peer: st.peer, bucket: st.bucket, path: path });
  });

  function route() {
    var st = parseHash();
    view.peer = st.peer; view.bucket = st.bucket; view.path = st.path;
    view.node = null; view.nodeLoaded = false; view.nodeSig = "";

    if (st.peer) {
      var peerUrl = st.peer.replace(/\/$/, "") + "/gun";
      window.$gun.opt({ peers: [peerUrl] });
    }

    if (!st.peer || !st.bucket) { stopNodeWatch(); view.nodeLoaded = true; renderCurrentView(); return; }

    ensureSceneSub(st.peer, st.bucket);
    watchNode(st.peer, st.bucket, st.path);
    renderCurrentView();
  }

  window.addEventListener("hashchange", route);
  route();
})();
"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--app-root", default="explorer")
    p.add_argument("--discovery", default="local")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--peer", action="append", default=[])
    a = p.parse_args()

    hc = HyperClient(
        root=a.app_root,
        discovery=a.discovery,
        peers=a.peer or None,
        port=a.port,
    )
    hc.connect()

    hc.mount("root/x", html=HTML, js=JS, fixed=True, layer=9999)
    print("Explorer:", hc.browser_url)

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()