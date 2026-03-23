#!/usr/bin/env python3
"""
HyperFlow — mouse & keyboard sharing across machines.

    pip install zeroconf pynput

    # Machine A (Windows)
    python -m examples.hyperflow --discovery lan

    # Machine B (Mac)
    python -m examples.hyperflow --discovery lan

    Open http://localhost:8765/hyperflow on either machine.
    Click a machine to route your keyboard + mouse there.

Architecture:
    Each machine runs this script. It does three things:
    1. Lists its own input devices and writes them to the graph
    2. Shows a web UI with all machines and a "send input here" toggle
    3. When active: captures local mouse/keyboard, writes events to graph
       When target: reads events from graph, injects them locally

    The hyper relay syncs the graph between machines via Gun.
    pynput handles OS-level input capture and injection.
"""

import argparse
import json
import platform
import threading
import time
import sys

from HyperCoreSDK.client import HyperClient

# ---------------------------------------------------------------------------
# pynput availability
# ---------------------------------------------------------------------------
try:
    from pynput import keyboard, mouse
    from pynput.keyboard import Key, Controller as KBController
    from pynput.mouse import Button, Controller as MouseController
    HAS_PYNPUT = True
except ImportError:
    HAS_PYNPUT = False
    print("WARNING: pynput not installed — pip install pynput")
    print("         Input forwarding disabled, UI-only mode.")

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="HyperFlow — KVM over LAN")
parser.add_argument("--discovery", default="local", choices=["local", "lan", "trusted"])
parser.add_argument("--relay", default="auto", choices=["auto", "host", "join"])
parser.add_argument("--peers", nargs="*", default=[])
parser.add_argument("--port", type=int, default=8765)
parser.add_argument("--root", default="hyperflow")
args = parser.parse_args()

# ---------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------
hc = HyperClient(
    root=args.root, discovery=args.discovery, relay=args.relay,
    peers=[f"http://{p}:{args.port}" for p in args.peers], port=args.port,
)
hc.connect()

# Don't hc.clear() — that would wipe the other machine's registration.
# Only clean up our own UI mount point.
hc.remove("root/dash")

ME = hc.machine_id
MY_NAME = hc.machine_name
MY_OS = platform.system()  # "Windows" or "Darwin"

# ---------------------------------------------------------------------------
# Device enumeration (best-effort, cross-platform)
# ---------------------------------------------------------------------------
def list_devices():
    """Return a list of dicts describing connected input devices."""
    devices = []
    # pynput doesn't enumerate devices, but we can describe what's available
    devices.append({"name": "System Keyboard", "type": "keyboard", "id": "kb_0"})
    devices.append({"name": "System Mouse/Trackpad", "type": "mouse", "id": "mouse_0"})

    # On mac, try to list Bluetooth devices
    if MY_OS == "Darwin":
        try:
            import subprocess
            out = subprocess.check_output(
                ["system_profiler", "SPBluetoothDataType", "-json"],
                timeout=5, text=True
            )
            bt = json.loads(out)
            items = bt.get("SPBluetoothDataType", [{}])
            for section in items:
                connected = section.get("device_connected", section.get("devices_connected", []))
                if isinstance(connected, list):
                    for dev in connected:
                        if isinstance(dev, dict):
                            for name, info in dev.items():
                                devices.append({
                                    "name": f"BT: {name}",
                                    "type": "bluetooth",
                                    "id": f"bt_{name.replace(' ', '_').lower()}"
                                })
        except Exception:
            pass

    # On Windows, try WMI for keyboards/mice
    if MY_OS == "Windows":
        try:
            import subprocess
            out = subprocess.check_output(
                ["powershell", "-Command",
                 "Get-PnpDevice -Class Keyboard,Mouse -Status OK | Select-Object FriendlyName,Class | ConvertTo-Json"],
                timeout=5, text=True
            )
            devs = json.loads(out)
            if isinstance(devs, dict): devs = [devs]
            for d in devs:
                name = d.get("FriendlyName", "Unknown")
                cls = d.get("Class", "").lower()
                dtype = "keyboard" if "keyboard" in cls else "mouse"
                devices.append({
                    "name": name,
                    "type": dtype,
                    "id": f"pnp_{name.replace(' ', '_').lower()[:20]}"
                })
        except Exception:
            pass

    return devices

