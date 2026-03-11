# Scene System — Complete Reference for Generative Agents

You are building content for a **reactive display surface**. You write Python scripts that push HTML, CSS, JavaScript, and data to a browser via a real-time graph database. Whatever you write appears on screen instantly for all connected viewers.

This is not a web app framework. It is a programmable screen. You are the director. The screen does what you tell it.

---

## 1. HOW IT WORKS

There are three pieces. You only touch one of them.

**Relay** (`relay/server.js`) — A Node.js server running GunDB. It holds named **buckets** (think: channels). Each bucket has a **scene graph** — a flat key-value store of **fragments**. You never modify the relay.

**Shell** (`shell/index.html`) — A blank HTML page that connects to GunDB and renders whatever fragments exist in its bucket. When a fragment is created, it appears. When it's updated, it re-renders. When it's removed, it disappears. The shell has no opinions about what it displays. You never modify the shell.

**Your script** — A Python program that uses `SceneWriter` to write fragments to a bucket. This is the only thing you write. Your script is the broadcast.

### Fragment types

A fragment is a named slot in the scene graph. Each fragment can carry any combination of:

| Field  | What it does |
|--------|-------------|
| `html` | Injected into a `<div id="frag-{key}">` inside `<div id="scene">` |
| `css`  | Injected as a `<style id="css-{key}">` in `<head>` |
| `js`   | Executed as an async function body. Runs once on creation, again on update. Has access to `window`, `document`, and all globals. |
| `link` | JSON array of `{rel, href}` objects. Creates `<link>` or `<script>` tags in `<head>`. |

Every fragment is **independent**. They layer on top of each other in DOM order. You control z-index, positioning, and visibility through CSS and HTML.

### Loading external resources with `link`

The `link` field is how you pull in external files — stylesheets, JavaScript libraries, fonts, or any resource you'd put in a `<head>` tag. Each entry becomes a real DOM element. When the fragment is removed, its linked resources are removed too.

```python
# Load a stylesheet
s.put("theme", link=SceneWriter.links(
    ("stylesheet", "https://fonts.googleapis.com/css2?family=Orbitron&display=swap"),
))

# Load a JavaScript library
s.put("libs", link=SceneWriter.links(
    ("script", "https://cdnjs.cloudflare.com/ajax/libs/animejs/3.2.2/anime.min.js"),
))

# Load multiple resources in one fragment
s.put("deps", link=SceneWriter.links(
    ("stylesheet", "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"),
    ("script", "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"),
    ("script", "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"),
))

# Remove the fragment to unload all its linked resources
s.remove("deps")
```

The `SceneWriter.links()` helper builds the JSON. The `rel` field determines the tag type: `"stylesheet"` creates a `<link>`, `"script"` creates a `<script>`. Any valid `rel` value works (`"preconnect"`, `"icon"`, etc.).

You can also build the JSON manually:
```python
s.put("custom", link=json.dumps([
    {"rel": "stylesheet", "href": "https://example.com/style.css"},
    {"rel": "script", "href": "https://example.com/lib.js", "type": "module"},
]))
```

Resources are tagged with `data-owner="{fragment_key}"` in the DOM. When you call `s.remove("custom")` or `s.put("custom", link=new_links)`, the old tags are cleaned up first.

### Timing model

Your script runs sequentially. When you call `s.put(...)`, the fragment appears on screen within milliseconds (GunDB push). When you call `s.wait(5)`, your script sleeps for 5 seconds. The screen stays live — animations continue, clocks tick, viewers interact. When your script resumes and writes the next fragment, the screen updates again.

This means **your script IS the timeline**. The sequence of `put`, `remove`, and `wait` calls defines what happens and when. There is no separate scheduling system.

---

## 2. THE SDK

### SceneWriter

```python
from sdk.scene import SceneWriter

s = SceneWriter("http://localhost:8765", "my_bucket")

# Write a fragment (any combination of fields)
s.put("header", html="<h1>Hello</h1>")
s.put("styles", css="h1 { color: red; }")
s.put("logic", js="console.log('running');")
s.put("libs", link=SceneWriter.links(("script", "https://cdn.example.com/lib.js")))

# Remove a fragment (disappears from screen)
s.remove("header")

# Clear all fragments in the bucket
s.clear()

# Sleep (screen stays live, script pauses)
s.wait(5)
```

