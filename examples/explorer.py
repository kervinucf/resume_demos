#!/usr/bin/env python3
"""
Explorer — browse the hypergraph through the public API.

No Gun internals in the browser.
No scene.map().
No wire listeners.
No polling.

Uses only:
  GET /                      -> roots
  GET /<path>                -> node
  GET /<path>.tree           -> subtree structure
  GET /<path>.search?q=...   -> indexed subtree search
  GET /<path>.events         -> live invalidation
  GET /<path>.stream         -> live preview

Run:
    python -m examples.explorer --app-root explorer --port 8766

Then open:
    http://localhost:8766/explorer
"""

import argparse
import time
from HyperCoreSDK.client import HyperClient

HTML = """
<style>
  #app {
    min-height: 100vh;
    box-sizing: border-box;
    background: #fff;
    color: #111;
    font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  #app * { box-sizing: border-box; }
  #app a { color: #2563eb; text-decoration: none; }
  #app a:hover { text-decoration: underline; }
  #app .shell {
    display: grid;
    grid-template-columns: 320px 1fr;
    min-height: 100vh;
  }
  #app .side {
    border-right: 1px solid #e5e7eb;
    padding: 16px;
    overflow: auto;
  }
  #app .main {
    padding: 18px 20px 40px;
    overflow: auto;
  }
  #app h1, #app h2, #app h3 {
    margin: 0 0 12px;
    font-weight: 600;
    color: #111827;
  }
  #app h1 { font-size: 22px; }
  #app h2 { font-size: 16px; margin-top: 22px; }
  #app h3 { font-size: 14px; margin-top: 16px; }
  #app p, #app ul, #app pre { margin: 0 0 14px; }
  #app .muted { color: #6b7280; }
  #app .crumbs { margin-bottom: 14px; }
  #app .live {
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: #22c55e;
    margin-right: 6px;
    vertical-align: middle;
  }
  #app .row {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }
  #app .pill {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    background: #eef2ff;
    color: #4338ca;
    font-size: 12px;
  }
  #app .box {
    border: 1px solid #e5e7eb;
    background: #fafafa;
    border-radius: 8px;
    padding: 12px;
    margin-bottom: 14px;
  }
  #app pre {
    white-space: pre-wrap;
    word-break: break-word;
    padding: 12px;
    border: 1px solid #e5e7eb;
    background: #fafafa;
    border-radius: 8px;
    overflow: auto;
  }
  #app ul.tree,
  #app ul.links,
  #app ul.results,
  #app ul.roots {
    list-style: none;
    padding-left: 0;
    margin-left: 0;
  }
  #app ul.tree ul {
    list-style: none;
    padding-left: 16px;
    margin: 4px 0 0;
    border-left: 1px dashed #e5e7eb;
  }
  #app li.node {
    margin: 3px 0;
  }
  #app li.node .self {
    font-weight: 600;
  }
  #app .selected {
    background: #eff6ff;
    border-radius: 6px;
    padding: 2px 6px;
  }
  #app form.search {
    display: flex;
    gap: 8px;
    margin: 10px 0 14px;
  }
  #app input[type="text"] {
    width: 100%;
    padding: 8px 10px;
    border: 1px solid #d1d5db;
    border-radius: 8px;
    font: inherit;
  }
  #app button {
    border: 1px solid #d1d5db;
    background: #fff;
    padding: 8px 10px;
    border-radius: 8px;
    font: inherit;
    cursor: pointer;
  }
  #app button:hover { background: #f9fafb; }
  #app iframe.preview {
    width: 100%;
    min-height: 280px;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    background: #fff;
  }
</style>

<div id="app">
  <div class="shell">
    <aside class="side" id="side"></aside>
    <main class="main" id="main"></main>
  </div>
</div>
"""

