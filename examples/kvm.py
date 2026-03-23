#!/usr/bin/env python3
"""
HyperFlow KVM — switch a Bluetooth keyboard/mouse between two machines.

    pip install zeroconf
    Mac:     brew install blueutil
    Windows: run terminal as Administrator (needed for BT control)

    python -m examples.kvm --discovery lan     # both machines

How it works:
    Your G915 (or any multi-host BT device) is paired to both machines.
    Click "→ Use here" to grab a device to your machine.
    Click "Release" to let it go back to the other.

    Mac:     blueutil --connect / --disconnect (no admin needed)
    Windows: Enable/Disable-PnpDevice (needs Run as Administrator)

    When Mac connects, the device auto-disconnects from Windows.
    When Mac releases, Windows can re-grab it (or it auto-reconnects).
"""

import argparse
import json
import platform
import subprocess
import sys
import time
import traceback

from HyperCoreSDK.client import HyperClient

MY_OS = platform.system()


# ============================================================
# BT Backends
# ============================================================

def guess_type(name):
    n = name.lower()
    for kw in ("keyboard", "keychron", "k380", "g915", "mx keys", "hhkb"):
        if kw in n: return "keyboard"
    for kw in ("mouse", "trackpad", "mx master", "g502", "logitech m"):
        if kw in n: return "mouse"
    for kw in ("airpod", "headphone", "buds", "speaker", "jabra", "sony wh", "beats"):
        if kw in n: return "audio"
    for kw in ("xbox", "controller", "gamepad", "dualsense"):
        if kw in n: return "gamepad"
    for kw in ("iphone", "ipad", "galaxy"):
        if kw in n: return "phone"
    return "other"

ICONS = {"keyboard": "⌨️", "mouse": "🖱️", "audio": "🎧",
         "gamepad": "🎮", "phone": "📱"}
def icon_for(t): return ICONS.get(t, "📶")


class BT:
    def scan(self): return []
    def connect(self, a): return False, "no bt backend"
    def disconnect(self, a): return False, "no bt backend"
    def name(self): return "none"
    def is_admin(self): return True


class MacBT(BT):
    def name(self): return "blueutil"
    def is_admin(self): return True  # blueutil doesn't need admin

    def _cmd(self, *a):
        try:
            out = subprocess.check_output(
                ["blueutil"] + list(a), timeout=10, text=True,
                stderr=subprocess.STDOUT)
            return True, out.strip()
        except FileNotFoundError:
            return False, "blueutil not found — brew install blueutil"
        except subprocess.CalledProcessError as e:
            return False, (e.output or str(e)).strip()
        except Exception as e:
            return False, str(e)

    def scan(self):
        ok, out = self._cmd("--paired", "--format", "json")
        if not ok: return []
        try: raw = json.loads(out)
        except: return []
        return [{"address": d.get("address",""), "name": d.get("name","?"),
                 "connected": bool(d.get("connected")),
                 "type": guess_type(d.get("name",""))}
                for d in raw if d.get("address")]

    def connect(self, addr):
        ok, out = self._cmd("--connect", addr)
        time.sleep(1)
        return (True, f"connected {addr}") if ok else (False, out)

    def disconnect(self, addr):
        ok, out = self._cmd("--disconnect", addr)
        return (True, f"released {addr}") if ok else (False, out)