### Boot

```python
from sdk.boot import boot

boot(s, "MY NETWORK")  # Clears bucket, pushes CSS + globe + clock + logo + ticker
```

Boot pushes these fragments:
- `styles` — The full broadcast CSS from `sdk/broadcast.css` (or your custom CSS)
- `boot` — Globe.gl initialization + font links + HTMX
- `clock` — UTC clock updater
- `logo` — Corner network bug
- `holding` — Ambient standby screen
- `ticker` — Initial scrolling ticker

After boot, the viewer sees a spinning globe with network branding. You then overwrite or add fragments to build your broadcast.

### CSS control

```python
# Use default broadcast.css
boot(s, "NETWORK")

# Use a completely different stylesheet
boot(s, "NETWORK", css_path="/path/to/cyberpunk.css")

# Generate CSS programmatically
boot(s, "NETWORK", css_string="body { background: #000; } .chyron { font-family: 'Courier'; }")

# Push additional CSS at any time
s.put("extra_styles", css=".my-panel { background: navy; border: 1px solid gold; }")
```

### Timeline DSL

The timeline DSL is optional syntactic sugar. You can use it for complex sequences, or just call `s.put()` / `s.remove()` / `s.wait()` directly.

```python
from sdk.timeline import (
    GlobeState, compile,
    wait, pan, sweep, set_chyron, clear_chyron,
    set_ticker, show_bumper, set_sidebar, hide_sidebar,
    show, hide, refresh, clear_layer, init_layer, note,
)

globe = GlobeState()

timeline = [
    note("Starting broadcast"),
    show_bumper("weather"),                           # Full-screen transition
    set_chyron("TOKYO — Clear skies, 24°C"),          # Lower-third
    set_ticker("Tokyo 24°C ••• London 12°C ••• NYC 18°C"),  # Scrolling bottom bar
    sweep(35.68, 139.69, duration=4000, altitude=0.5, hold=8),  # Camera move + hold
    wait(5),
    clear_chyron(),
]

compile(s, timeline, globe)  # Executes each cue via SceneWriter
```

**Available cues:**

| Cue | Effect |
|-----|--------|
| `wait(seconds)` | Pause script execution |
| `pan(lat, lng, altitude, duration)` | Animate globe camera |
| `sweep(lat, lng, duration, altitude, hold)` | Pan + wait (most common camera move) |
| `set_chyron(text, source="", breaking=False)` | Show lower-third overlay |
| `clear_chyron()` | Remove lower-third |
| `set_ticker(text)` | Scrolling text at bottom |
| `show_bumper(segment)` | Full-screen segment transition (3.5s) |
| `set_sidebar(items)` | Right-side rundown panel. Items: `[{"title": "...", "subtitle": "..."}]` |
| `hide_sidebar()` | Remove sidebar |
| `show(layer, elements)` | Add elements to a globe layer |
| `hide(layer, ids)` | Remove elements from a globe layer |
| `refresh(*layers)` | Push accumulated layer changes to the globe |
| `clear_layer(layer)` | Empty a globe layer |
| `init_layer(layer, properties)` | Configure how a globe layer renders |
| `note(message)` | Log a message (no screen effect) |

---

## 3. THE DISPLAY SURFACE

### What's available in the browser

After boot, the browser environment has:

| Global | What it is |
|--------|-----------|
| `window.$root` | The `<div id="scene">` element — the root container |
| `window.$globe` | The Globe.gl instance (Three.js under the hood) |
| `window.$scene` | GunDB reference to the current bucket's scene graph |
| `window.$gun` | The GunDB instance |
| `window.$SEA` | GunDB's SEA encryption module |
| `window.$bucket` | Current bucket name (string) |
| `document` | Standard DOM — you have full access |

**Loaded libraries** (available after boot):
- **Globe.gl** — 3D globe with html, points, arcs, rings, hex, labels layers
- **Three.js** (r128, via Globe.gl) — Full 3D scene access via `window.$globe.scene()`
- **HTMX** (1.9.10) — Hypermedia-driven interactions via HTML attributes
- **GunDB + SEA** — Real-time sync + encryption

