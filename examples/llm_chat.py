#!/usr/bin/env python3
import json
import time
import requests
from HyperCoreSDK.client import HyperClient

MODEL_A = "qwen3:14b"
MODEL_B = "gemma3:12b"
TURN_DELAY = 1.0
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"

hc = HyperClient(relay="http://localhost:8765", root="demo_group_chat_ollama")
hc.start_relay()
hc.clear()

APP_HTML = """
<div style="width:100%;height:100%;display:flex;flex-direction:column;background:#0f172a;color:#e2e8f0;font-family:Arial,sans-serif">
  <div style="flex:0 0 auto;padding:12px 14px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center;gap:12px;background:#0f172a">
    <div style="min-width:0">
      <div style="font-size:14px;font-weight:700">Group Chat: You + Two Local Models</div>
      <div data-bind-text="status" style="font-size:12px;color:#94a3b8;margin-top:4px;min-height:16px"></div>
    </div>
    <div style="display:flex;gap:8px;align-items:center;font-size:12px;color:#94a3b8;white-space:nowrap;flex:0 0 auto">
      <span data-bind-text="model_a"></span>
      <span>·</span>
      <span data-bind-text="model_b"></span>
    </div>
  </div>

  <div
    id="chat_list"
    data-children
    style="
      flex:1 1 auto;
      min-height:0;
      overflow:auto;
      display:flex;
      flex-direction:column;
      gap:10px;
      padding:14px;
      overscroll-behavior:contain;
      scrollbar-gutter:stable;
      contain:layout style paint;
    "
  ></div>

  <div style="flex:0 0 auto;padding:10px 14px;border-top:1px solid #334155;background:#0b1220;display:flex;flex-direction:column;gap:10px">
    <textarea
      id="prompt"
      placeholder="jump into the conversation..."
      style="
        width:100%;
        height:96px;
        resize:none;
        padding:12px;
        background:#111827;
        color:#e2e8f0;
        border:1px solid #334155;
        border-radius:10px;
        outline:none;
        box-sizing:border-box;
        line-height:1.45;
      "
    ></textarea>
    <div style="display:flex;justify-content:space-between;align-items:center;gap:10px">
      <div style="font-size:12px;color:#64748b">Enter to send · Shift+Enter for newline</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button id="start_auto" style="padding:10px 14px;background:#1d4ed8;color:#eff6ff;border:0;border-radius:10px;font-weight:700;cursor:pointer">Start Auto</button>
        <button id="clear" style="padding:10px 14px;background:#334155;color:#e2e8f0;border:0;border-radius:10px;font-weight:700;cursor:pointer">Clear</button>
        <button id="send" style="padding:10px 14px;background:#e2e8f0;color:#0f172a;border:0;border-radius:10px;font-weight:700;cursor:pointer">Send</button>
      </div>
    </div>
  </div>
</div>
"""

APP_JS = r"""
(function(){
  const prompt = document.getElementById("prompt");
  const send = document.getElementById("send");
  const clear = document.getElementById("clear");
  const startAuto = document.getElementById("start_auto");
  const list = document.getElementById("chat_list");

  if (!prompt || !send || !clear || !startAuto || !list || send.dataset.on) return;
  send.dataset.on = "1";

  let rafId = null;
  let stickToBottom = true;

  function distanceFromBottom() {
    return list.scrollHeight - list.scrollTop - list.clientHeight;
  }

  function updateStickiness() {
    stickToBottom = distanceFromBottom() < 120;
  }

  function scheduleScroll(force, smooth) {
    if (rafId) cancelAnimationFrame(rafId);
    rafId = requestAnimationFrame(() => {
      rafId = requestAnimationFrame(() => {
        if (force || stickToBottom) {
          list.scrollTo({
            top: list.scrollHeight,
            behavior: smooth ? "smooth" : "auto"
          });
        }
      });
    });
  }

  list.addEventListener("scroll", updateStickiness, { passive: true });

  const post = (payload) => {
    const path = "inbox/" + Date.now() + "_" + Math.random().toString(36).slice(2, 7);
    window.$scene.get(path).put({ data: JSON.stringify(payload) });
  };

  const doSend = () => {
    const text = prompt.value.trim();
    if (!text) return;
    post({ type: "user_message", text, ts: Date.now() });
    prompt.value = "";
    prompt.focus();
    stickToBottom = true;
    scheduleScroll(true, true);
  };

  send.onclick = doSend;

  clear.onclick = () => {
    post({ type: "clear", ts: Date.now() });
    stickToBottom = true;
    scheduleScroll(true, false);
  };

  startAuto.onclick = () => {
    post({ type: "start_auto", ts: Date.now() });
    stickToBottom = true;
    scheduleScroll(true, true);
  };

  prompt.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      doSend();
    }
  });

  const observer = new MutationObserver(() => {
    scheduleScroll(false, false);
  });

  observer.observe(list, {
    childList: true,
    subtree: true,
    characterData: true
  });

  const resizeObserver = new ResizeObserver(() => {
    scheduleScroll(false, false);
  });
  resizeObserver.observe(list);

  setTimeout(() => {
    stickToBottom = true;
    scheduleScroll(true, false);
  }, 50);
})();
"""

