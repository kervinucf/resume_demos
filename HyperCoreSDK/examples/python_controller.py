#!/usr/bin/env python3
import time
from random import choice
from HyperCoreSDK.client import HyperClient

hc = HyperClient(relay="http://localhost:8765", root="demo_move_slider")
hc.start_relay()
hc.clear()

BOX = """
<div data-bind-style="background:color"
     style="width:100%;height:100%;display:flex;flex-direction:column">
  <div data-bind-text="label"></div>
  <div data-children style="flex:1;display:flex;min-height:0"></div>
</div>
"""

SLIDER = """
<div style="position:fixed;top:12px;left:12px;z-index:9999;background:#111;color:#eee;padding:10px;border-radius:8px;font-family:sans-serif">
  <label>speed </label>
  <input id="speed" type="range" min="0" max="100" step="1" value="50">
  <span id="speed_val">1.00</span>
</div>
"""

SLIDER_JS = r"""
(function(){
  const s = document.getElementById("speed");
  const v = document.getElementById("speed_val");
  if (!s || !v || s.dataset.on) return;
  s.dataset.on = "1";

  function mapSpeed(x){
    const t = Number(x) / 100;
    return (0.01 * Math.pow(200, t)).toFixed(2); // 0.01 -> 2.00
  }

  function send(){
    const value = mapSpeed(s.value);
    v.textContent = value;
    window.$scene.get("controls/speed").put({ value });
  }

  s.oninput = send;
  send();
})();
"""


def mount_layout(base: str, layout: str) -> list[tuple[str, str]]:
    items = hc.expand_layout(base, layout)
    for _, key in items:
        hc.mount(key, html=BOX)
    return items

layouts = [
    "[a][b]~[c]",
    "[a]~[b][c]",
    "[b][a]~[c]",
    "[c]~[a][b]",
]

colors = ["red", "orange", "blue", "green", "purple", "yellow"]

hc.mount("root/slider", html=SLIDER, js=SLIDER_JS, fixed=True, layer=20)

delay = 1.0
i = 0

while True:
    snap = hc.snapshot() or {}
    ctl = snap.get("controls/speed") or {}

    try:
        if "value" in ctl:
            delay = float(ctl["value"])
    except Exception:
        pass

    print("delay =", delay, "ctl =", ctl)

    hc.remove("root/boxes")

    layout = layouts[i % len(layouts)]
    items = mount_layout("root/boxes", layout)

    for label, key in items:
        hc.write(key, label=label, color=choice(colors))

    i += 1

    target = time.time() + delay
    while time.time() < target:
        snap = hc.snapshot() or {}
        ctl = snap.get("controls/speed") or {}
        try:
            if "value" in ctl:
                delay = float(ctl["value"])
        except Exception:
            pass
        time.sleep(0.01)