**Available via CDN** (load with `link` field):
- Any library on cdnjs, unpkg, or jsdelivr
- D3.js, Chart.js, Tone.js, TensorFlow.js, Babylon.js, A-Frame, PixiJS
- Anything you can load with a `<script>` tag

### Globe layers

The globe supports six data-driven layers. Each renders arrays of objects with lat/lng coordinates.

| Layer | Method | Element shape |
|-------|--------|--------------|
| `html` | `htmlElementsData` | Arbitrary HTML elements floating on the globe surface |
| `points` | `pointsData` | Colored dots. Fields: `lat`, `lng`, `color`, `altitude`, `radius` |
| `arcs` | `arcsData` | Curved lines between two points. Fields: `startLat`, `startLng`, `endLat`, `endLng`, `color` |
| `rings` | `ringsData` | Propagating ring pulses. Fields: `lat`, `lng`, `color`, `maxR`, `propagationSpeed`, `repeatPeriod` |
| `hex` | `hexBinPointsData` | Hexagonal bins that extrude based on data density. Fields: `lat`, `lng`, `weight` |
| `labels` | `labelsData` | Text labels on the surface. Fields: `lat`, `lng`, `text`, `color`, `size` |

**Workflow:** Call `init_layer` to set rendering properties (how elements look), then `show` to add data, then `refresh` to push updates to the globe.

```python
# Initialize the HTML layer to render arbitrary divs
init_layer("html", {
    "htmlElement": "d=>{const e=document.createElement('div');e.innerHTML=d.html.trim();return e.firstChild}"
})

# Add a marker
show("html", {"id": "tokyo", "lat": 35.68, "lng": 139.69, "html": "<div class='my-marker'>🗼</div>"})
refresh("html")

# Later, remove it
hide("html", ["tokyo"])
refresh("html")
```

### Fragment JavaScript execution

The `js` field of a fragment runs as an **async function body**. This means:
- You can use `await`
- You have access to all globals (`window`, `document`, `window.$globe`, etc.)
- You can create elements, start intervals, attach event listeners, load scripts
- You can write to the GunDB graph (`window.$scene.get('key').put({...})`)
- You can modify the Three.js scene directly
- The code runs once when the fragment is created, and again if updated

```python
s.put("my_logic", js="""
// This runs in the browser
const el = document.createElement('div');
el.innerHTML = '<h1>Hello from JS</h1>';
el.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:999;color:white;font-size:4rem;';
window.$root.appendChild(el);

// Fade out after 3 seconds
setTimeout(() => { el.style.transition = 'opacity 1s'; el.style.opacity = '0'; }, 3000);
setTimeout(() => el.remove(), 4000);
""")
```

```python
# Load a library then use it
s.put("tone_setup", js="""
await new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = 'https://cdnjs.cloudflare.com/ajax/libs/tone/14.8.49/Tone.js';
    script.onload = resolve;
    script.onerror = reject;
    document.head.appendChild(script);
});
// Now Tone.js is available
const synth = new Tone.Synth().toDestination();
synth.triggerAttackRelease('C4', '8n');
""")
```

---

## 4. WHAT YOU CAN BUILD

The system is a programmable screen. The fragment model imposes no constraints on what HTML, CSS, or JS you push. Here are categories of things that work, with concrete implementation patterns.

### Live data displays
Push HTML tables, charts, or dashboards as fragments. Update them on a timer or when data changes. Use CSS grid/flex for layout. Scroll long content with `overflow-y: auto` and CSS animation.

### Full-screen video
```python
s.put("video", html='<div style="position:fixed;inset:0;z-index:50"><iframe src="https://www.youtube.com/embed/VIDEO_ID?autoplay=1&mute=1&controls=0" style="width:100%;height:100%;border:none" allow="autoplay"></iframe></div>')
s.wait(30)
s.remove("video")
```

### Live video calls
Embed any WebRTC-based service (Daily, Jitsi, Whereby) via iframe. The fragment's JS can initialize a WebRTC peer connection directly if you want lower-level control.

### Interactive elements with HTMX
HTMX is loaded by default. Any HTML fragment can use `hx-get`, `hx-post`, `hx-trigger`, etc. to make HTTP requests to your relay's fragment endpoints or any external server.

