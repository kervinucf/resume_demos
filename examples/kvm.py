#!/usr/bin/env python3
import ctypes
import json
import os
import platform
import queue
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn, TCPServer, StreamRequestHandler

# ============================================================
# CONFIG — edit on EACH machine.
# Same file on both machines, but ROLE / peer IP differ.
#
# IMPORTANT
# - INPUT_OWNER=True only on the machine the physical keyboard/mouse are attached to.
# - The other machine should use INPUT_OWNER=False.
# - Switch target with the UI buttons.
# - Emergency return-to-local hotkey while forwarding: F12
# ============================================================
ROLE = os.environ.get("KVM_ROLE", "mac").strip().lower()  # mac | windows
INPUT_OWNER = os.environ.get("KVM_INPUT_OWNER", "true" if ROLE == "mac" else "false").strip().lower() in {"1", "true", "yes", "on"}

RELAY_URL = os.environ.get("KVM_RELAY_URL", "http://localhost:8765")
ROOT_NAME = os.environ.get("KVM_ROOT_NAME", f"hyper_kvm_{ROLE}")

CONTROL_HOST = os.environ.get("KVM_CONTROL_HOST", "0.0.0.0")
CONTROL_PORT = int(os.environ.get("KVM_CONTROL_PORT", "8766" if ROLE == "mac" else "8767"))
INPUT_PORT = int(os.environ.get("KVM_INPUT_PORT", "9966" if ROLE == "mac" else "9967"))
AUTH_TOKEN = os.environ.get("KVM_AUTH_TOKEN", "change-me")

PEER_HOST = os.environ.get("KVM_PEER_HOST", "192.168.1.50" if ROLE == "mac" else "192.168.1.40")
PEER_CONTROL_PORT = int(os.environ.get("KVM_PEER_CONTROL_PORT", "8767" if ROLE == "mac" else "8766"))
PEER_INPUT_PORT = int(os.environ.get("KVM_PEER_INPUT_PORT", "9967" if ROLE == "mac" else "9966"))
PEER_LABEL = os.environ.get("KVM_PEER_LABEL", "Windows" if ROLE == "mac" else "Mac")

SUPPRESS_LOCAL_WHEN_FORWARDING = os.environ.get("KVM_SUPPRESS_LOCAL", "true").strip().lower() in {"1", "true", "yes", "on"}
CAPTURE_MOUSE = os.environ.get("KVM_CAPTURE_MOUSE", "true").strip().lower() in {"1", "true", "yes", "on"}
MOUSE_MOVE_MIN_INTERVAL = float(os.environ.get("KVM_MOUSE_INTERVAL", "0.010"))
PEER_TIMEOUT_SECONDS = float(os.environ.get("KVM_PEER_TIMEOUT", "1.0"))
HEARTBEAT_INTERVAL_SECONDS = float(os.environ.get("KVM_HEARTBEAT", "3.0"))
LOOP_SLEEP_SECONDS = float(os.environ.get("KVM_LOOP_SLEEP", "0.08"))
MAX_LOG_LINES = int(os.environ.get("KVM_MAX_LOG_LINES", "24"))
EMERGENCY_RETURN_KEY = os.environ.get("KVM_RETURN_KEY", "f12").strip().lower()

if ROLE not in {"mac", "windows"}:
    raise SystemExit("ROLE must be 'mac' or 'windows'")

IS_MAC = ROLE == "mac"
IS_WINDOWS = ROLE == "windows"
LOCAL_LABEL = "Mac" if IS_MAC else "Windows"
PEER_ROLE = "windows" if IS_MAC else "mac"

# ============================================================
# Environment bootstrap
# ============================================================
def ensure_node_path() -> str | None:
    candidates = [shutil.which("node"), "/opt/homebrew/bin/node", "/usr/local/bin/node"]
    nvm_root = Path.home() / ".nvm/versions/node"
    if nvm_root.exists():
        try:
            for version_dir in sorted(nvm_root.iterdir(), reverse=True):
                candidates.append(str(version_dir / "bin" / "node"))
        except Exception:
            pass

    for cand in candidates:
        if cand and Path(cand).exists():
            node_path = str(Path(cand).resolve())
            node_dir = str(Path(node_path).parent)
            cur = os.environ.get("PATH", "")
            parts = [p for p in cur.split(os.pathsep) if p]
            if node_dir not in parts:
                os.environ["PATH"] = os.pathsep.join([node_dir] + parts)
            return node_path
    return None


