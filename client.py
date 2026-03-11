import atexit
import json
import logging
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlparse
import urllib.error
from pathlib import Path
import os
import re

log = logging.getLogger(__name__)

RE_COMPONENT = re.compile(r'data-component="([^"]+)"')
RE_META = re.compile(r'<meta\s+data-prop="([^"]+)"\s+data-type="([^"]+)"\s+data-default="([^"]*)"')
RE_API_TEMPLATE = re.compile(r'<template\s+data-api>(.*?)</template>', re.DOTALL)
RE_SCRIPT_BLOCK = re.compile(r'<script\b[^>]*>.*?</script>', re.DOTALL | re.IGNORECASE)


def strip_scripts(html: str) -> str:
    return RE_SCRIPT_BLOCK.sub("", html)


def extract_props(html: str):
    props = []
    api_match = RE_API_TEMPLATE.search(html)
    if not api_match:
        return props

    api_block = api_match.group(1)
    for match in RE_META.finditer(api_block):
        props.append({
            "name": match.group(1),
            "type": match.group(2),
            "default": match.group(3),
        })
    return props


def build_proxy_class(component_name, props, raw_html):
    class_name = "".join(word.capitalize() for word in component_name.split("_"))

    script_blocks = RE_SCRIPT_BLOCK.findall(raw_html)
    html_only = strip_scripts(raw_html)

    template = html_only

    template = re.sub(
        r'data-component="[^"]+"',
        'data-component="{_name}" {_subscribe}',
        template,
        count=1
    )

    for p in props:
        prop_name = p['name']
        pattern = rf'(<[^>]+data-slot="{prop_name}"[^>]*>)(.*?)(</[^>]+>)'
        template = re.sub(pattern, rf'\g<1>{{{prop_name}}}\g<3>', template, flags=re.DOTALL)

    if script_blocks:
        template = template + "\n" + "\n".join(script_blocks)

    template = template.replace('{', '{{').replace('}', '}}')
    template = template.replace('{{_name}}', '{_name}').replace('{{_subscribe}}', '{_subscribe}')

    for p in props:
        template = template.replace(f'{{{{{p["name"]}}}}}', f'{{{p["name"]}}}')

    lines = [
        f"class {class_name}:",
        f'    """Generated proxy for {component_name}."""',
        f'    TEMPLATE = """{template}"""\n',
        f"    def __init__(self, graph=None, node_path: str = None):",
        f"        self._g = graph",
        f"        self._node = node_path",
    ]

    for p in props:
        lines.append(f"        self.{p['name']} = '{p['default']}'")

    lines.append("\n    def sync(self):")
    lines.append("        if self._g and self._node:")
    lines.append("            payload = {")
    for p in props:
        lines.append(f"                '{p['name']}': self.{p['name']},")
    lines.append("            }")
    lines.append("            self._g.write(self._node, **payload)\n")

    lines.append("    def render(self, name: str = '', subscribe: str = '') -> str:")
    lines.append('        sub_attr = f\'data-subscribe="{subscribe}"\' if subscribe else \'\'')
    lines.append("        return self.TEMPLATE.format(")
    lines.append("            _name=name or self._node or '',")
    lines.append("            _subscribe=sub_attr,")
    for p in props:
        lines.append(f"            {p['name']}=self.{p['name']},")
    lines.append("        )\n")

    return "\n".join(lines)


class NodeBuilder:
    def __init__(self, client, path: str):
        self._client = client
        self._path = path
        self._data = {}
        self._meta = {}
        self._links = {}
        self._actions = {}

    def write(self, **fields):
        self._data.update(fields)
        return self

    def meta(self, **fields):
        self._meta.update(fields)
        return self

    def link(self, **rels):
        self._links.update(rels)
        return self

    def action(self, **actions):
        self._actions.update(actions)
        return self

    def provider(self, name: str, ts: int | None = None):
        self._meta["provider"] = name
        self._meta["ts"] = ts if ts is not None else int(time.time() * 1000)
        return self

    def commit(self):
        payload = {}
        if self._data:
            payload["data"] = self._data
        if self._meta:
            payload["meta"] = self._meta
        if self._links:
            payload["links"] = self._links
        if self._actions:
            payload["actions"] = self._actions
        self._client.write(self._path, **payload)
        return self


