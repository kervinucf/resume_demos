#!/usr/bin/env python3
"""
HyperFlow KVM — hot-swap Bluetooth devices between machines.

No input interception. No accessibility permissions. No pynput.
Just tells the Bluetooth adapter to connect or disconnect devices.

    pip install zeroconf
    Mac:  brew install blueutil
    Win:  (built-in PowerShell)

    # Machine A (Mac)
    python -m examples.kvm --discovery lan

    # Machine B (Windows)
    python -m examples.kvm --discovery lan

    Open http://localhost:8765/kvm on either machine.
    See every BT device on every machine.
    Click "Steal" to yank a device to your machine.
"""

import argparse
import json
import platform
import subprocess
import time
import traceback

from HyperCoreSDK.client import HyperClient

MY_OS = platform.system()  # "Darwin" or "Windows"


# ============================================================
# Bluetooth backends
# ============================================================

def guess_type(name):
    n = name.lower()
    for kw in ("keyboard", "keychron", "k380", "k860", "g915", "mx keys", "hhkb"):
        if kw in n: return "keyboard"
    for kw in ("mouse", "trackpad", "mx master", "g502", "g pro", "m720", "ergo"):
        if kw in n: return "mouse"
    for kw in ("airpod", "headphone", "buds", "speaker", "jabra", "sony wh", "beats"):
        if kw in n: return "audio"
    for kw in ("xbox", "controller", "gamepad", "dualsense", "joycon"):
        if kw in n: return "gamepad"
    for kw in ("iphone", "ipad", "galaxy", "pixel"):
        if kw in n: return "phone"
    return "other"


def dev_icon(t):
    return {"keyboard": "⌨️", "mouse": "🖱️", "audio": "🎧",
            "gamepad": "🎮", "phone": "📱"}.get(t, "📶")


class BT:
    """Base — no BT support."""
    def scan(self):       return []
    def connect(self, a): return False, "no bluetooth backend"
    def disconnect(self, a): return False, "no bluetooth backend"
    def label(self):      return "none"


class MacBT(BT):
    """blueutil — brew install blueutil"""

    def label(self): return "blueutil"

    def _blu(self, *args):
        try:
            out = subprocess.check_output(
                ["blueutil"] + list(args), timeout=10, text=True,
                stderr=subprocess.STDOUT)
            return True, out.strip()
        except FileNotFoundError:
            return False, "blueutil not found — run: brew install blueutil"
        except subprocess.CalledProcessError as e:
            return False, e.output.strip() if e.output else str(e)
        except Exception as e:
            return False, str(e)

    def scan(self):
        ok, out = self._blu("--paired", "--format", "json")
        if not ok:
            print(f"  blueutil scan failed: {out}")
            return []
        try:
            raw = json.loads(out)
        except json.JSONDecodeError:
            return []
        return [{
            "address": d.get("address", ""),
            "name":    d.get("name", "Unknown"),
            "connected": bool(d.get("connected")),
            "type":    guess_type(d.get("name", "")),
        } for d in raw if d.get("address")]

    def connect(self, addr):
        ok, out = self._blu("--connect", addr)
        if ok:
            time.sleep(1.5)  # give BT a moment
            return True, f"connected {addr}"
        return False, out

    def disconnect(self, addr):
        ok, out = self._blu("--disconnect", addr)
        return (True, f"disconnected {addr}") if ok else (False, out)


