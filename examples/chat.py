#!/usr/bin/env python3
"""
Chat — two machines, same WiFi, zero config.

    pip install zeroconf

    # Machine A
    python -m examples.chat --discovery lan

    # Machine B (same command, finds A via mDNS, syncs via Gun)
    python -m examples.chat --discovery lan

    Both print: open in browser: http://localhost:8765/chat
    Open that on each machine. Messages sync automatically.
"""

import argparse
import json
import time
from HyperCoreSDK.client import HyperClient

parser = argparse.ArgumentParser(description="HyperChat")
parser.add_argument("--discovery", default="local", choices=["local", "lan", "trusted"])
parser.add_argument("--relay", default="auto", choices=["auto", "host", "join"])
parser.add_argument("--peers", nargs="*", default=[])
parser.add_argument("--port", type=int, default=8765)
parser.add_argument("--root", default="chat")
args = parser.parse_args()

hc = HyperClient(
    root=args.root,
    discovery=args.discovery,
    relay=args.relay,
    peers=[f"http://{p}:{args.port}" for p in args.peers],
    port=args.port,
)
hc.connect()
hc.clear()

CHAT_HTML = """
<div style="width:100%;height:100%;display:flex;flex-direction:column;background:#111827;color:#e5e7eb;font-family:Arial,sans-serif">
  <div style="padding:12px 14px;border-bottom:1px solid #374151;display:flex;justify-content:space-between;align-items:center">
    <span data-bind-text="title" style="font-weight:700"></span>
    <span data-bind-text="mode" style="font-size:11px;color:#6b7280;background:#1f2937;padding:4px 8px;border-radius:4px"></span>
  </div>
  <div data-children style="flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:8px;padding:12px"></div>
  <div style="display:flex;gap:8px;padding:10px;border-top:1px solid #374151">
    <input id="u" value="guest" style="width:90px;padding:10px;background:#1f2937;color:#e5e7eb;border:1px solid #374151;border-radius:8px;outline:none">
    <input id="m" placeholder="message" style="flex:1;padding:10px;background:#1f2937;color:#e5e7eb;border:1px solid #374151;border-radius:8px;outline:none">
    <button id="s" style="padding:10px 14px;background:#e5e7eb;color:#111827;border:0;border-radius:8px;font-weight:700;cursor:pointer">Send</button>
  </div>
</div>
"""

MSG_HTML = """
<div style="padding:8px 10px;background:#1f2937;border:1px solid #374151;border-radius:10px">
  <div style="display:flex;justify-content:space-between;align-items:baseline">
    <span data-bind-text="user" style="font-size:12px;color:#93c5fd"></span>
    <span data-bind-text="machine" style="font-size:10px;color:#4b5563"></span>
  </div>
  <div data-bind-text="text" style="font-size:14px;line-height:1.35;margin-top:4px"></div>
</div>
"""

CHAT_JS = r"""
(function(){
  const m=document.getElementById("m"),u=document.getElementById("u"),s=document.getElementById("s");
  if(!m||!u||!s||s.dataset.on)return; s.dataset.on=1;
  const send=()=>{
    const text=m.value.trim(); if(!text) return;
    window.$scene.get("inbox/"+Date.now()+"_"+Math.random().toString(36).slice(2,7)).put({
      data: JSON.stringify({user:u.value.trim()||"guest", text:text})
    });
    m.value=""; m.focus();
  };
  s.onclick=send;
  m.addEventListener("keydown",e=>e.key==="Enter"&&send());
})();
"""

hc.mount("root/chat", html=CHAT_HTML, js=CHAT_JS, fixed=True, layer=10)
hc.write("root/chat", title="general", mode=f"{hc.discovery} · {hc.machine_id[:16]}")

seen = set()
while True:
    for k, v in (hc.snapshot() or {}).items():
        if not k.startswith("inbox/") or k in seen:
            continue
        seen.add(k)
        try:
            raw = v.get("data", "{}")
            msg = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            continue
        out = "root/chat/" + k.split("/", 1)[1]
        hc.mount(out, html=MSG_HTML)
        hc.write(out, user=msg.get("user", "guest"),
                 text=msg.get("text", ""), machine=hc.machine_id[:16])
        hc.remove(k)
    time.sleep(0.1)