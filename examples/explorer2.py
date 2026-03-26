#!/usr/bin/env python3
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
</style>
<div id="app">loading...</div>
"""

JS = r"""
(function () {
  var app = document.getElementById("app");
  if (!app || app.dataset.on) return;
  app.dataset.on = "1";

  var renderQueued = false;
  var gunByPeer = {};

  var state = {
    peer: "",
    bucket: "",
    path: "",
    dirLoaded: false,
    nodeLoaded: false,
    dirNode: null,
    node: null,
    dirRef: null,
    nodeRef: null
  };

  function queueRender() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(function () {
      renderQueued = false;
      renderPathView();
    });
  }

  function uniq(xs) {
    var out = [], seen = {};
    for (var i = 0; i < xs.length; i++) {
      var x = String(xs[i] || "").replace(/\/$/, "");
      if (!x || seen[x]) continue;
      seen[x] = 1;
      out.push(x);
    }
    return out;
  }

  function peers() {
    var xs = [];
    if (Array.isArray(window.$peers)) xs = xs.concat(window.$peers);
    xs.push(location.origin);
    return uniq(xs);
  }

  function esc(x) {
    return String(x == null ? "" : x)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function parts(path) {
    return String(path || "").split("/").filter(Boolean);
  }

  function join(parts) {
    return parts.filter(Boolean).join("/");
  }

  function parentPath(path) {
    var p = parts(path);
    if (!p.length) return "";
    p.pop();
    return join(p);
  }

  function parseHash() {
    var q = new URLSearchParams(location.hash.replace(/^#/, ""));
    return {
      peer: q.get("peer") || "",
      bucket: q.get("bucket") || "",
      path: q.get("path") || ""
    };
  }

  function makeHash(st) {
    var q = new URLSearchParams();
    if (st.peer) q.set("peer", st.peer);
    if (st.bucket) q.set("bucket", st.bucket);
    if (st.path) q.set("path", st.path);
    return "#" + q.toString();
  }

  function a(label, st) {
    return '<a href="' + makeHash(st) + '">' + esc(label) + "</a>";
  }

  async function getJSON(url) {
    var r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(url + " -> HTTP " + r.status);
    return await r.json();
  }

  function crumbs(st) {
    var out = [];
    out.push(a("peers", {}));
    if (st.peer) out.push(" / " + a(st.peer, { peer: st.peer }));
    if (st.bucket) out.push(" / " + a(st.bucket, { peer: st.peer, bucket: st.bucket }));
    if (st.path) {
      var p = parts(st.path), acc = [];
      for (var i = 0; i < p.length; i++) {
        acc.push(p[i]);
        out.push(" / " + a(p[i], {
          peer: st.peer,
          bucket: st.bucket,
          path: join(acc)
        }));
      }
    }
    return out.join("");
  }

  function cleanNodeData(d) {
    var out = {};
    var keys = Object.keys(d || {});
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      if (k === "_" || k === "#" || k === ">") continue;
      if (d[k] !== null && d[k] !== undefined) out[k] = d[k];
    }
    delete out.remove;
    return out;
  }

  function hasAnyFields(d) {
    return !!d && Object.keys(d).length > 0;
  }

  function parseArrayish(value) {
    if (!value) return [];
    if (Array.isArray(value)) return value;
    if (typeof value === "string") {
      try {
        var parsed = JSON.parse(value);
        return Array.isArray(parsed) ? parsed : [];
      } catch (_) {
        return [];
      }
    }
    return [];
  }

  function normalizeDirChildren(dirNode) {
    var xs = parseArrayish(dirNode && dirNode.children);
    var out = [];
    for (var i = 0; i < xs.length; i++) {
      var it = xs[i];
      if (!it) continue;

      if (typeof it === "string") {
        out.push({
          name: it.split("/").slice(-1)[0],
          path: it,
          rel: "child",
          peer: "",
          bucket: ""
        });
        continue;
      }

      if (!it.path) continue;

      out.push({
        name: it.name || it.label || it.path.split("/").slice(-1)[0],
        path: it.path,
        rel: it.rel || "child",
        peer: it.peer || "",
        bucket: it.bucket || ""
      });
    }
    return out;
  }

  function normalizeLinks(node, st) {
    var raw = node && node.links;
    var xs = parseArrayish(raw);
    var out = [];
    for (var i = 0; i < xs.length; i++) {
      var it = xs[i];

      if (typeof it === "string") {
        out.push({
          label: it,
          peer: st.peer,
          bucket: st.bucket,
          path: it,
          rel: ""
        });
        continue;
      }

      if (!it || !it.path) continue;

      out.push({
        label: it.label || it.name || it.rel || it.path,
        peer: it.peer || st.peer,
        bucket: it.bucket || st.bucket,
        path: it.path,
        rel: it.rel || ""
      });
    }
    return out;
  }

  function dirSceneKey(path) {
    return path ? "dirs/" + path : "dirs/root";
  }

  function peerGun(peer) {
    peer = String(peer || "").replace(/\/$/, "");
    if (!gunByPeer[peer]) {
      gunByPeer[peer] = Gun({ peers: [peer + "/gun"] });
    }
    return gunByPeer[peer];
  }

  function sceneRef(peer, bucket, scenePath) {
    return peerGun(peer).get(bucket).get("scene").get(scenePath);
  }

  function stopWatchers() {
    if (state.dirRef && typeof state.dirRef.off === "function") {
      try { state.dirRef.off(); } catch (_) {}
    }
    if (state.nodeRef && typeof state.nodeRef.off === "function") {
      try { state.nodeRef.off(); } catch (_) {}
    }
    state.dirRef = null;
    state.nodeRef = null;
  }

  function watchDir(peer, bucket, path) {
    var key = dirSceneKey(path);
    var ref = sceneRef(peer, bucket, key);
    state.dirRef = ref;

    function onDir(data) {
      var st = parseHash();
      if (st.peer !== peer || st.bucket !== bucket || st.path !== path) return;
      var clean = cleanNodeData(data);
      state.dirNode = hasAnyFields(clean) ? clean : null;
      state.dirLoaded = true;
      queueRender();
    }

    ref.once(onDir);
    ref.on(onDir);
  }

  function watchNode(peer, bucket, path) {
    if (!path) {
      state.node = null;
      state.nodeLoaded = true;
      queueRender();
      return;
    }

    var ref = sceneRef(peer, bucket, path);
    state.nodeRef = ref;

    function onNode(data) {
      var st = parseHash();
      if (st.peer !== peer || st.bucket !== bucket || st.path !== path) return;
      var clean = cleanNodeData(data);
      state.node = hasAnyFields(clean) ? clean : null;
      state.nodeLoaded = true;
      queueRender();
    }

    ref.once(onNode);
    ref.on(onNode);
  }

  async function peerBuckets(peer) {
    try {
      var meta = await getJSON(peer + "/");
      return Array.isArray(meta.buckets) ? meta.buckets.slice().sort() : [];
    } catch (_) {
      return [];
    }
  }

  async function renderHome() {
    var xs = peers();
    var currentBucket = window.$bucket || "";
    var rows = await Promise.all(xs.map(async function (peer) {
      var buckets = await peerBuckets(peer);
      return { peer: peer, buckets: buckets };
    }));

    var html = "<div class='crumbs'>" + crumbs({}) + "</div>";
    html += "<h1>Peers</h1>";

    if (currentBucket) {
      html += "<p class='muted'>Current bucket: <strong>" + esc(currentBucket) + "</strong></p>";
    }

    html += "<ul>";
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      html += "<li>";
      html += a(row.peer, { peer: row.peer });

      if (currentBucket && row.buckets.indexOf(currentBucket) !== -1) {
        html += " — " + a(currentBucket, { peer: row.peer, bucket: currentBucket });
      } else if (row.buckets.length) {
        html += " <span class='muted'>(" + row.buckets.length + " buckets)</span>";
      } else {
        html += " <span class='muted'>(unreachable or empty)</span>";
      }

      html += "</li>";
    }
    html += "</ul>";

    app.innerHTML = html;
  }

  async function renderBuckets(st) {
    var buckets = await peerBuckets(st.peer);
    var html = "<div class='crumbs'>" + crumbs(st) + "</div>";
    html += "<h1>" + esc(st.peer) + "</h1>";
    html += "<ul>";

    for (var i = 0; i < buckets.length; i++) {
      html += "<li>" + a(buckets[i], {
        peer: st.peer,
        bucket: buckets[i]
      }) + "</li>";
    }

    html += "</ul>";
    app.innerHTML = html;
  }

  function renderPathView() {
    var st = {
      peer: state.peer,
      bucket: state.bucket,
      path: state.path
    };

    var html = "<div class='crumbs'>" + crumbs(st) + "</div>";
    html += "<p>" + a("..", {
      peer: st.peer,
      bucket: st.bucket,
      path: parentPath(st.path)
    }) + "</p>";

    html += "<h1>" + esc(st.path || "/") + "</h1>";

    if (!state.dirLoaded || !state.nodeLoaded) {
      html += "<p class='muted'>Loading…</p>";
      app.innerHTML = html;
      return;
    }

    var dirNode = state.dirNode;
    var node = state.node;
    var children = normalizeDirChildren(dirNode);

    if (children.length) {
      html += "<div class='section'><h2>Children</h2><ul>";
      for (var i = 0; i < children.length; i++) {
        var ch = children[i];
        html += "<li>" + a(ch.name, {
          peer: ch.peer || st.peer,
          bucket: ch.bucket || st.bucket,
          path: ch.path
        }) + "</li>";
      }
      html += "</ul></div>";
    }

    if (dirNode && dirNode.html) {
      html += "<div class='section'><h2>Directory</h2>";
      html += "<div data-rendered-html>" + String(dirNode.html) + "</div>";
      html += "</div>";
    }

    if (node) {
      html += "<div class='section'><h2>Node</h2>";
      html += "<pre>" + esc(JSON.stringify(node, null, 2)) + "</pre></div>";

      var links = normalizeLinks(node, st);
      if (links.length) {
        html += "<div class='section'><h2>Links</h2><ul>";
        for (var j = 0; j < links.length; j++) {
          var it = links[j];
          html += "<li>" + a(it.label, {
            peer: it.peer,
            bucket: it.bucket,
            path: it.path
          }) + "</li>";
        }
        html += "</ul></div>";
      }

      if (node.html) {
        html += "<div class='section'><h2>Rendered HTML</h2>";
        html += "<div data-rendered-html>" + String(node.html) + "</div>";
        html += "</div>";
      }
    }

    if (!dirNode && !node) {
      html += "<p class='muted'>No directory or node at this path.</p>";
    }

    app.innerHTML = html;
  }

  app.addEventListener("click", function (e) {
    var el = e.target.closest("[data-rendered-html] a");
    if (!el) return;

    var st = parseHash();
    var href = String(el.getAttribute("href") || "").trim();
    var dataPath = String(el.getAttribute("data-path") || "").trim();

    if (dataPath) {
      e.preventDefault();
      location.hash = makeHash({
        peer: st.peer,
        bucket: st.bucket,
        path: dataPath
      });
      return;
    }

    if (href && href !== "#") {
      if (href.charAt(0) === "#") {
        e.preventDefault();
        location.hash = href;
        return;
      }

      if (!/^(https?:|mailto:|tel:|javascript:)/i.test(href)) {
        e.preventDefault();
        location.hash = makeHash({
          peer: st.peer,
          bucket: st.bucket,
          path: href.replace(/^\/+/, "")
        });
        return;
      }

      return;
    }

    var path = String(el.textContent || "").trim();
    if (!path) return;

    e.preventDefault();
    location.hash = makeHash({
      peer: st.peer,
      bucket: st.bucket,
      path: path
    });
  });

  async function route() {
    var st = parseHash();

    state.peer = st.peer;
    state.bucket = st.bucket;
    state.path = st.path;

    if (!st.peer) {
      stopWatchers();
      state.dirLoaded = false;
      state.nodeLoaded = false;
      state.dirNode = null;
      state.node = null;
      await renderHome();
      return;
    }

    if (!st.bucket) {
      stopWatchers();
      state.dirLoaded = false;
      state.nodeLoaded = false;
      state.dirNode = null;
      state.node = null;
      await renderBuckets(st);
      return;
    }

    stopWatchers();
    state.dirLoaded = false;
    state.nodeLoaded = false;
    state.dirNode = null;
    state.node = null;
    renderPathView();

    watchDir(st.peer, st.bucket, st.path);
    watchNode(st.peer, st.bucket, st.path);
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