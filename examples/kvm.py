#!/usr/bin/env python3
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ============================================================
# CONFIG — edit on EACH machine before running.
# Same file on both machines, different ROLE / peer settings.
# ============================================================
ROLE = os.environ.get("BT_ROLE", "mac").strip().lower()  # mac | windows
RELAY_URL = os.environ.get("BT_RELAY_URL", "http://localhost:8765")
ROOT_NAME = os.environ.get("BT_ROOT_NAME", f"bt_switch_peer_{ROLE}")

SERVER_HOST = os.environ.get("BT_SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("BT_SERVER_PORT", "8766" if ROLE == "mac" else "8767"))
AUTH_TOKEN = os.environ.get("BT_AUTH_TOKEN", "change-me")

PEER_HOST = os.environ.get("BT_PEER_HOST", "192.168.1.50" if ROLE == "mac" else "192.168.1.40")
PEER_PORT = int(os.environ.get("BT_PEER_PORT", "8767" if ROLE == "mac" else "8766"))
PEER_LABEL = os.environ.get("BT_PEER_LABEL", "Windows" if ROLE == "mac" else "Mac")

# macOS: Bluetooth MAC addresses to control with blueutil.
# Example:
# MAC_DEVICE_ADDRESSES = ["E3-4B-CE-C4-38-32"]
MAC_DEVICE_ADDRESSES = [
    # "E3-4B-CE-C4-38-32",
]

DEVICE_LABELS = {
    # "E3-4B-CE-C4-38-32": "Keyboard",
}

MAC_RELEASE_DELAY_SECONDS = float(os.environ.get("BT_MAC_RELEASE_DELAY", "0.3"))
MAC_ACQUIRE_DELAY_SECONDS = float(os.environ.get("BT_MAC_ACQUIRE_DELAY", "0.6"))

# Windows: choose ONE of these strategies.
# 1) Vendor/custom commands (best when available)
WINDOWS_ACQUIRE_COMMANDS = [
    # [r"C:\\Path\\VendorTool.exe", "connect"],
]
WINDOWS_RELEASE_COMMANDS = [
    # [r"C:\\Path\\VendorTool.exe", "disconnect"],
]

# 2) PnP instance IDs (generic fallback, often requires Administrator)
# Find in Device Manager -> device -> Properties -> Details -> Device instance path
WINDOWS_DEVICE_INSTANCE_IDS = [
    # r"BTHENUM\\DEV_E34BCEC43832\\8&2A1F4B63&0&BLUETOOTHDEVICE_E34BCEC43832",
]
WINDOWS_DISABLE_DEVICE_ON_RELEASE = False
WINDOWS_OPEN_SETTINGS_ON_FALLBACK = True

PEER_TIMEOUT_SECONDS = float(os.environ.get("BT_PEER_TIMEOUT", "1.5"))
HEARTBEAT_INTERVAL_SECONDS = 4.0
LOOP_SLEEP_SECONDS = 0.10
MAX_LOG_ITEMS = 18

if ROLE not in {"mac", "windows"}:
    raise SystemExit("ROLE must be 'mac' or 'windows'")

IS_MAC = ROLE == "mac"
IS_WINDOWS = ROLE == "windows"
LOCAL_LABEL = "Mac" if IS_MAC else "Windows"
PEER_ROLE = "windows" if IS_MAC else "mac"
LOCAL_PLATFORM = platform.system()


# ============================================================
# ENV / HYPERCORE BOOTSTRAP
# ============================================================
def ensure_node_path() -> str | None:
    """Make sure the SDK can spawn its relay with node, even from IDEs."""
    preferred = [
        shutil.which("node"),
        "/opt/homebrew/bin/node",
        "/usr/local/bin/node",
    ]

    # common nvm bins
    nvm_root = Path.home() / ".nvm/versions/node"
    if nvm_root.exists():
        for version_dir in sorted(nvm_root.iterdir(), reverse=True):
            candidate = version_dir / "bin" / "node"
            preferred.append(str(candidate))

    chosen = None
    for cand in preferred:
        if cand and Path(cand).exists():
            chosen = str(Path(cand).resolve())
            break

    if not chosen:
        return None

    node_dir = str(Path(chosen).parent)
    current = os.environ.get("PATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    if node_dir not in parts:
        os.environ["PATH"] = os.pathsep.join([node_dir] + parts)
    return chosen


NODE_BIN = ensure_node_path()

try:
    from HyperCoreSDK import HyperClient
except Exception:
    # compatibility with some demo layouts
    from HyperCoreSDK.client import HyperClient

hc = HyperClient(relay=RELAY_URL, root=ROOT_NAME)


# ============================================================
# STATE
# ============================================================
state_lock = threading.Lock()
server_events: list[dict] = []
processed_action_keys: deque[str] = deque(maxlen=500)
processed_action_set: set[str] = set()
log_paths: deque[str] = deque(maxlen=MAX_LOG_ITEMS)

state = {
    "active_target": "Unknown",
    "desired_target_role": None,
    "peer_status": "Unknown",
    "peer_seen_at": "Never",
    "local_result": "Idle",
    "peer_result": "Idle",
    "health_text": "Starting…",
    "last_switch_source": "None",
    "last_peer_payload": "None",
}


# ============================================================
# UI
# ============================================================
APP_HTML = """
<div style="width:100%;height:100%;display:flex;flex-direction:column;background:#0f172a;color:#e2e8f0;font-family:Arial,sans-serif">
  <div style="padding:18px 20px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;background:#111827">
    <div style="min-width:0">
      <div style="font-size:12px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Bluetooth Hot Switch</div>
      <div data-bind-text="header_title" style="font-size:24px;font-weight:800;margin-top:4px"></div>
      <div data-bind-text="header_subtitle" style="font-size:13px;color:#cbd5e1;margin-top:6px;line-height:1.4"></div>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <button id="btn_mac" style="padding:12px 18px;background:#2563eb;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:700">Switch to Mac</button>
      <button id="btn_windows" style="padding:12px 18px;background:#16a34a;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:700">Switch to Windows</button>
      <button id="btn_reconnect_here" style="padding:12px 18px;background:#0f766e;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:700">Reconnect Here</button>
      <button id="btn_disconnect_here" style="padding:12px 18px;background:#475569;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:700">Disconnect Here</button>
      <button id="btn_ping" style="padding:12px 18px;background:#7c3aed;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:700">Peer Ping</button>
    </div>
  </div>

  <div style="display:grid;grid-template-columns:repeat(6,minmax(140px,1fr));gap:12px;padding:16px 20px;border-bottom:1px solid #334155;background:#0b1220">
    <div style="background:#111827;border:1px solid #334155;border-radius:10px;padding:14px">
      <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">This machine</div>
      <div data-bind-text="local_label" style="font-size:22px;font-weight:800;margin-top:6px"></div>
    </div>
    <div style="background:#111827;border:1px solid #334155;border-radius:10px;padding:14px">
      <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Active target</div>
      <div data-bind-text="active_target" style="font-size:22px;font-weight:800;margin-top:6px"></div>
    </div>
    <div style="background:#111827;border:1px solid #334155;border-radius:10px;padding:14px">
      <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Peer status</div>
      <div data-bind-text="peer_status" style="font-size:14px;font-weight:800;margin-top:6px;line-height:1.35"></div>
    </div>
    <div style="background:#111827;border:1px solid #334155;border-radius:10px;padding:14px">
      <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Local result</div>
      <div data-bind-text="local_result" style="font-size:14px;font-weight:800;margin-top:6px;line-height:1.35;white-space:pre-wrap"></div>
    </div>
    <div style="background:#111827;border:1px solid #334155;border-radius:10px;padding:14px">
      <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Peer result</div>
      <div data-bind-text="peer_result" style="font-size:14px;font-weight:800;margin-top:6px;line-height:1.35;white-space:pre-wrap"></div>
    </div>
    <div style="background:#111827;border:1px solid #334155;border-radius:10px;padding:14px">
      <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Peer seen</div>
      <div data-bind-text="peer_seen_at" style="font-size:14px;font-weight:800;margin-top:6px"></div>
    </div>
  </div>

  <div style="padding:16px 20px;border-bottom:1px solid #334155;display:flex;flex-direction:column;gap:8px;background:#111827">
    <div data-bind-text="health_text" style="font-size:14px;color:#e2e8f0;line-height:1.45"></div>
    <div data-bind-text="config_text" style="font-size:12px;color:#94a3b8;line-height:1.55;white-space:pre-wrap"></div>
  </div>

  <div style="padding:16px 20px 8px 20px;font-size:12px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.14em">Activity</div>
  <div data-children style="flex:1;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:8px;padding:0 20px 20px 20px"></div>
</div>
"""

APP_JS = r"""
(function(){
  const macBtn = document.getElementById("btn_mac");
  const winBtn = document.getElementById("btn_windows");
  const reconnectBtn = document.getElementById("btn_reconnect_here");
  const disconnectBtn = document.getElementById("btn_disconnect_here");
  const pingBtn = document.getElementById("btn_ping");

  if (!macBtn || !winBtn || !reconnectBtn || !disconnectBtn || !pingBtn || macBtn.dataset.on) return;
  macBtn.dataset.on = "1";

  window.sendAction = (type, payload = {}) => {
    const path = "inbox/" + Date.now() + "_" + Math.random().toString(36).slice(2,7);
    window.$scene.get(path).put({
      data: JSON.stringify({ type, ...payload, ts: Date.now() })
    });
  };

  macBtn.onclick = () => window.sendAction("switch_target", { target: "mac" });
  winBtn.onclick = () => window.sendAction("switch_target", { target: "windows" });
  reconnectBtn.onclick = () => window.sendAction("reconnect_local");
  disconnectBtn.onclick = () => window.sendAction("disconnect_local");
  pingBtn.onclick = () => window.sendAction("probe_peer");
})();
"""

LOG_HTML = """
<div style="background:#111827;border:1px solid #334155;border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:6px">
  <div data-bind-text="title" style="font-size:13px;font-weight:800;color:#f8fafc"></div>
  <div data-bind-text="detail" style="font-size:12px;color:#cbd5e1;line-height:1.5;white-space:pre-wrap"></div>
</div>
"""


# ============================================================
# LOW-LEVEL HELPERS
# ============================================================
def now_label() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def shell_join(parts: list[str]) -> str:
    return " ".join(parts)


def run_command(parts: list[str], timeout: int = 20) -> tuple[bool, str]:
    try:
        completed = subprocess.run(parts, capture_output=True, text=True, timeout=timeout)
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        ok = completed.returncode == 0
        msg = stdout or stderr or f"exit={completed.returncode}"
        return ok, msg
    except FileNotFoundError:
        return False, f"Command not found: {parts[0]}"
    except subprocess.TimeoutExpired:
        return False, f"Timed out: {shell_join(parts)}"
    except Exception as e:
        return False, str(e)


def http_post_json(url: str, payload: dict, timeout: float = PEER_TIMEOUT_SECONDS) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw or "{}")


def http_get_json(url: str, timeout: float = PEER_TIMEOUT_SECONDS) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw or "{}")


