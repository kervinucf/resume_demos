#!/usr/bin/env python3
import json
import os
import platform
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

try:
    from HyperCoreSDK import HyperClient
except Exception:
    from HyperCoreSDK.client import HyperClient

# ============================================================
# CONFIG — edit these values on EACH machine before running.
# Run the same script on both computers.
# ============================================================
RELAY_URL = os.environ.get("BT_RELAY_URL", "http://localhost:8765")
ROOT_NAME = os.environ.get("BT_ROOT_NAME", "bt_switch_shared")
SERVER_HOST = os.environ.get("BT_SERVER_HOST", "0.0.0.0")
AUTH_TOKEN = os.environ.get("BT_AUTH_TOKEN", "change-me")


ROLE = "windows"
SERVER_PORT = 8767

PEER_LABEL = "Mac"
PEER_HOST = "192.168.1.40"   # your Mac IP
PEER_PORT = 8766

MAC_DEVICE_ADDRESSES = []

WINDOWS_CONNECT_COMMANDS = [
    # optional vendor/custom commands here
]

WINDOWS_DISCONNECT_COMMANDS = [
    # optional vendor/custom commands here
]

# Optional: force a target role button to also open Bluetooth settings on that machine.
OPEN_WINDOWS_BLUETOOTH_SETTINGS_WHEN_NO_COMMANDS = True
MAX_LOGS = 12

# Optional: point directly at node if your IDE PATH is broken.
# Leave blank to auto-detect.
NODE_BIN = os.environ.get("BT_NODE_BIN", "")


# ============================================================
# APP STATE
# ============================================================
IS_MAC = ROLE == "mac"
IS_WINDOWS = ROLE == "windows"
LOCAL_LABEL = "Mac" if IS_MAC else "Windows"
LOCAL_PLATFORM = platform.system()

hc = HyperClient(relay=RELAY_URL, root=ROOT_NAME)
server_events = []
server_events_lock = threading.Lock()
log_items = []
active_target = "Unknown"
last_result = "Idle"
last_peer_result = "Idle"
last_local_result = "Idle"
health_text = "Ready"


# ============================================================
# HYPERCORE UI
# ============================================================
APP_HTML = """
<div style="width:100%;height:100%;display:flex;flex-direction:column;background:#0f172a;color:#e2e8f0;font-family:Arial,sans-serif">
  <div style="padding:18px 20px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap">
    <div>
      <div style="font-size:12px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Bluetooth Hot Switch</div>
      <div data-bind-text="header_title" style="font-size:24px;font-weight:700;margin-top:4px"></div>
      <div data-bind-text="header_subtitle" style="font-size:13px;color:#cbd5e1;margin-top:6px"></div>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <button id="btn_mac" style="padding:12px 18px;background:#2563eb;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:700">Switch to Mac</button>
      <button id="btn_windows" style="padding:12px 18px;background:#16a34a;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:700">Switch to Windows</button>
      <button id="btn_release" style="padding:12px 18px;background:#475569;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:700">Release This Machine</button>
      <button id="btn_probe" style="padding:12px 18px;background:#7c3aed;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:700">Peer Ping</button>
    </div>
  </div>

  <div style="display:grid;grid-template-columns:repeat(4,minmax(180px,1fr));gap:12px;padding:16px 20px;border-bottom:1px solid #334155">
    <div style="background:#111827;border:1px solid #334155;border-radius:10px;padding:14px">
      <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">This machine</div>
      <div data-bind-text="local_label" style="font-size:22px;font-weight:700;margin-top:6px"></div>
    </div>
    <div style="background:#111827;border:1px solid #334155;border-radius:10px;padding:14px">
      <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Active target</div>
      <div data-bind-text="active_target" style="font-size:22px;font-weight:700;margin-top:6px"></div>
    </div>
    <div style="background:#111827;border:1px solid #334155;border-radius:10px;padding:14px">
      <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Local result</div>
      <div data-bind-text="last_local_result" style="font-size:14px;font-weight:700;margin-top:6px;line-height:1.35"></div>
    </div>
    <div style="background:#111827;border:1px solid #334155;border-radius:10px;padding:14px">
      <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Peer result</div>
      <div data-bind-text="last_peer_result" style="font-size:14px;font-weight:700;margin-top:6px;line-height:1.35"></div>
    </div>
  </div>

  <div style="padding:16px 20px;border-bottom:1px solid #334155;display:flex;flex-direction:column;gap:8px;background:#111827">
    <div data-bind-text="health_text" style="font-size:14px;color:#e2e8f0"></div>
    <div data-bind-text="config_text" style="font-size:12px;color:#94a3b8;line-height:1.5;white-space:pre-wrap"></div>
  </div>

  <div style="padding:16px 20px 8px 20px;font-size:12px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Activity</div>
  <div data-children style="flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:8px;padding:0 20px 20px 20px"></div>
</div>
"""

