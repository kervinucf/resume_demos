// --- GLOBAL SCHEDULER ---
// This acts as a traffic controller to ensure we never block the main thread.
// It processes exactly ONE component initialization per frame (120hz friendly).
if (!window.$hyperScheduler) {
  window.$hyperScheduler = {
    queue: [],
    running: false,
    push(task) {
      this.queue.push(task);
      if (!this.running) {
        this.running = true;
        this.runNext();
      }
    },
    runNext() {
      if (this.queue.length === 0) {
        this.running = false;
        return;
      }

      // Execute exactly ONE task, then yield back to the browser
      const task = this.queue.shift();
      task();

      requestAnimationFrame(() => this.runNext());
    }
  };
}

class HyperNode extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.container = document.createElement('div');
    this.container.style.cssText = `
      width: 100%;
      height: 100%;
      min-height: 20px;
      box-sizing: border-box;
      font-family: inherit;
    `;
    this.shadowRoot.appendChild(this.container);

    this._lastHtml = null;
    this._renderQueued = false;
    this._compiledBindings = [];
    this.value = undefined;
  }

  connectedCallback() {
    // 1. Massive 1500px root margin means it starts booting 2 screens ahead of your scroll
    this.observer = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          // 2. Instead of booting immediately and causing jank, push to the time-slicer
          window.$hyperScheduler.push(() => this.initAndMount());
          this.observer.unobserve(this);
        }
      });
    }, { rootMargin: '1500px' });

    this.observer.observe(this);
  }

  disconnectedCallback() {
    if (this.observer) this.observer.disconnect();
  }

  initAndMount() {
    if (!window.$gun) {
      const srcUrl = new URL(this.getAttribute('src') || location.href);
      const backendUrl = `${srcUrl.protocol}//${srcUrl.host}/gun`;
      window.$gun = Gun({ peers: [backendUrl] });
    }

    this.gun = window.$gun;
    this.mount();
  }

  compileBindings() {
    this._compiledBindings = [];
    const nodes = this.container.querySelectorAll('[data-bind-text],[data-bind-html],[data-bind-style]');

    for (let i = 0; i < nodes.length; i++) {
      const el = nodes[i];
      this._compiledBindings.push({
        el: el,
        textPath: el.dataset.bindText ? el.dataset.bindText.split('.') : null,
        htmlPath: el.dataset.bindHtml ? el.dataset.bindHtml.split('.') : null,
        stylePaths: el.dataset.bindStyle ? el.dataset.bindStyle.split(';').map(s => {
          const parts = s.split(':');
          return parts.length === 2 ? { prop: parts[0].trim(), path: parts[1].trim().split('.') } : null;
        }).filter(Boolean) : null
      });
    }
  }

  mount() {
    const rawSrc = this.getAttribute('src') || '';
    if (!rawSrc) return;

    const urlObj = new URL(rawSrc, location.origin);
    let path = urlObj.pathname.replace(/\/+$/, '').slice(1);
    if (path.endsWith('.stream')) path = path.slice(0, -7);

    const parts = path.split('.').filter(Boolean);
    if (!parts.length) return;

    const root = parts[0];
    const rest = parts.slice(1);
    const FIELD_NAMES = new Set(['data', 'html', 'css', 'js', 'fixed', 'layer']);

    let fieldStart = -1;
    for (let i = 0; i < rest.length; i++) {
      if (FIELD_NAMES.has(rest[i])) { fieldStart = i; break; }
    }

    const nodeSegs = fieldStart === -1 ? rest : rest.slice(0, fieldStart);
    const scenePath = nodeSegs.length ? nodeSegs.join('/') : '__root__';
    const fieldPath = fieldStart === -1 ? [] : rest.slice(fieldStart);

    const bindingTargets = {};
    const literalCtx = {};

    for (const [key, val] of urlObj.searchParams.entries()) {
      if (val.startsWith('$')) {
        let v = val.slice(1);
        if (v.endsWith('.stream')) v = v.slice(0, -7);
        const bParts = v.split('.');
        let bFieldStart = -1;
        for (let i = 0; i < bParts.slice(1).length; i++) {
          if (FIELD_NAMES.has(bParts.slice(1)[i])) { bFieldStart = i; break; }
        }
        bindingTargets[key] = {
          root: bParts[0],
          scenePath: bFieldStart === -1 ? bParts.slice(1).join('/') : bParts.slice(1, bFieldStart + 1).join('/'),
          fieldPath: bFieldStart === -1 ? [] : bParts.slice(1).slice(bFieldStart)
        };
      } else {
        literalCtx[key] = val;
      }
    }

    let currentNode = {};
    let currentData = {};
    let boundCtx = {};

    const dig = (v, p) => {
      let cur = v;
      for (let i = 0; i < p.length; i++) {
        if (cur == null) return undefined;
        cur = cur[p[i]];
      }
      return cur;
    };

    const clean = (v) => {
      if (!v || typeof v !== 'object') return v;
      const out = {};
      for (const k in v) {
        if (k !== '_' && k !== '#' && k !== '>') {
          if (v[k] !== undefined && v[k] !== null) out[k] = v[k];
        }
      }
      return out;
    };

    const renderValue = (v) => {
      if (v == null) {
        if (this.container.textContent !== '') this.container.textContent = '';
        return;
      }
      if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
        const s = String(v);
        if (this.container.textContent !== s) this.container.textContent = s;
        return;
      }
      this.container.innerHTML = '<pre style="margin:0;padding:0;white-space:pre-wrap;word-break:break-word;">' + JSON.stringify(v, null, 2) + '</pre>';
    };

    const buildContext = () => Object.assign({},
      currentNode && currentNode.data ? currentNode.data : {},
      currentData, literalCtx, boundCtx
    );

    const fastBindData = (ctx) => {
      for (let i = 0; i < this._compiledBindings.length; i++) {
        const b = this._compiledBindings[i];

        if (b.textPath) {
          const val = dig(ctx, b.textPath);
          if (val !== undefined) {
            const strVal = String(val);
            if (b.el.textContent !== strVal) b.el.textContent = strVal;
          }
        }

        if (b.htmlPath) {
          const val = dig(ctx, b.htmlPath);
          if (val !== undefined) {
            const strVal = String(val);
            if (b.el.innerHTML !== strVal) b.el.innerHTML = strVal;
          }
        }

        if (b.stylePaths) {
          for (let j = 0; j < b.stylePaths.length; j++) {
            const sp = b.stylePaths[j];
            const val = dig(ctx, sp.path);
            if (val !== undefined && b.el.style[sp.prop] !== val) {
              b.el.style[sp.prop] = val;
            }
          }
        }
      }
    };

    const executeRender = () => {
      if (fieldPath.length) {
        const targetVal = fieldPath[0] === 'data' ? dig(currentData, fieldPath.slice(1)) : dig(currentNode, fieldPath);
        this.value = targetVal;
        renderValue(targetVal);
        return;
      }

      if (currentNode && currentNode.html != null) {
        const ctx = buildContext();
        this.value = ctx;

        if (this._lastHtml !== currentNode.html) {
          this.container.innerHTML = String(currentNode.html);
          this._lastHtml = currentNode.html;
          this.compileBindings();

          if (currentNode.js) {
            try {
              const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
              window.hyper_context = { params: ctx, literal: literalCtx, bindings: bindingTargets };
              new AsyncFunction(String(currentNode.js))();
            } catch (e) {}
          }
        }

        fastBindData(ctx);
        return;
      }

      if (currentNode && currentNode.data !== undefined) {
        const ctx = buildContext();
        this.value = ctx;
        renderValue(ctx);
        return;
      }

      this.value = currentNode;
      renderValue(currentNode);
    };

    const triggerRender = () => {
      if (this._renderQueued) return;
      this._renderQueued = true;
      requestAnimationFrame(() => {
        this._renderQueued = false;
        executeRender();
        this.dispatchEvent(new CustomEvent('hyper-update', {
          detail: { value: this.value, path: path },
          bubbles: true
        }));
      });
    };

    this.gun.get(root).get('scene').get(scenePath).on(node => {
      currentNode = clean(node) || {};
      triggerRender();
    });

    this.gun.get(root).get('scene').get(scenePath).get('data').on(d => {
      currentData = clean(d) || {};
      triggerRender();
    });

    Object.keys(bindingTargets).forEach(name => {
      const info = bindingTargets[name];
      if (info.fieldPath[0] === 'data') {
        this.gun.get(info.root).get('scene').get(info.scenePath).get('data').on(d => {
          const out = info.fieldPath.length > 1 ? dig(clean(d)||{}, info.fieldPath.slice(1)) : clean(d);
          if (out !== undefined) { boundCtx[name] = out; triggerRender(); }
        });
      }
    });
  }
}

customElements.define('hyper-node', HyperNode);