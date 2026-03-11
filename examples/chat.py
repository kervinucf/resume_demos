#!/usr/bin/env python3
import json, time
from HyperCoreSDK import HyperClient

hc = HyperClient(relay="http://localhost:8765", root="demo_chat_tiny")
hc.start_relay()
hc.clear()

CHAT = """
<div style="width:100%;height:100%;display:flex;flex-direction:column;background:#111827;color:#e5e7eb;font-family:Arial,sans-serif">
  <div data-bind-text="title" style="padding:12px 14px;border-bottom:1px solid #374151;font-weight:700"></div>
  <div data-children style="flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:8px;padding:12px"></div>
  <div style="display:flex;gap:8px;padding:10px;border-top:1px solid #374151">
    <input id="u" value="guest" style="width:90px;padding:10px;background:#1f2937;color:#e5e7eb;border:1px solid #374151;border-radius:8px;outline:none">
    <input id="m" placeholder="message" style="flex:1;padding:10px;background:#1f2937;color:#e5e7eb;border:1px solid #374151;border-radius:8px;outline:none">
    <button id="s" style="padding:10px 14px;background:#e5e7eb;color:#111827;border:0;border-radius:8px;font-weight:700;cursor:pointer">Send</button>
  </div>
</div>
"""

MSG = """
<div style="padding:8px 10px;background:#1f2937;border:1px solid #374151;border-radius:10px">
  <div data-bind-text="user" style="font-size:12px;color:#93c5fd;margin-bottom:4px"></div>
  <div data-bind-text="text" style="font-size:14px;line-height:1.35"></div>
</div>
"""

JS = r"""
(function(){
  const m=document.getElementById("m"),u=document.getElementById("u"),s=document.getElementById("s");
  if(!m||!u||!s||s.dataset.on)return; s.dataset.on=1;
  const send=()=>{
    const text=m.value.trim(); if(!text) return;
    window.$scene.get("inbox/"+Date.now()+"_"+Math.random().toString(36).slice(2,7)).put({
      data: JSON.stringify({user:u.value.trim()||"guest", text})
    });
    m.value=""; m.focus();
  };
  s.onclick=send;
  m.addEventListener("keydown",e=>e.key==="Enter"&&send());
})();
"""

hc.mount("root/chat", html=CHAT, js=JS, fixed=True, layer=10)
hc.write("root/chat", title="general")

seen = set()
while True:
    for k, v in (hc.snapshot() or {}).items():
        if not k.startswith("inbox/") or k in seen:
            continue
        try:
            msg = json.loads(v["data"])
        except Exception:
            continue
        out = "root/chat/" + k.split("/", 1)[1]
        hc.mount(out, html=MSG)
        hc.write(out, user=msg.get("user", "guest"), text=msg.get("text", ""))
        seen.add(k)
    time.sleep(0.1)