def add_log(title: str, detail: str):
    item_id = f"log_{uuid.uuid4().hex[:8]}"
    path = f"root/app/{item_id}"
    hc.mount(path, html=LOG_HTML, layer=5)
    hc.write(path, title=f"[{now_label()}] {title}", detail=detail)

    if len(log_paths) == log_paths.maxlen:
        old = log_paths.popleft()
        hc.remove(old)
    log_paths.append(path)


def push_ui():
    cfg = [
        f"Role: {ROLE}",
        f"Platform: {LOCAL_PLATFORM}",
        f"Peer: {PEER_LABEL} @ http://{PEER_HOST}:{PEER_PORT}",
        f"Relay: {RELAY_URL}",
        f"HTTP server: http://{SERVER_HOST}:{SERVER_PORT}",
        f"Node: {NODE_BIN or 'not found on PATH'}",
    ]
    if IS_MAC:
        cfg.append(f"Configured macOS Bluetooth devices: {len(MAC_DEVICE_ADDRESSES)}")
    else:
        cfg.append(f"Configured Windows acquire commands: {len(WINDOWS_ACQUIRE_COMMANDS)}")
        cfg.append(f"Configured Windows release commands: {len(WINDOWS_RELEASE_COMMANDS)}")
        cfg.append(f"Configured Windows instance IDs: {len(WINDOWS_DEVICE_INSTANCE_IDS)}")
        cfg.append(f"Disable on release: {WINDOWS_DISABLE_DEVICE_ON_RELEASE}")

    with state_lock:
        ui_state = dict(state)

    hc.write(
        "root/app",
        header_title=f"{LOCAL_LABEL} controller",
        header_subtitle="Peer discovery stays on. Local Bluetooth actions always run immediately. Peer updates are best-effort.",
        local_label=LOCAL_LABEL,
        active_target=ui_state["active_target"],
        peer_status=ui_state["peer_status"],
        peer_seen_at=ui_state["peer_seen_at"],
        local_result=ui_state["local_result"],
        peer_result=ui_state["peer_result"],
        health_text=ui_state["health_text"],
        config_text="\n".join(cfg),
    )


