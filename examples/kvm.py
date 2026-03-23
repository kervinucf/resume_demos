#!/usr/bin/env python3
"""
HyperFlow — switch Bluetooth devices between machines.

    pip install zeroconf

    Mac:    brew install blueutil
    Win:    (uses built-in PowerShell)

    # Both machines
    python -m examples.hyperflow --discovery lan

    Open http://localhost:8765/hyperflow
    See all BT devices on all machines. Click to steal a device.

How it works:
    Each machine scans its Bluetooth adapter and writes the device list
    to the shared graph. The web UI shows every device on every machine.
    When you click "connect" on a device, that machine's script runs
    the native BT connect command. The device switches over.

    No input interception. No accessibility permissions. No pynput.
    Just Bluetooth connection management.
"""

import argparse
import json
import platform
import subprocess
import time

from HyperCoreSDK.client import HyperClient

MY_OS = platform.system()

# ---------------------------------------------------------------------------
# Bluetooth backends
# ---------------------------------------------------------------------------

class BTBackend:
    """Base — no-op."""
    def scan(self): return []
    def connect(self, addr): return False, "not supported"
    def disconnect(self, addr): return False, "not supported"
    def name(self): return "none"


class MacBT(BTBackend):
    """Uses blueutil (brew install blueutil)."""

    def name(self): return "blueutil"

    def _run(self, *args):
        try:
            out = subprocess.check_output(["blueutil"] + list(args), timeout=10, text=True)
            return True, out.strip()
        except FileNotFoundError:
            return False, "blueutil not found — brew install blueutil"
        except subprocess.CalledProcessError as e:
            return False, str(e)
        except Exception as e:
            return False, str(e)

    def scan(self):
        ok, out = self._run("--paired", "--format", "json")
        if not ok:
            print(f"BT scan failed: {out}")
            return []

        try:
            raw = json.loads(out)
        except:
            return []

        devices = []
        for d in raw:
            devices.append({
                "address": d.get("address", ""),
                "name": d.get("name", "Unknown"),
                "connected": d.get("connected", False),
                "paired": True,
                "type": self._guess_type(d.get("name", "")),
            })
        return devices

    def connect(self, addr):
        ok, out = self._run("--connect", addr)
        if ok:
            # wait a beat for connection
            time.sleep(1)
            return True, f"connected {addr}"
        return False, out

    def disconnect(self, addr):
        ok, out = self._run("--disconnect", addr)
        return ok, out if not ok else f"disconnected {addr}"

    def _guess_type(self, name):
        n = name.lower()
        if any(k in n for k in ["keyboard", "keychron", "hhkb", "k380", "k860", "mx keys"]):
            return "keyboard"
        if any(k in n for k in ["mouse", "trackpad", "mx master", "m720", "ergo"]):
            return "mouse"
        if any(k in n for k in ["airpod", "headphone", "buds", "speaker", "jabra", "sony"]):
            return "audio"
        return "other"


class WinBT(BTBackend):
    """Uses PowerShell to manage Bluetooth."""

    def name(self): return "powershell"

    def scan(self):
        try:
            # Get paired BT devices
            ps = (
                'Get-PnpDevice -Class Bluetooth -Status OK | '
                'Select-Object FriendlyName,InstanceId,Status | '
                'ConvertTo-Json'
            )
            out = subprocess.check_output(
                ["powershell", "-Command", ps], timeout=10, text=True)
            raw = json.loads(out)
            if isinstance(raw, dict):
                raw = [raw]

            devices = []
            for d in raw:
                name = d.get("FriendlyName", "Unknown")
                iid = d.get("InstanceId", "")
                # Extract BT address from InstanceId if present
                addr = self._extract_addr(iid)
                if not addr or "radio" in name.lower() or "enumerator" in name.lower():
                    continue
                devices.append({
                    "address": addr,
                    "name": name,
                    "connected": d.get("Status") == "OK",
                    "paired": True,
                    "type": self._guess_type(name),
                })
            return devices
        except Exception as e:
            print(f"BT scan failed: {e}")
            return []

    def connect(self, addr):
        # Windows BT connect via PowerShell/devcon is limited
        # The most reliable way is via the BT settings UI
        # For programmatic control we can try:
        try:
            ps = f'''
            $device = Get-PnpDevice | Where-Object {{ $_.InstanceId -like "*{addr.replace(':','')}*" }}
            if ($device) {{ Enable-PnpDevice -InstanceId $device.InstanceId -Confirm:$false }}
            '''
            subprocess.check_output(["powershell", "-Command", ps], timeout=10, text=True)
            return True, f"enabled {addr}"
        except Exception as e:
            return False, str(e)

    def disconnect(self, addr):
        try:
            ps = f'''
            $device = Get-PnpDevice | Where-Object {{ $_.InstanceId -like "*{addr.replace(':','')}*" }}
            if ($device) {{ Disable-PnpDevice -InstanceId $device.InstanceId -Confirm:$false }}
            '''
            subprocess.check_output(["powershell", "-Command", ps], timeout=10, text=True)
            return True, f"disabled {addr}"
        except Exception as e:
            return False, str(e)

    def _extract_addr(self, instance_id):
        """Try to pull a BT MAC from the PnP InstanceId."""
        # Format: BTHENUM\{...}_VID&..._PID&...\{addr}
        # or BLUETOOTHDEVICE\xx:xx:xx:xx:xx:xx
        parts = instance_id.replace("\\", "/").split("/")
        for part in parts:
            clean = part.replace("-", "").replace(":", "")
            if len(clean) == 12 and all(c in "0123456789abcdefABCDEF" for c in clean):
                # Format as XX:XX:XX:XX:XX:XX
                return ":".join(clean[i:i+2] for i in range(0, 12, 2)).upper()
        return instance_id[:20]  # fallback — use truncated ID

    def _guess_type(self, name):
        n = name.lower()
        if any(k in n for k in ["keyboard", "keychron", "k380"]): return "keyboard"
        if any(k in n for k in ["mouse", "trackpad", "mx master"]): return "mouse"
        if any(k in n for k in ["airpod", "headphone", "buds", "speaker"]): return "audio"
        return "other"


