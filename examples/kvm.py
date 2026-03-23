#!/usr/bin/env python3
"""
HyperFlow — switch Bluetooth devices between machines.

    pip install zeroconf
    Mac:    brew install blueutil
    Win:    (built-in PowerShell)

    python -m examples.hyperflow --discovery lan   # both machines
    open http://localhost:8765/hyperflow
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
    def scan(self): return []
    def connect(self, addr): return False, "not supported"
    def disconnect(self, addr): return False, "not supported"
    def name(self): return "none"


class MacBT(BTBackend):
    def name(self): return "blueutil"

    def _run(self, *a):
        try:
            return True, subprocess.check_output(["blueutil"] + list(a), timeout=10, text=True).strip()
        except FileNotFoundError:
            return False, "blueutil not found — brew install blueutil"
        except Exception as e:
            return False, str(e)

    def scan(self):
        ok, out = self._run("--paired", "--format", "json")
        if not ok: return []
        try: raw = json.loads(out)
        except: return []
        devices = []
        for d in raw:
            devices.append({
                "address": d.get("address", ""),
                "name": d.get("name", "Unknown"),
                "connected": d.get("connected", False),
                "type": guess_type(d.get("name", "")),
            })
        return devices

    def connect(self, addr):
        ok, out = self._run("--connect", addr)
        time.sleep(1)
        return ok, f"connected {addr}" if ok else out

    def disconnect(self, addr):
        ok, out = self._run("--disconnect", addr)
        return ok, f"disconnected {addr}" if ok else out


class WinBT(BTBackend):
    def name(self): return "powershell"

    NOISE = {
        "generic attribute profile", "generic access profile",
        "device information service", "service discovery service",
        "bluetooth le generic attribute service",
        "bluetooth device (rfcomm protocol tdi)",
    }

    def scan(self):
        try:
            ps = (
                'Get-PnpDevice -Class Bluetooth,BTHLE,BTHLEDevice -Status OK '
                '-ErrorAction SilentlyContinue | '
                'Select-Object FriendlyName,InstanceId,Status,Class | ConvertTo-Json'
            )
            out = subprocess.check_output(["powershell", "-Command", ps], timeout=10, text=True)
            raw = json.loads(out)
            if isinstance(raw, dict): raw = [raw]

            seen = {}
            for d in raw:
                name = d.get("FriendlyName", "Unknown")
                iid = d.get("InstanceId", "")
                nl = name.lower().strip()
                if nl in self.NOISE: continue
                if any(s in nl for s in ["radio", "enumerator", "protocol tdi"]): continue
                if "bthledevice" in iid.lower() and "{0000" in iid.lower(): continue

                addr = self._addr(iid)
                if not addr: continue
                if addr in seen:
                    if len(name) < len(seen[addr]["name"]): seen[addr]["name"] = name
                    continue

                seen[addr] = {
                    "address": addr, "name": name,
                    "connected": d.get("Status") == "OK",
                    "type": guess_type(name), "iid": iid,
                }
            return list(seen.values())
        except Exception as e:
            print(f"BT scan: {e}")
            return []

    def connect(self, addr):
        try:
            ps = f'$d=Get-PnpDevice|Where-Object{{$_.InstanceId -like "*{addr.replace(":","").replace("-","")}*"}};if($d){{Enable-PnpDevice -InstanceId $d.InstanceId -Confirm:$false}}'
            subprocess.check_output(["powershell", "-Command", ps], timeout=10, text=True)
            return True, f"enabled {addr}"
        except Exception as e:
            return False, str(e)

    def disconnect(self, addr):
        try:
            ps = f'$d=Get-PnpDevice|Where-Object{{$_.InstanceId -like "*{addr.replace(":","").replace("-","")}*"}};if($d){{Disable-PnpDevice -InstanceId $d.InstanceId -Confirm:$false}}'
            subprocess.check_output(["powershell", "-Command", ps], timeout=10, text=True)
            return True, f"disabled {addr}"
        except Exception as e:
            return False, str(e)

    def _addr(self, iid):
        for part in iid.replace("\\", "/").split("/"):
            clean = part.replace("-", "").replace(":", "")
            if len(clean) == 12 and all(c in "0123456789abcdefABCDEF" for c in clean):
                return ":".join(clean[i:i+2] for i in range(0, 12, 2)).upper()
        return ""


def guess_type(name):
    n = name.lower()
    if any(k in n for k in ["keyboard", "keychron", "k380", "g915", "mx keys"]): return "keyboard"
    if any(k in n for k in ["mouse", "trackpad", "mx master", "g502"]): return "mouse"
    if any(k in n for k in ["airpod", "headphone", "buds", "speaker"]): return "audio"
    if any(k in n for k in ["xbox", "controller", "gamepad"]): return "gamepad"
    if any(k in n for k in ["iphone", "ipad", "phone"]): return "phone"
    return "other"


def get_bt():
    if MY_OS == "Darwin":
        b = MacBT()
        ok, _ = b._run("--version")
        if ok: print("bluetooth: blueutil"); return b
        print("bluetooth: blueutil not found — brew install blueutil")
    if MY_OS == "Windows":
        print("bluetooth: powershell"); return WinBT()
    print("bluetooth: not supported"); return BTBackend()


def dev_icon(t):
    return {"keyboard": "⌨️", "mouse": "🖱️", "audio": "🎧", "gamepad": "🎮", "phone": "📱"}.get(t, "📶")


# ---------------------------------------------------------------------------
# Args & Connect
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser()
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
# Helper: write machine data using 'data' field
# The relay only snapshots entries with CONTENT_FIELDS (html, css, js, data, etc.)
# Raw fields like machine_id, name, os get silently dropped.
# So we wrap everything in data=json.dumps({...})
# ---------------------------------------------------------------------------
def write_machine_info(devices):
    info = {"machine_id": ME, "name": MY_NAME, "os": MY_OS,
            "bt": bt.name(), "devices": devices, "t": time.time()}
    hc.write(f"_machines/{ME}/info", data=json.dumps(info))

def write_heartbeat():
    hc.write(f"_machines/{ME}/presence", data=json.dumps({"status": "online", "t": time.time()}))

def read_machine_info(snap_value):
    """Parse machine info from snapshot entry."""
    raw = snap_value.get("data", "{}")
    if isinstance(raw, str):
        try: return json.loads(raw)
        except: return {}
    return raw if isinstance(raw, dict) else {}

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
  window.hfAction=function(action,machine,addr){
    window.$scene.get("inbox/"+Date.now()+"_"+Math.random().toString(36).slice(2,7)).put({
      data:JSON.stringify({type:action,machine:machine,address:addr})
    });
  };
  var btn=document.getElementById("btn_scan");
  if(btn&&!btn.dataset.on){btn.dataset.on=1;btn.onclick=function(){window.hfAction("rescan","","");};}
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

def device_row_html(dev, machine_id, is_me):
    addr = dev.get("address", "?")
    name = dev.get("name", "Unknown")
    connected = dev.get("connected", False)
    icon = dev_icon(dev.get("type", "other"))
    dot = "#34d399" if connected else "#555"
    ctxt = "connected" if connected else "paired"

    if is_me and connected:
        btns = f'<button onclick="window.hfAction(\'disconnect\',\'{machine_id}\',\'{addr}\')" style="padding:4px 10px;background:#1a1a1a;color:#ef4444;border:1px solid #333;border-radius:4px;cursor:pointer;font-size:11px">Disconnect</button>'
    elif is_me:
        btns = f'<button onclick="window.hfAction(\'connect\',\'{machine_id}\',\'{addr}\')" style="padding:4px 10px;background:#818cf8;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:11px">Connect</button>'
    elif connected:
        btns = f'<button onclick="window.hfAction(\'steal\',\'{machine_id}\',\'{addr}\')" style="padding:4px 10px;background:#f59e0b;color:#000;border:none;border-radius:4px;cursor:pointer;font-size:11px;font-weight:600">Steal →</button>'
    else:
        btns = ''

    return f"""<div style="padding:12px 20px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1a1a1a">
  <div style="display:flex;align-items:center;gap:10px">
    <div style="font-size:16px">{icon}</div>
    <div><div style="font-size:13px;font-weight:500">{name}</div><div style="font-size:10px;color:#555">{addr}</div></div>
  </div>
  <div style="display:flex;align-items:center;gap:8px">
    <div style="width:8px;height:8px;border-radius:50%;background:{dot}"></div>
    <span style="font-size:11px;color:{dot}">{ctxt}</span>
    {btns}
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Mount & initial data
# ---------------------------------------------------------------------------
hc.mount("root/dash", html=DASH_HTML, js=DASH_JS, fixed=True, layer=10)
hc.write("root/dash", status="scanning...", me=f"{MY_NAME} · {MY_OS}", log="starting...")

