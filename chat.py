#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import string
import time
import traceback

from HyperCoreSDK.client import HyperClient

CHAT_HTML = """
<div style="display:flex;flex-direction:column;width:100vw;height:100vh;background:#0f172a;color:#e5e7eb;font-family:sans-serif">
  <div style="padding:12px 16px;border-bottom:1px solid #374151;font-weight:700" data-bind-text="title"></div>
  <div data-children style="flex:1;overflow:auto;padding:12px;display:flex;flex-direction:column;gap:10px"></div>
  <div style="display:flex;gap:8px;padding:10px;border-top:1px solid #374151">
    <input id="user" value="guest" style="width:90px;padding:8px;border:1px solid #475569;border-radius:8px;background:#111827;color:#e5e7eb" />
    <input id="text" placeholder="message" style="flex:1;padding:8px;border:1px solid #475569;border-radius:8px;background:#111827;color:#e5e7eb" />
    <button id="send" style="padding:8px 12px;border:1px solid #475569;border-radius:8px;background:#1f2937;color:#e5e7eb;cursor:pointer">Send</button>
  </div>
</div>
"""

MESSAGE_HTML = """
<div style="background:#111827;border:1px solid #374151;border-radius:12px;padding:10px 12px">
  <div data-bind-text="user" style="font-size:12px;color:#93c5fd"></div>
  <div data-bind-text="text" style="font-size:14px;margin-top:4px;white-space:pre-wrap"></div>
</div>
"""


def rand_suffix(n: int = 5) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))



def snapshot_dict(hc: HyperClient) -> dict:
    snap = hc.snapshot()
    return snap if isinstance(snap, dict) else {}



def mount_chat_root(hc: HyperClient) -> None:
    hc.mount(
        "root/chat",
        html=CHAT_HTML,
        fixed=True,
        layer=10,
        js=hc.actions_js(
            send={
                "fields": ["user", "text"],
                "trigger": "send",
                "submit": "text",
            }
        ),
    )
    hc.at("root/chat").write(data={"title": "Chat Room Beta"})



def mount_message_node(hc: HyperClient, path: str) -> None:
    hc.mount(path, html=MESSAGE_HTML)



def drain_inbox(hc: HyperClient):
    snap = snapshot_dict(hc)
    for key in sorted(snap.keys()):
        if not key.startswith("inbox/"):
            continue

        node = snap.get(key) or {}
        raw = node.get("data")
        if raw is None:
            hc.remove(key)
            continue

        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            print("[INBOX BAD JSON]", key, repr(raw), exc)
            hc.remove(key)
            continue

        if not isinstance(payload, dict):
            print("[INBOX BAD PAYLOAD]", key, repr(payload))
            hc.remove(key)
            continue

        action_name = str(payload.get("_action", "unknown"))
        yield key, action_name, payload



def write_message(hc: HyperClient, user: str, msg: str) -> str:
    msg_id = f"msg_{int(time.time() * 1000)}_{rand_suffix()}"
    path = f"root/chat/{msg_id}"

    mount_message_node(hc, path)
    hc.at(path).write(data={"user": user, "text": msg})

    snap = snapshot_dict(hc)
    print("[MSG WRITE]", path, snap.get(path))
    return path



def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--root", default="chat8")
    p.add_argument("--discovery", default="local")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--no-clear", action="store_true")
    args = p.parse_args()

    hc = HyperClient(root=args.root, discovery=args.discovery, port=args.port)
    hc.connect()

    if not args.no_clear:
        hc.clear()

    mount_chat_root(hc)

    print("[CHAT BOOT]", {"root": args.root, "port": args.port, "discovery": args.discovery})
    print("[CHAT READY]", hc.at("root/chat").stream_url())

    while True:
        handled_any = False

        for inbox_key, action_name, payload in drain_inbox(hc):
            handled_any = True
            print("[ACTION]", inbox_key, action_name, payload)

            if action_name != "send":
                print("[ACTION DROP]", inbox_key, action_name)
                hc.remove(inbox_key)
                continue

            user = str(payload.get("user", "guest")).strip() or "guest"
            msg = str(payload.get("text", "")).strip()

            if not msg:
                print("[ACTION EMPTY]", inbox_key, payload)
                hc.remove(inbox_key)
                continue

            try:
                out_path = write_message(hc, user, msg)
            except Exception:
                print("[ACTION WRITE FAIL]", inbox_key)
                traceback.print_exc()
                continue

            hc.remove(inbox_key)
            print("[ACTION ACK]", inbox_key, "->", out_path)

        if not handled_any:
            time.sleep(0.10)


if __name__ == "__main__":
    main()