def get_bt():
    if MY_OS == "Darwin":
        b = MacBT()
        ok, _ = b._run("--version")
        if ok:
            print("bluetooth: blueutil (macOS)")
            return b
        print("bluetooth: blueutil not found — brew install blueutil")

    if MY_OS == "Windows":
        print("bluetooth: powershell (Windows)")
        return WinBT()

    print("bluetooth: not supported on this OS")
    return BTBackend()


# ---------------------------------------------------------------------------
# Args & Connect
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="HyperFlow — BT device switching")
parser.add_argument("--discovery", default="local", choices=["local", "lan", "trusted"])
parser.add_argument("--relay", default="auto", choices=["auto", "host", "join"])
parser.add_argument("--peers", nargs="*", default=[])
parser.add_argument("--port", type=int, default=8765)
parser.add_argument("--root", default="hyperflow")
args = parser.parse_args()

hc = HyperClient(root=args.root, discovery=args.discovery, relay=args.relay,
    peers=[f"http://{p}:{args.port}" for p in args.peers], port=args.port)
hc.connect()
hc.clear()

ME = hc.machine_id
MY_NAME = hc.machine_name
bt = get_bt()

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

DASH_HTML = """
<div style="width:100%;height:100%;background:#0a0a0a;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,sans-serif;display:flex;flex-direction:column">

  <div style="padding:20px 24px;background:#111;border-bottom:1px solid #222;display:flex;justify-content:space-between;align-items:center">
    <div style="display:flex;align-items:center;gap:12px">
      <div style="font-size:22px;font-weight:800;letter-spacing:1px;color:#818cf8">HYPERFLOW</div>
      <div style="font-size:12px;color:#555;background:#1a1a1a;padding:4px 10px;border-radius:4px" data-bind-text="status">scanning...</div>
    </div>
    <div style="display:flex;gap:8px;align-items:center">
      <button id="btn_scan" style="padding:6px 14px;background:#1a1a1a;color:#818cf8;border:1px solid #333;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">Rescan</button>
      <div style="font-size:11px;color:#555" data-bind-text="me"></div>
    </div>
  </div>

  <div style="flex:1;overflow:auto;padding:24px">
    <div data-children style="display:flex;flex-direction:column;gap:16px"></div>
  </div>

  <div style="padding:12px 24px;background:#111;border-top:1px solid #222;font-size:11px;color:#555" data-bind-text="log">ready</div>

</div>
"""

DASH_JS = r"""
(function(){
  if(document.getElementById("_hf"))return;
  var m=document.createElement("div");m.id="_hf";m.style.display="none";document.body.appendChild(m);

  window.hfAction=function(action, machine, addr){
    window.$scene.get("inbox/"+Date.now()+"_"+Math.random().toString(36).slice(2,7)).put({
      data:JSON.stringify({type:action, machine:machine, address:addr})
    });
  };

  var btn=document.getElementById("btn_scan");
  if(btn&&!btn.dataset.on){
    btn.dataset.on=1;
    btn.onclick=function(){ window.hfAction("rescan","",""); };
  }
})();
"""