# ---------------------------------------------------------------------------
# UI Templates
# ---------------------------------------------------------------------------

DASH_HTML = """
<div style="width:100%;height:100%;background:#0a0a0a;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,sans-serif;display:flex;flex-direction:column">

  <div style="padding:20px 24px;background:#111;border-bottom:1px solid #222;display:flex;justify-content:space-between;align-items:center">
    <div style="display:flex;align-items:center;gap:12px">
      <div style="font-size:22px;font-weight:800;letter-spacing:1px;color:#818cf8">HYPERFLOW</div>
      <div style="font-size:12px;color:#555;background:#1a1a1a;padding:4px 10px;border-radius:4px" data-bind-text="status">connecting...</div>
    </div>
    <div style="font-size:11px;color:#555" data-bind-text="me"></div>
  </div>

  <div style="flex:1;display:flex;padding:24px;gap:24px;overflow:auto">
    <div data-children style="display:flex;gap:24px;flex:1"></div>
  </div>

  <div style="padding:12px 24px;background:#111;border-top:1px solid #222;display:flex;justify-content:space-between;align-items:center">
    <div style="font-size:11px;color:#555">click a machine to route keyboard + mouse there</div>
    <div style="font-size:11px;color:#555" data-bind-text="hint"></div>
  </div>

</div>
"""

DASH_JS = r"""
(function(){
  if (document.getElementById("_hf_init")) return;
  var m = document.createElement("div"); m.id = "_hf_init"; m.style.display = "none";
  document.body.appendChild(m);

  window.hfSelectTarget = function(machineId) {
    window.$scene.get("inbox/" + Date.now() + "_" + Math.random().toString(36).slice(2,7)).put({
      data: JSON.stringify({ type: "set_target", target: machineId })
    });
  };
})();
"""