def set_state(**kwargs):
    with state_lock:
        state.update(kwargs)


# ============================================================
# BLUETOOTH ADAPTERS — macOS
# ============================================================
def find_blueutil() -> str | None:
    candidates = [
        shutil.which("blueutil"),
        "/opt/homebrew/bin/blueutil",
        "/usr/local/bin/blueutil",
    ]
    for cand in candidates:
        if cand and Path(cand).exists():
            return str(Path(cand))
    return None


def mac_disconnect_all() -> tuple[bool, str]:
    if not MAC_DEVICE_ADDRESSES:
        return False, "No MAC_DEVICE_ADDRESSES configured."
    blueutil = find_blueutil()
    if not blueutil:
        return False, "blueutil not found. Install it with: brew install blueutil"

    ok_all = True
    lines = []
    for addr in MAC_DEVICE_ADDRESSES:
        ok, out = run_command([blueutil, "--disconnect", addr])
        label = DEVICE_LABELS.get(addr, addr)
        lines.append(f"disconnect {label}: {'ok' if ok else 'failed'} — {out}")
        ok_all = ok_all and ok
    return ok_all, "\n".join(lines)


def mac_connect_all() -> tuple[bool, str]:
    if not MAC_DEVICE_ADDRESSES:
        return False, "No MAC_DEVICE_ADDRESSES configured."
    blueutil = find_blueutil()
    if not blueutil:
        return False, "blueutil not found. Install it with: brew install blueutil"

    ok_all = True
    lines = []
    for addr in MAC_DEVICE_ADDRESSES:
        ok, out = run_command([blueutil, "--connect", addr])
        label = DEVICE_LABELS.get(addr, addr)
        lines.append(f"connect {label}: {'ok' if ok else 'failed'} — {out}")
        ok_all = ok_all and ok
    return ok_all, "\n".join(lines)