class HyperClient:
    def __init__(
        self,
        relay="http://localhost:8765",
        root="default",
        token=None,
        relay_script=None,
    ):
        self.relay = relay.rstrip("/")
        self.root = root
        self.token = token
        self._proc = None
        self._relay_script = relay_script or (
            Path(__file__).resolve().parent / "src" / "relay.js"
        )

    @property
    def base(self):
        return f"{self.relay}/{self.root}"

    @property
    def gun_relay(self):
        return f"{self.relay}/gun"

    def _headers(self, write=False):
        headers = {"Content-Type": "application/json"}
        if write and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, path, method="GET", data=None, write=False):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(
            f"{self.base}/{path}",
            data=body,
            method=method,
            headers=self._headers(write),
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                if not raw:
                    return {}
                return json.loads(raw)
        except Exception as e:
            log.error("%s %s: %s", method, path, e)
            return None

    def _put(self, key, **data):
        result = self._request(f"scene/{key}", "PUT", data, write=True)
        return result is not None and result.get("ok", False)

    def _delete(self, key):
        return self._request(f"scene/{key}", "DELETE", write=True) is not None

    # ----------------------------
    # Display API
    # ----------------------------

    def mount(self, key, **fragment):
        """Mount a visual fragment. Fields may include html, css, js, link, layer."""
        return self._put(key, **fragment)

    def unmount(self, key):
        """Remove a visual fragment from the display."""
        return self._delete(key)

    def mutate(self, key, **fields):
        """Update fields on an existing fragment without replacing the rest."""
        snap = self._request(f"scene/{key}") or {}
        snap.update(fields)
        return self._put(key, **snap)

    def clear(self):
        return self._request("api/clear", "POST", write=True) is not None

    def keys(self):
        return self._request("api/keys") or []

    def snapshot(self):
        return self._request("api/snapshot") or {}

    # ----------------------------
    # Graph API
    # ----------------------------

    def write(self, path, **fields):
        if not fields:
            return False

        clean = {}
        for key, value in fields.items():
            if isinstance(value, (dict, list)):
                clean[key] = json.dumps(value)
            else:
                clean[key] = value

        return self._put(path, **clean)

    def remove(self, path):
        return self._delete(path)

    def node(self, path: str) -> NodeBuilder:
        return NodeBuilder(self, path)

    # ----------------------------
    # Relay process helpers
    # ----------------------------

    def _wait_for_relay(self, timeout=10, interval=0.3):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                req = urllib.request.Request(self.relay, method="GET")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    data = json.loads(resp.read())
                    if data.get("relay"):
                        return True
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                pass
            time.sleep(interval)
        raise RuntimeError(f"Relay not ready after {timeout}s")

    def start_relay(self):
        if self._proc and self._proc.poll() is None:
            return self._proc

        parsed = urlparse(self.relay)
        port = parsed.port or 8765

        env = os.environ.copy()
        env["PORT"] = str(port)

        self._proc = subprocess.Popen(
            ["node", str(self._relay_script)],
            stdout=sys.stdout,
            stderr=sys.stderr,
            env=env,
        )
        atexit.register(self.stop_relay)

        self._wait_for_relay()

        log.info("━" * 56)
        log.info("  %s/%s/", self.relay, self.root)
        log.info("━" * 56)
        return self._proc
    def stop_relay(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    # ----------------------------
    # Utilities
    # ----------------------------

    @staticmethod
    def expand_layout(base: str, layout: str) -> list[tuple[str, str]]:
        out = []
        i = 0
        row_mode = False

        while i < len(layout):
            ch = layout[i]

            if ch.isspace():
                i += 1
                continue

            if ch == "[":
                end = layout.index("]", i)
                name = layout[i + 1:end].strip()
                key = f"{base}~{name}" if row_mode else f"{base}/{name}"
                out.append((name.upper(), key))
                i = end + 1
                continue

            if ch == "~":
                row_mode = True
                i += 1
                continue

            i += 1

        return out

    def wait(self, seconds):
        time.sleep(seconds)

    @staticmethod
    def links(*resources):
        return json.dumps([{"rel": rel, "href": href} for rel, href in resources])

    @staticmethod
    def generate_proxies(COMPONENTS_DIR="components", OUTPUT_FILE="proxies.py"):
        classes_code = ['"""AUTO-GENERATED API PROXIES. DO NOT EDIT."""\n']

        for root, _, files in os.walk(COMPONENTS_DIR):
            for file in files:
                if not file.endswith(".html"):
                    continue

                path = os.path.join(root, file)
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()

                comp_match = RE_COMPONENT.search(strip_scripts(content))
                if not comp_match:
                    continue

                props = extract_props(content)
                classes_code.append(build_proxy_class(comp_match.group(1), props, content))

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(classes_code))

    @staticmethod
    def load_runtime_js(relay="http://localhost:8765", root="default"):
        relay = relay.rstrip("/")
        relay_gun = f"{relay}/gun"

        return f"""
(function () {{
  'use strict';

  if (typeof Gun === 'undefined') {{
    console.error('[hyper] Gun is not loaded. Include <script src="{relay_gun}/gun.js"></script> first.');
    return;
  }}

  const RELAY = {json.dumps(relay_gun)};
  const ROOT = {json.dumps(root)};

  window.$gun = Gun({{ peers: [RELAY] }});
  window.$root = window.$gun.get(ROOT);
  window.$scene = window.$root.get('scene');

  const mounted = new Map();
  const RESERVED = new Set([
    '_', '#', '>', 'data', 'meta', 'links', 'actions',
    'layer', 'html', 'css', 'js', 'link', 'json'
  ]);

  function parseMaybeJSON(v) {{
    if (typeof v !== 'string') return v;
    const t = v.trim();
    if (!t) return v;
    if ((t.startsWith('{{') && t.endsWith('}}')) || (t.startsWith('[') && t.endsWith(']'))) {{
      try {{ return JSON.parse(t); }} catch (_) {{}}
    }}
    return v;
  }}

  function gunPath(root, path) {{
    return String(path || '')
      .split('/')
      .filter(Boolean)
      .reduce((node, part) => node.get(part), root);
  }}

  function collect(root, selector) {{
    const out = [];
    if (root && root.matches && root.matches(selector)) out.push(root);
    if (root && root.querySelectorAll) out.push(...root.querySelectorAll(selector));
    return out;
  }}

  function executeScripts(root) {{
    for (const script of collect(root, 'script[data-part="js"]')) {{
      if (script.dataset.executed === '1') continue;
      script.dataset.executed = '1';
      try {{
        (0, eval)(script.textContent);
      }} catch (e) {{
        console.error('[runtime script]', e);
      }}
    }}
  }}

  function parseSchema(el) {{
    const schema = {{}};
    const tpl = el.querySelector(':scope > template[data-api]');
    if (!tpl) return schema;

    for (const meta of tpl.content.querySelectorAll('meta[data-prop]')) {{
      schema[meta.dataset.prop] = {{
        type: meta.dataset.type || 'string',
        default: meta.dataset.default ?? ''
      }};
    }}
    return schema;
  }}

  function normalize(payload) {{
    if (!payload || typeof payload !== 'object') return {{}};

    const out = {{}};
    const data = parseMaybeJSON(payload.data);
    const meta = parseMaybeJSON(payload.meta);
    const links = parseMaybeJSON(payload.links);
    const actions = parseMaybeJSON(payload.actions);

    if (data && typeof data === 'object' && !Array.isArray(data)) {{
      Object.assign(out, data);
    }}

    for (const [k, v] of Object.entries(payload)) {{
      if (RESERVED.has(k) || v === null) continue;
      out[k] = parseMaybeJSON(v);
    }}

    if (meta !== undefined) out._meta = meta;
    if (links !== undefined) out._links = links;
    if (actions !== undefined) out._actions = actions;
    out._raw = payload;

    return out;
  }}

  function setSlotValue(slot, val) {{
    if (val === undefined || val === null) return;

    const key = slot.dataset.slot;

    if (key === 'color') {{
      slot.style.background = String(val);
      return;
    }}

    if (slot.dataset.mode === 'html') {{
      slot.innerHTML = String(val);
    }} else {{
      slot.textContent = String(val);
    }}
  }}

  function cloneTemplateNode(host, templateName) {{
    const tpl = host.querySelector(`:scope > template[data-component-template="${{templateName}}"]`);
    if (!tpl) return null;
    const fragment = tpl.content.cloneNode(true);
    const wrap = document.createElement('div');
    wrap.appendChild(fragment);
    return wrap.firstElementChild;
  }}

  class BaseComponent {{
    constructor(el, name, schema) {{
      this._el = el;
      this._name = name;
      this._schema = schema || {{}};
      this._state = {{}};
      this._subs = [];
      this._eachState = new Map();

      for (const [prop, spec] of Object.entries(this._schema)) {{
        this._state[prop] = parseMaybeJSON(spec.default);
      }}

      this._render();
    }}

    start() {{
      const bind = this._el.getAttribute('data-bind');
      const subscribe = this._el.getAttribute('data-subscribe');
      const path = bind || subscribe;
      if (path) this._subscribe(path);
    }}

    _subscribe(path) {{
      const chain = gunPath(window.$scene, path);
      const cb = (data) => this._receive(normalize(data));
      chain.on(cb);
      this._subs.push({{ chain, cb }});
    }}

    _receive(data) {{
      if (!data || typeof data !== 'object') return;
      Object.assign(this._state, data);
      this._render();
      this._renderEach();
    }}

    _render() {{
      for (const slot of this._el.querySelectorAll('[data-slot]')) {{
        if (slot.closest('template')) continue;
        const key = slot.dataset.slot;
        const val = this._state[key];
        setSlotValue(slot, val);
      }}

      for (const block of this._el.querySelectorAll('[data-block]')) {{
        if (block.closest('template')) continue;
        const key = block.dataset.block;
        const val = this._state[key];
        block.style.display = val ? '' : 'none';
      }}
    }}

    _renderEach() {{
      const eachNodes = this._el.querySelectorAll('[data-each]');
      for (const container of eachNodes) {{
        const stateKey = container.dataset.each;
        const templateName = container.dataset.template;
        const pathPrefix = container.dataset.pathPrefix || '';
        const values = parseMaybeJSON(this._state[stateKey]) || [];
        const items = Array.isArray(values) ? values : [];

        let local = this._eachState.get(container);
        if (!local) {{
          local = new Map();
          this._eachState.set(container, local);
        }}

        const wanted = new Set(items.map(String));

        for (const [itemKey, entry] of local.entries()) {{
          if (!wanted.has(itemKey)) {{
            if (entry.node && entry.node.remove) entry.node.remove();
            local.delete(itemKey);
          }}
        }}

        for (const rawItem of items) {{
          const itemKey = String(rawItem);
          if (local.has(itemKey)) continue;

          const node = cloneTemplateNode(this._el, templateName);
          if (!node) continue;

          const childName = `${{templateName}}_${{itemKey}}`;
          node.setAttribute('data-component', childName);

          const bindPath = pathPrefix ? `${{pathPrefix}}${{itemKey}}` : itemKey;
          node.setAttribute('data-bind', bindPath);

          container.appendChild(node);
          local.set(itemKey, {{ node }});

          if (window.mountComponents) window.mountComponents(node);
        }}
      }}
    }}
  }}

  function mountOne(el) {{
    const name = el.getAttribute('data-component') || '';
    if (!name) return;
    if (mounted.has(el)) return mounted.get(el);

    const schema = parseSchema(el);
    const klassName = el.getAttribute('data-class');
    const Klass = (klassName && window[klassName]) || BaseComponent;

    const instance = new Klass(el, name, schema);
    mounted.set(el, instance);

    if (typeof instance.start === 'function') {{
      instance.start();
    }}

    return instance;
  }}

  function mountComponents(root = document) {{
    executeScripts(root);
    for (const el of collect(root, '[data-component]')) {{
      if (el.closest('template')) continue;
      mountOne(el);
    }}
  }}

  function scanExistingDOM() {{
    mountComponents(document);
  }}

  function observeDOM() {{
    const target = document.body || document.documentElement;
    if (!target) return;

    const observer = new MutationObserver((mutations) => {{
      for (const m of mutations) {{
        for (const node of m.addedNodes) {{
          if (!node || node.nodeType !== 1) continue;
          mountComponents(node);
        }}
      }}
    }});

    observer.observe(target, {{ childList: true, subtree: true }});
  }}

  window.BaseComponent = BaseComponent;
  window.mountComponents = mountComponents;
  window.gunPath = (path) => gunPath(window.$scene, path);
  window.$hyper = {{
    relay: RELAY,
    root: ROOT,
    scene: window.$scene,
    gun: window.$gun
  }};

  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', () => {{
      scanExistingDOM();
      observeDOM();
    }});
  }} else {{
    scanExistingDOM();
    observeDOM();
  }}
}})();
"""

    def runtime_script_tag(self):
        return f'<script src="{self.gun_relay}/gun.js"></script>'

    def runtime_bootstrap_tag(self):
        return "<script>" + self.load_runtime_js(self.relay, self.root) + "</script>"

    def runtime_tags(self):
        return self.runtime_script_tag() + "\n" + self.runtime_bootstrap_tag()

    def __repr__(self):
        return f"<HyperClient {self.relay}/{self.root}>"