class WinBT(BT):
    """PowerShell — built-in on Windows."""

    def label(self): return "powershell"

    # BLE service names that are NOT real devices
    JUNK = frozenset({
        "generic attribute profile", "generic access profile",
        "device information service", "service discovery service",
        "bluetooth le generic attribute service",
        "bluetooth device (rfcomm protocol tdi)",
        "microsoft bluetooth le enumerator",
        "microsoft bluetooth enumerator",
    })

    def scan(self):
        try:
            ps = (
                'Get-PnpDevice -Class Bluetooth,BTHLE -Status OK '
                '-ErrorAction SilentlyContinue | '
                'Select-Object FriendlyName,InstanceId,Status | '
                'ConvertTo-Json -Compress'
            )
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps],
                timeout=15, text=True, stderr=subprocess.DEVNULL)
            raw = json.loads(out)
            if isinstance(raw, dict): raw = [raw]
        except Exception as e:
            print(f"  powershell scan failed: {e}")
            return []

        seen = {}
        for d in raw:
            name = (d.get("FriendlyName") or "").strip()
            iid  = (d.get("InstanceId") or "")

            # skip junk
            if name.lower() in self.JUNK:
                continue
            if any(j in name.lower() for j in ("radio", "enumerator")):
                continue
            # skip GATT service entries (they have a UUID in the instance ID)
            if "bthledevice" in iid.lower():
                continue
            if not name:
                continue

            addr = self._addr_from_iid(iid)
            if not addr:
                continue

            # dedupe by address — keep shortest/cleanest name
            if addr in seen:
                if len(name) < len(seen[addr]["name"]):
                    seen[addr]["name"] = name
                continue

            seen[addr] = {
                "address":   addr,
                "name":      name,
                "connected": True,  # Status=OK means it's active
                "type":      guess_type(name),
                "iid":       iid,
            }

        return list(seen.values())

    def connect(self, addr):
        return self._set_device(addr, enable=True)

    def disconnect(self, addr):
        return self._set_device(addr, enable=False)

    def _set_device(self, addr, enable=True):
        verb = "Enable" if enable else "Disable"
        clean = addr.replace(":", "").replace("-", "").upper()
        ps = (
            f'$devs = Get-PnpDevice | Where-Object {{ '
            f'$_.InstanceId -like "*{clean}*" }}; '
            f'foreach ($d in $devs) {{ '
            f'{verb}-PnpDevice -InstanceId $d.InstanceId -Confirm:$false '
            f'-ErrorAction SilentlyContinue }}'
        )
        try:
            subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps],
                timeout=15, text=True, stderr=subprocess.DEVNULL)
            return True, f"{verb.lower()}d {addr}"
        except Exception as e:
            return False, str(e)

    def _addr_from_iid(self, iid):
        """Extract a BT MAC address from a PnP InstanceId string."""
        # Examples:
        #   BTHLE\DEV_E34BCEC43832\...    → E3:4B:CE:C4:38:32
        #   BTHENUM\DEV_08FF44207BE0\...  → 08:FF:44:20:7B:E0
        for part in iid.replace("\\", "/").split("/"):
            # DEV_XXXXXXXXXXXX pattern
            if part.upper().startswith("DEV_"):
                hex_part = part[4:].replace("-", "")
                if len(hex_part) >= 12 and all(c in "0123456789abcdefABCDEF" for c in hex_part[:12]):
                    h = hex_part[:12].upper()
                    return ":".join(h[i:i+2] for i in range(0, 12, 2))
            # raw 12-hex-digit part
            clean = part.replace("-", "").replace(":", "")
            if len(clean) == 12 and all(c in "0123456789abcdefABCDEF" for c in clean):
                h = clean.upper()
                return ":".join(h[i:i+2] for i in range(0, 12, 2))
        return ""


def make_bt():
    if MY_OS == "Darwin":
        b = MacBT()
        ok, _ = b._blu("--version")
        if ok:
            print(f"bluetooth backend: blueutil (macOS)")
            return b
        print("blueutil not found — run: brew install blueutil")

    if MY_OS == "Windows":
        print(f"bluetooth backend: powershell (Windows)")
        return WinBT()

    print("no bluetooth backend available")
    return BT()


# ============================================================
# Hyper graph helpers
# ============================================================
# The relay only keeps snapshot entries with CONTENT_FIELDS:
#   html, css, js, data, meta, links, actions, layer, fixed, portal, ...
# Arbitrary fields like name=, os= get silently dropped.
# So ALL machine data must go through data=json.dumps({...}).

def pack(obj):
    """Wrap a dict in the data field for the relay."""
    return json.dumps(obj)