```python
# A voting widget that posts to an external endpoint
s.put("poll", html="""
<div style="position:fixed;bottom:100px;right:20px;z-index:200;background:#111;padding:20px;border:1px solid #333;border-radius:8px;">
  <p style="color:white;margin-bottom:12px;">What should we cover next?</p>
  <button hx-post="/my_bucket/frag/vote_result" hx-vals='{"choice":"sports"}' hx-target="#vote-status" style="margin:4px;padding:8px 16px;cursor:pointer;">⚽ Sports</button>
  <button hx-post="/my_bucket/frag/vote_result" hx-vals='{"choice":"weather"}' hx-target="#vote-status" style="margin:4px;padding:8px 16px;cursor:pointer;">🌤 Weather</button>
  <div id="vote-status" style="color:#888;margin-top:8px;"></div>
</div>
""")
```

### 3D scenes beyond the globe
Access the Three.js scene via `window.$globe.scene()` and renderer via `window.$globe.renderer()`. Add meshes, lights, particles, shaders. Or replace the globe entirely by removing the `boot` fragment and initializing your own Three.js scene.

```python
s.put("particles", js="""
const scene = window.$globe.scene();
const geometry = new THREE.BufferGeometry();
const positions = new Float32Array(3000);
for (let i = 0; i < 3000; i++) positions[i] = (Math.random() - 0.5) * 400;
geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
const material = new THREE.PointsMaterial({ size: 0.5, color: 0x00ffff });
scene.add(new THREE.Points(geometry, material));
""")
```

### Canvas-based graphics
Create `<canvas>` elements in HTML fragments and draw on them from JS fragments. Useful for custom charts, generative art, or game renders.

### Audio
Use the Web Audio API or Tone.js (load via link fragment). Play sounds on events, create ambient soundscapes, build a radio station.

### Games
The fragment model supports any browser game. Push the game's HTML/CSS as one fragment, its logic as a JS fragment. Use `window.$scene` for real-time multiplayer state via GunDB. Inputs from one viewer propagate to all viewers.

### Picture-in-picture layouts
Stack multiple fragments with different z-index and position values. A common pattern: globe at z-index 0, data panel at z-index 50 with `position:fixed;top:0;left:0;width:40%;height:100%`, chyron at z-index 100.

### Multi-panel layouts
Push a CSS fragment that defines a grid layout on `#scene`, then push HTML fragments that target grid areas.

```python
s.put("layout_css", css="""
#scene {
  display: grid;
  grid-template-columns: 1fr 1fr;
  grid-template-rows: 1fr 1fr;
  height: 100vh;
}
""")
s.put("panel_1", html="<div style='background:#111;padding:20px;'>Panel 1</div>")
s.put("panel_2", html="<div style='background:#1a1a1a;padding:20px;'>Panel 2</div>")
s.put("panel_3", html="<div style='background:#222;padding:20px;'>Panel 3</div>")
s.put("panel_4", html="<div style='background:#0a0a0a;padding:20px;'>Panel 4</div>")
```

### Audience-reactive content
Read from the GunDB graph in JS fragments to react to audience input in real-time. One viewer writes a value, all viewers see the update.

```python
s.put("reactions", js="""
window.$scene.get('reactions').on(data => {
    if (!data) return;
    // data is updated in real-time across all viewers
    document.getElementById('reaction-count').textContent = data.count || 0;
});
""")
```

### Iframes to anything
Any URL that allows embedding works inside an iframe fragment. Google Maps, Figma, Observable notebooks, CodePen, Shadertoy, Spotify embeds, Google Sheets, PDF viewers, webcam feeds.

### Encrypted content
GunDB's SEA module is available. Fragments can contain encrypted payloads that only viewers with the right key can decrypt. The shell has built-in support for this: encrypted fragments show a "LOCKED" placeholder with a click-to-decrypt prompt.

---

## 5. MULTI-BUCKET ARCHITECTURE

Each bucket is an independent display surface. A script can write to multiple buckets simultaneously.

```python
news = SceneWriter("http://localhost:8765", "news_channel")
sports = SceneWriter("http://localhost:8765", "sports_channel")

boot(news, "NEWS NETWORK")
boot(sports, "SPORTS NET")

# These run on different screens / in different browser tabs
news.put("headline", html="<div>Breaking: ...</div>")
sports.put("scores", html="<div>Game update: ...</div>")
```