MACHINE_HTML = """
<div style="background:#111;border:1px solid #222;border-radius:10px;overflow:hidden">
  <div style="padding:14px 20px;background:#151515;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #222">
    <div style="display:flex;align-items:center;gap:10px">
      <div data-bind-text="icon" style="font-size:20px"></div>
      <div>
        <div data-bind-text="mname" style="font-weight:700;font-size:15px"></div>
        <div data-bind-text="msub" style="font-size:11px;color:#555"></div>
      </div>
    </div>
    <div data-bind-text="mbadge" style="font-size:11px;padding:3px 8px;border-radius:4px;background:#1a1a1a;color:#818cf8"></div>
  </div>
  <div data-children style="display:flex;flex-direction:column"></div>
</div>
"""

DEVICE_HTML_TEMPLATE = """
<div style="padding:12px 20px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1a1a1a">
  <div style="display:flex;align-items:center;gap:10px">
    <div style="font-size:16px">{icon}</div>
    <div>
      <div style="font-size:13px;font-weight:500">{name}</div>
      <div style="font-size:10px;color:#555">{addr}</div>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:8px">
    <div style="width:8px;height:8px;border-radius:50%;background:{dot}"></div>
    <span style="font-size:11px;color:{dot}">{conn_text}</span>
    {buttons}
  </div>
</div>
"""


def device_icon(dtype):
    if dtype == "keyboard": return "⌨️"
    if dtype == "mouse": return "🖱️"
    if dtype == "audio": return "🎧"
    return "📶"


def device_row(dev, machine_id, is_me):
    addr = dev.get("address", "?")
    name = dev.get("name", "Unknown")
    connected = dev.get("connected", False)
    dtype = dev.get("type", "other")

    dot = "#34d399" if connected else "#555"
    conn_text = "connected" if connected else "paired"
    icon = device_icon(dtype)

    if is_me:
        if connected:
            buttons = f'<button onclick="window.hfAction(\'disconnect\',\'{machine_id}\',\'{addr}\')" style="padding:4px 10px;background:#1a1a1a;color:#ef4444;border:1px solid #333;border-radius:4px;cursor:pointer;font-size:11px">Disconnect</button>'
        else:
            buttons = f'<button onclick="window.hfAction(\'connect\',\'{machine_id}\',\'{addr}\')" style="padding:4px 10px;background:#818cf8;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:11px">Connect</button>'
    else:
        if connected:
            buttons = f'<button onclick="window.hfAction(\'steal\',\'{machine_id}\',\'{addr}\')" style="padding:4px 10px;background:#f59e0b;color:#000;border:none;border-radius:4px;cursor:pointer;font-size:11px;font-weight:600">Steal →</button>'
        else:
            buttons = '<span style="font-size:11px;color:#333">—</span>'

    return DEVICE_HTML_TEMPLATE.format(
        icon=icon, name=name, addr=addr, dot=dot, conn_text=conn_text, buttons=buttons)


# ---------------------------------------------------------------------------
# Mount
# ---------------------------------------------------------------------------
hc.mount("root/dash", html=DASH_HTML, js=DASH_JS, fixed=True, layer=10)
hc.write("root/dash", status="scanning...", me=f"{MY_NAME} · {MY_OS}", log="starting up...")

# Initial scan
devices = bt.scan()
hc.write(f"_machines/{ME}/info",
    machine_id=ME, name=MY_NAME, os=MY_OS, bt=bt.name(),
    devices=json.dumps(devices), t=time.time())

print(f"found {len(devices)} BT device(s)")
for d in devices:
    c = "✓" if d["connected"] else "·"
    print(f"  {c} {d['name']} ({d['address']})")

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
seen = set()
last_hb = 0
last_ui = 0
last_scan = time.time()
log_msg = "ready"