def enable_windows_dpi_awareness() -> None:
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


ensure_node_path()
enable_windows_dpi_awareness()

try:
    from HyperCoreSDK import HyperClient
except Exception:
    from HyperCoreSDK.client import HyperClient

from pynput import keyboard as pk
from pynput import mouse as pm

hc = HyperClient(relay=RELAY_URL, root=ROOT_NAME)

# ============================================================
# State
# ============================================================
state_lock = threading.RLock()
log_lines = deque(maxlen=MAX_LOG_LINES)
processed_actions = deque(maxlen=500)
processed_actions_set = set()
ui_dirty = True

state = {
    "target_role": ROLE,
    "peer_online": False,
    "peer_status": "Unknown",
    "peer_seen": "Never",
    "forwarding": False,
    "capture": "Idle",
    "transport": "Disconnected",
    "last_error": "None",
    "last_action": "Starting",
}

shutdown_event = threading.Event()
outgoing_events: queue.Queue[dict] = queue.Queue(maxsize=4000)
peer_socket_lock = threading.Lock()
peer_socket = None
keyboard_listener = None
mouse_listener = None
last_mouse_move_sent = 0.0

key_controller = pk.Controller()
mouse_controller = pm.Controller()

# ============================================================
# UI
# ============================================================
APP_HTML = """
<div style="width:100%;height:100%;display:flex;flex-direction:column;background:#0f172a;color:#e2e8f0;font-family:Arial,sans-serif">
  <div style="padding:18px 20px;border-bottom:1px solid #334155;background:#111827;display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap">
    <div>
      <div style="font-size:12px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Peer KVM Switcher</div>
      <div data-bind-text="title" style="font-size:24px;font-weight:800;margin-top:6px"></div>
      <div data-bind-text="subtitle" style="font-size:13px;color:#cbd5e1;margin-top:6px;line-height:1.45"></div>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <button id="btn_mac" style="padding:12px 18px;background:#2563eb;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:700">Use Mac</button>
      <button id="btn_windows" style="padding:12px 18px;background:#16a34a;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:700">Use Windows</button>
      <button id="btn_local" style="padding:12px 18px;background:#475569;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:700">Return Local</button>
      <button id="btn_ping" style="padding:12px 18px;background:#7c3aed;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:700">Peer Ping</button>
    </div>
  </div>

  <div style="display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:12px;padding:16px 20px;border-bottom:1px solid #334155;background:#0b1220">
    <div style="background:#111827;border:1px solid #334155;border-radius:10px;padding:12px">
      <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">This machine</div>
      <div data-bind-text="local_label" style="font-size:22px;font-weight:800;margin-top:6px"></div>
    </div>
    <div style="background:#111827;border:1px solid #334155;border-radius:10px;padding:12px">
      <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Input owner</div>
      <div data-bind-text="input_owner" style="font-size:22px;font-weight:800;margin-top:6px"></div>
    </div>
    <div style="background:#111827;border:1px solid #334155;border-radius:10px;padding:12px">
      <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Active target</div>
      <div data-bind-text="active_target" style="font-size:22px;font-weight:800;margin-top:6px"></div>
    </div>
    <div style="background:#111827;border:1px solid #334155;border-radius:10px;padding:12px">
      <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Forwarding</div>
      <div data-bind-text="forwarding" style="font-size:14px;font-weight:800;margin-top:6px;line-height:1.35"></div>
    </div>
    <div style="background:#111827;border:1px solid #334155;border-radius:10px;padding:12px">
      <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Peer</div>
      <div data-bind-text="peer_status" style="font-size:14px;font-weight:800;margin-top:6px;line-height:1.35"></div>
    </div>
    <div style="background:#111827;border:1px solid #334155;border-radius:10px;padding:12px">
      <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Transport</div>
      <div data-bind-text="transport" style="font-size:14px;font-weight:800;margin-top:6px;line-height:1.35"></div>
    </div>
  </div>

  <div style="padding:14px 20px;border-bottom:1px solid #334155;background:#111827;display:flex;flex-direction:column;gap:8px">
    <div data-bind-text="help_text" style="font-size:13px;color:#cbd5e1;line-height:1.55;white-space:pre-wrap"></div>
    <div data-bind-text="error_text" style="font-size:12px;color:#fca5a5;line-height:1.5;white-space:pre-wrap"></div>
  </div>

  <div style="padding:12px 20px 8px 20px;font-size:12px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Recent activity</div>
  <pre data-bind-text="activity" style="flex:1;min-height:0;overflow:auto;margin:0 20px 20px 20px;padding:14px;background:#111827;border:1px solid #334155;border-radius:10px;color:#cbd5e1;font-size:12px;line-height:1.5;white-space:pre-wrap"></pre>
</div>
"""