class WinBT(BT):
    def name(self): return "powershell"

    def is_admin(self):
        """Check if running as Administrator."""
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except: return False

    JUNK = frozenset({
        "generic attribute profile", "generic access profile",
        "device information service", "service discovery service",
        "bluetooth le generic attribute service",
        "bluetooth device (rfcomm protocol tdi)",
    })

    def scan(self):
        raw = []
        # Query each BT class separately — comma syntax fails on some PS versions
        for cls in ("Bluetooth", "BTHLE"):
            try:
                ps = (
                    f'Get-PnpDevice -Class {cls} -ErrorAction SilentlyContinue | '
                    f'Select-Object FriendlyName,InstanceId,Status | ConvertTo-Json -Compress')
                out = subprocess.check_output(
                    ["powershell", "-NoProfile", "-Command", ps],
                    timeout=15, text=True, stderr=subprocess.DEVNULL)
                if not out.strip():
                    continue
                parsed = json.loads(out)
                if isinstance(parsed, dict): parsed = [parsed]
                raw.extend(parsed)
            except Exception:
                pass

        if not raw:
            # last resort — get everything and filter by InstanceId prefix
            try:
                ps = (
                    'Get-PnpDevice -ErrorAction SilentlyContinue | '
                    'Where-Object { $_.InstanceId -like "BTH*" -or $_.InstanceId -like "BTHLE*" } | '
                    'Select-Object FriendlyName,InstanceId,Status | ConvertTo-Json -Compress')
                out = subprocess.check_output(
                    ["powershell", "-NoProfile", "-Command", ps],
                    timeout=15, text=True, stderr=subprocess.DEVNULL)
                if out.strip():
                    parsed = json.loads(out)
                    if isinstance(parsed, dict): parsed = [parsed]
                    raw = parsed
            except Exception as e:
                print(f"  scan fallback error: {e}")
                return []

        seen = {}
        for d in raw:
            name = (d.get("FriendlyName") or "").strip()
            iid = (d.get("InstanceId") or "")
            status = (d.get("Status") or "")

            if name.lower() in self.JUNK: continue
            if any(j in name.lower() for j in ("radio","enumerator")): continue
            if "bthledevice" in iid.lower(): continue
            if not name: continue

            addr = self._addr(iid)
            if not addr: continue

            if addr in seen:
                if len(name) < len(seen[addr]["name"]): seen[addr]["name"] = name
                continue

            seen[addr] = {
                "address": addr, "name": name,
                "connected": status == "OK",
                "type": guess_type(name),
            }
        return list(seen.values())

    def connect(self, addr):
        return self._toggle(addr, True)

    def disconnect(self, addr):
        return self._toggle(addr, False)

    def _toggle(self, addr, enable):
        verb = "Enable" if enable else "Disable"
        clean = addr.replace(":","").replace("-","").upper()
        ps = (
            f'$devs = Get-PnpDevice | Where-Object {{ $_.InstanceId -like "*{clean}*" }}; '
            f'foreach ($d in $devs) {{ {verb}-PnpDevice -InstanceId $d.InstanceId -Confirm:$false }}'
        )
        try:
            subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps],
                timeout=15, text=True, stderr=subprocess.STDOUT)
            return True, f"{verb.lower()}d {addr}"
        except subprocess.CalledProcessError as e:
            msg = (e.output or str(e)).strip()
            if "Access" in msg or "denied" in msg.lower() or "admin" in msg.lower():
                return False, "needs Administrator — right-click terminal → Run as Administrator"
            return False, msg
        except Exception as e:
            return False, str(e)

    def _addr(self, iid):
        for part in iid.replace("\\","/").split("/"):
            if part.upper().startswith("DEV_"):
                h = part[4:].replace("-","")[:12]
                if len(h) == 12 and all(c in "0123456789abcdefABCDEF" for c in h):
                    return ":".join(h[i:i+2] for i in range(0,12,2)).upper()
            clean = part.replace("-","").replace(":","")
            if len(clean) == 12 and all(c in "0123456789abcdefABCDEF" for c in clean):
                return ":".join(clean[i:i+2] for i in range(0,12,2)).upper()
        return ""


def make_bt():
    if MY_OS == "Darwin":
        b = MacBT()
        ok, _ = b._cmd("--version")
        if ok: print("bluetooth: blueutil"); return b
        print("blueutil not found — brew install blueutil")
    if MY_OS == "Windows":
        b = WinBT()
        if not b.is_admin():
            print("⚠️  NOT RUNNING AS ADMINISTRATOR")
            print("   Right-click your terminal → Run as Administrator")
            print("   BT disconnect/connect will fail without admin rights.\n")
        else:
            print("bluetooth: powershell (admin ✓)")
        return b
    return BT()