A **channel guide** is itself just another bucket that contains an iframe-switching UI. The demo uses a JS fragment that builds channel cards and swaps an iframe's `src` between bucket URLs.

Viewers open `http://localhost:8765/guide/` and see a channel selection screen. Pressing a number key or clicking a card loads that channel's bucket in the iframe. Each channel runs independently — they don't know about each other.

---

## 6. PATTERNS AND CONVENTIONS

### Fragment naming
Use descriptive keys: `chyron`, `ticker`, `sidebar`, `logo`, `holding`, `panel_scores`, `globe_html`. The boot process uses: `styles`, `boot`, `clock`, `logo`, `holding`, `ticker`. Avoid overwriting these unless you intend to replace them.

### Layering
Fragments render in DOM order (creation order). Use CSS `position: fixed` and `z-index` to control visual stacking. Convention: globe at 0, panels at 50, chrome (chyron/ticker/logo) at 90-101, bumpers at 200.

### The "never blank" principle
Always have a holding pattern — something visually interesting on screen between content segments. A slowly rotating globe with subtle branding is better than a black screen.

### Fragment lifecycle
- `s.put("key", html="...")` — Creates or updates. If the key exists, the DOM element is updated in place (innerHTML replacement).
- `s.remove("key")` — Removes the DOM element, style tag, and all associated link/script tags.
- `s.clear()` — Removes all fragments. Screen goes blank. Follow with `boot()` to reinitialize.

### CSS strategy
Push one `styles` fragment with your full stylesheet early (boot does this). Push additional CSS fragments for segment-specific styles. Remove them when the segment ends. Class-based styling keeps your HTML fragments clean and your CSS swappable.

### JS fragment lifecycle
JS runs every time the fragment is created or updated. Guard against double-initialization:
```python
s.put("my_logic", js="""
if (window._myLogicInit) return;
window._myLogicInit = true;
// ... one-time setup ...
""")
```

### Data flow
Your Python script fetches data (APIs, databases, files, AI models — anything), transforms it into HTML/CSS/JS strings, and pushes it as fragments. The browser has no idea where the data came from. It just renders what it receives.

---

## 7. FILE STRUCTURE

```
broadcast/
  relay/
    server.js         — GunDB relay + REST API (do not modify)
    package.json
  shell/
    index.html        — Blank renderer (do not modify)
  sdk/
    scene.py          — SceneWriter class
    timeline.py       — Timeline DSL + compiler
    chrome.py         — HTML generators (logo, chyron, ticker, bumper, sidebar, markers)
    boot.py           — Bucket initialization
    data.py           — Public API fetchers (earthquakes, weather, news, sports)
    broadcast.css     — Default visual theme
  demos/
    demo_linear.py    — Single continuous channel
    demo_network.py   — Multi-channel network with guide
```

### Running

```bash
# Terminal 1
cd relay && npm install && npm start

# Terminal 2
cd broadcast && python demos/demo_linear.py

# Browser
open http://localhost:8765/live/
```

---

## 8. WRITING A NEW BROADCAST

A broadcast script follows this pattern:

```python
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sdk.scene import SceneWriter
from sdk.boot import boot

RELAY = "http://localhost:8765"
BUCKET = "my_show"

s = SceneWriter(RELAY, BUCKET)
boot(s, "MY NETWORK")

# --- Your show starts here ---

# Push any HTML you want
s.put("intro", html="""
<div style="position:fixed;inset:0;display:flex;align-items:center;justify-content:center;z-index:100;background:black;">
  <h1 style="font-size:6vw;color:white;font-family:Georgia,serif;opacity:0;animation:fade-in 2s forwards;">
    Welcome to the Show
  </h1>
</div>
""")
s.wait(4)
s.remove("intro")

# Show a data panel
s.put("dashboard", html=my_html_string)
s.wait(30)
s.remove("dashboard")

# Keep going forever, or end
```

There are no required patterns. No base classes to inherit. No lifecycle hooks. Write fragments, wait, remove fragments. That's the entire programming model.

The screen is yours. Build whatever you can imagine.