APP_JS = r"""
(function(){
  const macBtn = document.getElementById("btn_mac");
  const winBtn = document.getElementById("btn_windows");
  const localBtn = document.getElementById("btn_local");
  const pingBtn = document.getElementById("btn_ping");
  if (!macBtn || !winBtn || !localBtn || !pingBtn || macBtn.dataset.on) return;
  macBtn.dataset.on = "1";

  window.sendAction = (type, payload = {}) => {
    const path = "inbox/" + Date.now() + "_" + Math.random().toString(36).slice(2, 7);
    window.$scene.get(path).put({
      data: JSON.stringify({ type, ...payload, ts: Date.now() })
    });
  };

  macBtn.onclick = () => window.sendAction("switch_target", { target: "mac" });
  winBtn.onclick = () => window.sendAction("switch_target", { target: "windows" });
  localBtn.onclick = () => window.sendAction("return_local");
  pingBtn.onclick = () => window.sendAction("probe_peer");
})();
"""

# ============================================================
# Helpers
# ============================================================
def now_label() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    global ui_dirty
    with state_lock:
        log_lines.appendleft(f"[{now_label()}] {message}")
        ui_dirty = True
    print(message, flush=True)


def set_state(**kwargs) -> None:
    global ui_dirty
    with state_lock:
        for k, v in kwargs.items():
            state[k] = v
        ui_dirty = True


def get_screen_size() -> tuple[int, int]:
    if IS_WINDOWS:
        try:
            user32 = ctypes.windll.user32
            return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
        except Exception:
            pass
    if IS_MAC:
        try:
            from AppKit import NSScreen  # type: ignore
            frame = NSScreen.mainScreen().frame()
            return int(frame.size.width), int(frame.size.height)
        except Exception:
            pass
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        w = int(root.winfo_screenwidth())
        h = int(root.winfo_screenheight())
        root.destroy()
        return w, h
    except Exception:
        return 1920, 1080


SCREEN_W, SCREEN_H = get_screen_size()


def key_token(key) -> str:
    if isinstance(key, pk.KeyCode):
        if key.char:
            return key.char.lower()
        if key.vk is not None:
            return f"vk:{key.vk}"
        return "keycode"
    if isinstance(key, pk.Key):
        name = getattr(key, "name", None)
        if name in {"ctrl_l", "ctrl_r", "ctrl"}:
            return "ctrl"
        if name in {"alt_l", "alt_r", "alt", "alt_gr"}:
            return "alt"
        if name in {"cmd", "cmd_l", "cmd_r"}:
            return "cmd"
        if name in {"shift", "shift_l", "shift_r"}:
            return "shift"
        return name or str(key)
    return str(key)


def serialize_key(key) -> dict:
    if isinstance(key, pk.KeyCode):
        if key.char is not None:
            return {"kind": "char", "char": key.char}
        if key.vk is not None:
            return {"kind": "vk", "vk": int(key.vk)}
        raise ValueError("Unsupported KeyCode")
    if isinstance(key, pk.Key):
        return {"kind": "special", "name": getattr(key, "name", str(key))}
    raise ValueError(f"Unsupported key type: {type(key)}")


