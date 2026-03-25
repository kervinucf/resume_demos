#!/usr/bin/env python3
import argparse, time
from HyperCoreSDK.client import HyperClient
from HyperCoreSDK.ui import page, bar, scroll, card, row, span, text, input, btn

p = argparse.ArgumentParser()
p.add_argument("--discovery", default="local")
p.add_argument("--port", type=int, default=8765)
a = p.parse_args()

hc = HyperClient(root="chat3", discovery=a.discovery, port=a.port)
hc.connect()
hc.clear()

hc.mount(
    "root/chat",
    html=page(
        bar(span("title", "font-weight:700")),
        scroll(),
        row(
            input("user", value="guest", width="90px"),
            input("text", placeholder="message"),
            btn("Send", id="send"),
            gap="8px", style="padding:10px;border-top:1px solid #374151",
        ),
    ),
    fixed=True,
    layer=10,
    js=hc.actions_js(
        send={"fields": ["user", "text"], "trigger": "send", "submit": "text"},
    )
)
hc.write("root/chat", title="Chat Room Beta")

n = 0
while True:
    for act in hc.actions():
        if act.name == "send":
            n += 1
            hc.mount(
                f"root/chat/msg_{n}",
                html=card(
                    span("user", "font-size:12px;color:#93c5fd"),
                    text("text", "font-size:14px;margin-top:4px"),
                )
            )
            hc.write(f"root/chat/msg_{n}", user=act["user"], text=act["text"])
    time.sleep(0.1)
