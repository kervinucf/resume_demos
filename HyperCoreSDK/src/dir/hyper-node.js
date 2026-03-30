(() => {
  if (window.customElements.get("hyper-node")) return;

  const FIELD_NAMES = new Set(["data", "html", "css", "js", "fixed", "layer", "schema", "trust"]);
  const META_KEYS = new Set(["_", "#", ">"]);
  const IDLE_TTL_MS = 30000;
  const OVERSCAN_PX = 2200;
  const DEFAULT_INTRINSIC_SIZE = 96;

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

  function dig(value, path) {
    let cur = value;
    for (let i = 0; i < path.length; i++) {
      if (cur == null) return undefined;
      cur = cur[path[i]];
    }
    return cur;
  }

  function parseMaybeJSON(value) {
    if (value === "") return "";
    try {
      return JSON.parse(value);
    } catch (_) {
      return value;
    }
  }

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
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

  function stableStringify(value) {
    try {
      return JSON.stringify(value == null ? null : clean(value));
    } catch (_) {
      return String(value);
    }
  }

  function isSchemaLeafSpec(value) {
    return isObject(value) && (
      Object.prototype.hasOwnProperty.call(value, "default") ||
      Object.prototype.hasOwnProperty.call(value, "type") ||
      Object.prototype.hasOwnProperty.call(value, "required") ||
      Object.prototype.hasOwnProperty.call(value, "enum") ||
      Object.prototype.hasOwnProperty.call(value, "const")
    );
  }

  function deriveDefaults(spec) {
    if (spec === undefined) return undefined;

    if (Array.isArray(spec)) {
      return clean(spec);
    }

    if (!isObject(spec)) {
      return undefined;
    }

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
      Object.prototype.hasOwnProperty.call(schema, "public") ||
      Object.prototype.hasOwnProperty.call(schema, "secure") ||
      Object.prototype.hasOwnProperty.call(schema, "local");

    if (hasNamespace) {
      return {
        public: isObject(schema.public) ? schema.public : {},
        secure: isObject(schema.secure) ? schema.secure : {},
        local: isObject(schema.local) ? schema.local : {},
      };
    }

    return {
      public: {},
      secure: {},
      local: isObject(schema) ? schema : {},
    };
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

    if (/^https?:\/\//i.test(v)) {
      return parseSource(v, baseHref);
    }

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
          idleTimer: null,
        };
        this.entries.set(key, entry);
      }

      entry.watchers.add(cb);

      if (entry.idleTimer) {
        clearTimeout(entry.idleTimer);
        entry.idleTimer = null;
      }

      if (!entry.chain) {
        entry.chain = chainFactory(this.gun);
        entry.chain.on((raw) => {
          const next = projector ? projector(raw) : raw;
          entry.value = next;
          entry.hasValue = true;

          for (const fn of entry.watchers) {
            try {
              fn(next);
            } catch (err) {
              console.error("[hyper-node] watcher error", err);
            }
          }
        });
      }

      if (entry.hasValue) cb(entry.value);

      return () => {
        entry.watchers.delete(cb);

        if (entry.watchers.size === 0) {
          entry.idleTimer = setTimeout(() => {
            if (entry.watchers.size > 0) return;

            if (entry.chain && typeof entry.chain.off === "function") {
              try {
                entry.chain.off();
              } catch (_) {}
            }

            entry.chain = null;
            entry.idleTimer = null;
          }, IDLE_TTL_MS);
        }
      };
    }
  }

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
              ? styleSpec
                  .split(";")
                  .map((pair) => {
                    const parts = pair.split(":");
                    if (parts.length !== 2) return null;
                    return {
                      prop: parts[0].trim(),
                      path: parts[1].trim().split("."),
                    };
                  })
                  .filter(Boolean)
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

      const bindingRefs = compiled.bindings.map((def) => ({
        el: resolveNode(fragment, def.nodePath),
        textPath: def.textPath,
        htmlPath: def.htmlPath,
        stylePaths: def.stylePaths,
      }));

      return { fragment, bindingRefs };
    }
  }

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

    onScroll() {
      this.schedule();
    }

    onResize() {
      this.schedule();
    }

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

  const initialSecureValue = clean(window.$secureContext || {}) || {};

  const runtime = window.$hyperRuntime || {
    peers: new Map(),
    templates: new TemplateRegistry(),
    viewport: new ViewportManager(),
    resizeObserver: null,

    secure: {
      value: initialSecureValue,
      listeners: new Set(),

      get() {
        return this.value || {};
      },

      set(next) {
        const cleaned = clean(next || {}) || {};
        const prevSig = stableStringify(this.value || {});
        const nextSig = stableStringify(cleaned);
        if (prevSig === nextSig) return this.value;

        this.value = cleaned;

        for (const fn of this.listeners) {
          try {
            fn(this.value);
          } catch (err) {
            console.error("[hyper-node] secure listener error", err);
          }
        }

        window.dispatchEvent(
          new CustomEvent("hyper-secure-context-change", {
            detail: { value: this.value },
          })
        );

        return this.value;
      },

      merge(patch) {
        return this.set(deepMerge(this.get(), patch || {}));
      },

      subscribe(fn) {
        this.listeners.add(fn);
        return () => this.listeners.delete(fn);
      },
    },

    getPeer(peerUrl) {
      let peer = this.peers.get(peerUrl);
      if (!peer) {
        peer = new PeerRuntime(peerUrl);
        this.peers.set(peerUrl, peer);
      }
      return peer;
    },

    prewarm(peerUrl) {
      return this.getPeer(peerUrl);
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
      try {
        this.resizeObserver.unobserve(node);
      } catch (_) {}
    },

    subscribeNode(info, cb) {
      const peer = this.getPeer(info.peerUrl);
      const key = `node|${info.peerUrl}|${info.root}|${info.scenePath}`;

      return peer.subscribe(
        key,
        (gun) => gun.get(info.root).get("scene").get(info.scenePath),
        (raw) => clean(raw) || {},
        cb
      );
    },

    subscribeData(info, cb) {
      const peer = this.getPeer(info.peerUrl);
      const key = `data|${info.peerUrl}|${info.root}|${info.scenePath}`;

      return peer.subscribe(
        key,
        (gun) => gun.get(info.root).get("scene").get(info.scenePath).get("data"),
        (raw) => clean(raw) || {},
        cb
      );
    },
  };

  window.$hyperRuntime = runtime;

  try {
    Object.defineProperty(window, "$secureContext", {
      configurable: true,
      enumerable: true,
      get() {
        return runtime.secure.get();
      },
      set(v) {
        runtime.secure.set(v);
      },
    });
  } catch (_) {
    window.$secureContext = runtime.secure.get();
  }

  window.$setSecureContext = function (next) {
    return runtime.secure.set(next);
  };

  window.$mergeSecureContext = function (patch) {
    return runtime.secure.merge(patch);
  };

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

      this._descriptor = null;
      this._unsubs = [];
      this._offSecure = null;
      this._renderQueued = false;
      this._bindingRefs = [];
      this._lastHtml = null;
      this._lastCss = null;
      this._lastJsSignature = null;

      this.currentNode = {};
      this.currentData = {};
      this.literalCtx = {};
      this.boundCtx = Object.create(null);
      this.bindingTargets = Object.create(null);
      this.value = undefined;

      this.schema = { public: {}, secure: {}, local: {} };
      this.schemaSignature = stableStringify(this.schema);
      this.localState = {};
      this.publicContext = {};
      this.secureContext = {};
      this.localContext = {};
      this.bindingContext = {};
    }

    connectedCallback() {
      runtime.viewport.register(this);
      runtime.observeSize(this);

      this._offSecure = runtime.secure.subscribe(() => {
        this.requestRender();
      });

      this.mount();
    }

    disconnectedCallback() {
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
      for (const off of this._unsubs) {
        try {
          off();
        } catch (_) {}
      }
      this._unsubs = [];
      this._renderQueued = false;
    }

    getTrustMode() {
      const attrTrust = (this.getAttribute("trust") || "").trim();
      const nodeTrust = this.currentNode && this.currentNode.trust != null
        ? String(this.currentNode.trust).trim()
        : "";
      const literalTrust = this.literalCtx && this.literalCtx._trust != null
        ? String(this.literalCtx._trust).trim()
        : "";

      return attrTrust || nodeTrust || literalTrust || "public";
    }

    mount() {
      this.teardown();

      const rawSrc = this.getAttribute("src") || "";
      if (!rawSrc) return;

      const descriptor = parseSource(rawSrc, location.href);
      if (!descriptor) return;

      this._descriptor = descriptor;
      this.currentNode = {};
      this.currentData = {};
      this.literalCtx = {};
      this.boundCtx = Object.create(null);
      this.bindingTargets = Object.create(null);

      runtime.prewarm(descriptor.peerUrl);

      for (const [key, val] of descriptor.searchParams.entries()) {
        const raw = String(val || "");
        if (raw.startsWith("$")) {
          const bindingInfo = parseBindingRef(raw, descriptor.href, descriptor.peerUrl);
          if (bindingInfo) {
            this.bindingTargets[key] = bindingInfo;
            runtime.prewarm(bindingInfo.peerUrl);
          }
        } else {
          this.literalCtx[key] = parseMaybeJSON(raw);
        }
      }

      const rerender = () => this.requestRender();

      if (!descriptor.fieldPath.length) {
        this._unsubs.push(
          runtime.subscribeNode(descriptor, (value) => {
            this.currentNode = isObject(value) ? value : {};
            rerender();
          })
        );

        this._unsubs.push(
          runtime.subscribeData(descriptor, (value) => {
            this.currentData = isObject(value) ? value : {};
            rerender();
          })
        );
      } else if (descriptor.fieldPath[0] === "data") {
        this._unsubs.push(
          runtime.subscribeData(descriptor, (value) => {
            this.currentData = isObject(value) ? value : {};
            rerender();
          })
        );
      } else {
        this._unsubs.push(
          runtime.subscribeNode(descriptor, (value) => {
            this.currentNode = isObject(value) ? value : {};
            rerender();
          })
        );
      }

      for (const [name, info] of Object.entries(this.bindingTargets)) {
        if (info.fieldPath[0] === "data") {
          this._unsubs.push(
            runtime.subscribeData(info, (value) => {
              this.boundCtx[name] =
                info.fieldPath.length > 1
                  ? dig(value, info.fieldPath.slice(1))
                  : value;
              rerender();
            })
          );
        } else {
          this._unsubs.push(
            runtime.subscribeNode(info, (value) => {
              this.boundCtx[name] =
                info.fieldPath.length > 0
                  ? dig(value, info.fieldPath)
                  : value;
              rerender();
            })
          );
        }
      }

      this.requestRender();
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

    mergeSchema(schema) {
      const next = deepMerge(this.schema, normalizeSchema(schema));
      const nextSig = stableStringify(next);
      if (nextSig === this.schemaSignature) return this.schema;

      this.schema = next;
      this.schemaSignature = nextSig;

      const secureDefaults = deriveDefaults(this.schema.secure) || {};
      const nextSecure = deepMerge(secureDefaults, runtime.secure.get() || {});
      if (stableStringify(nextSecure) !== stableStringify(runtime.secure.get() || {})) {
        runtime.secure.set(nextSecure);
      }

      const localDefaults = deriveDefaults(this.schema.local) || {};
      const nextLocal = deepMerge(localDefaults, this.localState || {});
      if (stableStringify(nextLocal) !== stableStringify(this.localState || {})) {
        this.localState = nextLocal;
      }

      this.requestRender();
      return this.schema;
    }

    defineSchema(schema) {
      return this.mergeSchema(schema);
    }

    getSchema() {
      return this.schema;
    }

    getPublic() {
      return this.publicContext;
    }

    getSecure() {
      return this.secureContext;
    }

    getLocal() {
      return this.localContext;
    }

    getContext() {
      return this.bindingContext;
    }

    setLocal(next) {
      const cleaned = clean(next || {}) || {};
      if (stableStringify(cleaned) === stableStringify(this.localState || {})) {
        return this.localState;
      }
      this.localState = cleaned;
      this.requestRender();
      return this.localState;
    }

    mergeLocal(patch) {
      return this.setLocal(deepMerge(this.localState || {}, patch || {}));
    }

    setSecure(next) {
      return runtime.secure.set(next);
    }

    mergeSecure(patch) {
      return runtime.secure.merge(patch);
    }

    buildRawPublicContext() {
      const out = {};

      if (isObject(this.currentNode.data)) Object.assign(out, this.currentNode.data);
      if (isObject(this.currentData)) Object.assign(out, this.currentData);

      Object.assign(out, this.literalCtx);
      Object.assign(out, this.boundCtx);

      return out;
    }

    buildContexts() {
      const publicDefaults = deriveDefaults(this.schema.public) || {};
      const secureDefaults = deriveDefaults(this.schema.secure) || {};
      const localDefaults = deriveDefaults(this.schema.local) || {};

      const publicCtx = deepMerge(publicDefaults, this.buildRawPublicContext());
      const secureCtx = deepMerge(secureDefaults, runtime.secure.get() || {});
      const localCtx = deepMerge(localDefaults, this.localState || {});

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

    applyTemplate(html) {
      const compiled = runtime.templates.clone(html);
      this.container.replaceChildren(compiled.fragment);
      this._bindingRefs = compiled.bindingRefs;
      this._lastHtml = html;
      this._lastJsSignature = null;
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
        getSchema: () => this.getSchema(),
        getPublic: () => this.getPublic(),

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
      const js = this.currentNode && this.currentNode.js != null
        ? String(this.currentNode.js)
        : "";

      if (!js) return;

      const signature = `${this._lastHtml || ""}::${js}::${this.getTrustMode()}::${this.schemaSignature}`;
      if (signature === this._lastJsSignature) return;

      this._lastJsSignature = signature;

      try {
        const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
        const hyperContext = this.buildHyperContext(publicCtx, secureCtx, localCtx, bindCtx);

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
      this.dispatchEvent(
        new CustomEvent("hyper-update", {
          detail: {
            value: this.value,
            public: this.publicContext,
            secure: this.secureContext,
            local: this.localContext,
            schema: this.schema,
            src: this._descriptor ? this._descriptor.href : null,
            path: this._descriptor ? this._descriptor.normalized : null,
            trust: this.getTrustMode(),
          },
          bubbles: true,
          composed: true,
        })
      );
    }

    render() {
      if (!this._descriptor) return;

      if (this.currentNode && isObject(this.currentNode.schema)) {
        this.mergeSchema(this.currentNode.schema);
      }

      const fieldPath = this._descriptor.fieldPath;
      const { publicCtx, secureCtx, localCtx, bindCtx } = this.buildContexts();

      if (fieldPath.length) {
        const value =
          fieldPath[0] === "data"
            ? dig(this.currentData, fieldPath.slice(1))
            : dig(this.currentNode, fieldPath);

        this.value = value;
        this.renderValue(value);
        this.dispatchUpdate();
        return;
      }

      if (this.currentNode && this.currentNode.html != null) {
        const html = String(this.currentNode.html || "");
        const css = this.currentNode && this.currentNode.css != null
          ? String(this.currentNode.css)
          : "";

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

      if (Object.keys(bindCtx).length > 0) {
        this.value = bindCtx;
        this.renderValue(bindCtx);
        this.dispatchUpdate();
        return;
      }

      this.value = this.currentNode;
      this.renderValue(this.currentNode);
      this.dispatchUpdate();
    }
  }

  customElements.define("hyper-node", HyperNode);
})();