APP_JS = r"""
(function(){
  const macBtn = document.getElementById("btn_mac");
  const winBtn = document.getElementById("btn_windows");
  const releaseBtn = document.getElementById("btn_release");
  const probeBtn = document.getElementById("btn_probe");
  if (!macBtn || !winBtn || !releaseBtn || !probeBtn || macBtn.dataset.on) return;
  macBtn.dataset.on = 1;

  window.sendAction = (type, target = null) => {
    const path = "inbox/" + Date.now() + "_" + Math.random().toString(36).slice(2,7);
    window.$scene.get(path).put({
      data: JSON.stringify({ type, target, timestamp: Date.now() })
    });
  };

  macBtn.onclick = () => window.sendAction("switch", "mac");
  winBtn.onclick = () => window.sendAction("switch", "windows");
  releaseBtn.onclick = () => window.sendAction("release_local");
  probeBtn.onclick = () => window.sendAction("probe_peer");
})();
"""

LOG_HTML = """
<div style="background:#111827;border:1px solid #334155;border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:6px">
  <div data-bind-text="title" style="font-size:13px;font-weight:700;color:#f8fafc"></div>
  <div data-bind-text="detail" style="font-size:12px;color:#cbd5e1;line-height:1.45;white-space:pre-wrap"></div>
</div>
"""


# ============================================================
# UTILITIES
# ============================================================
def shell_join(parts):
    return " ".join(parts)


def run_command(parts):
    try:
        completed = subprocess.run(parts, capture_output=True, text=True, timeout=20)
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        ok = completed.returncode == 0
        text = stdout or stderr or f"exit={completed.returncode}"
        return ok, text
    except FileNotFoundError:
        return False, f"Command not found: {parts[0]}"
    except subprocess.TimeoutExpired:
        return False, f"Timed out: {shell_join(parts)}"
    except Exception as e:
        return False, str(e)


def post_json(url, payload, timeout=4):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def local_timestamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def add_log(title, detail):
    global log_items
    item_id = f"log_{uuid.uuid4().hex[:10]}"
    path = f"root/app/{item_id}"
    hc.mount(path, html=LOG_HTML, layer=5)
    hc.write(path, title=f"[{local_timestamp()}] {title}", detail=detail)
    log_items.append(path)
    while len(log_items) > MAX_LOGS:
        old = log_items.pop(0)
        hc.remove(old)


def push_ui():
    config_lines = [
        f"Role: {ROLE}",
        f"Platform: {LOCAL_PLATFORM}",
        f"Peer: {PEER_LABEL} @ http://{PEER_HOST}:{PEER_PORT}",
        f"Relay: {RELAY_URL}",
        f"HTTP server: http://{SERVER_HOST}:{SERVER_PORT}",
    ]
    if IS_MAC:
        config_lines.append(f"Configured macOS Bluetooth devices: {len(MAC_DEVICE_ADDRESSES)}")
    else:
        config_lines.append(f"Configured Windows connect commands: {len(WINDOWS_CONNECT_COMMANDS)}")
        config_lines.append(f"Configured Windows disconnect commands: {len(WINDOWS_DISCONNECT_COMMANDS)}")

    hc.write(
        "root/app",
        header_title=f"{LOCAL_LABEL} controller",
        header_subtitle="Run this same script on both computers. Buttons call the local machine and the peer machine.",
        local_label=LOCAL_LABEL,
        active_target=active_target,
        last_local_result=last_local_result,
        last_peer_result=last_peer_result,
        health_text=health_text,
        config_text="\n".join(config_lines),
    )


