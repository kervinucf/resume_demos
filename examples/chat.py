#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from HyperCoreSDK.client import HyperClient

PANEL_HTML = """
<div
  style="font-family:system-ui,sans-serif;display:grid;gap:12px;padding:16px;border-radius:16px;border:1px solid rgba(0,0,0,.12);box-shadow:0 8px 30px rgba(0,0,0,.08)"
  data-bind-style="background:secure.prefs.cardBg;color:secure.prefs.textColor"
>
  <div style="display:flex;justify-content:space-between;align-items:center;gap:12px">
    <div>
      <div style="font-size:12px;opacity:.7;text-transform:uppercase;letter-spacing:.08em">Hyper-node micro app</div>
      <div style="font-size:22px;font-weight:700">
        <span data-bind-text="public.city"></span>
      </div>
    </div>
    <div style="font-size:48px;line-height:1">
      <span data-bind-text="public.temp"></span><span data-bind-text="secure.user.units"></span>
    </div>
  </div>

  <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px">
    <div style="padding:10px;border-radius:12px;background:rgba(255,255,255,.08)">
      <div style="font-size:12px;opacity:.75">Condition</div>
      <div style="font-weight:700" data-bind-text="public.cond"></div>
    </div>
    <div style="padding:10px;border-radius:12px;background:rgba(255,255,255,.08)">
      <div style="font-size:12px;opacity:.75">Viewer</div>
      <div style="font-weight:700" data-bind-text="secure.user.name"></div>
    </div>
    <div style="padding:10px;border-radius:12px;background:rgba(255,255,255,.08)">
      <div style="font-size:12px;opacity:.75">Role</div>
      <div style="font-weight:700" data-bind-text="secure.user.role"></div>
    </div>
  </div>

  <div style="padding:10px 12px;border-radius:12px;background:rgba(255,255,255,.08)">
    <strong>Banner:</strong>
    <span data-bind-text="secure.ui.banner"></span>
  </div>

  <div id="note-wrap" data-bind-style="display:secure.ui.noteDisplay">
    <div style="padding:10px 12px;border-radius:12px;background:rgba(255,255,255,.08)">
      <strong>Internal note:</strong>
      <span data-bind-text="public.note"></span>
    </div>
  </div>

  <div style="display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px">
    <button id="toggle-note" style="padding:10px 12px;border-radius:12px;border:0;cursor:pointer">Toggle note</button>
    <button id="toggle-units" style="padding:10px 12px;border-radius:12px;border:0;cursor:pointer">Toggle units</button>
    <button id="switch-user" style="padding:10px 12px;border-radius:12px;border:0;cursor:pointer">Switch user</button>
    <button id="toggle-theme" style="padding:10px 12px;border-radius:12px;border:0;cursor:pointer">Toggle theme</button>
    <button id="toggle-debug" style="padding:10px 12px;border-radius:12px;border:0;cursor:pointer">Toggle debug</button>
  </div>

  <div style="font-size:12px;opacity:.75">
    Local clicks:
    <span data-bind-text="local.stats.clicks"></span>
  </div>

  <pre
    id="dom-dump"
    data-bind-style="display:local.ui.dumpDisplay"
    style="margin:0;padding:12px;border-radius:12px;background:#0b1020;color:#e5e7eb;white-space:pre-wrap;word-break:break-word;overflow:auto;min-height:160px"
  ></pre>
</div>
"""