def mac_release() -> tuple[bool, str]:
    return mac_disconnect_all()


def mac_acquire() -> tuple[bool, str]:
    # Make the reconnect more deterministic by doing a local drop before connect.
    _, release_text = mac_disconnect_all()
    time.sleep(MAC_ACQUIRE_DELAY_SECONDS)
    ok, connect_text = mac_connect_all()
    return ok, f"pre-release:\n{release_text}\n\nacquire:\n{connect_text}"


def mac_reconnect() -> tuple[bool, str]:
    ok1, txt1 = mac_disconnect_all()
    time.sleep(MAC_RELEASE_DELAY_SECONDS)
    ok2, txt2 = mac_connect_all()
    return (ok1 and ok2), f"disconnect:\n{txt1}\n\nreconnect:\n{txt2}"


# ============================================================
# BLUETOOTH ADAPTERS — Windows
# ============================================================
def windows_open_bluetooth_settings() -> tuple[bool, str]:
    return run_command(["cmd", "/c", "start", "", "ms-settings:bluetooth"])


def windows_run_commands(cmds: list[list[str]]) -> tuple[bool, str] | None:
    if not cmds:
        return None
    ok_all = True
    lines = []
    for cmd in cmds:
        ok, out = run_command(cmd)
        ok_all = ok_all and ok
        lines.append(f"{shell_join(cmd)} => {'ok' if ok else 'failed'} — {out}")
    return ok_all, "\n".join(lines)


def windows_pnputil(args: list[str]) -> tuple[bool, str]:
    tool = shutil.which("pnputil") or shutil.which("pnputil.exe")
    if not tool:
        return False, "pnputil not found."
    return run_command([tool] + args)


def windows_restart_instance_ids() -> tuple[bool, str] | None:
    if not WINDOWS_DEVICE_INSTANCE_IDS:
        return None
    ok_all = True
    lines = []
    for instance_id in WINDOWS_DEVICE_INSTANCE_IDS:
        ok, out = windows_pnputil(["/restart-device", instance_id])
        ok_all = ok_all and ok
        lines.append(f"restart {instance_id}: {'ok' if ok else 'failed'} — {out}")
    return ok_all, "\n".join(lines)