# ============================================================
# LOCAL DEVICE CONTROL
# ============================================================
def ensure_blueutil():
    return run_command(["blueutil", "--version"])


def mac_release():
    if not MAC_DEVICE_ADDRESSES:
        return False, "No MAC_DEVICE_ADDRESSES configured."

    ok, msg = ensure_blueutil()
    if not ok:
        return False, "blueutil is required on macOS. Install it first. " + msg

    ok_all = True
    messages = []
    for addr in MAC_DEVICE_ADDRESSES:
        ok, text = run_command(["blueutil", "--disconnect", addr])
        label = DEVICE_LABELS.get(addr, addr)
        messages.append(f"disconnect {label}: {'ok' if ok else 'failed'} — {text}")
        ok_all = ok_all and ok
    return ok_all, "\n".join(messages)


def mac_acquire():
    if not MAC_DEVICE_ADDRESSES:
        return False, "No MAC_DEVICE_ADDRESSES configured."

    ok, msg = ensure_blueutil()
    if not ok:
        return False, "blueutil is required on macOS. Install it first. " + msg

    ok_all = True
    messages = []
    for addr in MAC_DEVICE_ADDRESSES:
        ok, text = run_command(["blueutil", "--connect", addr])
        label = DEVICE_LABELS.get(addr, addr)
        messages.append(f"connect {label}: {'ok' if ok else 'failed'} — {text}")
        ok_all = ok_all and ok
    return ok_all, "\n".join(messages)


def open_windows_bluetooth_settings():
    return run_command(["powershell", "-NoProfile", "-Command", "Start-Process 'ms-settings:bluetooth'"])


def run_windows_command_list(command_list, empty_message):
    if not command_list:
        if OPEN_WINDOWS_BLUETOOTH_SETTINGS_WHEN_NO_COMMANDS:
            ok, text = open_windows_bluetooth_settings()
            return ok, empty_message + f" Opened Bluetooth settings. {text}"
        return False, empty_message

    ok_all = True
    messages = []
    for cmd in command_list:
        ok, text = run_command(cmd)
        messages.append(f"{shell_join(cmd)} => {'ok' if ok else 'failed'} — {text}")
        ok_all = ok_all and ok
    return ok_all, "\n".join(messages)


def windows_release():
    return run_windows_command_list(
        WINDOWS_DISCONNECT_COMMANDS,
        "No WINDOWS_DISCONNECT_COMMANDS configured. Direct Bluetooth HID release is not reliably scriptable on stock Windows.",
    )


def windows_acquire():
    return run_windows_command_list(
        WINDOWS_CONNECT_COMMANDS,
        "No WINDOWS_CONNECT_COMMANDS configured. Direct Bluetooth HID acquire is not reliably scriptable on stock Windows.",
    )


def local_release():
    if IS_MAC:
        return mac_release()
    if IS_WINDOWS:
        return windows_release()
    return False, f"Unsupported ROLE={ROLE}"


def local_acquire():
    if IS_MAC:
        return mac_acquire()
    if IS_WINDOWS:
        return windows_acquire()
    return False, f"Unsupported ROLE={ROLE}"