def unpack(snap_val):
    """Read a data-field entry back out."""
    if not isinstance(snap_val, dict):
        return {}
    raw = snap_val.get("data", "{}")
    if isinstance(raw, str):
        try: return json.loads(raw)
        except: return {}
    return raw if isinstance(raw, dict) else {}


# ============================================================
# UI templates
# ============================================================

DASH_HTML = """
<div style="width:100%;height:100%;background:#09090b;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;display:flex;flex-direction:column">
  <div style="padding:20px 24px;background:#18181b;border-bottom:1px solid #27272a;display:flex;justify-content:space-between;align-items:center">
    <div style="display:flex;align-items:center;gap:14px">
      <div style="font-size:24px;font-weight:900;letter-spacing:2px;background:linear-gradient(135deg,#818cf8,#c084fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent">HYPERFLOW</div>
      <div style="font-size:12px;color:#71717a;background:#27272a;padding:5px 12px;border-radius:6px" data-bind-text="status">starting...</div>
    </div>
    <div style="display:flex;gap:10px;align-items:center">
      <button id="btn_scan" style="padding:7px 16px;background:#27272a;color:#a78bfa;border:1px solid #3f3f46;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">↻ Rescan All</button>
      <div style="font-size:11px;color:#52525b" data-bind-text="me"></div>
    </div>
  </div>
  <div style="flex:1;overflow:auto;padding:24px">
    <div data-children style="display:flex;flex-direction:column;gap:20px"></div>
  </div>
  <div style="padding:14px 24px;background:#18181b;border-top:1px solid #27272a;display:flex;justify-content:space-between;align-items:center">
    <div style="font-size:12px;color:#52525b" data-bind-text="log">ready</div>
    <div style="font-size:11px;color:#3f3f46" data-bind-text="ts"></div>
  </div>
</div>
"""

DASH_JS = r"""
(function(){
  if(document.getElementById("_hf"))return;
  var m=document.createElement("div");m.id="_hf";m.style.display="none";document.body.appendChild(m);
  window.hf=function(action,machine,addr){
    window.$scene.get("inbox/"+Date.now()+"_"+Math.random().toString(36).slice(2,7)).put({
      data:JSON.stringify({type:action,machine:machine,address:addr})
    });
  };
  var b=document.getElementById("btn_scan");
  if(b&&!b.dataset.on){b.dataset.on=1;b.onclick=function(){window.hf("rescan","","");};}
})();
"""

MACHINE_HTML = """
<div style="background:#18181b;border:1px solid #27272a;border-radius:12px;overflow:hidden">
  <div style="padding:16px 20px;background:#1c1c1f;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #27272a">
    <div style="display:flex;align-items:center;gap:12px">
      <div data-bind-text="icon" style="font-size:22px"></div>
      <div>
        <div data-bind-text="mname" style="font-weight:700;font-size:16px"></div>
        <div data-bind-text="msub" style="font-size:11px;color:#52525b;margin-top:2px"></div>
      </div>
    </div>
    <div data-bind-text="badge" style="font-size:11px;font-weight:600;padding:4px 10px;border-radius:6px;background:#27272a;color:#a78bfa"></div>
  </div>
  <div data-children style="display:flex;flex-direction:column"></div>
</div>
"""