PANEL_JS = r"""
(function(){
  if (!hyperElement || hyperElement.__weatherMicroAppBound) return;
  hyperElement.__weatherMicroAppBound = true;

  hyperElement.defineSchema({
    public: {
      city: { type: "string", default: "Unknown" },
      temp: { type: "number", default: 0 },
      cond: { type: "string", default: "Unknown" },
      units: { type: "string", default: "°F" },
      note: { type: "string", default: "" }
    },
    secure: {
      user: {
        id: { type: "string", default: "guest-0" },
        name: { type: "string", default: "Guest" },
        role: { type: "string", default: "viewer" },
        units: { type: "string", default: "°F" }
      },
      prefs: {
        theme: { type: "string", default: "paper" },
        cardBg: { type: "string", default: "#ffffff" },
        textColor: { type: "string", default: "#111827" }
      },
      ui: {
        noteDisplay: { type: "string", default: "block" },
        banner: { type: "string", default: "Private context active" }
      }
    },
    local: {
      stats: {
        clicks: { type: "number", default: 0 }
      },
      ui: {
        dumpDisplay: { type: "string", default: "block" }
      }
    }
  });

  function $(id){
    return document.getElementById(id);
  }

  function getThemePack(name){
    if (name === "midnight") {
      return {
        theme: "midnight",
        cardBg: "#0f172a",
        textColor: "#e2e8f0"
      };
    }
    return {
      theme: "paper",
      cardBg: "#ffffff",
      textColor: "#111827"
    };
  }

  function currentSecure(){
    return hyperElement.getSecure();
  }

  function currentLocal(){
    return hyperElement.getLocal();
  }

  function bumpClicks(){
    var local = currentLocal();
    var clicks = (((local || {}).stats || {}).clicks || 0) + 1;
    hyperElement.mergeLocal({ stats: { clicks: clicks } });
  }

  function dump(tag){
    var hc = window.hyperContext || window.hyper_context || null;
    var payload = {
      tag: tag,
      trust: hc ? hc.trust : null,
      mode: hc ? hc.mode : null,
      publicParamsSeenByNodeJS: hc ? hc.params : null,
      publicCtx: hyperElement.getPublic(),
      secureCtx: hyperElement.getSecure(),
      localCtx: hyperElement.getLocal(),
      schema: hyperElement.getSchema(),
      dom: {
        city: $("toggle-note") ? $("toggle-note").closest("div[data-bind-style]") ? null : null : null,
        viewer: $("switch-user") ? true : false,
        noteDisplay: (document.getElementById("note-wrap") || {}).style ? document.getElementById("note-wrap").style.display : null
      }
    };

    var pre = $("dom-dump");
    if (pre) pre.textContent = JSON.stringify(payload, null, 2);
  }

  $("toggle-note").addEventListener("click", function(){
    var secure = currentSecure();
    var visible = (((secure || {}).ui || {}).noteDisplay || "block") !== "none";

    hyperElement.mergeSecure({
      ui: {
        noteDisplay: visible ? "none" : "block",
        banner: visible ? "Private note hidden locally" : "Private note visible locally"
      }
    });

    bumpClicks();
  });

  $("toggle-units").addEventListener("click", function(){
    var secure = currentSecure();
    var nextUnits = (((secure || {}).user || {}).units || "°F") === "°F" ? "°C" : "°F";

    hyperElement.mergeSecure({
      user: { units: nextUnits },
      ui: { banner: "Units switched locally to " + nextUnits }
    });

    bumpClicks();
  });

  $("switch-user").addEventListener("click", function(){
    var secure = currentSecure();
    var currentName = (((secure || {}).user || {}).name || "Guest");

    var next = currentName === "Bob"
      ? { id: "user-789", name: "Ava", role: "operator", units: "°C" }
      : { id: "user-123", name: "Bob", role: "admin", units: "°F" };

    hyperElement.mergeSecure({
      user: next,
      ui: { banner: "Viewer switched locally to " + next.name + " (" + next.role + ")" }
    });

    bumpClicks();
  });

  $("toggle-theme").addEventListener("click", function(){
    var secure = currentSecure();
    var currentTheme = (((secure || {}).prefs || {}).theme || "paper");
    var nextTheme = currentTheme === "midnight" ? "paper" : "midnight";
    var pack = getThemePack(nextTheme);

    hyperElement.mergeSecure({
      prefs: pack,
      ui: { banner: "Theme switched locally to " + pack.theme }
    });

    bumpClicks();
  });

  $("toggle-debug").addEventListener("click", function(){
    var local = currentLocal();
    var current = ((((local || {}).ui || {}).dumpDisplay) || "block");
    hyperElement.mergeLocal({
      ui: {
        dumpDisplay: current === "none" ? "block" : "none"
      }
    });

    bumpClicks();
  });

  hyperElement.addEventListener("hyper-update", function(){
    dump("hyper-update");
  });

  dump("boot");
})();
"""

def pretty(obj) -> str:
    try:
        return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
    except Exception:
        return repr(obj)

def seed_weather(hc: HyperClient) -> None:
    print("[PY] mounting source node weather/nyc")
    hc.write(
        "weather/nyc",
        data={
            "city": "New York",
            "temp": 72,
            "cond": "Sunny",
            "units": "°F",
            "note": "source-node",
        },
    )

    print("[PY] mounting panel weather/nyc/panel")
    hc.mount("weather/nyc/panel", html=PANEL_HTML, js=PANEL_JS)

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="demo111")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--discovery", default="local")
    args = ap.parse_args()

    hc = HyperClient(root=args.root, port=args.port, discovery=args.discovery)
    hc.connect()
    seed_weather(hc)

    base_url = hc.stream_url("weather/nyc/panel")
    leaf_url = hc.render_url(
        "weather/nyc/panel",
        params={
            "city": hc.bind("weather/nyc/data/city"),
            "temp": hc.bind("weather/nyc/data/temp"),
            "cond": hc.bind("weather/nyc/data/cond"),
            "units": hc.bind("weather/nyc/data/units"),
            "note": hc.bind("weather/nyc/data/note"),
            "_v": 7,
        },
    )

    print("\n[PY] OPEN THESE URLS")
    print("[PY] base_url =", base_url)
    print("[PY] leaf_url =", leaf_url)

    i = 0
    temps = [72, 74, 71, 69, 76]
    conds = ["Sunny", "Breezy", "Cloudy", "Rain", "Partly Cloudy"]

    while True:
        temp = temps[i % len(temps)]
        cond = conds[i % len(conds)]
        payload = {
            "city": "New York",
            "temp": temp,
            "cond": cond,
            "units": "°F",
            "note": f"source-node-{i}",
        }

        print("\n[PY] write weather/nyc data =")
        print(pretty(payload))
        hc.write("weather/nyc", data=payload)

        node = hc.read("weather/nyc")
        panel = hc.read("weather/nyc/panel")

        print("[PY] read weather/nyc =")
        print(pretty(node))
        print("[PY] read weather/nyc/panel =")
        print(pretty(panel))

        i += 1

if __name__ == "__main__":
    main()