# ============================================================
# PEER CONTROL SERVER
# ============================================================
class PeerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ping":
            self._send_json(200, {"ok": True, "role": ROLE, "label": LOCAL_LABEL})
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/command":
            self._send_json(404, {"ok": False, "error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw or "{}")
        except Exception as e:
            self._send_json(400, {"ok": False, "error": str(e)})
            return

        token = payload.get("token", "")
        if AUTH_TOKEN and token != AUTH_TOKEN:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        action = payload.get("action")
        if action not in ("acquire", "release", "ping"):
            self._send_json(400, {"ok": False, "error": "bad action"})
            return

        with server_events_lock:
            server_events.append({
                "type": "server_action",
                "action": action,
                "from": payload.get("from", "peer"),
                "timestamp": time.time(),
            })

        self._send_json(200, {"ok": True, "queued": action, "role": ROLE, "label": LOCAL_LABEL})

    def log_message(self, format, *args):
        return

    def _send_json(self, status, obj):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode("utf-8"))


def start_peer_server():
    server = HTTPServer((SERVER_HOST, SERVER_PORT), PeerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def drain_server_events():
    with server_events_lock:
        items = list(server_events)
        server_events.clear()
    return items


def ping_peer():
    try:
        url = f"http://{PEER_HOST}:{PEER_PORT}/ping"
        with urllib.request.urlopen(url, timeout=3) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw or "{}")
            return True, f"Peer reachable: {data.get('label', 'unknown')} ({data.get('role', 'unknown')})"
    except Exception as e:
        return False, f"Peer not reachable: {e}"


def send_peer_command(action):
    url = f"http://{PEER_HOST}:{PEER_PORT}/command"
    payload = {"action": action, "token": AUTH_TOKEN, "from": LOCAL_LABEL}
    try:
        data = post_json(url, payload, timeout=5)
        return bool(data.get("ok")), json.dumps(data)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8")
        except Exception:
            raw = str(e)
        return False, f"HTTP {e.code}: {raw}"
    except Exception as e:
        return False, str(e)


# ============================================================
# SWITCH ORCHESTRATION
# ============================================================
def handle_probe_peer():
    global last_peer_result, health_text
    ok, text = ping_peer()
    last_peer_result = text
    health_text = "Peer reachable" if ok else "Peer unreachable"
    add_log("Peer ping", text)


def handle_local_release_button():
    global last_local_result, health_text
    ok, text = local_release()
    last_local_result = text
    health_text = "Released local devices" if ok else "Local release needs attention"
    add_log("Local release", text)


def handle_switch(target_role):
    global active_target, last_result, last_local_result, last_peer_result, health_text
    if target_role not in ("mac", "windows"):
        add_log("Switch ignored", f"Unknown target_role={target_role}")
        return

    target_label = "Mac" if target_role == "mac" else "Windows"
    local_should_acquire = target_role == ROLE
    peer_action = "release" if local_should_acquire else "acquire"
    local_action_name = "acquire" if local_should_acquire else "release"

    local_ok, local_text = local_acquire() if local_should_acquire else local_release()
    peer_ok, peer_text = send_peer_command(peer_action)

    if local_ok or peer_ok:
        active_target = target_label
    last_local_result = f"{local_action_name}: {local_text}"
    last_peer_result = f"{peer_action}: {peer_text}"
    health_text = "Switch command sent" if (local_ok or peer_ok) else "Switch had errors"
    last_result = f"target={target_label}"

    add_log(
        f"Switch to {target_label}",
        f"Local {local_action_name}: {'ok' if local_ok else 'failed'}\n{local_text}\n\nPeer {peer_action}: {'ok' if peer_ok else 'failed'}\n{peer_text}",
    )


def handle_server_action(action, source):
    global active_target, last_local_result, health_text
    if action == "ping":
        add_log("Peer ping received", f"from={source}")
        return

    if action == "acquire":
        ok, text = local_acquire()
        if ok:
            active_target = LOCAL_LABEL
        last_local_result = f"peer requested acquire: {text}"
        health_text = "Peer asked this machine to acquire" if ok else "Peer acquire had issues"
        add_log("Peer requested acquire", f"from={source}\n{text}")
        return

    if action == "release":
        ok, text = local_release()
        last_local_result = f"peer requested release: {text}"
        health_text = "Peer asked this machine to release" if ok else "Peer release had issues"
        add_log("Peer requested release", f"from={source}\n{text}")
        return


# ============================================================
# RELAY / NODE PATH HELPERS
# ============================================================
def _common_node_bins():
    bins = []
    if NODE_BIN:
        bins.append(NODE_BIN)

    bins.extend([
        shutil.which("node"),
        "/opt/homebrew/bin/node",
        "/usr/local/bin/node",
    ])

    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    if nvm_root.exists():
        for version_dir in sorted(nvm_root.iterdir(), reverse=True):
            candidate = version_dir / "bin" / "node"
            bins.append(str(candidate))

    seen = set()
    out = []
    for item in bins:
        if not item:
            continue
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _find_node_bin():
    for node_bin in _common_node_bins():
        if Path(node_bin).exists():
            return node_bin
    return None


def _ensure_node_in_path():
    node_bin = _find_node_bin()
    if not node_bin:
        return None

    node_dir = str(Path(node_bin).parent)
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    if node_dir not in parts:
        parts.insert(0, node_dir)
        os.environ["PATH"] = os.pathsep.join(parts)
    return node_bin


def _relay_is_reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=0.7) as r:
            return r.status < 500
    except Exception:
        return False


