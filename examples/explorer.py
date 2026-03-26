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
  var keysPoll = null;
  var nodePoll = null;

  var view = {
    peer: "",
    bucket: "",
    path: "",
    keys: [],
    keysLoaded: false,
    node: null,
    nodeLoaded: false,
    keysSig: "",
    nodeSig: ""
  };

  function queueRender() {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(function () {
      renderQueued = false;
      renderCurrentView();
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

  function encPath(path) {
    return parts(path).map(encodeURIComponent).join("/");
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

  function parentPath(path) {
    var p = parts(path);
    if (!p.length) return "";
    p.pop();
    return join(p);
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

  function exact(keys, path) {
    if (!path) return false;
    for (var i = 0; i < keys.length; i++) {
      if (keys[i] === path) return true;
    }
    return false;
  }

  function children(keys, prefix) {
    var out = {};
    var pref = prefix ? prefix + "/" : "";
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      if (prefix) {
        if (k !== prefix && k.indexOf(pref) !== 0) continue;
      }
      var rest = prefix ? (k === prefix ? "" : k.slice(pref.length)) : k;
      if (!rest) continue;
      var head = rest.split("/")[0];
      var full = prefix ? prefix + "/" + head : head;
      out[full] = 1;
    }
    return Object.keys(out).sort();
  }

  function normalizeLinks(node, st) {
    if (!node || !node.links) return [];
    var raw = node.links;

    if (typeof raw === "string") {
      try { raw = JSON.parse(raw); } catch (_) { raw = []; }
    }
    if (!Array.isArray(raw)) return [];

    var out = [];
    for (var i = 0; i < raw.length; i++) {
      var it = raw[i];

      if (typeof it === "string") {
        out.push({
          label: it,
          peer: st.peer,
          bucket: st.bucket,
          path: it
        });
        continue;
      }

      if (!it || !it.path) continue;

      out.push({
        label: it.label || it.rel || it.path,
        peer: it.peer || st.peer,
        bucket: it.bucket || st.bucket,
        path: it.path
      });
    }
    return out;
  }

  async function peerBuckets(peer) {
    try {
      var meta = await getJSON(peer + "/");
      return Array.isArray(meta.buckets) ? meta.buckets.slice().sort() : [];
    } catch (_) {
      return [];
    }
  }

  function sig(x) {
    try { return JSON.stringify(x); }
    catch (_) { return String(Math.random()); }
  }

  function stopPolls() {
    if (keysPoll) clearInterval(keysPoll);
    if (nodePoll) clearInterval(nodePoll);
    keysPoll = null;
    nodePoll = null;
  }

  async function refreshKeys(peer, bucket) {
    try {
      var keys = await getJSON(peer + "/" + encodeURIComponent(bucket) + "/api/keys");
      var st = parseHash();
      if (st.peer !== peer || st.bucket !== bucket) return;

      var next = Array.isArray(keys) ? keys.slice().sort() : [];
      var nextSig = sig(next);
      if (nextSig !== view.keysSig) {
        view.keys = next;
        view.keysSig = nextSig;
        view.keysLoaded = true;
        queueRender();
      } else if (!view.keysLoaded) {
        view.keys = next;
        view.keysLoaded = true;
        queueRender();
      }
    } catch (_) {}
  }

  async function refreshNode(peer, bucket, path) {
    var st = parseHash();
    if (st.peer !== peer || st.bucket !== bucket || st.path !== path) return;

    if (!path) {
      if (view.node !== null || !view.nodeLoaded) {
        view.node = null;
        view.nodeSig = "null";
        view.nodeLoaded = true;
        queueRender();
      }
      return;
    }

    try {
      var node = await getJSON(
        peer + "/" + encodeURIComponent(bucket) + "/scene/" + encPath(path)
      );
      var st2 = parseHash();
      if (st2.peer !== peer || st2.bucket !== bucket || st2.path !== path) return;

      var nextSig = sig(node);
      if (nextSig !== view.nodeSig) {
        view.node = node;
        view.nodeSig = nextSig;
        view.nodeLoaded = true;
        queueRender();
      } else if (!view.nodeLoaded) {
        view.node = node;
        view.nodeLoaded = true;
        queueRender();
      }
    } catch (_) {}
  }

  function startPolls(peer, bucket, path) {
    stopPolls();

    refreshKeys(peer, bucket);
    refreshNode(peer, bucket, path);

    keysPoll = setInterval(function () {
      refreshKeys(peer, bucket);
    }, 2000);

    nodePoll = setInterval(function () {
      refreshNode(peer, bucket, path);
    }, 500);
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

  function renderCurrentView() {
    var st = {
      peer: view.peer,
      bucket: view.bucket,
      path: view.path
    };

    if (!st.peer) {
      renderHome();
      return;
    }

    if (!st.bucket) {
      renderBuckets(st);
      return;
    }

    var keys = view.keys || [];
    var path = st.path || "";
    var kids = children(keys, path);
    var isNode = exact(keys, path);

    var html = "<div class='crumbs'>" + crumbs(st) + "</div>";
    html += "<p>" + a("..", {
      peer: st.peer,
      bucket: st.bucket,
      path: parentPath(path)
    }) + "</p>";

    html += "<h1>" + esc(path || "/") + "</h1>";

    if (!view.keysLoaded) {
      html += "<p class='muted'>Loading…</p>";
      app.innerHTML = html;
      return;
    }

    if (kids.length) {
      html += "<div class='section'><h2>Children</h2><ul>";
      for (var i = 0; i < kids.length; i++) {
        var child = kids[i];
        var label = parts(child).slice(-1)[0];
        html += "<li>" + a(label, {
          peer: st.peer,
          bucket: st.bucket,
          path: child
        }) + "</li>";
      }
      html += "</ul></div>";
    }

    if (!isNode) {
      html += "<p class='muted'>No node at this path.</p>";
      app.innerHTML = html;
      return;
    }

    if (!view.nodeLoaded) {
      html += "<p class='muted'>Loading node…</p>";
      app.innerHTML = html;
      return;
    }

    if (!view.node) {
      html += "<p class='muted'>Node is empty or missing.</p>";
      app.innerHTML = html;
      return;
    }

    var node = view.node;

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

    app.innerHTML = html;
  }

  app.addEventListener("click", function (e) {
    var el = e.target.closest("[data-rendered-html] a");
    if (!el) return;

    var st = parseHash();
    var href = String(el.getAttribute("href") || "").trim();

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

    var path = String(el.getAttribute("data-path") || el.textContent || "").trim();
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

    view.peer = st.peer;
    view.bucket = st.bucket;
    view.path = st.path;
    view.keys = [];
    view.keysLoaded = false;
    view.node = null;
    view.nodeLoaded = false;
    view.keysSig = "";
    view.nodeSig = "";

    if (!st.peer) {
      stopPolls();
      view.keysLoaded = true;
      view.nodeLoaded = true;
      renderCurrentView();
      return;
    }

    if (!st.bucket) {
      stopPolls();
      view.keysLoaded = true;
      view.nodeLoaded = true;
      renderCurrentView();
      return;
    }

    renderCurrentView();
    startPolls(st.peer, st.bucket, st.path);
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