devices = bt.scan()
write_machine_info(devices)
write_heartbeat()

# give the relay a moment to process
time.sleep(0.5)

print(f"found {len(devices)} BT device(s)")
for d in devices:
    c = "✓" if d.get("connected") else "·"
    print(f"  {c} {d['name']} ({d['address']})")

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
seen = set()
last_hb = time.time()
last_ui = 0
last_scan = time.time()
log_msg = "ready"

while True:
    try:
        now = time.time()
        snap = hc.snapshot() or {}

        # heartbeat every 3s
        if now - last_hb > 3.0:
            write_heartbeat()
            last_hb = now

        # rescan every 15s
        if now - last_scan > 15.0:
            devices = bt.scan()
            write_machine_info(devices)
            last_scan = now

        # inbox
        for k, v in list(snap.items()):
            if not k.startswith("inbox/") or k in seen: continue
            seen.add(k)
            try:
                raw = v.get("data", "{}"); msg = json.loads(raw) if isinstance(raw, str) else raw
                action = msg.get("type", "")
                addr = msg.get("address", "")
                tmach = msg.get("machine", "")

                if action == "rescan":
                    devices = bt.scan()
                    write_machine_info(devices)
                    last_scan = now
                    log_msg = f"rescanned — {len(devices)} devices"

                elif action == "connect" and tmach == ME:
                    ok, txt = bt.connect(addr); log_msg = txt; print(f"connect: {txt}")
                    devices = bt.scan(); write_machine_info(devices)

                elif action == "disconnect" and tmach == ME:
                    ok, txt = bt.disconnect(addr); log_msg = txt; print(f"disconnect: {txt}")
                    devices = bt.scan(); write_machine_info(devices)

                elif action == "steal":
                    log_msg = f"stealing {addr}..."
                    hc.write(f"_cmd/{tmach}", data=json.dumps({"action": "disconnect", "address": addr, "t": now}))
                    time.sleep(2)
                    ok, txt = bt.connect(addr); log_msg = f"steal: {txt}"; print(log_msg)
                    devices = bt.scan(); write_machine_info(devices)
            except Exception as e:
                log_msg = f"error: {e}"; print(log_msg)
            hc.remove(k)

        # check commands
        cmd_raw = snap.get(f"_cmd/{ME}")
        if cmd_raw:
            cmd = read_machine_info(cmd_raw)
            if cmd.get("action") == "disconnect" and now - float(cmd.get("t", 0)) < 10:
                addr = cmd.get("address", "")
                print(f"cmd: disconnect {addr}")
                bt.disconnect(addr)
                devices = bt.scan(); write_machine_info(devices)
            hc.remove(f"_cmd/{ME}")

        # UI refresh every 1.5s
        if now - last_ui > 1.5:
            last_ui = now

            # gather machines from snapshot
            machines = {}
            alive = set()
            for k, v in snap.items():
                if k.startswith("_machines/") and k.endswith("/info"):
                    mid = k.split("/")[1]
                    machines[mid] = read_machine_info(v)
                elif k.startswith("_machines/") and k.endswith("/presence"):
                    mid = k.split("/")[1]
                    p = read_machine_info(v)
                    try:
                        if now - float(p.get("t", 0)) < 10: alive.add(mid)
                    except: pass

            hc.write("root/dash",
                status=f"{len(alive)} machine(s) online",
                log=log_msg)

            # mount machine cards
            for mid, info in machines.items():
                is_me = mid == ME
                mn = str(info.get("name", mid[:16]))[:20]
                mo = str(info.get("os", "?"))
                mbt = str(info.get("bt", "?"))
                mdevs = info.get("devices", [])
                if isinstance(mdevs, str):
                    try: mdevs = json.loads(mdevs)
                    except: mdevs = []

                mpath = f"root/dash/m_{mid[:12]}"
                icon = "💻" if mo == "Darwin" else "🖥️"
                badge = "THIS MACHINE" if is_me else ("online" if mid in alive else "offline")

                hc.mount(mpath, html=MACHINE_HTML, layer=5)
                hc.write(mpath, icon=icon, mname=mn, msub=f"{mo} · {mbt}", mbadge=badge)

                for i, dev in enumerate(mdevs):
                    dpath = f"{mpath}/d_{i}"
                    hc.mount(dpath, html=device_row_html(dev, mid, is_me), layer=5)

        time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nshutting down...")
        hc.stop()
        break
    except Exception as e:
        print(f"loop error: {e}")
        time.sleep(1)