def _start_relay_with_explicit_node(node_bin: str):
    relay_script = getattr(hc, "_relay_script", None)
    if relay_script is None:
        raise RuntimeError("HyperCoreSDK client is missing _relay_script.")

    if getattr(hc, "_proc", None) and hc._proc.poll() is None:
        return

    parsed_port = 8765
    try:
        from urllib.parse import urlparse
        parsed = urlparse(RELAY_URL)
        parsed_port = parsed.port or 8765
    except Exception:
        pass

    env = os.environ.copy()
    env["PORT"] = str(parsed_port)

    hc._proc = subprocess.Popen(
        [node_bin, str(relay_script)],
        stdout=None,
        stderr=None,
        env=env,
    )

    waiter = getattr(hc, "_wait_for_relay", None)
    if callable(waiter):
        waiter()
    else:
        deadline = time.time() + 10
        while time.time() < deadline:
            if _relay_is_reachable(RELAY_URL):
                return
            time.sleep(0.25)
        raise RuntimeError("Relay not ready after 10s")


def ensure_relay_started():
    node_bin = _ensure_node_in_path()

    if _relay_is_reachable(RELAY_URL):
        return

    if not node_bin:
        raise RuntimeError(
            "HyperCoreSDK needs Node.js to run the local relay, but `node` was not found. "
            "Set BT_NODE_BIN, or add /opt/homebrew/bin or your ~/.nvm node bin to PATH."
        )

    try:
        hc.start_relay()
    except FileNotFoundError:
        _start_relay_with_explicit_node(node_bin)


# ============================================================
# UI INIT + MAIN LOOP
# ============================================================
def init_ui():
    ensure_relay_started()
    hc.clear()
    hc.mount("root/app", html=APP_HTML, js=APP_JS, fixed=True, layer=10)
    push_ui()
    add_log(
        "Controller ready",
        "Edit the config section at the top of this file on each machine. Then run the same script on both computers.",
    )
    if IS_MAC and not MAC_DEVICE_ADDRESSES:
        add_log("Config needed", "Add your keyboard/mouse Bluetooth MAC addresses to MAC_DEVICE_ADDRESSES.")
    if IS_WINDOWS and not WINDOWS_CONNECT_COMMANDS and not WINDOWS_DISCONNECT_COMMANDS:
        add_log(
            "Windows note",
            "Direct Bluetooth HID switching is not exposed cleanly on stock Windows. Add vendor or custom commands if you have them, otherwise the script can open Bluetooth settings as a fallback.",
        )


def main():
    global health_text
    init_ui()
    start_peer_server()
    health_text = f"Listening for peer commands on http://{SERVER_HOST}:{SERVER_PORT}"
    push_ui()

    seen = set()
    while True:
        snap = hc.snapshot() or {}

        for event in drain_server_events():
            if event.get("type") == "server_action":
                handle_server_action(event.get("action"), event.get("from", "peer"))
                push_ui()

        for key, value in snap.items():
            if not key.startswith("inbox/") or key in seen:
                continue

            seen.add(key)
            try:
                raw_data = value.get("data", {})
                msg = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
                action_type = msg.get("type")

                if action_type == "switch":
                    handle_switch((msg.get("target") or "").strip().lower())
                elif action_type == "release_local":
                    handle_local_release_button()
                elif action_type == "probe_peer":
                    handle_probe_peer()
                else:
                    add_log("Unknown action", json.dumps(msg))

            except Exception as e:
                add_log("Error processing UI action", str(e))

            hc.remove(key)
            push_ui()

        time.sleep(0.1)


if __name__ == "__main__":
    main()