while True:
    now = time.time()
    snap = hc.snapshot() or {}

    # heartbeat
    if now - last_hb > 2.0:
        hc.write(f"_machines/{ME}/presence", status="online", t=now)
        last_hb = now

    # periodic rescan
    if now - last_scan > 15.0:
        devices = bt.scan()
        hc.write(f"_machines/{ME}/info",
            machine_id=ME, name=MY_NAME, os=MY_OS, bt=bt.name(),
            devices=json.dumps(devices), t=now)
        last_scan = now

    # inbox
    for k, v in snap.items():
        if not k.startswith("inbox/") or k in seen:
            continue
        seen.add(k)
        try:
            raw = v.get("data", "{}")
            msg = json.loads(raw) if isinstance(raw, str) else raw
            action = msg.get("type", "")
            addr = msg.get("address", "")
            target_machine = msg.get("machine", "")

            if action == "rescan":
                devices = bt.scan()
                hc.write(f"_machines/{ME}/info",
                    machine_id=ME, name=MY_NAME, os=MY_OS, bt=bt.name(),
                    devices=json.dumps(devices), t=now)
                last_scan = now
                log_msg = f"rescanned — {len(devices)} devices"

            elif action == "connect" and target_machine == ME:
                ok, msg_text = bt.connect(addr)
                log_msg = msg_text
                print(f"connect {addr}: {msg_text}")
                # rescan after action
                devices = bt.scan()
                hc.write(f"_machines/{ME}/info",
                    machine_id=ME, name=MY_NAME, os=MY_OS, bt=bt.name(),
                    devices=json.dumps(devices), t=now)

            elif action == "disconnect" and target_machine == ME:
                ok, msg_text = bt.disconnect(addr)
                log_msg = msg_text
                print(f"disconnect {addr}: {msg_text}")
                devices = bt.scan()
                hc.write(f"_machines/{ME}/info",
                    machine_id=ME, name=MY_NAME, os=MY_OS, bt=bt.name(),
                    devices=json.dumps(devices), t=now)

            elif action == "steal":
                # "steal" means: disconnect from that machine, connect to ME
                # We write a disconnect command for the other machine
                # and a connect command for ourselves
                log_msg = f"stealing {addr} from {target_machine[:12]}..."
                print(log_msg)

                # tell the other machine to disconnect
                hc.write(f"_cmd/{target_machine}", action="disconnect", address=addr, t=now)

                # wait a moment, then connect locally
                time.sleep(2)
                ok, msg_text = bt.connect(addr)
                log_msg = f"steal: {msg_text}"
                print(log_msg)
                devices = bt.scan()
                hc.write(f"_machines/{ME}/info",
                    machine_id=ME, name=MY_NAME, os=MY_OS, bt=bt.name(),
                    devices=json.dumps(devices), t=now)

        except Exception as e:
            log_msg = f"error: {e}"
            print(f"error: {e}")
        hc.remove(k)

    # check for commands directed at us
    cmd = snap.get(f"_cmd/{ME}")
    if cmd and cmd.get("action") == "disconnect":
        addr = cmd.get("address", "")
        cmd_t = float(cmd.get("t", 0))
        if now - cmd_t < 10:  # fresh command
            print(f"command: disconnect {addr}")
            bt.disconnect(addr)
            devices = bt.scan()
            hc.write(f"_machines/{ME}/info",
                machine_id=ME, name=MY_NAME, os=MY_OS, bt=bt.name(),
                devices=json.dumps(devices), t=now)
        hc.remove(f"_cmd/{ME}")

    # UI refresh
    if now - last_ui > 1.5:
        last_ui = now

        machines = {}
        for k, v in snap.items():
            if k.startswith("_machines/") and k.endswith("/info"):
                machines[k.split("/")[1]] = v

        alive = set()
        for k, v in snap.items():
            if k.startswith("_machines/") and k.endswith("/presence"):
                try:
                    if now - float(v.get("t", 0)) < 10: alive.add(k.split("/")[1])
                except: pass

        hc.write("root/dash",
            status=f"{len(alive)} machine(s) online",
            log=log_msg)

        for mid, info in machines.items():
            is_me = mid == ME
            mn = info.get("name", mid[:16])
            if not isinstance(mn, str): mn = str(mn)[:16]
            mo = info.get("os", "?")
            if not isinstance(mo, str): mo = "?"
            mbt = info.get("bt", "?")
            if not isinstance(mbt, str): mbt = "?"

            try:
                mdevs = json.loads(info["devices"]) if isinstance(info.get("devices"), str) else []
            except: mdevs = []

            mpath = f"root/dash/m_{mid[:12]}"
            icon = "💻" if mo == "Darwin" else "🖥️" if mo == "Windows" else "🖥️"
            badge = "THIS MACHINE" if is_me else ("online" if mid in alive else "offline")

            hc.mount(mpath, html=MACHINE_HTML, layer=5)
            hc.write(mpath, icon=icon, mname=mn, msub=f"{mo} · {mbt}",
                mbadge=badge)

            # mount device rows
            for i, dev in enumerate(mdevs):
                dpath = f"{mpath}/d_{i}"
                row_html = device_row(dev, mid, is_me)
                hc.mount(dpath, html=row_html, layer=5)

    time.sleep(0.1)