def windows_enable_instance_ids() -> tuple[bool, str] | None:
    if not WINDOWS_DEVICE_INSTANCE_IDS:
        return None
    ok_all = True
    lines = []
    for instance_id in WINDOWS_DEVICE_INSTANCE_IDS:
        ok, out = windows_pnputil(["/enable-device", instance_id])
        ok_all = ok_all and ok
        lines.append(f"enable {instance_id}: {'ok' if ok else 'failed'} — {out}")
    return ok_all, "\n".join(lines)


def windows_disable_instance_ids() -> tuple[bool, str] | None:
    if not WINDOWS_DEVICE_INSTANCE_IDS:
        return None
    ok_all = True
    lines = []
    for instance_id in WINDOWS_DEVICE_INSTANCE_IDS:
        ok, out = windows_pnputil(["/disable-device", instance_id])
        ok_all = ok_all and ok
        lines.append(f"disable {instance_id}: {'ok' if ok else 'failed'} — {out}")
    return ok_all, "\n".join(lines)


def windows_release() -> tuple[bool, str]:
    custom = windows_run_commands(WINDOWS_RELEASE_COMMANDS)
    if custom is not None:
        return custom

    if WINDOWS_DISABLE_DEVICE_ON_RELEASE:
        disabled = windows_disable_instance_ids()
        if disabled is not None:
            return disabled

    if WINDOWS_OPEN_SETTINGS_ON_FALLBACK:
        ok, out = windows_open_bluetooth_settings()
        return ok, (
            "No Windows release commands configured. "
            "Opened Bluetooth settings as fallback.\n" + out
        )

    return False, "No Windows release path configured."


def windows_acquire() -> tuple[bool, str]:
    custom = windows_run_commands(WINDOWS_ACQUIRE_COMMANDS)
    if custom is not None:
        return custom

    if WINDOWS_DISABLE_DEVICE_ON_RELEASE:
        enabled = windows_enable_instance_ids()
        if enabled is not None:
            return enabled

    restarted = windows_restart_instance_ids()
    if restarted is not None:
        return restarted

    if WINDOWS_OPEN_SETTINGS_ON_FALLBACK:
        ok, out = windows_open_bluetooth_settings()
        return ok, (
            "No Windows acquire commands or instance IDs configured. "
            "Opened Bluetooth settings as fallback.\n" + out
        )

    return False, "No Windows acquire path configured."


def windows_reconnect() -> tuple[bool, str]:
    rel_ok, rel_txt = windows_release()
    time.sleep(0.5)
    acq_ok, acq_txt = windows_acquire()
    return (rel_ok and acq_ok), f"release:\n{rel_txt}\n\nacquire:\n{acq_txt}"


# ============================================================
# LOCAL ADAPTER DISPATCH
# ============================================================
def local_release() -> tuple[bool, str]:
    if IS_MAC:
        return mac_release()
    if IS_WINDOWS:
        return windows_release()
    return False, f"Unsupported role/platform: {ROLE}/{LOCAL_PLATFORM}"


def local_acquire() -> tuple[bool, str]:
    if IS_MAC:
        return mac_acquire()
    if IS_WINDOWS:
        return windows_acquire()
    return False, f"Unsupported role/platform: {ROLE}/{LOCAL_PLATFORM}"


def local_reconnect() -> tuple[bool, str]:
    if IS_MAC:
        return mac_reconnect()
    if IS_WINDOWS:
        return windows_reconnect()
    return False, f"Unsupported role/platform: {ROLE}/{LOCAL_PLATFORM}"


# ============================================================
# PEER SERVER / DISCOVERY
# ============================================================
class PeerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ping":
            self._send_json(200, {
                "ok": True,
                "role": ROLE,
                "label": LOCAL_LABEL,
                "active_target": state.get("active_target", "Unknown"),
                "ts": now_label(),
            })
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/intent":
            self._send_json(404, {"ok": False, "error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw or "{}")
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"bad json: {e}"})
            return

        if AUTH_TOKEN and payload.get("token") != AUTH_TOKEN:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        target = str(payload.get("target", "")).strip().lower()
        if target not in {"mac", "windows"}:
            self._send_json(400, {"ok": False, "error": "target must be mac or windows"})
            return

        source = str(payload.get("from", "peer")).strip() or "peer"
        with state_lock:
            server_events.append({
                "type": "peer_intent",
                "target": target,
                "from": source,
                "raw": payload,
            })

        self._send_json(200, {
            "ok": True,
            "queued": True,
            "target": target,
            "role": ROLE,
            "label": LOCAL_LABEL,
        })

    def log_message(self, fmt, *args):
        return

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = None