def device_row(dev, machine_id, is_me):
    addr = dev.get("address", "?")
    name = dev.get("name", "Unknown")
    conn = dev.get("connected", False)
    icon = dev_icon(dev.get("type", "other"))
    dot  = "#4ade80" if conn else "#52525b"
    txt  = "connected" if conn else "paired"

    # buttons depend on context
    if is_me and conn:
        btn = (f'<button onclick="window.hf(\'disconnect\',\'{machine_id}\',\'{addr}\')" '
               f'style="padding:5px 12px;background:transparent;color:#f87171;border:1px solid #7f1d1d;'
               f'border-radius:5px;cursor:pointer;font-size:11px;font-weight:600">Disconnect</button>')
    elif is_me and not conn:
        btn = (f'<button onclick="window.hf(\'connect\',\'{machine_id}\',\'{addr}\')" '
               f'style="padding:5px 12px;background:#6d28d9;color:#fff;border:none;'
               f'border-radius:5px;cursor:pointer;font-size:11px;font-weight:600">Connect</button>')
    elif not is_me and conn:
        btn = (f'<button onclick="window.hf(\'steal\',\'{machine_id}\',\'{addr}\')" '
               f'style="padding:5px 12px;background:#d97706;color:#000;border:none;'
               f'border-radius:5px;cursor:pointer;font-size:11px;font-weight:700">⚡ Steal</button>')
    else:
        btn = ''

    return f'''<div style="padding:12px 20px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1c1c1f">
  <div style="display:flex;align-items:center;gap:12px">
    <span style="font-size:18px">{icon}</span>
    <div>
      <div style="font-size:13px;font-weight:500;color:#e4e4e7">{name}</div>
      <div style="font-size:10px;color:#52525b;font-family:monospace">{addr}</div>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:10px">
    <div style="display:flex;align-items:center;gap:5px">
      <div style="width:7px;height:7px;border-radius:50%;background:{dot}"></div>
      <span style="font-size:11px;color:{dot}">{txt}</span>
    </div>
    {btn}
  </div>
</div>'''


# ============================================================
# Main
# ============================================================

parser = argparse.ArgumentParser(description="HyperFlow KVM")
parser.add_argument("--discovery", default="local", choices=["local", "lan", "trusted"])
parser.add_argument("--relay", default="auto", choices=["auto", "host", "join"])
parser.add_argument("--peers", nargs="*", default=[])
parser.add_argument("--port", type=int, default=8765)
parser.add_argument("--root", default="kvm")
args = parser.parse_args()

hc = HyperClient(
    root=args.root, discovery=args.discovery, relay=args.relay,
    peers=[f"http://{p}:{args.port}" for p in args.peers], port=args.port)
hc.connect()
hc.clear()

ME      = hc.machine_id
MY_NAME = hc.machine_name
bt      = make_bt()

# ── helpers ──

def publish_devices(devs):
    """Write this machine's info + device list to the graph."""
    hc.write(f"_m/{ME}/info", data=pack({
        "id": ME, "name": MY_NAME, "os": MY_OS,
        "bt": bt.label(), "devices": devs, "t": time.time(),
    }))

def publish_heartbeat():
    hc.write(f"_m/{ME}/hb", data=pack({"t": time.time()}))

def read_machines(snap):
    """Return {machine_id: info_dict} from snapshot."""
    machines = {}
    for k, v in snap.items():
        if k.startswith("_m/") and k.endswith("/info"):
            mid = k.split("/")[1]
            machines[mid] = unpack(v)
    return machines

def read_alive(snap, now):
    """Return set of machine IDs with recent heartbeats."""
    alive = set()
    for k, v in snap.items():
        if k.startswith("_m/") and k.endswith("/hb"):
            mid = k.split("/")[1]
            hb = unpack(v)
            try:
                if now - float(hb.get("t", 0)) < 10:
                    alive.add(mid)
            except: pass
    return alive


# ── initial setup ──

hc.mount("root/dash", html=DASH_HTML, js=DASH_JS, fixed=True, layer=10)
hc.write("root/dash", status="scanning bluetooth...", me=f"{MY_NAME} · {MY_OS}", log="starting...", ts="")

print(f"\nscanning bluetooth...")
devices = bt.scan()
publish_devices(devices)
publish_heartbeat()
time.sleep(0.5)  # let the relay process

print(f"found {len(devices)} device(s):")
for d in devices:
    c = "●" if d.get("connected") else "○"
    print(f"  {c} {d['name']}  {d['address']}")
print()


# ── main loop ──

seen    = set()
last_hb = time.time()
last_ui = 0
last_sc = time.time()
logmsg  = "ready"