MSG_SYSTEM = """
<div style="align-self:center;max-width:min(900px,92%);background:#1e293b;color:#cbd5e1;border:1px solid #334155;border-radius:999px;padding:8px 14px">
  <div data-bind-text="text" style="font-size:12px;line-height:1.5;white-space:pre-wrap;word-break:break-word;text-align:center"></div>
</div>
"""

MSG_USER = """
<div style="align-self:flex-end;max-width:min(820px,84%);min-width:160px;background:#1d4ed8;color:#eff6ff;border-radius:16px 16px 4px 16px;padding:12px 14px;box-shadow:0 2px 10px rgba(0,0,0,0.18);box-sizing:border-box">
  <div style="font-size:12px;opacity:0.9;margin-bottom:6px;min-height:16px"><span data-bind-text="meta"></span></div>
  <div data-bind-text="text" style="font-size:14px;line-height:1.5;white-space:pre-wrap;word-break:break-word;min-height:22px"></div>
</div>
"""

MSG_MODEL_A = """
<div style="align-self:flex-start;max-width:min(900px,88%);min-width:180px;background:#111827;color:#e5e7eb;border:1px solid #334155;border-radius:16px 16px 16px 4px;padding:12px 14px;box-shadow:0 2px 10px rgba(0,0,0,0.18);box-sizing:border-box">
  <div style="font-size:12px;color:#93c5fd;margin-bottom:6px;min-height:16px"><span data-bind-text="meta"></span></div>
  <div data-bind-text="text" style="font-size:14px;line-height:1.6;white-space:pre-wrap;word-break:break-word;min-height:22px"></div>
</div>
"""

MSG_MODEL_B = """
<div style="align-self:flex-start;max-width:min(900px,88%);min-width:180px;background:#172554;color:#dbeafe;border:1px solid #1d4ed8;border-radius:16px 16px 16px 4px;padding:12px 14px;box-shadow:0 2px 10px rgba(0,0,0,0.18);box-sizing:border-box">
  <div style="font-size:12px;color:#93c5fd;margin-bottom:6px;min-height:16px"><span data-bind-text="meta"></span></div>
  <div data-bind-text="text" style="font-size:14px;line-height:1.6;white-space:pre-wrap;word-break:break-word;min-height:22px"></div>
</div>
"""

MSG_ERROR = """
<div style="align-self:flex-start;max-width:min(900px,88%);min-width:180px;background:#3f0d12;color:#fecaca;border:1px solid #7f1d1d;border-radius:16px 16px 16px 4px;padding:12px 14px;box-sizing:border-box">
  <div style="font-size:12px;color:#fda4af;margin-bottom:6px;min-height:16px"><span data-bind-text="meta"></span></div>
  <div data-bind-text="text" style="font-size:14px;line-height:1.6;white-space:pre-wrap;word-break:break-word;min-height:22px"></div>
</div>
"""

hc.mount("root/chat", html=APP_HTML, js=APP_JS, fixed=True, layer=10)
hc.write("root/chat", status="Ready", model_a=MODEL_A, model_b=MODEL_B)

message_counter = 0
message_paths = []
seen = set()

auto_running = False
next_speaker = MODEL_A
last_auto_turn = 0.0

# [{"speaker": "...", "text": "..."}]
transcript = []

# model -> mounted message path currently being filled in
pending_paths = {
    MODEL_A: None,
    MODEL_B: None,
}


def new_message_path():
    global message_counter
    message_counter += 1
    path = f"root/chat/msg_{message_counter:06d}"
    message_paths.append(path)
    return path


def message_html_for(kind: str):
    if kind == "system":
        return MSG_SYSTEM
    if kind == "user":
        return MSG_USER
    if kind == "model_a":
        return MSG_MODEL_A
    if kind == "model_b":
        return MSG_MODEL_B
    return MSG_ERROR


def add_message(kind: str, meta: str = "", text: str = ""):
    path = new_message_path()
    hc.mount(path, html=message_html_for(kind), layer=5)

    if kind == "system":
        hc.write(path, text=text)
    else:
        hc.write(path, meta=meta, text=text)

    return path


def begin_model_bubble(model: str):
    kind = "model_a" if model == MODEL_A else "model_b"
    path = new_message_path()
    pending_paths[model] = path
    hc.mount(path, html=message_html_for(kind), layer=5)
    hc.write(path, meta=f"{model} · thinking…", text="…")
    return path