def start_peer_server():
    global server
    server = ThreadingHTTPServer((SERVER_HOST, SERVER_PORT), PeerHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()


heartbeat_stop = threading.Event()


def heartbeat_loop():
    while not heartbeat_stop.is_set():
        ok, text = ping_peer()
        if ok:
            set_state(peer_status="Online", peer_seen_at=now_label())
        else:
            set_state(peer_status="Offline")
        # only refresh peer_result when it was idle or stale discovery information
        with state_lock:
            if state["peer_result"] in {"Idle", "Discovery offline", "Discovery online"}:
                state["peer_result"] = "Discovery online" if ok else "Discovery offline"
        try:
            push_ui()
        except Exception:
            pass
        heartbeat_stop.wait(HEARTBEAT_INTERVAL_SECONDS)


def ping_peer() -> tuple[bool, str]:
    try:
        data = http_get_json(f"http://{PEER_HOST}:{PEER_PORT}/ping")
        label = data.get("label", "peer")
        role = data.get("role", "unknown")
        return True, f"Peer reachable: {label} ({role})"
    except Exception as e:
        return False, f"Peer offline: {e}"


def send_peer_intent(target: str) -> tuple[bool, str]:
    payload = {
        "token": AUTH_TOKEN,
        "target": target,
        "from": LOCAL_LABEL,
        "ts": int(time.time() * 1000),
    }
    try:
        data = http_post_json(f"http://{PEER_HOST}:{PEER_PORT}/intent", payload)
        return bool(data.get("ok")), json.dumps(data)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = str(e)
        return False, f"HTTP {e.code}: {body}"
    except Exception as e:
        return False, str(e)


# ============================================================
# SWITCH ORCHESTRATION
# ============================================================
def set_active_target_from_role(role_name: str):
    set_state(
        active_target="Mac" if role_name == "mac" else "Windows",
        desired_target_role=role_name,
    )


def perform_local_for_target(target_role: str, source: str) -> tuple[bool, str, str]:
    local_mode = "acquire" if ROLE == target_role else "release"
    if local_mode == "acquire":
        ok, detail = local_acquire()
    else:
        ok, detail = local_release()
    set_state(
        local_result=f"{local_mode}: {'ok' if ok else 'failed'}\n{detail}",
        last_switch_source=source,
    )
    return ok, local_mode, detail


def handle_switch_target(target_role: str, source: str = "local_button"):
    if target_role not in {"mac", "windows"}:
        add_log("Switch ignored", f"Unknown target: {target_role}")
        return

    set_active_target_from_role(target_role)
    local_ok, local_mode, local_detail = perform_local_for_target(target_role, source)

    peer_ok, peer_text = send_peer_intent(target_role)
    if peer_ok:
        set_state(peer_result=f"intent sent\n{peer_text}", peer_status="Online", peer_seen_at=now_label())
    else:
        set_state(peer_result=f"peer offline / failed\n{peer_text}", peer_status="Offline")

    set_state(
        health_text=(
            f"Target set to {'Mac' if target_role == 'mac' else 'Windows'}. "
            f"Local {local_mode} ran {'successfully' if local_ok else 'with issues'}. "
            f"Peer update is best-effort."
        )
    )
    add_log(
        f"Switch to {'Mac' if target_role == 'mac' else 'Windows'}",
        f"Source: {source}\n\nLocal {local_mode}: {'ok' if local_ok else 'failed'}\n{local_detail}\n\nPeer notify: {'ok' if peer_ok else 'failed'}\n{peer_text}",
    )


def handle_reconnect_local(source: str = "local_button"):
    ok, detail = local_reconnect()
    set_state(
        local_result=f"reconnect: {'ok' if ok else 'failed'}\n{detail}",
        health_text=f"Local reconnect {'completed' if ok else 'needs attention'}.",
        last_switch_source=source,
    )
    add_log("Reconnect here", f"Source: {source}\n\n{detail}")


def handle_disconnect_local(source: str = "local_button"):
    ok, detail = local_release()
    set_state(
        local_result=f"disconnect: {'ok' if ok else 'failed'}\n{detail}",
        health_text=f"Local disconnect {'completed' if ok else 'needs attention'}.",
        last_switch_source=source,
    )
    add_log("Disconnect here", f"Source: {source}\n\n{detail}")


def handle_probe_peer():
    ok, detail = ping_peer()
    if ok:
        set_state(peer_status="Online", peer_seen_at=now_label(), peer_result=detail, health_text="Peer reachable.")
    else:
        set_state(peer_status="Offline", peer_result=detail, health_text="Peer unreachable. Local switching still works.")
    add_log("Peer ping", detail)


def handle_peer_intent(target_role: str, source: str, raw_payload: dict):
    set_active_target_from_role(target_role)
    ok, local_mode, detail = perform_local_for_target(target_role, f"peer:{source}")
    set_state(
        peer_status="Online",
        peer_seen_at=now_label(),
        health_text=(
            f"Peer requested target={'Mac' if target_role == 'mac' else 'Windows'}. "
            f"Local {local_mode} ran {'successfully' if ok else 'with issues'}."
        ),
        last_peer_payload=json.dumps(raw_payload),
    )
    add_log(
        f"Peer intent → {'Mac' if target_role == 'mac' else 'Windows'}",
        f"From: {source}\nLocal action: {local_mode}\nSuccess: {ok}\n\n{detail}",
    )


# ============================================================
# HYPERCORE BOOT / ACTION LOOP
# ============================================================
def boot_hypercore():
    try:
        hc.start_relay()
    except Exception as e:
        # If relay is already up, keep going. Otherwise the UI will fail visibly later.
        add_log("Relay start notice", str(e))
    hc.clear()
    hc.mount("root/app", html=APP_HTML, js=APP_JS, fixed=True, layer=10)


def remember_processed(key: str):
    if len(processed_action_keys) == processed_action_keys.maxlen:
        old = processed_action_keys.popleft()
        processed_action_set.discard(old)
    processed_action_keys.append(key)
    processed_action_set.add(key)


def already_processed(key: str) -> bool:
    return key in processed_action_set


def init_app():
    boot_hypercore()
    set_state(
        health_text=f"Listening for peer commands on http://{SERVER_HOST}:{SERVER_PORT}",
        peer_result="Idle",
        local_result="Idle",
    )
    push_ui()
    add_log(
        "Controller ready",
        "Buttons write serialized inbox actions. Peer discovery is separate from local Bluetooth actions.\n"
        "Local actions always run immediately; peer updates are best-effort.",
    )
    if IS_MAC and not MAC_DEVICE_ADDRESSES:
        add_log("Config needed", "Add your keyboard/mouse Bluetooth addresses to MAC_DEVICE_ADDRESSES.")
    if IS_WINDOWS and not WINDOWS_ACQUIRE_COMMANDS and not WINDOWS_RELEASE_COMMANDS and not WINDOWS_DEVICE_INSTANCE_IDS:
        add_log(
            "Windows config needed",
            "Add vendor commands or Windows device instance IDs. Without them the app can only open Bluetooth settings as fallback.",
        )
    push_ui()


def drain_server_events() -> list[dict]:
    with state_lock:
        items = list(server_events)
        server_events.clear()
    return items


def main_loop():
    while True:
        for event in drain_server_events():
            if event.get("type") == "peer_intent":
                handle_peer_intent(event.get("target", ""), event.get("from", "peer"), event.get("raw", {}))
                push_ui()

        snap = hc.snapshot() or {}
        for key, value in list(snap.items()):
            if not key.startswith("inbox/") or already_processed(key):
                continue
            remember_processed(key)

            try:
                raw_data = value.get("data", {})
                msg = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
                action = str(msg.get("type", "")).strip()

                if action == "switch_target":
                    handle_switch_target(str(msg.get("target", "")).strip().lower())
                elif action == "reconnect_local":
                    handle_reconnect_local()
                elif action == "disconnect_local":
                    handle_disconnect_local()
                elif action == "probe_peer":
                    handle_probe_peer()
                else:
                    add_log("Unknown action", json.dumps(msg))
            except Exception as e:
                add_log("Action error", str(e))
            finally:
                hc.remove(key)
                push_ui()

        time.sleep(LOOP_SLEEP_SECONDS)


def main():
    init_app()
    start_peer_server()
    heartbeat = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat.start()
    push_ui()
    main_loop()


if __name__ == "__main__":
    main()