# ============================================================
# Graph helpers
# ============================================================

def pack(o):   return json.dumps(o)
def unpack(v):
    if not isinstance(v, dict): return {}
    raw = v.get("data", "{}")
    if isinstance(raw, str):
        try: return json.loads(raw)
        except: return {}
    return raw if isinstance(raw, dict) else {}


# ============================================================
# UI
# ============================================================

SHELL = """
<div style="width:100%;height:100%;background:#09090b;color:#fafafa;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;display:flex;flex-direction:column">
  <div style="padding:20px 28px;background:#18181b;border-bottom:1px solid #27272a;display:flex;justify-content:space-between;align-items:center">
    <div style="display:flex;align-items:center;gap:16px">
      <div style="font-size:26px;font-weight:900;letter-spacing:2px;background:linear-gradient(135deg,#818cf8,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent">KVM</div>
      <div style="font-size:13px;color:#52525b;background:#27272a;padding:5px 14px;border-radius:8px" data-bind-text="status">starting...</div>
    </div>
    <div style="display:flex;gap:10px;align-items:center">
      <button id="btn_scan" style="padding:8px 18px;background:#27272a;color:#a78bfa;border:1px solid #3f3f46;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600">↻ Rescan</button>
      <span style="font-size:11px;color:#3f3f46" data-bind-text="me"></span>
    </div>
  </div>
  <div style="flex:1;overflow:auto;padding:28px">
    <div data-children style="display:flex;flex-direction:column;gap:24px"></div>
  </div>
  <div style="padding:14px 28px;background:#18181b;border-top:1px solid #27272a;display:flex;justify-content:space-between">
    <span style="font-size:12px;color:#52525b" data-bind-text="log">ready</span>
    <span style="font-size:11px;color:#3f3f46" data-bind-text="ts"></span>
  </div>
</div>
"""

SHELL_JS = r"""
(function(){
  if(document.getElementById("_k"))return;
  var m=document.createElement("div");m.id="_k";m.style.display="none";document.body.appendChild(m);
  window.kvm=function(act,mid,addr){
    window.$scene.get("inbox/"+Date.now()+"_"+Math.random().toString(36).slice(2,7)).put({
      data:JSON.stringify({type:act,machine:mid,address:addr})
    });
  };
  var b=document.getElementById("btn_scan");
  if(b&&!b.dataset.on){b.dataset.on=1;b.onclick=function(){window.kvm("rescan","","");};}
})();
"""

MCARD = """
<div style="background:#18181b;border:1px solid #27272a;border-radius:14px;overflow:hidden">
  <div style="padding:18px 22px;background:#1c1c1f;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #27272a">
    <div style="display:flex;align-items:center;gap:14px">
      <span data-bind-text="ic" style="font-size:24px"></span>
      <div>
        <div data-bind-text="nm" style="font-weight:700;font-size:17px"></div>
        <div data-bind-text="sub" style="font-size:12px;color:#52525b;margin-top:2px"></div>
      </div>
    </div>
    <div data-bind-text="bg" style="font-size:12px;font-weight:600;padding:5px 12px;border-radius:8px;background:#27272a"></div>
  </div>
  <div data-children style="display:flex;flex-direction:column"></div>
</div>
"""


