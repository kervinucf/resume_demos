(() => {
  if (window.customElements.get("hyper-node")) return;

  const FIELD_NAMES = new Set([
    "data", "html", "css", "js", "fixed", "layer", "schema", "trust"
  ]);
  const META_KEYS = new Set(["_", "#", ">"]);
  const DEFAULT_INTRINSIC_SIZE = 96;
  const OVERSCAN_PX = 2200;
  const CONTRACT_KEYS = new Set([
    "manifest", "schema", "links", "actions", "events",
    "html", "css", "js", "trust"
  ]);

  // Keys that must never leak from secure into public or vice versa
  const RESERVED_DATA_KEYS = new Set(["html", "css", "js", "trust"]);

  const LOG = true;
  const LOG_PAYLOAD_LIMIT = 1200;

  function safePreview(value, max = LOG_PAYLOAD_LIMIT) {
    let text;
    try {
      text = JSON.stringify(value, null, 2);
    } catch (_) {
      try { text = String(value); } catch (_) { text = "[unprintable]"; }
    }
    if (text.length > max) {
      return text.slice(0, max) + ` ... [truncated ${text.length - max} chars]`;
    }
    return text;
  }

  function log(node, stage, message, payload) {
    if (!LOG) return;
    const label = node && node.id ? `#${node.id}` : "(unbound)";
    if (payload === undefined) {
      console.log(`[hyper-node ${label}] ${stage}: ${message}`);
    } else {
      console.log(`[hyper-node ${label}] ${stage}: ${message}\n${safePreview(payload)}`);
    }
  }

  function warn(node, stage, message, payload) {
    const label = node && node.id ? `#${node.id}` : "(unbound)";
    if (payload === undefined) {
      console.warn(`[hyper-node ${label}] ${stage}: ${message}`);
    } else {
      console.warn(`[hyper-node ${label}] ${stage}: ${message}\n${safePreview(payload)}`);
    }
  }

  function isObject(v) {
    return !!v && typeof v === "object" && !Array.isArray(v);
  }

  function clean(value) {
    if (Array.isArray(value)) return value.map(clean);
    if (!value || typeof value !== "object") return value;

    const out = {};
    for (const [k, v] of Object.entries(value)) {
      if (META_KEYS.has(k)) continue;
      if (v === undefined || v === null) continue;
      out[k] = clean(v);
    }
    return out;
  }

  function deepMerge(base, patch) {
    if (!isObject(base)) base = {};
    if (!isObject(patch)) return base;

    const out = { ...base };
    for (const [k, v] of Object.entries(patch)) {
      if (isObject(v) && isObject(out[k])) out[k] = deepMerge(out[k], v);
      else out[k] = v;
    }
    return out;
  }

  function dig(value, path) {
    let cur = value;
    for (let i = 0; i < path.length; i++) {
      if (cur == null) return undefined;
      cur = cur[path[i]];
    }
    return cur;
  }

  function stableStringify(value) {
    try { return JSON.stringify(clean(value)); }
    catch (_) { return String(value); }
  }

  function parseMaybeJSON(value) {
    if (value === "") return "";
    try { return JSON.parse(value); } catch (_) { return value; }
  }

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // ─── STATE BOUNDARY: sanitize data to prevent key misrouting ────────
  function sanitizePublicData(data) {
    if (!isObject(data)) return data;
    const out = {};
    for (const [k, v] of Object.entries(data)) {
      if (RESERVED_DATA_KEYS.has(k)) continue;
      out[k] = v;
    }
    return out;
  }

  function normalizeRawPath(raw) {
    let v = String(raw || "").trim();
    if (!v) return "";
    if (v.startsWith("$")) v = v.slice(1);
    const q = v.indexOf("?");
    if (q !== -1) v = v.slice(0, q);
    const h = v.indexOf("#");
    if (h !== -1) v = v.slice(0, h);
    v = v.replace(/^\/+/, "");
    if (v.endsWith(".stream")) v = v.slice(0, -7);
    v = v.replace(/\//g, ".");
    return v;
  }

  function parseNormalizedPath(raw) {
    const normalized = normalizeRawPath(raw);
    if (!normalized) return null;

    const parts = normalized.split(".").filter(Boolean);
    if (!parts.length) return null;

    const root = parts[0];
    const rest = parts.slice(1);

    let fieldStart = -1;
    for (let i = 0; i < rest.length; i++) {
      if (FIELD_NAMES.has(rest[i])) {
        fieldStart = i;
        break;
      }
    }

    const nodeSegs = fieldStart === -1 ? rest : rest.slice(0, fieldStart);
    const fieldPath = fieldStart === -1 ? [] : rest.slice(fieldStart);

    return {
      normalized,
      root,
      scenePath: nodeSegs.length ? nodeSegs.join("/") : "__root__",
      fieldPath,
    };
  }

  function parseSource(rawSrc, baseHref) {
    const url = new URL(rawSrc, baseHref || location.href);
    const pathname = decodeURIComponent(url.pathname || "");
    const parsed = parseNormalizedPath(pathname);
    if (!parsed) return null;

    return {
      ...parsed,
      href: url.href,
      url,
      searchParams: url.searchParams,
      peerUrl: url.origin + "/gun",
    };
  }

  function parseBindingRef(rawValue, baseHref, fallbackPeerUrl) {
    let v = String(rawValue || "").trim();
    if (!v) return null;
    if (v.startsWith("$")) v = v.slice(1);

    if (/^https?:\/\//i.test(v)) return parseSource(v, baseHref);

    const parsed = parseNormalizedPath(v);
    if (!parsed) return null;

    const base = new URL(baseHref || location.href);
    return {
      ...parsed,
      href: v,
      url: base,
      searchParams: new URLSearchParams(),
      peerUrl: fallbackPeerUrl || base.origin + "/gun",
    };
  }

  function isSchemaLeafSpec(value) {
    return isObject(value) && (
      "default" in value ||
      "type" in value ||
      "required" in value ||
      "enum" in value ||
      "const" in value
    );
  }

  function deriveDefaults(spec) {
    if (spec === undefined) return undefined;
    if (Array.isArray(spec)) return clean(spec);
    if (!isObject(spec)) return undefined;

    if (isSchemaLeafSpec(spec)) {
      return spec.default !== undefined ? clean(spec.default) : undefined;
    }

    const out = {};
    for (const [k, v] of Object.entries(spec)) {
      const next = deriveDefaults(v);
      if (next !== undefined) out[k] = next;
    }
    return Object.keys(out).length ? out : {};
  }

  function normalizeSchema(schema) {
    if (!isObject(schema)) return { public: {}, secure: {}, local: {} };

    const hasNamespace =
      "public" in schema || "secure" in schema || "local" in schema;

    if (hasNamespace) {
      return {
        public: isObject(schema.public) ? schema.public : {},
        secure: isObject(schema.secure) ? schema.secure : {},
        local: isObject(schema.local) ? schema.local : {},
      };
    }

    return { public: {}, secure: {}, local: isObject(schema) ? schema : {} };
  }

  function hashString(str) {
    let h = 2166136261;
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return (h >>> 0).toString(36);
  }

  function nodeIdFromSource(src) {
    return `hyper_${hashString(String(src || ""))}`;
  }

  function splitAppPayload(value) {
    const src = clean(value) || {};
    const data = isObject(src.data) ? clean(src.data) : null;
    const candidate = data || src;

    const contract = {};
    let hasContract = false;

    for (const key of CONTRACT_KEYS) {
      if (candidate[key] !== undefined) {
        contract[key] = candidate[key];
        hasContract = true;
      }
    }

    const publicData = {};

    if (data) {
      for (const [k, v] of Object.entries(data)) {
        if (!CONTRACT_KEYS.has(k) && !RESERVED_DATA_KEYS.has(k)) publicData[k] = v;
      }
    } else {
      for (const [k, v] of Object.entries(src)) {
        if (!CONTRACT_KEYS.has(k) && k !== "data" && !RESERVED_DATA_KEYS.has(k)) publicData[k] = v;
      }
    }

    return {
      contract: hasContract ? contract : null,
      publicData: sanitizePublicData(publicData),
    };
  }

  // ─── CONTRACT VALIDATION (client-side) ──────────────────────────────

  function validateContract(node, contract) {
    const warnings = [];
    if (!contract) return warnings;

    // Only validate manifest if something has been declared
    const manifest = contract.manifest;
    if (manifest && Object.keys(manifest).length > 0) {
      if (!manifest.name) warnings.push("manifest missing 'name'");
      if (!manifest.version) warnings.push("manifest missing 'version'");
    }

    const schema = contract.schema;
    if (schema) {
      for (const ns of ["public", "secure", "local"]) {
        if (schema[ns] && !isObject(schema[ns])) {
          warnings.push(`schema.${ns} is not an object`);
        }
      }
    }

    if (isObject(contract.actions)) {
      for (const [name, spec] of Object.entries(contract.actions)) {
        if (!isObject(spec)) warnings.push(`action "${name}" has no spec object`);
      }
    }

    if (isObject(contract.events)) {
      for (const [name, spec] of Object.entries(contract.events)) {
        if (!isObject(spec)) warnings.push(`event "${name}" has no spec object`);
      }
    }

    for (const w of warnings) {
      warn(node, "contract", w);
    }

    return warnings;
  }

  // ─── TEMPLATE REGISTRY ─────────────────────────────────────────────

  class TemplateRegistry {
    constructor() {
      this.map = new Map();
    }

    get(html) {
      let compiled = this.map.get(html);
      if (compiled) return compiled;

      const tpl = document.createElement("template");
      tpl.innerHTML = html;

      const bindings = [];
      const path = [];

      const walk = (node) => {
        if (node && node.nodeType === 1) {
          const el = node;
          const textSpec = el.getAttribute("data-bind-text");
          const htmlSpec = el.getAttribute("data-bind-html");
          const styleSpec = el.getAttribute("data-bind-style");

          if (textSpec || htmlSpec || styleSpec) {
            const stylePaths = styleSpec
              ? styleSpec.split(";").map((pair) => {
                  const parts = pair.split(":");
                  if (parts.length !== 2) return null;
                  return {
                    prop: parts[0].trim(),
                    path: parts[1].trim().split("."),
                  };
                }).filter(Boolean)
              : null;

            bindings.push({
              nodePath: path.slice(),
              textPath: textSpec ? textSpec.split(".") : null,
              htmlPath: htmlSpec ? htmlSpec.split(".") : null,
              stylePaths,
            });
          }
        }

        let child = node.firstChild;
        let index = 0;
        while (child) {
          path.push(index);
          walk(child);
          path.pop();
          child = child.nextSibling;
          index++;
        }
      };

      walk(tpl.content);

      compiled = { template: tpl, bindings };
      this.map.set(html, compiled);
      return compiled;
    }

    clone(html) {
      const compiled = this.get(html);
      const fragment = compiled.template.content.cloneNode(true);

      const resolveNode = (root, nodePath) => {
        let cur = root;
        for (let i = 0; i < nodePath.length; i++) {
          cur = cur.childNodes[nodePath[i]];
          if (!cur) return null;
        }
        return cur;
      };

      return {
        fragment,
        bindingRefs: compiled.bindings.map((def) => ({
          el: resolveNode(fragment, def.nodePath),
          textPath: def.textPath,
          htmlPath: def.htmlPath,
          stylePaths: def.stylePaths,
        })),
      };
    }
  }

  // ─── VIEWPORT MANAGER ──────────────────────────────────────────────

  class ViewportManager {
    constructor() {
      this.nodes = new Set();
      this.scheduled = false;
      this.onScroll = this.onScroll.bind(this);
      this.onResize = this.onResize.bind(this);

      window.addEventListener("scroll", this.onScroll, { passive: true });
      window.addEventListener("resize", this.onResize, { passive: true });
      window.addEventListener("orientationchange", this.onResize, { passive: true });
    }

    register(node) {
      this.nodes.add(node);
      this.schedule();
    }

    unregister(node) {
      this.nodes.delete(node);
    }

    onScroll() { this.schedule(); }
    onResize() { this.schedule(); }

    schedule() {
      if (this.scheduled) return;
      this.scheduled = true;
      requestAnimationFrame(() => {
        this.scheduled = false;
        this.measure();
      });
    }

    measure() {
      const topBound = -OVERSCAN_PX;
      const bottomBound = window.innerHeight + OVERSCAN_PX;

      for (const node of this.nodes) {
        if (!node.isConnected) continue;
        const rect = node.getBoundingClientRect();
        const active = rect.bottom >= topBound && rect.top <= bottomBound;
        node.__setViewportActive(active);
      }
    }
  }

  // ─── BROWSER STORE ─────────────────────────────────────────────────

  class BrowserHyperStore {
    constructor() {
      this.gun = window.$browserGun || Gun();
      window.$browserGun = this.gun;
      this.watchers = new Map();
    }

    envelopePath(nodeId, section) {
      return this.gun.get("hyper").get("nodes").get(nodeId).get(section);
    }

    putSection(nodeId, section, value) {
      this.envelopePath(nodeId, section).put(clean(value || {}));
    }

    putMeta(nodeId, patch) {
      this.envelopePath(nodeId, "meta").put(clean(patch || {}));
    }

    subscribeSection(nodeId, section, cb) {
      const key = `${nodeId}:${section}`;
      let entry = this.watchers.get(key);

      if (!entry) {
        entry = {
          callbacks: new Set(),
          chain: this.envelopePath(nodeId, section),
        };
        entry.chain.on((value) => {
          const cleaned = clean(value) || {};
          for (const fn of entry.callbacks) {
            try { fn(cleaned); } catch (err) { console.error(err); }
          }
        });
        this.watchers.set(key, entry);
      }

      entry.callbacks.add(cb);

      return () => {
        entry.callbacks.delete(cb);
      };
    }
  }

  // ─── PEER RUNTIME ──────────────────────────────────────────────────

  class PeerRuntime {
    constructor(peerUrl) {
      this.peerUrl = peerUrl;
      this.gun = Gun({ peers: [peerUrl] });
      this.entries = new Map();
    }

    subscribe(key, chainFactory, projector, cb) {
      let entry = this.entries.get(key);

      if (!entry) {
        entry = {
          value: undefined,
          hasValue: false,
          watchers: new Set(),
          chain: null,
        };
        this.entries.set(key, entry);
      }

      entry.watchers.add(cb);

      if (!entry.chain) {
        entry.chain = chainFactory(this.gun);
        entry.chain.on((raw) => {
          const next = projector ? projector(raw) : raw;
          entry.value = next;
          entry.hasValue = true;
          for (const fn of entry.watchers) {
            try { fn(next); } catch (err) { console.error(err); }
          }
        });
      }

      if (entry.hasValue) cb(entry.value);

      return () => {
        entry.watchers.delete(cb);
      };
    }
  }

  // ─── SECURE CONTEXT ────────────────────────────────────────────────
  // The secure context is a per-instance isolated store, NOT a global
  // singleton. Each hyper-node that declares secure schema gets its
  // own secure namespace. The host page sets secure context per node
  // through the element API, not through a single global.

  class SecureContextStore {
    constructor() {
      this.stores = new Map();  // nodeId -> { value, listeners }
      this.globalValue = clean(window.$secureContext || {}) || {};
      this.globalListeners = new Set();
    }

    // Global secure context for backward compat
    getGlobal() {
      return this.globalValue || {};
    }

    setGlobal(next) {
      const cleaned = clean(next || {}) || {};
      const prev = stableStringify(this.globalValue || {});
      const nextSig = stableStringify(cleaned);
      if (prev === nextSig) return this.globalValue;

      this.globalValue = cleaned;
      for (const fn of this.globalListeners) {
        try { fn(this.globalValue); } catch (err) { console.error(err); }
      }
      // Notify all per-node stores too
      for (const [, store] of this.stores) {
        const merged = deepMerge(this.globalValue, store.overlay || {});
        store.value = merged;
        for (const fn of store.listeners) {
          try { fn(merged); } catch (err) { console.error(err); }
        }
      }
      window.dispatchEvent(new CustomEvent("hyper-secure-context-change", {
        detail: { value: this.globalValue },
      }));
      return this.globalValue;
    }

    mergeGlobal(patch) {
      return this.setGlobal(deepMerge(this.getGlobal(), patch || {}));
    }

    subscribeGlobal(fn) {
      this.globalListeners.add(fn);
      return () => { this.globalListeners.delete(fn); };
    }

    // Per-node secure context
    getForNode(nodeId) {
      const store = this.stores.get(nodeId);
      if (!store) return this.globalValue || {};
      return store.value || this.globalValue || {};
    }

    setForNode(nodeId, overlay) {
      let store = this.stores.get(nodeId);
      if (!store) {
        store = { value: {}, overlay: {}, listeners: new Set() };
        this.stores.set(nodeId, store);
      }
      store.overlay = clean(overlay || {}) || {};
      store.value = deepMerge(this.globalValue, store.overlay);
      for (const fn of store.listeners) {
        try { fn(store.value); } catch (err) { console.error(err); }
      }
      return store.value;
    }

    mergeForNode(nodeId, patch) {
      const current = this.getForNode(nodeId);
      return this.setForNode(nodeId, deepMerge(current, patch || {}));
    }

    subscribeForNode(nodeId, fn) {
      let store = this.stores.get(nodeId);
      if (!store) {
        store = { value: deepMerge(this.globalValue, {}), overlay: {}, listeners: new Set() };
        this.stores.set(nodeId, store);
      }
      store.listeners.add(fn);
      return () => { store.listeners.delete(fn); };
    }
  }

  // ─── RUNTIME SINGLETON ─────────────────────────────────────────────

  const runtime = window.$hyperRuntime || {
    templates: new TemplateRegistry(),
    viewport: new ViewportManager(),
    browserStore: new BrowserHyperStore(),
    peers: new Map(),
    resizeObserver: null,
    secure: new SecureContextStore(),

    getPeer(peerUrl) {
      let peer = this.peers.get(peerUrl);
      if (!peer) {
        peer = new PeerRuntime(peerUrl);
        this.peers.set(peerUrl, peer);
      }
      return peer;
    },

    observeSize(node) {
      if (!this.resizeObserver) {
        this.resizeObserver = new ResizeObserver((entries) => {
          for (const entry of entries) {
            const host = entry.target;
            if (host && host.__hyperNodeRef) {
              host.__hyperNodeRef.__updateIntrinsicSize(entry.contentRect.height);
            }
          }
        });
      }
      this.resizeObserver.observe(node);
    },

    unobserveSize(node) {
      if (!this.resizeObserver) return;
      try { this.resizeObserver.unobserve(node); } catch (_) {}
    },
  };

  window.$hyperRuntime = runtime;

  // Backward-compat global secure context accessors
  try {
    Object.defineProperty(window, "$secureContext", {
      configurable: true,
      enumerable: true,
      get() { return runtime.secure.getGlobal(); },
      set(v) { runtime.secure.setGlobal(v); },
    });
  } catch (_) {}

  window.$setSecureContext = function (next) {
    return runtime.secure.setGlobal(next);
  };

  window.$mergeSecureContext = function (patch) {
    return runtime.secure.mergeGlobal(patch);
  };

  // ─── HYPER-NODE ELEMENT ─────────────────────────────────────────────

  class HyperNode extends HTMLElement {
    static get observedAttributes() {
      return ["src", "trust"];
    }

    constructor() {
      super();

      this.__hyperNodeRef = this;
      this.__viewportActive = true;
      this.__pendingOffscreen = false;
      this.__lastMeasuredHeight = DEFAULT_INTRINSIC_SIZE;

      this.attachShadow({ mode: "open" });

      this.styleEl = document.createElement("style");
      this.styleEl.textContent = `
        :host {
          display: block;
          box-sizing: border-box;
          min-width: 0;
          min-height: 0;
          contain: layout style paint;
          content-visibility: auto;
          contain-intrinsic-size: auto ${DEFAULT_INTRINSIC_SIZE}px;
        }
        #container {
          box-sizing: border-box;
          width: 100%;
          min-width: 0;
          min-height: 0;
          font: inherit;
        }
        pre {
          margin: 0;
          white-space: pre-wrap;
          word-break: break-word;
        }
      `;

      this.container = document.createElement("div");
      this.container.id = "container";

      this.shadowRoot.appendChild(this.styleEl);
      this.shadowRoot.appendChild(this.container);

      this.nodeId = null;

      this._descriptor = null;
      this._remoteUnsubs = [];
      this._localUnsubs = [];
      this._offSecure = null;
      this._renderQueued = false;
      this._bindingRefs = [];
      this._lastHtml = null;
      this._lastCss = null;
      this._lastJsSignature = null;

      // ── Rebinding safety ──
      // Track the generation of the current mount. When HTML changes
      // structurally, we bump the generation, which invalidates the
      // __bound guard and lets node JS re-bind to new DOM.
      this._mountGeneration = 0;

      this.schema = { public: {}, secure: {}, local: {} };
      this.schemaSignature = stableStringify(this.schema);

      this.publicContext = {};
      this.secureContext = {};
      this.localContext = {};
      this.bindingContext = {};

      this.contract = {
        manifest: {},
        schema: { public: {}, secure: {}, local: {} },
        links: {},
        actions: {},
        events: {},
        trust: "public",
        html: "",
        css: "",
        js: "",
      };

      this.stateEnvelope = {
        public: {},
        local: {},
        meta: {},
      };

      this.literalCtx = {};
      this.boundCtx = Object.create(null);
      this.bindingTargets = Object.create(null);

      this._actions = {};
      this._actionHandlers = {};
      this._events = {};
      this._links = {};
      this._manifest = {};
      this.value = undefined;
    }

    connectedCallback() {
      log(this, "lifecycle", "connected");

      runtime.viewport.register(this);
      runtime.observeSize(this);

      this._offSecure = runtime.secure.subscribeGlobal(() => {
        this.requestRender();
      });

      this.mount();
    }

    disconnectedCallback() {
      log(this, "lifecycle", "disconnected");

      runtime.viewport.unregister(this);
      runtime.unobserveSize(this);

      if (this._offSecure) {
        this._offSecure();
        this._offSecure = null;
      }

      this.teardown();
    }

    attributeChangedCallback(name, oldValue, newValue) {
      if (oldValue === newValue || !this.isConnected) return;
      if (name === "src") this.mount();
      if (name === "trust") this.requestRender();
    }

    __setViewportActive(active) {
      if (this.__viewportActive === active) return;
      this.__viewportActive = active;
      if (active && this.__pendingOffscreen) {
        this.__pendingOffscreen = false;
        this.requestRender();
      }
    }

    __updateIntrinsicSize(height) {
      const h = Math.max(1, Math.ceil(height || 0));
      if (!h || h === this.__lastMeasuredHeight) return;
      this.__lastMeasuredHeight = h;
      this.style.containIntrinsicSize = `auto ${h}px`;
    }

    teardown() {
      for (const off of this._remoteUnsubs) {
        try { off(); } catch (_) {}
      }
      for (const off of this._localUnsubs) {
        try { off(); } catch (_) {}
      }
      this._remoteUnsubs = [];
      this._localUnsubs = [];
      this._renderQueued = false;
    }

    getTrustMode() {
      const attrTrust = (this.getAttribute("trust") || "").trim();
      const contractTrust = this.contract && this.contract.trust != null
        ? String(this.contract.trust).trim()
        : "";
      return attrTrust || contractTrust || "public";
    }

    syncDerivedContractState() {
      this.schema = normalizeSchema(this.contract.schema || {});
      this.schemaSignature = stableStringify(this.schema);
      this._manifest = this.contract.manifest || {};
      this._links = this.contract.links || {};
      this._actions = this.contract.actions || {};
      this._events = this.contract.events || {};
    }

    applyContractNow(contractPatch) {
      const base = {
        manifest: {},
        schema: { public: {}, secure: {}, local: {} },
        links: {},
        actions: {},
        events: {},
        trust: "public",
        html: "",
        css: "",
        js: "",
      };

      const next = deepMerge(base, deepMerge(this.contract || {}, clean(contractPatch || {})));

      // ── Contract validation ──
      validateContract(this, next);

      this.contract = next;
      this.syncDerivedContractState();

      const localDefaults = deriveDefaults(this.schema.local) || {};
      this.stateEnvelope.local = deepMerge(localDefaults, this.stateEnvelope.local || {});

      const secureDefaults = deriveDefaults(this.schema.secure) || {};
      if (this.nodeId) {
        runtime.secure.mergeForNode(this.nodeId, secureDefaults);
      }

      const publicDefaults = deriveDefaults(this.schema.public) || {};
      this.stateEnvelope.public = deepMerge(publicDefaults, this.stateEnvelope.public || {});

      return this.contract;
    }

    applyPublicNow(nextPublic) {
      const publicDefaults = deriveDefaults(this.schema.public) || {};
      this.stateEnvelope.public = deepMerge(publicDefaults, sanitizePublicData(clean(nextPublic || {}) || {}));
      return this.stateEnvelope.public;
    }

    applyLocalNow(nextLocal) {
      const localDefaults = deriveDefaults(this.schema.local) || {};
      this.stateEnvelope.local = deepMerge(localDefaults, clean(nextLocal || {}) || {});
      return this.stateEnvelope.local;
    }

    persistContract() {
      runtime.browserStore.putSection(this.nodeId, "contract", this.contract);
    }

    persistPublic() {
      runtime.browserStore.putSection(this.nodeId, "public", this.stateEnvelope.public);
    }

    persistLocal() {
      runtime.browserStore.putSection(this.nodeId, "local", this.stateEnvelope.local);
    }

    initBrowserEnvelope() {
      runtime.browserStore.putSection(this.nodeId, "source", {
        path: this._descriptor ? this._descriptor.normalized : null,
        href: this._descriptor ? this._descriptor.href : null,
        peer: this._descriptor ? this._descriptor.peerUrl : null,
      });

      this.applyContractNow({
        manifest: {},
        schema: { public: {}, secure: {}, local: {} },
        links: {},
        actions: {},
        events: {},
        trust: this.getAttribute("trust") || "public",
        html: "",
        css: "",
        js: "",
      });
      this.persistContract();

      this.applyPublicNow({});
      this.persistPublic();

      this.applyLocalNow({});
      this.persistLocal();

      runtime.browserStore.putSection(this.nodeId, "meta", {
        stale: false,
        syncedAt: null,
      });

      this._localUnsubs.push(
        runtime.browserStore.subscribeSection(this.nodeId, "contract", (value) => {
          this.applyContractNow(value);
          this.requestRender();
        })
      );

      this._localUnsubs.push(
        runtime.browserStore.subscribeSection(this.nodeId, "public", (value) => {
          this.applyPublicNow(value);
          this.requestRender();
        })
      );

      this._localUnsubs.push(
        runtime.browserStore.subscribeSection(this.nodeId, "local", (value) => {
          this.applyLocalNow(value);
          this.requestRender();
        })
      );

      this._localUnsubs.push(
        runtime.browserStore.subscribeSection(this.nodeId, "meta", (value) => {
          this.stateEnvelope.meta = clean(value) || {};
          this.requestRender();
        })
      );

      // Per-node secure subscription
      this._localUnsubs.push(
        runtime.secure.subscribeForNode(this.nodeId, () => {
          this.requestRender();
        })
      );
    }

    mirrorRemoteToBrowser(descriptor) {
      const peer = runtime.getPeer(descriptor.peerUrl);

      this._remoteUnsubs.push(
        peer.subscribe(
          `node|${descriptor.peerUrl}|${descriptor.root}|${descriptor.scenePath}`,
          (gun) => gun.get(descriptor.root).get("scene").get(descriptor.scenePath),
          (raw) => clean(raw) || {},
          (nodeValue) => {
            const split = splitAppPayload(nodeValue);

            if (split.contract) {
              this.applyContractNow(deepMerge(
                {
                  trust: this.getAttribute("trust") || "public",
                  html: "",
                  css: "",
                  js: "",
                },
                split.contract
              ));
              this.persistContract();
            }

            const publicDefaults = deriveDefaults(this.schema.public) || {};
            const nextPublic = deepMerge(
              publicDefaults,
              deepMerge(
                this.literalCtx || {},
                deepMerge(this.boundCtx || {}, split.publicData || {})
              )
            );

            this.applyPublicNow(nextPublic);
            this.persistPublic();

            runtime.browserStore.putMeta(this.nodeId, {
              stale: false,
              syncedAt: new Date().toISOString(),
            });

            this.requestRender();
          }
        )
      );

      this._remoteUnsubs.push(
        peer.subscribe(
          `data|${descriptor.peerUrl}|${descriptor.root}|${descriptor.scenePath}`,
          (gun) => gun.get(descriptor.root).get("scene").get(descriptor.scenePath).get("data"),
          (raw) => clean(raw) || {},
          (dataValue) => {
            const split = splitAppPayload({ data: dataValue });

            if (split.contract) {
              this.applyContractNow(deepMerge(
                {
                  trust: this.getAttribute("trust") || "public",
                  html: "",
                  css: "",
                  js: "",
                },
                split.contract
              ));
              this.persistContract();
            }

            const publicDefaults = deriveDefaults(this.schema.public) || {};
            const nextPublic = deepMerge(
              publicDefaults,
              deepMerge(
                this.literalCtx || {},
                deepMerge(this.boundCtx || {}, split.publicData || {})
              )
            );

            this.applyPublicNow(nextPublic);
            this.persistPublic();

            runtime.browserStore.putMeta(this.nodeId, {
              stale: false,
              syncedAt: new Date().toISOString(),
            });

            this.requestRender();
          }
        )
      );
    }

    subscribeBindings() {
      const updatePublicEnvelope = () => {
        const publicDefaults = deriveDefaults(this.schema.public) || {};
        const nextPublic = deepMerge(
          publicDefaults,
          deepMerge(
            this.literalCtx || {},
            this.boundCtx || {}
          )
        );
        this.applyPublicNow(nextPublic);
        this.persistPublic();
        this.requestRender();
      };

      for (const [name, info] of Object.entries(this.bindingTargets)) {
        const peer = runtime.getPeer(info.peerUrl);

        if (info.fieldPath[0] === "data") {
          this._remoteUnsubs.push(
            peer.subscribe(
              `bind:data|${info.peerUrl}|${info.root}|${info.scenePath}|${name}`,
              (gun) => gun.get(info.root).get("scene").get(info.scenePath).get("data"),
              (raw) => clean(raw) || {},
              (value) => {
                this.boundCtx[name] =
                  info.fieldPath.length > 1
                    ? dig(value, info.fieldPath.slice(1))
                    : value;
                updatePublicEnvelope();
              }
            )
          );
        } else {
          this._remoteUnsubs.push(
            peer.subscribe(
              `bind:node|${info.peerUrl}|${info.root}|${info.scenePath}|${name}`,
              (gun) => gun.get(info.root).get("scene").get(info.scenePath),
              (raw) => clean(raw) || {},
              (value) => {
                this.boundCtx[name] =
                  info.fieldPath.length > 0
                    ? dig(value, info.fieldPath)
                    : value;
                updatePublicEnvelope();
              }
            )
          );
        }
      }
    }

    mount() {
      this.teardown();

      const rawSrc = this.getAttribute("src") || "";
      if (!rawSrc) return;

      const descriptor = parseSource(rawSrc, location.href);
      if (!descriptor) return;

      this._descriptor = descriptor;
      this.nodeId = nodeIdFromSource(descriptor.href);

      // ── FIELD PATH: raw value subscription ──
      // When the src targets a specific field (e.g. data.temp),
      // don't load the node contract. Subscribe directly to the
      // full dot path in Gun and render the raw value.
      if (descriptor.fieldPath.length > 0) {
        this._mountGeneration++;

        // Build the full Gun chain for the entire dot path
        // e.g. demo111.weather.nyc.panel.data.temp
        //   -> gun.get("demo111").get("scene").get("weather/nyc/panel/data/temp")
        // But also try the more natural interpretation:
        //   -> gun.get("demo111").get("scene").get("weather/nyc/panel").get("data").get("temp")
        const fullScenePath = descriptor.scenePath +
          "/" + descriptor.fieldPath.join("/");

        const peer = runtime.getPeer(descriptor.peerUrl);

        // Subscribe to the field by walking the Gun chain
        // node.get("data").get("temp") for fieldPath = ["data", "temp"]
        this._remoteUnsubs.push(
          peer.subscribe(
            `field|${descriptor.peerUrl}|${descriptor.root}|${descriptor.scenePath}|${descriptor.fieldPath.join(".")}`,
            (gun) => {
              let chain = gun.get(descriptor.root).get("scene").get(descriptor.scenePath);
              for (const seg of descriptor.fieldPath) {
                chain = chain.get(seg);
              }
              return chain;
            },
            (raw) => {
              // Raw value — could be a primitive or an object
              if (raw && typeof raw === "object") return clean(raw);
              return raw;
            },
            (value) => {
              this.value = value;
              this.renderValue(value);
              this.dispatchEvent(new CustomEvent("hyper-update", {
                detail: { value, fieldPath: descriptor.fieldPath },
                bubbles: true,
                composed: true,
              }));
            }
          )
        );

        return;
      }

      // Bump mount generation to allow node JS to rebind
      this._mountGeneration++;

      this.literalCtx = {};
      this.boundCtx = Object.create(null);
      this.bindingTargets = Object.create(null);

      for (const [key, val] of descriptor.searchParams.entries()) {
        const raw = String(val || "");
        if (raw.startsWith("$")) {
          const bindingInfo = parseBindingRef(raw, descriptor.href, descriptor.peerUrl);
          if (bindingInfo) this.bindingTargets[key] = bindingInfo;
        } else {
          this.literalCtx[key] = parseMaybeJSON(raw);
        }
      }

      this.initBrowserEnvelope();
      this.mirrorRemoteToBrowser(descriptor);
      this.subscribeBindings();

      this.applyPublicNow(this.literalCtx || {});
      this.persistPublic();
      this.requestRender();
    }

    // ─── Contract declaration API ─────────────────────────────────────

    defineSchema(schema) {
      const nextSchema = deepMerge(this.schema, normalizeSchema(schema));
      const nextSig = stableStringify(nextSchema);
      if (nextSig === this.schemaSignature) return this.schema;

      this.applyContractNow({ schema: nextSchema });
      this.persistContract();

      this.applyLocalNow(this.stateEnvelope.local || {});
      this.persistLocal();

      this.requestRender();
      return this.schema;
    }

    defineManifest(manifest) {
      this.applyContractNow({ manifest: clean(manifest || {}) || {} });
      this.persistContract();
      this.requestRender();
      return this.contract.manifest;
    }

    defineLinks(links) {
      this.applyContractNow({ links: clean(links || {}) || {} });
      this.persistContract();
      this.requestRender();
      return this.contract.links;
    }

    defineActions(actions) {
      this.applyContractNow({ actions: clean(actions || {}) || {} });
      this.persistContract();
      this.requestRender();
      return this.contract.actions;
    }

    defineEvents(events) {
      this.applyContractNow({ events: clean(events || {}) || {} });
      this.persistContract();
      this.requestRender();
      return this.contract.events;
    }

    registerAction(name, fn, meta) {
      if (typeof name !== "string" || !name) return;
      if (typeof fn === "function") this._actionHandlers[name] = fn;
      if (meta !== undefined) {
        this.defineActions({ [name]: clean(meta) });
      }
    }

    // ── Event emission with contract enforcement ──
    emit(name, detail) {
      if (isObject(this._events) && Object.keys(this._events).length > 0) {
        if (!this._events[name]) {
          warn(this, "event", `emitting undeclared event "${name}" — declare it with defineEvents()`);
        }
      }
      this.dispatchEvent(new CustomEvent(name, {
        detail: clean(detail),
        bubbles: true,
        composed: true,
      }));
    }

    call(name, payload) {
      const fn = this._actionHandlers[name];
      if (typeof fn !== "function") {
        throw new Error(`No action registered for "${name}"`);
      }
      return fn(payload);
    }

    follow(rel) {
      return this.links()[rel];
    }

    state() {
      return {
        public: this.publicContext,
        secure: this.secureContext,
        local: this.localContext,
      };
    }

    links() {
      const src = this._descriptor ? this._descriptor.href : null;
      return deepMerge({ self: src }, this._links || {});
    }

    actions() {
      return { ...(this._actions || {}) };
    }

    events() {
      return { ...(this._events || {}) };
    }

    describe() {
      return {
        manifest: this._manifest || {},
        schema: this.schema,
        state: this.state(),
        links: this.links(),
        actions: this.actions(),
        events: this.events(),
        trust: this.getTrustMode(),
        src: this._descriptor ? this._descriptor.href : null,
        path: this._descriptor ? this._descriptor.normalized : null,
        meta: this.stateEnvelope.meta || {},
      };
    }

    getPublic() { return this.publicContext; }
    getSecure() { return this.secureContext; }
    getLocal() { return this.localContext; }
    getSchema() { return this.schema; }

    setLocal(next) {
      this.applyLocalNow(next);
      this.persistLocal();
      this.requestRender();
      return this.localContext;
    }

    mergeLocal(patch) {
      this.applyLocalNow(deepMerge(this.localContext || {}, patch || {}));
      this.persistLocal();
      this.requestRender();
      return this.localContext;
    }

    setSecure(next) {
      if (this.nodeId) {
        return runtime.secure.setForNode(this.nodeId, next);
      }
      return runtime.secure.setGlobal(next);
    }

    mergeSecure(patch) {
      if (this.nodeId) {
        return runtime.secure.mergeForNode(this.nodeId, patch);
      }
      return runtime.secure.mergeGlobal(patch);
    }

    requestRender() {
      if (!this.__viewportActive) {
        this.__pendingOffscreen = true;
        return;
      }

      if (this._renderQueued) return;
      this._renderQueued = true;

      requestAnimationFrame(() => {
        this._renderQueued = false;
        this.render();
      });
    }

    applyTemplate(html) {
      const compiled = runtime.templates.clone(html);
      this.container.replaceChildren(compiled.fragment);
      this._bindingRefs = compiled.bindingRefs;
      this._lastHtml = html;

      // ── Rebinding safety ──
      // When HTML changes structurally, invalidate the JS signature
      // so runNodeScript will re-execute, and bump the generation
      // so the __bound guard (keyed on generation) allows re-binding.
      this._lastJsSignature = null;
      this._mountGeneration++;
    }

    fastBind(ctx) {
      for (let i = 0; i < this._bindingRefs.length; i++) {
        const b = this._bindingRefs[i];
        if (!b || !b.el) continue;

        if (b.textPath) {
          const val = dig(ctx, b.textPath);
          if (val !== undefined) {
            const str = String(val);
            if (b.el.textContent !== str) b.el.textContent = str;
          }
        }

        if (b.htmlPath) {
          const val = dig(ctx, b.htmlPath);
          if (val !== undefined) {
            const str = String(val);
            if (b.el.innerHTML !== str) b.el.innerHTML = str;
          }
        }

        if (b.stylePaths) {
          for (let j = 0; j < b.stylePaths.length; j++) {
            const sp = b.stylePaths[j];
            const val = dig(ctx, sp.path);
            if (val !== undefined) {
              const str = String(val);
              if (b.el.style.getPropertyValue(sp.prop) !== str) {
                b.el.style.setProperty(sp.prop, str);
              }
            }
          }
        }
      }
    }

    buildContexts() {
      const publicDefaults = deriveDefaults(this.schema.public) || {};
      const secureDefaults = deriveDefaults(this.schema.secure) || {};
      const localDefaults = deriveDefaults(this.schema.local) || {};

      const publicCtx = deepMerge(publicDefaults, this.stateEnvelope.public || {});

      // ── State boundary: secure reads from per-node store ──
      const secureCtx = deepMerge(
        secureDefaults,
        this.nodeId ? runtime.secure.getForNode(this.nodeId) : runtime.secure.getGlobal()
      );

      const localCtx = deepMerge(localDefaults, this.stateEnvelope.local || {});

      const bindCtx = Object.assign({}, publicCtx, {
        public: publicCtx,
        secure: secureCtx,
        local: localCtx,
        schema: this.schema,
      });

      this.publicContext = publicCtx;
      this.secureContext = secureCtx;
      this.localContext = localCtx;
      this.bindingContext = bindCtx;

      return { publicCtx, secureCtx, localCtx, bindCtx };
    }

    buildHyperContext(publicCtx, secureCtx, localCtx, bindCtx) {
      const trustMode = this.getTrustMode();
      const params = trustMode === "trusted" ? bindCtx : publicCtx;

      const hyperContext = {
        mode: trustMode,
        trust: trustMode,
        source: this._descriptor ? this._descriptor.href : null,
        element: this,
        shadowRoot: this.shadowRoot,
        params,
        public: publicCtx,
        local: trustMode === "trusted" ? localCtx : undefined,
        secure: trustMode === "trusted" ? secureCtx : undefined,
        schema: this.schema,

        defineSchema: (schema) => this.defineSchema(schema),
        defineManifest: (manifest) => this.defineManifest(manifest),
        defineLinks: (links) => this.defineLinks(links),
        defineActions: (actions) => this.defineActions(actions),
        defineEvents: (events) => this.defineEvents(events),
        registerAction: (name, fn, meta) => this.registerAction(name, fn, meta),
        emit: (name, detail) => this.emit(name, detail),

        describe: () => this.describe(),
        state: () => this.state(),
        links: () => this.links(),
        actions: () => this.actions(),
        events: () => this.events(),
        follow: (rel) => this.follow(rel),
        call: (name, payload) => this.call(name, payload),

        getPublic: () => this.getPublic(),
        getSchema: () => this.getSchema(),

        getLocal: trustMode === "trusted" ? () => this.getLocal() : undefined,
        setLocal: trustMode === "trusted" ? (next) => this.setLocal(next) : undefined,
        mergeLocal: trustMode === "trusted" ? (patch) => this.mergeLocal(patch) : undefined,

        getSecure: trustMode === "trusted" ? () => this.getSecure() : undefined,
        setSecure: trustMode === "trusted" ? (next) => this.setSecure(next) : undefined,
        mergeSecure: trustMode === "trusted" ? (patch) => this.mergeSecure(patch) : undefined,
      };

      window.hyperContext = hyperContext;
      window.hyper_context = hyperContext;

      return hyperContext;
    }

    runNodeScript(publicCtx, secureCtx, localCtx, bindCtx) {
      const js = this.contract && this.contract.js != null
        ? String(this.contract.js)
        : "";

      if (!js) return;

      // ── Rebinding safety ──
      // Include mount generation in signature so that structural HTML
      // changes allow JS to re-execute and rebind to new DOM elements.
      const signature = `${this._mountGeneration}::${this._lastHtml || ""}::${js}::${this.getTrustMode()}::${this.schemaSignature}`;
      if (signature === this._lastJsSignature) return;

      this._lastJsSignature = signature;

      try {
        const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
        const hyperContext = this.buildHyperContext(publicCtx, secureCtx, localCtx, bindCtx);

        // ── Rebinding safety ──
        // Expose the current mount generation so node JS can key its
        // __bound guard on it instead of a simple boolean.
        // Pattern: if (hyperElement.__boundGen === hyperElement.__mountGen) return;
        //          hyperElement.__boundGen = hyperElement.__mountGen;
        this.__mountGen = this._mountGeneration;

        new AsyncFunction("document", "root", "hyperContext", "hyperElement", js).call(
          this,
          this.shadowRoot,
          this.shadowRoot,
          hyperContext,
          this
        );
      } catch (err) {
        console.error("[hyper-node] node js error", err);
      }
    }

    renderValue(value) {
      if (value == null) {
        if (this.container.textContent !== "") this.container.textContent = "";
        return;
      }

      if (
        typeof value === "string" ||
        typeof value === "number" ||
        typeof value === "boolean"
      ) {
        const str = String(value);
        if (
          this.container.childNodes.length !== 1 ||
          this.container.firstChild.nodeType !== Node.TEXT_NODE ||
          this.container.textContent !== str
        ) {
          this.container.textContent = str;
        }
        return;
      }

      const json = JSON.stringify(value, null, 2);
      const html = `<pre>${escapeHtml(json)}</pre>`;
      if (this.container.innerHTML !== html) {
        this.container.innerHTML = html;
      }
    }

    dispatchUpdate() {
      const detail = {
        value: this.value,
        public: this.publicContext,
        secure: this.secureContext,
        local: this.localContext,
        schema: this.schema,
        src: this._descriptor ? this._descriptor.href : null,
        path: this._descriptor ? this._descriptor.normalized : null,
        trust: this.getTrustMode(),
        describe: this.describe(),
      };
      this.dispatchEvent(new CustomEvent("hyper-update", {
        detail,
        bubbles: true,
        composed: true,
      }));
    }

    render() {
      const { publicCtx, secureCtx, localCtx, bindCtx } = this.buildContexts();

      const html = this.contract && this.contract.html != null
        ? String(this.contract.html)
        : "";

      const css = this.contract && this.contract.css != null
        ? String(this.contract.css)
        : "";

      if (html) {
        if (html !== this._lastHtml) {
          this.applyTemplate(html);
        }

        if (css !== this._lastCss) {
          this.styleEl.textContent = `
            :host {
              display: block;
              box-sizing: border-box;
              min-width: 0;
              min-height: 0;
              contain: layout style paint;
              content-visibility: auto;
              contain-intrinsic-size: auto ${this.__lastMeasuredHeight || DEFAULT_INTRINSIC_SIZE}px;
            }
            #container {
              box-sizing: border-box;
              width: 100%;
              min-width: 0;
              min-height: 0;
              font: inherit;
            }
            pre {
              margin: 0;
              white-space: pre-wrap;
              word-break: break-word;
            }
            ${css}
          `;
          this._lastCss = css;
        }

        this.fastBind(bindCtx);
        this.runNodeScript(publicCtx, secureCtx, localCtx, bindCtx);
        this.value = bindCtx;
        this.dispatchUpdate();
        return;
      }

      this.value = bindCtx;
      this.renderValue(bindCtx);
      this.dispatchUpdate();
    }
  }

  customElements.define("hyper-node", HyperNode);
})();