MACHINE_CARD_HTML = """
<div style="flex:1;min-width:280px;max-width:420px;background:#151515;border:2px solid #252525;border-radius:12px;display:flex;flex-direction:column;overflow:hidden;cursor:pointer;transition:border-color 0.2s"
     data-bind-style="borderColor:border_color">

  <div style="padding:16px 20px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #222">
    <div>
      <div data-bind-text="machine_name" style="font-weight:700;font-size:16px"></div>
      <div data-bind-text="machine_os" style="font-size:11px;color:#666;margin-top:2px"></div>
    </div>
    <div data-bind-text="role_badge" style="font-size:11px;font-weight:700;padding:4px 10px;border-radius:4px;background:#1a1a1a"></div>
  </div>

  <div style="padding:16px 20px;flex:1">
    <div style="font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px">Devices</div>
    <div data-bind-html="device_list" style="font-size:13px;line-height:1.8;color:#999"></div>
  </div>

  <div style="padding:12px 20px;background:#111;border-top:1px solid #222">
    <div data-bind-text="latency" style="font-size:11px;color:#555"></div>
  </div>

</div>
"""

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
current_target = ME  # which machine receives input (default: self)
capturing = False
kb_ctrl = KBController() if HAS_PYNPUT else None
mouse_ctrl = MouseController() if HAS_PYNPUT else None
input_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Input capture (runs in background thread when we're the source)
# ---------------------------------------------------------------------------
def start_capture():
    global capturing
    if not HAS_PYNPUT or capturing:
        return
    capturing = True

    def on_key_press(key):
        if current_target == ME:
            return  # no forwarding to self
        try:
            k = key.char if hasattr(key, 'char') and key.char else str(key)
        except:
            k = str(key)
        hc.write("_input/keyboard", machine=ME, target=current_target,
                 action="press", key=k, t=time.time())

    def on_key_release(key):
        if current_target == ME:
            return
        try:
            k = key.char if hasattr(key, 'char') and key.char else str(key)
        except:
            k = str(key)
        hc.write("_input/keyboard", machine=ME, target=current_target,
                 action="release", key=k, t=time.time())

    def on_mouse_move(x, y):
        if current_target == ME:
            return
        hc.write("_input/mouse", machine=ME, target=current_target,
                 action="move", x=x, y=y, t=time.time())

    def on_mouse_click(x, y, button, pressed):
        if current_target == ME:
            return
        hc.write("_input/mouse", machine=ME, target=current_target,
                 action="click", x=x, y=y,
                 button=str(button), pressed=pressed, t=time.time())

    def on_mouse_scroll(x, y, dx, dy):
        if current_target == ME:
            return
        hc.write("_input/mouse", machine=ME, target=current_target,
                 action="scroll", x=x, y=y, dx=dx, dy=dy, t=time.time())

    threading.Thread(target=lambda: keyboard.Listener(
        on_press=on_key_press, on_release=on_key_release
    ).start(), daemon=True).start()

    threading.Thread(target=lambda: mouse.Listener(
        on_move=on_mouse_move, on_click=on_mouse_click, on_scroll=on_mouse_scroll
    ).start(), daemon=True).start()

# ---------------------------------------------------------------------------
# Input injection (when we're the target, process events from graph)
# ---------------------------------------------------------------------------
KEY_MAP = {}
if HAS_PYNPUT:
    # Map string representations back to Key objects
    for attr in dir(Key):
        if not attr.startswith('_'):
            KEY_MAP[f"Key.{attr}"] = getattr(Key, attr)

def inject_keyboard(event):
    if not HAS_PYNPUT or not kb_ctrl:
        return
    k = event.get("key", "")
    action = event.get("action", "")

    # resolve key
    resolved = KEY_MAP.get(k)
    if not resolved:
        # single char
        if len(k) == 1:
            resolved = k
        else:
            return  # unknown key

    try:
        if action == "press":
            kb_ctrl.press(resolved)
        elif action == "release":
            kb_ctrl.release(resolved)
    except Exception as e:
        print(f"inject kb error: {e}")

def inject_mouse(event):
    if not HAS_PYNPUT or not mouse_ctrl:
        return
    action = event.get("action", "")
    try:
        if action == "move":
            mouse_ctrl.position = (int(event.get("x", 0)), int(event.get("y", 0)))
        elif action == "click":
            btn = Button.left
            if "right" in str(event.get("button", "")):
                btn = Button.right
            elif "middle" in str(event.get("button", "")):
                btn = Button.middle
            if event.get("pressed"):
                mouse_ctrl.press(btn)
            else:
                mouse_ctrl.release(btn)
        elif action == "scroll":
            mouse_ctrl.scroll(int(event.get("dx", 0)), int(event.get("dy", 0)))
    except Exception as e:
        print(f"inject mouse error: {e}")

# ---------------------------------------------------------------------------
# Mount UI
# ---------------------------------------------------------------------------
hc.mount("root/dash", html=DASH_HTML, js=DASH_JS, fixed=True, layer=10)
hc.write("root/dash", status="scanning...", me=f"{MY_NAME} ({MY_OS})", hint="")

# Register this machine
devices = list_devices()
machine_info = {
    "machine_id": ME, "name": MY_NAME, "os": MY_OS,
    "devices": devices, "t": time.time()
}
hc.write(f"_machines/{ME}/info", data=json.dumps(machine_info))

# Start capture thread
start_capture()

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
seen = set()
last_heartbeat = 0
last_ui_refresh = 0

while True:
    now = time.time()
    snap = hc.snapshot() or {}

    # --- heartbeat ---
    if now - last_heartbeat > 2.0:
        hc.write(f"_machines/{ME}/presence",
                 data=json.dumps({"status": "online", "t": now}))
        last_heartbeat = now

    # --- process inbox ---
    for k, v in snap.items():
        if not k.startswith("inbox/") or k in seen:
            continue
        seen.add(k)
        try:
            raw = v.get("data", "{}")
            msg = json.loads(raw) if isinstance(raw, str) else raw
            if msg.get("type") == "set_target":
                current_target = msg.get("target", ME)
                print(f"→ target set to: {current_target[:20]}")
        except Exception:
            pass
        hc.remove(k)

    # --- inject input if we're the target ---
    kb_event = snap.get("_input/keyboard")
    if kb_event and kb_event.get("target") == ME and kb_event.get("machine") != ME:
        inject_keyboard(kb_event)

    mouse_event = snap.get("_input/mouse")
    if mouse_event and mouse_event.get("target") == ME and mouse_event.get("machine") != ME:
        inject_mouse(mouse_event)

    # --- refresh UI ---
    if now - last_ui_refresh > 1.0:
        last_ui_refresh = now

        # gather all machines
        machines = {}
        for k, v in snap.items():
            if k.startswith("_machines/") and k.endswith("/info"):
                mid = k.split("/")[1]
                try:
                    info = json.loads(v.get("data", "{}")) if isinstance(v.get("data"), str) else v
                    machines[mid] = info
                except:
                    machines[mid] = v

        # check which machines are alive (heartbeat within 10s)
        alive = set()
        for k, v in snap.items():
            if k.startswith("_machines/") and k.endswith("/presence"):
                mid = k.split("/")[1]
                try:
                    pres = json.loads(v.get("data", "{}")) if isinstance(v.get("data"), str) else v
                    t = float(pres.get("t", 0))
                    if now - t < 10:
                        alive.add(mid)
                except:
                    pass

        print(f"[ui] {len(machines)} machine(s), {len(alive)} alive, {len(snap)} snapshot keys")

        # update status
        n_alive = len(alive)
        target_name = machines.get(current_target, {}).get("name", current_target[:16])
        status = f"{n_alive} machine{'s' if n_alive != 1 else ''} · sending to {target_name}"
        hint_text = "pynput active" if HAS_PYNPUT else "pynput not installed — UI only"
        hc.write("root/dash", status=status, hint=hint_text)

        # mount/update machine cards
        for mid, info in machines.items():
            card_path = f"root/dash/{mid[:16]}"
            m_name = info.get("name", mid[:16]) if isinstance(info.get("name"), str) else mid[:16]
            m_os = info.get("os", "?") if isinstance(info.get("os"), str) else "?"
            is_alive = mid in alive
            is_target = mid == current_target

            # device list HTML
            try:
                devs = info.get("devices", [])
                if isinstance(devs, str):
                    devs = json.loads(devs)
            except:
                devs = []
            dev_html = ""
            for d in devs:
                icon = "⌨️" if d.get("type") == "keyboard" else "🖱️" if d.get("type") == "mouse" else "📶"
                dev_html += f'<div>{icon} {d.get("name", "?")}</div>'
            if not dev_html:
                dev_html = '<div style="color:#444">no devices reported</div>'

            # role badge
            if is_target and mid == ME:
                badge = "⬤ LOCAL"
                badge_color = "#818cf8"
            elif is_target:
                badge = "⬤ TARGET"
                badge_color = "#34d399"
            else:
                badge = "○ IDLE"
                badge_color = "#555"

            border = "#818cf8" if is_target and mid == ME else "#34d399" if is_target else "#252525"
            if not is_alive:
                border = "#3f1515"
                badge = "✕ OFFLINE"
                badge_color = "#ef4444"

            # onclick to select
            onclick_js = f"window.hfSelectTarget('{mid}')"
            card_html = MACHINE_CARD_HTML.replace(
                'style="flex:1;',
                f'onclick="{onclick_js}" style="flex:1;'
            )

            hc.mount(card_path, html=card_html, layer=5)
            hc.write(card_path,
                machine_name=m_name,
                machine_os=m_os,
                role_badge=badge,
                device_list=dev_html,
                border_color=border,
                latency=f"{'online' if is_alive else 'offline'} · {m_os}")

    time.sleep(0.05)