JS = r"""
(function () {
  var app = document.getElementById("app");
  if (!app || app.dataset.on) return;
  app.dataset.on = "1";

  var side = document.getElementById("side");
  var main = document.getElementById("main");

  var state = {
    roots: [],
    root: "",
    path: "",
    q: "",
    rootMeta: null,
    tree: null,
    node: null,
    search: null,
    es: null,
    loading: false,
  };

  function esc(x) {
    return String(x == null ? "" : x)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function pathJoin(root, rel) {
    if (!root) return rel || "";
    if (!rel) return root;
    return root + "." + rel;
  }

  function relFromAbs(root, abs) {
    if (!root) return abs || "";
    if (abs === root) return "";
    return abs.indexOf(root + ".") === 0 ? abs.slice(root.length + 1) : abs;
  }

  function encPath(abs) {
    return encodeURIComponent(abs);
  }

  function parseHash() {
    var q = new URLSearchParams(location.hash.replace(/^#/, ""));
    return {
      root: q.get("root") || "",
      path: q.get("path") || "",
      q: q.get("q") || "",
    };
  }

  function setHash(next) {
    var q = new URLSearchParams();
    if (next.root) q.set("root", next.root);
    if (next.path) q.set("path", next.path);
    if (next.q) q.set("q", next.q);
    location.hash = "#" + q.toString();
  }

  function absolutePath() {
    return pathJoin(state.root, state.path);
  }

  function fetchJSON(url) {
    return fetch(url, { cache: "no-store" }).then(async function (r) {
      if (!r.ok) {
        var text = await r.text().catch(function () { return ""; });
        throw new Error(url + " -> HTTP " + r.status + " " + text);
      }
      return r.json();
    });
  }

  function closeStream() {
    if (state.es) {
      try { state.es.close(); } catch (_) {}
      state.es = null;
    }
  }

  function openStream() {
    closeStream();
    if (!state.root) return;

    var scope = absolutePath() || state.root;
    state.es = new EventSource("/" + encPath(scope) + ".events");

    state.es.addEventListener("snapshot", refreshCurrent);
    state.es.addEventListener("update", refreshCurrent);
    state.es.onerror = function () {};
  }

  function refreshRoots() {
    return fetchJSON("/").then(function (meta) {
      state.rootMeta = meta;
      state.roots = Array.isArray(meta.buckets) ? meta.buckets : [];
    }).catch(function () {
      state.rootMeta = null;
      state.roots = [];
    });
  }

  function refreshCurrent() {
    if (!state.root) {
      render();
      return Promise.resolve();
    }

    var abs = absolutePath() || state.root;
    var jobs = [
      fetchJSON("/" + encPath(state.root) + ".tree").then(function (x) { state.tree = x; }).catch(function () { state.tree = null; }),
      fetchJSON("/" + encPath(abs)).then(function (x) { state.node = x; }).catch(function () { state.node = null; }),
    ];

    if (state.q) {
      jobs.push(
        fetchJSON("/" + encPath(abs) + ".search?q=" + encodeURIComponent(state.q))
          .then(function (x) { state.search = x; })
          .catch(function () { state.search = { results: [] }; })
      );
    } else {
      state.search = null;
    }

    return Promise.all(jobs).then(render);
  }

  function normalizeNodeLinks(node) {
    if (!node || !node.links) return [];
    var raw = node.links;
    if (typeof raw === "string") {
      try { raw = JSON.parse(raw); } catch (_) { raw = []; }
    }
    if (!Array.isArray(raw)) return [];
    return raw.filter(Boolean);
  }

  function crumbs() {
    var out = ['<a href="#"></a>'];
    out[0] = '<a href="#">roots</a>';

    if (state.root) {
      out.push(" / ");
      out.push('<a href="' + "#root=" + encodeURIComponent(state.root) + '">' + esc(state.root) + '</a>');
    }

    if (state.path) {
      var parts = state.path.split(".");
      var acc = [];
      for (var i = 0; i < parts.length; i++) {
        acc.push(parts[i]);
        out.push(" / ");
        out.push('<a href="' + "#root=" + encodeURIComponent(state.root) + "&path=" + encodeURIComponent(acc.join(".")) + (state.q ? "&q=" + encodeURIComponent(state.q) : "") + '">' + esc(parts[i]) + '</a>');
      }
    }

    return out.join("");
  }

  function renderTreeNode(node) {
    if (!node) return "";
    var rel = relFromAbs(state.root, node.path);
    var selected = (rel === state.path) ? " selected" : "";
    var label = rel || state.root;
    var href = "#root=" + encodeURIComponent(state.root) +
      (rel ? "&path=" + encodeURIComponent(rel) : "") +
      (state.q ? "&q=" + encodeURIComponent(state.q) : "");

    var html = '<li class="node">' +
      '<a class="' + (selected ? "selected self" : "") + '" href="' + href + '">' +
      esc(label) +
      '</a>' +
      (node.type ? ' <span class="muted">(' + esc(node.type) + ')</span>' : '');

    if (Array.isArray(node.children) && node.children.length) {
      html += '<ul>';
      for (var i = 0; i < node.children.length; i++) {
        html += renderTreeNode(node.children[i]);
      }
      html += '</ul>';
    }

    html += '</li>';
    return html;
  }

  function renderSide() {
    var html = "<h2>Roots</h2>";
    html += '<ul class="roots">';
    if (!state.roots.length) {
      html += '<li class="muted">No roots</li>';
    } else {
      for (var i = 0; i < state.roots.length; i++) {
        var r = state.roots[i];
        html += '<li><a href="#root=' + encodeURIComponent(r) + '">' + esc(r) + '</a></li>';
      }
    }
    html += "</ul>";

    if (state.root) {
      html += "<h2>Tree</h2>";
      if (state.tree && state.tree.tree) {
        html += '<ul class="tree">' + renderTreeNode(state.tree.tree) + '</ul>';
      } else {
        html += '<p class="muted">No tree loaded.</p>';
      }
    }

    side.innerHTML = html;
  }

  function renderMain() {
    var html = '<div class="crumbs">' + crumbs() + '</div>';

    if (!state.root) {
      html += '<h1>Hypergraph Explorer</h1>';
      html += '<p class="muted">Choose a root from the left.</p>';
      main.innerHTML = html;
      return;
    }

    var abs = absolutePath() || state.root;

    html += '<div class="row">';
    html += '<h1><span class="live"></span>' + esc(abs) + '</h1>';
    html += '<span class="pill">api</span>';
    html += '</div>';

    html += '<div class="box">';
    html += '<div><strong>self</strong>: <a target="_blank" href="/' + encPath(abs) + '">/' + esc(abs) + '</a></div>';
    html += '<div><strong>tree</strong>: <a target="_blank" href="/' + encPath(abs) + '.tree">/' + esc(abs) + '.tree</a></div>';
    html += '<div><strong>events</strong>: <a target="_blank" href="/' + encPath(abs) + '.events">/' + esc(abs) + '.events</a></div>';
    html += '<div><strong>stream</strong>: <a target="_blank" href="/' + encPath(abs) + '.stream">/' + esc(abs) + '.stream</a></div>';
    html += '</div>';

    html += '<form class="search" id="search-form">';
    html += '<input id="search-input" type="text" placeholder="search this subtree" value="' + esc(state.q) + '"/>';
    html += '<button type="submit">Search</button>';
    html += '<button type="button" id="clear-search">Clear</button>';
    html += '</form>';

    if (state.search) {
      html += '<h2>Search</h2>';
      if (state.search.results && state.search.results.length) {
        html += '<ul class="results">';
        for (var i = 0; i < state.search.results.length; i++) {
          var it = state.search.results[i];
          var rel = relFromAbs(state.root, it.path);
          var href = "#root=" + encodeURIComponent(state.root) +
            (rel ? "&path=" + encodeURIComponent(rel) : "");
          html += '<li>';
          html += '<a href="' + href + '">' + esc(it.path) + '</a>';
          html += ' <span class="muted">[' + esc(it.match || "") + ']</span>';
          if (it.excerpt) html += '<div class="muted">' + esc(it.excerpt) + '</div>';
          html += '</li>';
        }
        html += '</ul>';
      } else {
        html += '<p class="muted">No matches.</p>';
      }
    }

    html += '<h2>Node</h2>';
    if (!state.node) {
      html += '<p class="muted">No node or branch metadata found.</p>';
    } else {
      html += '<pre>' + esc(JSON.stringify(state.node, null, 2)) + '</pre>';

      var apiLinks = state.node._links || null;
      if (apiLinks && typeof apiLinks === "object") {
        html += '<h2>API Links</h2><ul class="links">';
        Object.keys(apiLinks).forEach(function (k) {
          html += '<li><strong>' + esc(k) + '</strong>: <a target="_blank" href="' + esc(apiLinks[k]) + '">' + esc(apiLinks[k]) + '</a></li>';
        });
        html += '</ul>';
      }

      var semanticLinks = normalizeNodeLinks(state.node);
      if (semanticLinks.length) {
        html += '<h2>Links</h2><ul class="links">';
        for (var j = 0; j < semanticLinks.length; j++) {
          var link = semanticLinks[j];
          var target = link.path || "";
          var rel = relFromAbs(state.root, target);
          var href = "#root=" + encodeURIComponent(state.root) +
            (rel ? "&path=" + encodeURIComponent(rel) : "");
          html += '<li><a href="' + href + '">' + esc(link.label || link.rel || target) + '</a>';
          if (link.path) html += ' <span class="muted">(' + esc(link.path) + ')</span>';
          html += '</li>';
        }
        html += '</ul>';
      }

      if (state.node.html != null) {
        html += '<h2>Live Preview</h2>';
        html += '<iframe class="preview" src="/' + encPath(abs) + '.stream"></iframe>';
      }

      if (state.node.file && state.node._links && state.node._links.download) {
        html += '<h2>File</h2>';
        html += '<p><a target="_blank" href="' + esc(state.node._links.download) + '">download</a></p>';
      }
    }

    main.innerHTML = html;

    var form = document.getElementById("search-form");
    if (form) {
      form.onsubmit = function (e) {
        e.preventDefault();
        var input = document.getElementById("search-input");
        setHash({ root: state.root, path: state.path, q: input.value.trim() });
      };
    }

    var clear = document.getElementById("clear-search");
    if (clear) {
      clear.onclick = function () {
        setHash({ root: state.root, path: state.path, q: "" });
      };
    }
  }

  function render() {
    renderSide();
    renderMain();
  }

  function route() {
    var next = parseHash();

    var changed =
      next.root !== state.root ||
      next.path !== state.path ||
      next.q !== state.q;

    state.root = next.root;
    state.path = next.path;
    state.q = next.q;

    if (!changed) return;

    refreshRoots()
      .then(refreshCurrent)
      .then(openStream)
      .then(render)
      .catch(function (err) {
        console.error("[explorer]", err);
        render();
      });
  }

  window.addEventListener("hashchange", route);

  refreshRoots()
    .then(function () {
      var h = parseHash();
      if (!h.root && state.roots.length) {
        setHash({ root: state.roots[0], path: "", q: "" });
        return;
      }
      route();
    })
    .catch(route);
})();
"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--app-root", default="explorer")
    p.add_argument("--discovery", default="local")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--peer", action="append", default=[])
    a = p.parse_args()

    hc = HyperClient(
        root=a.app_root,
        discovery=a.discovery,
        peers=a.peer or None,
        port=a.port,
    )
    hc.connect()

    hc.at("root.x").write(
        html=HTML,
        js=JS,
        fixed=True,
        layer=9999,
    )

    print("Explorer:", hc.browser_url)

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()