def dev_row(dev, mid, is_me):
    addr = dev.get("address","?")
    name = dev.get("name","?")
    conn = dev.get("connected", False)
    ic   = icon_for(dev.get("type","other"))
    dot  = "#4ade80" if conn else "#52525b"
    st   = "connected" if conn else "paired"

    if is_me and conn:
        btn = (f'<button onclick="window.kvm(\'release\',\'{mid}\',\'{addr}\')" '
               f'style="padding:6px 14px;background:transparent;color:#f87171;'
               f'border:1px solid #7f1d1d;border-radius:6px;cursor:pointer;'
               f'font-size:12px;font-weight:600">Release</button>')
    elif is_me and not conn:
        btn = (f'<button onclick="window.kvm(\'grab\',\'{mid}\',\'{addr}\')" '
               f'style="padding:6px 14px;background:#6d28d9;color:#fff;border:none;'
               f'border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">'
               f'→ Use here</button>')
    elif not is_me and conn:
        btn = (f'<button onclick="window.kvm(\'steal\',\'{mid}\',\'{addr}\')" '
               f'style="padding:6px 14px;background:#d97706;color:#000;border:none;'
               f'border-radius:6px;cursor:pointer;font-size:12px;font-weight:700">'
               f'⚡ Grab to me</button>')
    else:
        btn = ''

    return f'''<div style="padding:14px 22px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #1c1c1f">
<div style="display:flex;align-items:center;gap:14px">
  <span style="font-size:20px">{ic}</span>
  <div>
    <div style="font-size:14px;font-weight:500">{name}</div>
    <div style="font-size:10px;color:#52525b;font-family:monospace">{addr}</div>
  </div>
</div>
<div style="display:flex;align-items:center;gap:12px">
  <div style="display:flex;align-items:center;gap:6px">
    <div style="width:8px;height:8px;border-radius:50%;background:{dot}"></div>
    <span style="font-size:11px;color:{dot}">{st}</span>
  </div>
  {btn}
</div>
</div>'''


# ============================================================
# Main
# ============================================================

parser = argparse.ArgumentParser(description="KVM — BT device switcher")
parser.add_argument("--discovery", default="local", choices=["local","lan","trusted"])
parser.add_argument("--relay", default="auto", choices=["auto","host","join"])
parser.add_argument("--peers", nargs="*", default=[])
parser.add_argument("--port", type=int, default=8765)
parser.add_argument("--root", default="kvm")
a = parser.parse_args()

hc = HyperClient(root=a.root, discovery=a.discovery, relay=a.relay,
    peers=[f"http://{p}:{a.port}" for p in a.peers], port=a.port)
hc.connect()
hc.clear()

ME   = hc.machine_id
NAME = hc.machine_name
bt   = make_bt()

def publish(devs):
    hc.write(f"_m/{ME}/i", data=pack({"id":ME,"name":NAME,"os":MY_OS,
        "bt":bt.name(),"admin":bt.is_admin(),"devices":devs,"t":time.time()}))

def heartbeat():
    hc.write(f"_m/{ME}/h", data=pack({"t":time.time()}))

def machines_from(snap, now):
    ms, alive = {}, set()
    for k,v in snap.items():
        if k.startswith("_m/") and k.endswith("/i"):
            ms[k.split("/")[1]] = unpack(v)
        elif k.startswith("_m/") and k.endswith("/h"):
            p = unpack(v)
            try:
                if now - float(p.get("t",0)) < 12: alive.add(k.split("/")[1])
            except: pass
    return ms, alive

# ── init ──

hc.mount("root/d", html=SHELL, js=SHELL_JS, fixed=True, layer=10)
hc.write("root/d", status="scanning...", me=f"{NAME} · {MY_OS}", log="starting...", ts="")

print("\nscanning bluetooth...")
devs = bt.scan()
publish(devs); heartbeat(); time.sleep(0.5)

print(f"found {len(devs)} device(s):")
for d in devs:
    s = "●" if d.get("connected") else "○"
    print(f"  {s} {d['name']}  {d['address']}")
if MY_OS == "Windows" and not bt.is_admin():
    print("\n⚠️  Run as Administrator for BT control to work!\n")
print()

# ── loop ──

seen = set()
last_hb = time.time()
last_ui = 0
last_sc = time.time()
log_msg = "ready"