while True:
    try:
        now  = time.time()
        snap = hc.snapshot() or {}

        # heartbeat
        if now - last_hb > 3:
            publish_heartbeat()
            last_hb = now

        # auto-rescan every 20s
        if now - last_sc > 20:
            devices = bt.scan()
            publish_devices(devices)
            last_sc = now

        # ── inbox ──
        for k, v in list(snap.items()):
            if not k.startswith("inbox/") or k in seen:
                continue
            seen.add(k)

            try:
                msg = unpack(v)
                act = msg.get("type", "")
                addr = msg.get("address", "")
                mid  = msg.get("machine", "")

                if act == "rescan":
                    print("rescan requested")
                    devices = bt.scan()
                    publish_devices(devices)
                    last_sc = now
                    logmsg = f"rescanned — {len(devices)} devices"

                elif act == "connect" and mid == ME:
                    print(f"connect {addr}")
                    ok, txt = bt.connect(addr)
                    logmsg = txt; print(f"  → {txt}")
                    devices = bt.scan(); publish_devices(devices)

                elif act == "disconnect" and mid == ME:
                    print(f"disconnect {addr}")
                    ok, txt = bt.disconnect(addr)
                    logmsg = txt; print(f"  → {txt}")
                    devices = bt.scan(); publish_devices(devices)

                elif act == "steal":
                    # steal = tell other machine to disconnect, then connect here
                    print(f"steal {addr} from {mid[:16]}")
                    logmsg = f"stealing {addr}..."

                    # ask other machine to disconnect
                    hc.write(f"_cmd/{mid}", data=pack({
                        "action": "disconnect", "address": addr, "t": now}))

                    # wait for BT to release
                    time.sleep(2.5)

                    # connect locally
                    ok, txt = bt.connect(addr)
                    logmsg = f"steal → {txt}"
                    print(f"  → {txt}")
                    devices = bt.scan(); publish_devices(devices)

            except Exception as e:
                logmsg = f"error: {e}"
                print(f"  inbox error: {e}")
                traceback.print_exc()

            hc.remove(k)

        # ── commands directed at us ──
        cmd_val = snap.get(f"_cmd/{ME}")
        if cmd_val:
            cmd = unpack(cmd_val)
            if cmd.get("action") == "disconnect" and now - float(cmd.get("t", 0)) < 15:
                addr = cmd.get("address", "")
                print(f"remote cmd: disconnect {addr}")
                ok, txt = bt.disconnect(addr)
                print(f"  → {txt}")
                devices = bt.scan(); publish_devices(devices)
            hc.remove(f"_cmd/{ME}")

        # ── UI refresh ──
        if now - last_ui > 1.5:
            last_ui = now

            machines = read_machines(snap)
            alive    = read_alive(snap, now)

            hc.write("root/dash",
                status=f"{len(alive)} machine(s) · {len(devices)} local device(s)",
                log=logmsg,
                ts=time.strftime("%H:%M:%S"))

            for mid, info in machines.items():
                is_me = (mid == ME)
                mn  = str(info.get("name", mid[:16]))[:24]
                mo  = str(info.get("os", "?"))
                mbt = str(info.get("bt", "?"))
                mdevs = info.get("devices", [])
                if isinstance(mdevs, str):
                    try: mdevs = json.loads(mdevs)
                    except: mdevs = []

                mp = f"root/dash/m_{mid[:12]}"
                icon = "💻" if "arwin" in mo else "🖥️"
                is_up = mid in alive

                if is_me:       badge = "⬤ THIS MACHINE"
                elif is_up:     badge = "● ONLINE"
                else:           badge = "○ OFFLINE"

                hc.mount(mp, html=MACHINE_HTML, layer=5)
                hc.write(mp, icon=icon, mname=mn,
                    msub=f"{mo} · {mbt} · {len(mdevs)} device(s)",
                    badge=badge)

                # device rows
                for i, dev in enumerate(mdevs):
                    dp = f"{mp}/d{i}"
                    hc.mount(dp, html=device_row(dev, mid, is_me), layer=5)

        time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nshutting down...")
        hc.stop()
        break
    except Exception as e:
        print(f"loop error: {e}")
        traceback.print_exc()
        time.sleep(2)