def deserialize_key(data: dict):
    kind = data.get("kind")
    if kind == "char":
        return pk.KeyCode.from_char(data["char"])
    if kind == "vk":
        return pk.KeyCode.from_vk(int(data["vk"]))
    if kind == "special":
        name = data["name"]
        if hasattr(pk.Key, name):
            return getattr(pk.Key, name)
        raise ValueError(f"Unknown special key: {name}")
    raise ValueError(f"Unknown key kind: {kind}")


def serialize_button(button) -> str:
    return getattr(button, "name", str(button).split(".")[-1])


def deserialize_button(name: str):
    if hasattr(pm.Button, name):
        return getattr(pm.Button, name)
    raise ValueError(f"Unknown mouse button: {name}")


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {AUTH_TOKEN}", "Content-Type": "application/json"}


def urlopen_json(url: str, method: str = "GET", payload: dict | None = None, timeout: float = PEER_TIMEOUT_SECONDS) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers.update(auth_headers())
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    if payload is None and AUTH_TOKEN:
        req.add_header("Authorization", f"Bearer {AUTH_TOKEN}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body else {}


def check_auth(header_value: str | None) -> bool:
    if not AUTH_TOKEN:
        return True
    return header_value == f"Bearer {AUTH_TOKEN}"


def peer_control_url(path: str) -> str:
    return f"http://{PEER_HOST}:{PEER_CONTROL_PORT}{path}"


def current_target_label() -> str:
    with state_lock:
        return "Mac" if state["target_role"] == "mac" else "Windows"

# ============================================================
# Capture + injection
# ============================================================
def clear_outgoing_queue() -> None:
    while True:
        try:
            outgoing_events.get_nowait()
        except queue.Empty:
            break


def close_peer_socket() -> None:
    global peer_socket
    with peer_socket_lock:
        if peer_socket is not None:
            try:
                peer_socket.close()
            except Exception:
                pass
            peer_socket = None
    set_state(transport="Disconnected")


def ensure_peer_socket() -> socket.socket:
    global peer_socket
    with peer_socket_lock:
        if peer_socket is not None:
            return peer_socket
        sock = socket.create_connection((PEER_HOST, PEER_INPUT_PORT), timeout=PEER_TIMEOUT_SECONDS)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        peer_socket = sock
        set_state(transport=f"Connected → {PEER_LABEL}:{PEER_INPUT_PORT}")
        return peer_socket


def send_event_nonblocking(event: dict) -> None:
    try:
        outgoing_events.put_nowait(event)
    except queue.Full:
        if event.get("t") != "mm":
            log("Dropped input event because outgoing queue is full")


def sender_worker() -> None:
    while not shutdown_event.is_set():
        try:
            event = outgoing_events.get(timeout=0.25)
        except queue.Empty:
            continue

        with state_lock:
            should_forward = INPUT_OWNER and state["target_role"] != ROLE

        if not should_forward:
            continue

        try:
            sock = ensure_peer_socket()
            payload = (json.dumps(event) + "\n").encode("utf-8")
            sock.sendall(payload)
        except Exception as exc:
            close_peer_socket()
            set_state(last_error=f"Send failed: {exc}", transport="Disconnected")
            log(f"Input transport error: {exc}")


def inject_event(event: dict) -> None:
    t = event.get("t")
    if t == "kp":
        key_controller.press(deserialize_key(event["key"]))
    elif t == "kr":
        key_controller.release(deserialize_key(event["key"]))
    elif t == "mm":
        nx = max(0.0, min(1.0, float(event.get("nx", 0.0))))
        ny = max(0.0, min(1.0, float(event.get("ny", 0.0))))
        x = int(nx * max(1, SCREEN_W - 1))
        y = int(ny * max(1, SCREEN_H - 1))
        mouse_controller.position = (x, y)
    elif t == "mc":
        nx = max(0.0, min(1.0, float(event.get("nx", 0.0))))
        ny = max(0.0, min(1.0, float(event.get("ny", 0.0))))
        mouse_controller.position = (int(nx * max(1, SCREEN_W - 1)), int(ny * max(1, SCREEN_H - 1)))
        button = deserialize_button(event["button"])
        if event.get("pressed"):
            mouse_controller.press(button)
        else:
            mouse_controller.release(button)
    elif t == "ms":
        mouse_controller.scroll(int(event.get("dx", 0)), int(event.get("dy", 0)))
    else:
        raise ValueError(f"Unknown event type: {t}")


class InputHandler(StreamRequestHandler):
    def handle(self):
        while not shutdown_event.is_set():
            line = self.rfile.readline()
            if not line:
                break
            try:
                event = json.loads(line.decode("utf-8"))
                inject_event(event)
            except Exception as exc:
                log(f"Inject error: {exc}")


class ThreadedTCPServer(ThreadingMixIn, TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_input_server() -> ThreadedTCPServer:
    server = ThreadedTCPServer((CONTROL_HOST, INPUT_PORT), InputHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


capture_pressed_tokens = set()


def on_press(key):
    token = key_token(key)
    capture_pressed_tokens.add(token)

    if token == EMERGENCY_RETURN_KEY:
        log(f"Emergency return hotkey ({EMERGENCY_RETURN_KEY})")
        switch_target(ROLE, source="hotkey", notify_peer=True)
        return

    try:
        payload = {"t": "kp", "key": serialize_key(key)}
    except Exception:
        return
    send_event_nonblocking(payload)


def on_release(key):
    token = key_token(key)
    capture_pressed_tokens.discard(token)

    if token == EMERGENCY_RETURN_KEY:
        return

    try:
        payload = {"t": "kr", "key": serialize_key(key)}
    except Exception:
        return
    send_event_nonblocking(payload)


def on_move(x, y):
    global last_mouse_move_sent
    now = time.monotonic()
    if (now - last_mouse_move_sent) < MOUSE_MOVE_MIN_INTERVAL:
        return
    last_mouse_move_sent = now
    send_event_nonblocking({
        "t": "mm",
        "nx": max(0.0, min(1.0, float(x) / max(1, SCREEN_W - 1))),
        "ny": max(0.0, min(1.0, float(y) / max(1, SCREEN_H - 1))),
    })


def on_click(x, y, button, pressed):
    send_event_nonblocking({
        "t": "mc",
        "nx": max(0.0, min(1.0, float(x) / max(1, SCREEN_W - 1))),
        "ny": max(0.0, min(1.0, float(y) / max(1, SCREEN_H - 1))),
        "button": serialize_button(button),
        "pressed": bool(pressed),
    })


def on_scroll(x, y, dx, dy):
    send_event_nonblocking({"t": "ms", "dx": int(dx), "dy": int(dy)})


def stop_listeners() -> None:
    global keyboard_listener, mouse_listener
    if keyboard_listener is not None:
        try:
            keyboard_listener.stop()
        except Exception:
            pass
        keyboard_listener = None
    if mouse_listener is not None:
        try:
            mouse_listener.stop()
        except Exception:
            pass
        mouse_listener = None
    capture_pressed_tokens.clear()
    clear_outgoing_queue()
    close_peer_socket()


def start_listeners() -> None:
    global keyboard_listener, mouse_listener
    stop_listeners()
    keyboard_listener = pk.Listener(on_press=on_press, on_release=on_release, suppress=SUPPRESS_LOCAL_WHEN_FORWARDING)
    keyboard_listener.start()
    if CAPTURE_MOUSE:
        mouse_listener = pm.Listener(on_move=on_move, on_click=on_click, on_scroll=on_scroll, suppress=SUPPRESS_LOCAL_WHEN_FORWARDING)
        mouse_listener.start()


def sync_capture_mode() -> None:
    with state_lock:
        should_forward = INPUT_OWNER and state["target_role"] != ROLE
    if should_forward:
        if keyboard_listener is None:
            try:
                start_listeners()
                set_state(capture=("Forwarding (suppressed)" if SUPPRESS_LOCAL_WHEN_FORWARDING else "Forwarding (not suppressed)"), forwarding=True)
                log(f"Forwarding local input to {PEER_LABEL}")
            except Exception as exc:
                set_state(capture="Capture failed", forwarding=False, last_error=str(exc))
                log(f"Failed to start capture: {exc}")
        else:
            set_state(forwarding=True)
    else:
        if keyboard_listener is not None or mouse_listener is not None:
            stop_listeners()
            log("Stopped forwarding; local machine owns input")
        set_state(capture="Local only", forwarding=False)

# ============================================================
# Peer control server
# ============================================================
def public_status() -> dict:
    with state_lock:
        return {
            "ok": True,
            "role": ROLE,
            "label": LOCAL_LABEL,
            "target": state["target_role"],
            "input_owner": INPUT_OWNER,
            "forwarding": state["forwarding"],
            "capture": state["capture"],
        }


class ControlHandler(BaseHTTPRequestHandler):
    server_version = "KVMControl/1.0"

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return

    def do_GET(self):
        if not check_auth(self.headers.get("Authorization")):
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        if self.path == "/ping":
            self._json(200, public_status())
            return
        self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        if not check_auth(self.headers.get("Authorization")):
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            self._json(400, {"ok": False, "error": "bad_json"})
            return

        if self.path == "/intent":
            target = str(payload.get("target", "")).strip().lower()
            if target not in {"mac", "windows"}:
                self._json(400, {"ok": False, "error": "bad_target"})
                return
            switch_target(target, source="peer", notify_peer=False)
            self._json(200, {"ok": True, "applied": target})
            return

        self._json(404, {"ok": False, "error": "not_found"})


def start_control_server() -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((CONTROL_HOST, CONTROL_PORT), ControlHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd

# ============================================================
# Peer operations
# ============================================================
def ping_peer() -> bool:
    try:
        resp = urlopen_json(peer_control_url("/ping"), method="GET")
        label = resp.get("label") or PEER_LABEL
        target = resp.get("target", "unknown")
        set_state(peer_online=True, peer_status=f"Online ({label})\nTarget: {target}", peer_seen=now_label())
        return True
    except Exception as exc:
        set_state(peer_online=False, peer_status=f"Offline\n{exc}")
        return False


def send_peer_intent(target: str) -> bool:
    try:
        urlopen_json(peer_control_url("/intent"), method="POST", payload={"target": target})
        return True
    except Exception as exc:
        set_state(last_error=f"Peer intent failed: {exc}")
        log(f"Peer intent failed: {exc}")
        return False


def heartbeat_worker() -> None:
    while not shutdown_event.is_set():
        ping_peer()
        shutdown_event.wait(HEARTBEAT_INTERVAL_SECONDS)

# ============================================================
# Core switching logic
# ============================================================
def switch_target(target: str, source: str = "local", notify_peer: bool = True) -> None:
    if target not in {"mac", "windows"}:
        return

    set_state(target_role=target, last_action=f"Switch → {target} ({source})")
    sync_capture_mode()

    if source != "peer" and notify_peer:
        ok = send_peer_intent(target)
        if ok:
            log(f"Asked peer to switch to {target}")


def return_local() -> None:
    switch_target(ROLE, source="local", notify_peer=True)

# ============================================================
# HyperCore rendering
# ============================================================
def render_ui(force: bool = False) -> None:
    global ui_dirty
    with state_lock:
        dirty = ui_dirty
        ui_dirty = False
        snapshot = dict(state)
        activity = "\n".join(log_lines) if log_lines else f"[{now_label()}] Ready"

    if not (force or dirty):
        return

    target_label = "Mac" if snapshot["target_role"] == "mac" else "Windows"
    forwarding_text = "Active" if snapshot["forwarding"] else "Local"
    help_text = (
        f"Physical keyboard/mouse attached here: {'Yes' if INPUT_OWNER else 'No'}\n"
        f"When target is this machine, input stays local. When target is {PEER_LABEL} and this machine is the input owner, keyboard/mouse events are forwarded to the peer.\n"
        f"Emergency return-to-local hotkey while forwarding: {EMERGENCY_RETURN_KEY.upper()}\n"
        f"Local control server: http://{CONTROL_HOST}:{CONTROL_PORT}\n"
        f"Local input server: tcp://{CONTROL_HOST}:{INPUT_PORT}\n"
        f"Peer: {PEER_LABEL} @ http://{PEER_HOST}:{PEER_CONTROL_PORT} and tcp://{PEER_HOST}:{PEER_INPUT_PORT}"
    )
    error_text = f"Capture: {snapshot['capture']}\nLast error: {snapshot['last_error']}"

    hc.write(
        "root/app",
        title=f"{LOCAL_LABEL} KVM",
        subtitle="One keyboard, two peers. Switch target; do not rely on Bluetooth host stealing.",
        local_label=LOCAL_LABEL,
        input_owner=("Yes" if INPUT_OWNER else "No"),
        active_target=target_label,
        forwarding=forwarding_text,
        peer_status=snapshot["peer_status"],
        transport=snapshot["transport"],
        help_text=help_text,
        error_text=error_text,
        activity=activity,
    )

# ============================================================
# HyperCore action loop
# ============================================================
def parse_action(v) -> dict:
    raw = v.get("data", {}) if isinstance(v, dict) else {}
    return json.loads(raw) if isinstance(raw, str) else raw


def action_loop() -> None:
    while not shutdown_event.is_set():
        try:
            snap = hc.snapshot() or {}
        except Exception:
            snap = {}

        for k, v in list(snap.items()):
            if not k.startswith("inbox/"):
                continue
            if k in processed_actions_set:
                try:
                    hc.remove(k)
                except Exception:
                    pass
                continue

            processed_actions.append(k)
            processed_actions_set.add(k)
            while len(processed_actions) > processed_actions.maxlen:
                old = processed_actions.popleft()
                processed_actions_set.discard(old)

            try:
                msg = parse_action(v)
                action = msg.get("type")
                if action == "switch_target":
                    target = str(msg.get("target", "")).strip().lower()
                    log(f"UI: switch to {target}")
                    switch_target(target, source="ui", notify_peer=True)
                elif action == "return_local":
                    log("UI: return local")
                    return_local()
                elif action == "probe_peer":
                    ok = ping_peer()
                    log("Peer ping ok" if ok else "Peer ping failed")
            except Exception as exc:
                set_state(last_error=f"Action error: {exc}")
                log(f"Action error: {exc}")
            finally:
                try:
                    hc.remove(k)
                except Exception:
                    pass

        render_ui()
        time.sleep(LOOP_SLEEP_SECONDS)

# ============================================================
# Main
# ============================================================
def main() -> None:
    hc.start_relay()
    hc.clear()
    hc.mount("root/app", html=APP_HTML, js=APP_JS, fixed=True, layer=10)

    log(f"Starting {LOCAL_LABEL} KVM | input_owner={INPUT_OWNER}")
    log(f"Control server on http://{CONTROL_HOST}:{CONTROL_PORT}")
    log(f"Input server on tcp://{CONTROL_HOST}:{INPUT_PORT}")
    log(f"Peer configured as {PEER_LABEL} @ {PEER_HOST}")
    if IS_MAC:
        log("macOS: grant Accessibility permission to your terminal/Python before forwarding input.")

    control_server = start_control_server()
    input_server = start_input_server()
    sender_thread = threading.Thread(target=sender_worker, daemon=True)
    sender_thread.start()
    heartbeat_thread = threading.Thread(target=heartbeat_worker, daemon=True)
    heartbeat_thread.start()

    set_state(capture="Local only", transport="Disconnected")
    render_ui(force=True)

    try:
        action_loop()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_event.set()
        stop_listeners()
        close_peer_socket()
        try:
            control_server.shutdown()
            control_server.server_close()
        except Exception:
            pass
        try:
            input_server.shutdown()
            input_server.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