def finalize_model_bubble(model: str, text: str):
    path = pending_paths.get(model)
    if not path:
        # Fallback: create a normal bubble if something lost the pending path.
        kind = "model_a" if model == MODEL_A else "model_b"
        add_message(kind, model, text)
        return

    hc.write(path, meta=model, text=text)
    pending_paths[model] = None


def fail_model_bubble(model: str, text: str):
    path = pending_paths.get(model)
    if not path:
        add_message("error", "system", text)
        return

    hc.remove(path)
    pending_paths[model] = None
    add_message("error", "system", text)


def clear_messages_only():
    global message_counter, message_paths
    for path in message_paths:
        try:
            hc.remove(path)
        except Exception:
            pass
    message_counter = 0
    message_paths = []


def reset_app():
    global auto_running, next_speaker, last_auto_turn, transcript, pending_paths, seen
    clear_messages_only()
    hc.write("root/chat", status="Ready", model_a=MODEL_A, model_b=MODEL_B)
    auto_running = False
    next_speaker = MODEL_A
    last_auto_turn = 0.0
    transcript = []
    pending_paths = {
        MODEL_A: None,
        MODEL_B: None,
    }
    seen = set()


def build_prompt_for(model_name: str):
    other = MODEL_B if model_name == MODEL_A else MODEL_A
    recent = transcript[-12:]
    convo = "\n".join(f"{m['speaker']}: {m['text']}" for m in recent) if recent else "(no conversation yet)"

    system_text = (
        f"You are {model_name} in a 3-way chat with 'you' and '{other}'. "
        "Speak like one participant in the conversation. "
        "Reply in 1-3 sentences max. "
        "Do not prefix your answer with your name. "
        "Do not output JSON. "
        "Do not stay silent. "
        "Directly continue the conversation."
    )

    user_text = f"Conversation so far:\n{convo}\n\nNow reply as {model_name}."

    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]


def ollama_chat(model: str) -> str:
    messages = build_prompt_for(model)

    resp = requests.post(
        OLLAMA_CHAT_URL,
        json={
            "model": model,
            "messages": messages,
            "stream": False,
            "think": False,
            "keep_alive": "10m",
        },
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    msg = data.get("message", {}) or {}

    content = (msg.get("content") or "").strip()
    thinking = (msg.get("thinking") or "").strip()

    if content:
        return content
    if thinking:
        return thinking
    return ""


def add_user_turn(text: str):
    transcript.append({"speaker": "you", "text": text})
    add_message("user", "you", text)


def add_model_turn(model: str):
    global next_speaker, last_auto_turn, auto_running

    hc.write("root/chat", status=f"{model} is thinking...")
    begin_model_bubble(model)

    try:
        reply = ollama_chat(model)
    except Exception as e:
        fail_model_bubble(model, f"Ollama error: {e}")
        hc.write("root/chat", status="Ollama error")
        auto_running = False
        return

    if not reply:
        fail_model_bubble(model, f"{model} returned no visible text. Stopping auto mode.")
        hc.write("root/chat", status="Stopped: empty model reply")
        auto_running = False
        return

    transcript.append({"speaker": model, "text": reply})
    finalize_model_bubble(model, reply)

    next_speaker = MODEL_B if model == MODEL_A else MODEL_A
    last_auto_turn = time.time()
    hc.write("root/chat", status=f"Auto running · next: {next_speaker}")


while True:
    snap = hc.snapshot() or {}

    for k, v in snap.items():
        if not k.startswith("inbox/") or k in seen:
            continue

        seen.add(k)

        try:
            raw = v.get("data", {})
            msg = raw if isinstance(raw, dict) else json.loads(raw)
            action = msg.get("type")

            if action == "clear":
                reset_app()
                hc.remove(k)
                continue

            if action == "start_auto":
                if not auto_running:
                    add_message("system", text="Starting autonomous group chat...")
                    if not transcript:
                        transcript.append({"speaker": "you", "text": "Hey, both of you — start chatting."})
                        add_message("user", "you", "Hey, both of you — start chatting.")
                    auto_running = True
                    next_speaker = MODEL_A
                    last_auto_turn = 0.0
                    hc.write("root/chat", status=f"Auto running · next: {next_speaker}")
                hc.remove(k)
                continue

            if action == "user_message":
                user_text = (msg.get("text") or "").strip()
                if user_text:
                    add_user_turn(user_text)
                    auto_running = True
                    next_speaker = MODEL_A
                    last_auto_turn = 0.0
                    hc.write("root/chat", status=f"Auto running · next: {next_speaker}")
                hc.remove(k)
                continue

            hc.remove(k)

        except Exception as e:
            add_message("error", "system", f"Server error: {e}")
            hc.write("root/chat", status="Server error")
            hc.remove(k)

    if auto_running and (time.time() - last_auto_turn >= TURN_DELAY):
        add_model_turn(next_speaker)

    time.sleep(0.1)