while True:
    try:
        now = time.time()
        snap = hc.snapshot() or {}

        if now - last_hb > 3: heartbeat(); last_hb = now
        if now - last_sc > 20: devs = bt.scan(); publish(devs); last_sc = now

        # ── inbox ──
        for k,v in list(snap.items()):
            if not k.startswith("inbox/") or k in seen: continue
            seen.add(k)
            try:
                msg = unpack(v)
                act  = msg.get("type","")
                addr = msg.get("address","")
                mid  = msg.get("machine","")

                if act == "rescan":
                    print("rescan")
                    devs = bt.scan(); publish(devs); last_sc = now
                    log_msg = f"rescanned — {len(devs)} devices"

                elif act == "grab" and mid == ME:
                    # connect a device to this machine
                    print(f"grab {addr}")
                    ok, txt = bt.connect(addr)
                    log_msg = txt; print(f"  → {txt}")
                    devs = bt.scan(); publish(devs)

                elif act == "release" and mid == ME:
                    # disconnect a device from this machine
                    print(f"release {addr}")
                    ok, txt = bt.disconnect(addr)
                    log_msg = txt; print(f"  → {txt}")
                    devs = bt.scan(); publish(devs)

                elif act == "steal":
                    # steal = tell other machine to release, then grab here
                    print(f"steal {addr} from {mid[:16]}")
                    log_msg = f"grabbing {addr}..."

                    # ask other machine to release
                    hc.write(f"_cmd/{mid}", data=pack(
                        {"action":"release","address":addr,"t":now}))

                    # wait for BT handoff
                    time.sleep(2.5)

                    # grab locally
                    ok, txt = bt.connect(addr)
                    log_msg = f"grab → {txt}"; print(f"  → {txt}")
                    devs = bt.scan(); publish(devs)

            except Exception as e:
                log_msg = f"error: {e}"; print(f"  error: {e}")
                traceback.print_exc()
            hc.remove(k)

        # ── remote commands ──
        cv = snap.get(f"_cmd/{ME}")
        if cv:
            cmd = unpack(cv)
            if cmd.get("action") == "release" and now - float(cmd.get("t",0)) < 15:
                addr = cmd.get("address","")
                print(f"remote: release {addr}")
                ok, txt = bt.disconnect(addr)
                print(f"  → {txt}")
                devs = bt.scan(); publish(devs)
            hc.remove(f"_cmd/{ME}")

        # ── UI ──
        if now - last_ui > 1.5:
            last_ui = now
            ms, alive = machines_from(snap, now)

            hc.write("root/d",
                status=f"{len(alive)} machine(s) · {len(devs)} local devices",
                log=log_msg, ts=time.strftime("%H:%M:%S"))

            for mid, info in ms.items():
                is_me = mid == ME
                mn  = str(info.get("name",mid[:16]))[:24]
                mo  = str(info.get("os","?"))
                mbt = str(info.get("bt","?"))
                adm = info.get("admin", True)
                md  = info.get("devices",[])
                if isinstance(md, str):
                    try: md = json.loads(md)
                    except: md = []

                mp = f"root/d/m_{mid[:12]}"
                ic = "💻" if "arwin" in mo else "🖥️"
                up = mid in alive

                if is_me:  bg = "⬤ THIS MACHINE"
                elif up:   bg = "● ONLINE"
                else:      bg = "○ OFFLINE"

                warn = "" if adm else " ⚠️ needs admin"
                hc.mount(mp, html=MCARD, layer=5)
                hc.write(mp, ic=ic, nm=mn,
                    sub=f"{mo} · {mbt} · {len(md)} device(s){warn}", bg=bg)

                for i, dv in enumerate(md):
                    dp = f"{mp}/d{i}"
                    hc.mount(dp, html=dev_row(dv, mid, is_me), layer=5)

        time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nshutting down..."); hc.stop(); break
    except Exception as e:
        print(f"loop error: {e}"); traceback.print_exc(); time.sleep(2)