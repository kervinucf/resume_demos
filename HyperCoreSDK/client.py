"""
HyperClient — two flags, zero config.

    discovery  =  local | lan | trusted
    relay      =  auto | host | join

Every machine runs its own relay. Browser always opens localhost.
Relays peer with each other via Gun. mDNS for zero-config LAN discovery.

    pip install zeroconf

    hc = HyperClient(root="chat", discovery="lan")
    hc.connect()
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

MDNS_TYPE = "_hyper._tcp.local."


# ------------------------------------------------------------------
# Action — what Python receives from the browser
# ------------------------------------------------------------------

class Action(dict):
    """A browser action. Has a .name and field access via [] or .get()."""

    def __init__(self, name: str, fields: dict):
        super().__init__(fields)
        self.name = name

    def __repr__(self):
        return f"Action({self.name!r}, {dict(self)})"


# ------------------------------------------------------------------
# mDNS helpers
# ------------------------------------------------------------------

def _mdns_ok():
    try:
        import zeroconf; return True  # noqa
    except ImportError:
        return False

def _mdns_advertise(mid, port, root):
    from zeroconf import Zeroconf, ServiceInfo
    ip = _local_ip()
    info = ServiceInfo(MDNS_TYPE, f"{mid}.{MDNS_TYPE}",
        addresses=[socket.inet_aton(ip)], port=port,
        properties={b"root": root.encode(), b"machine": mid.encode()})
    zc = Zeroconf(); zc.register_service(info)
    log.info("mDNS: advertising %s at %s:%d", mid, ip, port)
    return zc, info

def _mdns_stop(h):
    if not h: return
    try: h[0].unregister_service(h[1]); h[0].close()
    except: pass

def _mdns_browse(timeout=3.0, own=""):
    from zeroconf import Zeroconf, ServiceBrowser
    found = []
    class L:
        def add_service(self, zc, st, name):
            info = zc.get_service_info(st, name)
            if not info: return
            p = {k.decode(): v.decode() for k, v in (info.properties or {}).items()}
            if p.get("machine") == own: return
            a = info.parsed_addresses()
            if a: found.append(f"http://{a[0]}:{info.port}")
        def remove_service(self, *a): pass
        def update_service(self, *a): pass
    zc = Zeroconf(); ServiceBrowser(zc, MDNS_TYPE, L())
    dl = time.time() + timeout
    while time.time() < dl:
        if found: break
        time.sleep(0.1)
    zc.close(); return found

def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); a = s.getsockname()[0]; s.close(); return a
    except: return "127.0.0.1"


class HyperClient:
    def __init__(self, relay_url=None, root="default", token=None, relay_script=None,
                 discovery="local", relay="auto", peers=None, port=8765, machine_name=None):
        self.root = root
        self.token = token
        self.port = int(port)
        self.discovery = discovery.lower()
        self.relay_mode = relay.lower()
        self._proc = None
        self._mdns = None
        self._relay_script = relay_script or str(Path(__file__).resolve().parent / "src" / "relay.js")
        self.machine_id = os.getenv("HYPER_MACHINE_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.machine_name = machine_name or socket.gethostname()
        self.relay_url = (relay_url.rstrip("/") if relay_url else f"http://127.0.0.1:{self.port}")
        self._explicit = [self._gun(p) for p in (peers or [])]
        self.peers: List[str] = []

    # ------------------------------------------------------------------
    def connect(self):
        remote = self._discover()
        self.peers = self._build_peers(remote)
        if self.relay_mode == "join" and not remote:
            raise RuntimeError("No peers found and relay='join'.")
        self._start_relay()
        if self.discovery == "lan" and _mdns_ok():
            self._mdns = _mdns_advertise(self.machine_id, self.port, self.root)
        self._register()
        r = "peered" if remote else "standalone"
        log.info("")
        log.info("━" * 50)
        log.info("  %s · %s · %s", self.discovery, r, self.machine_id[:20])
        log.info("")
        log.info("  open in browser:")
        log.info("  \033[1mhttp://localhost:%d/%s\033[0m", self.port, self.root)
        log.info("")
        if remote: log.info("  peered with: %s", remote)
        log.info("━" * 50)
        log.info("")

    def start_relay(self): self.connect()

    # ------------------------------------------------------------------
    @property
    def base(self): return f"{self.relay_url}/{self.root}"
    @property
    def gun_relay(self): return f"{self.relay_url}/gun"
    @property
    def browser_url(self): return f"http://localhost:{self.port}/{self.root}"

    # ------------------------------------------------------------------
    def _discover(self):
        if self.discovery == "local": return []
        if self.discovery == "trusted":
            return [p.rstrip("/").replace("/gun", "") for p in self._explicit]
        if _mdns_ok():
            log.info("mDNS: scanning...")
            f = _mdns_browse(timeout=3.0, own=self.machine_id)
            if f: log.info("mDNS: found %d peer(s): %s", len(f), f)
            else: log.info("mDNS: no peers (we'll be first)")
            return f
        log.warning("zeroconf not installed — pip install zeroconf")
        return []

    def _build_peers(self, remote):
        p = [f"http://127.0.0.1:{self.port}/gun"]
        if self.discovery in ("lan", "trusted"):
            lp = f"http://{_local_ip()}:{self.port}/gun"
            if lp not in p: p.append(lp)
        for b in remote:
            g = self._gun(b)
            if g not in p: p.append(g)
        for x in self._explicit:
            if x not in p: p.append(x)
        return p

    # ------------------------------------------------------------------
    def _start_relay(self):
        if self._proc and self._proc.poll() is None: return
        env = os.environ.copy()
        env["PORT"] = str(self.port)
        env["HYPER_BIND_HOST"] = "127.0.0.1" if self.discovery == "local" else "0.0.0.0"
        env["HYPER_PEERS"] = json.dumps(self.peers)
        self._proc = subprocess.Popen(
            [self._find_node(), str(self._relay_script)],
            stdout=sys.stdout, stderr=sys.stderr, env=env)
        atexit.register(self.stop)
        self._wait()

    def stop(self):
        _mdns_stop(self._mdns); self._mdns = None
        if self._proc and self._proc.poll() is None: self._proc.terminate()
        self._proc = None

    def stop_relay(self): self.stop()

    # ------------------------------------------------------------------
    # Scene
    # ------------------------------------------------------------------

    def mount(self, key, *, html="", css="", js="", fixed=False, layer=0, **kw):
        return self._put(f"scene/{key}", {"html": html, "css": css, "js": js,
            "fixed": fixed, "layer": layer, **kw})

    def unmount(self, key): return self._delete(f"scene/{key}")

    def write(self, path, **fields):
        if not fields: return False
        c = {}
        for k, v in fields.items(): c[k] = json.dumps(v) if isinstance(v, (dict, list)) else v
        return self._put(f"scene/{path}", c)

    def read(self, path): return self._get(f"scene/{path}") or {}
    def remove(self, path): return self._delete(f"scene/{path}")
    def clear(self): return self._post("api/clear") is not None
    def snapshot(self): return self._get("api/snapshot") or {}
    def keys(self): return self._get("api/keys") or []

    # ------------------------------------------------------------------
    # Actions — declared, generated, consumed
    # ------------------------------------------------------------------

    @staticmethod
    def actions_js(**action_defs):
        """Generate JS from action declarations.

        Each kwarg is an action name mapped to a dict:
            fields:  list of element IDs to read .value from
            trigger: element ID whose click fires the action
            submit:  (optional) element ID to clear after fire + Enter to submit

        Example:
            hc.actions_js(
                send={
                    "fields":  ["user", "text"],
                    "trigger": "send",
                    "submit":  "text",
                }
            )

        Generates JS that:
            - Guards against double-init (dataset.on)
            - Reads .value from each field element
            - Calls action({ _action: "send", user: "...", text: "..." })
            - Clears the submit field
            - Wires Enter key on the submit field
        """
        lines = ["(function(){"]

        # Collect all element IDs we need
        all_ids = set()
        for name, defn in action_defs.items():
            for fid in defn.get("fields", []):
                all_ids.add(fid)
            all_ids.add(defn["trigger"])

        # Get elements + guard
        guard_id = list(action_defs.values())[0]["trigger"]
        for eid in sorted(all_ids):
            lines.append(f'  var el_{eid}=document.getElementById("{eid}");')
        lines.append(f'  if(!el_{guard_id}||el_{guard_id}.dataset.on)return;')
        lines.append(f'  el_{guard_id}.dataset.on=1;')

        # Generate a function for each action
        for name, defn in action_defs.items():
            fields = defn.get("fields", [])
            trigger = defn["trigger"]
            submit = defn.get("submit")

            fn_name = f"do_{name}"
            field_reads = ", ".join(
                f'{fid}:el_{fid}.value.trim()' for fid in fields
            )
            lines.append(f'  function {fn_name}(){{')
            if submit:
                lines.append(f'    if(!el_{submit}.value.trim())return;')
            lines.append(f'    action({{_action:"{name}",{field_reads}}});')
            if submit:
                lines.append(f'    el_{submit}.value="";')
            lines.append(f'  }}')

            # Wire trigger click
            lines.append(f'  el_{trigger}.onclick={fn_name};')

            # Wire Enter on submit field
            if submit:
                lines.append(f'  el_{submit}.onkeydown=function(e){{if(e.key==="Enter"){fn_name}();}};')

        lines.append("})();")
        return "\n".join(lines)

    def actions(self):
        """Yield each pending browser action as an Action object.

        Actions have a .name (from _action field) and dict-style field access.
        Cleanup is automatic.
        """
        snap = self.snapshot()
        for key in list(snap):
            if not key.startswith("inbox/"):
                continue
            raw = snap[key].get("data", "{}")
            try:
                msg = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                msg = {}
            self.remove(key)
            name = msg.pop("_action", "unknown")
            yield Action(name, msg)

    # ------------------------------------------------------------------
    # Machines
    # ------------------------------------------------------------------

    def _register(self):
        self.write(f"_machines/{self.machine_id}/info",
            machine_id=self.machine_id, name=self.machine_name,
            discovery=self.discovery, peers=json.dumps(self.peers), t=time.time())

    def heartbeat(self):
        self.write(f"_machines/{self.machine_id}/presence", status="online", t=time.time())

    def machines(self):
        s = self.snapshot(); o = {}
        for k, v in s.items():
            if k.startswith("_machines/") and k.endswith("/info"): o[k.split("/")[1]] = v
        return o

    # ------------------------------------------------------------------
    def load_runtime_js(self):
        return f"""(function(){{if(typeof Gun==='undefined')return;window.$gun=Gun({{peers:{json.dumps(self.peers)}}});window.$root=window.$gun.get({json.dumps(self.root)});window.$scene=window.$root.get('scene')}})();"""

    def runtime_script_tag(self): return f'<script src="{self.gun_relay}/gun.js"></script>'
    def runtime_bootstrap_tag(self): return "<script>" + self.load_runtime_js() + "</script>"
    def runtime_tags(self): return self.runtime_script_tag() + "\n" + self.runtime_bootstrap_tag()

    @staticmethod
    def expand_layout(base, layout):
        out, rm, i = [], False, 0
        while i < len(layout):
            c = layout[i]
            if c.isspace(): i += 1; continue
            if c == "[":
                e = layout.index("]", i); n = layout[i+1:e].strip()
                out.append((n.upper(), f"{base}~{n}" if rm else f"{base}/{n}")); i = e + 1; continue
            if c == "~": rm = True; i += 1; continue
            i += 1
        return out

    @staticmethod
    def links(*r): return json.dumps([{"rel": a, "href": b} for a, b in r])
    def wait(self, s): time.sleep(s)
    def node(self, p): return _NB(self, p)

    # ------------------------------------------------------------------
    def _h(self, w=False):
        h = {"Content-Type": "application/json"}
        if w and self.token: h["Authorization"] = f"Bearer {self.token}"
        return h

    def _get(self, p): return self._req(p, "GET")
    def _post(self, p, d=None): return self._req(p, "POST", d)
    def _put(self, p, d):
        r = self._req(p, "PUT", d, True)
        return r is not None and (r.get("ok", False) if isinstance(r, dict) else True)
    def _delete(self, p): return self._req(p, "DELETE", write=True) is not None

    def _req(self, path, method="GET", data=None, write=False):
        url = f"{self.base}/{path}"
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, method=method, headers=self._h(write))
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read(); return json.loads(raw) if raw else {}
        except Exception as e:
            log.error("%s %s → %s", method, path, e); return None

    def _probe(self, base, timeout=2.0):
        try:
            with urllib.request.urlopen(urllib.request.Request(base, method="GET"), timeout=timeout) as r:
                return bool(json.loads(r.read()).get("relay"))
        except: pass
        try:
            p = urllib.parse.urlparse(base)
            s = socket.create_connection((p.hostname or "127.0.0.1", p.port or self.port), timeout=timeout)
            s.close(); return True
        except: return False

    def _wait(self, timeout=10.0):
        dl = time.time() + timeout
        while time.time() < dl:
            if self._probe(f"http://127.0.0.1:{self.port}", 1): return
            time.sleep(0.3)
        raise RuntimeError(f"Relay not ready after {timeout}s")

    def _find_node(self):
        from shutil import which
        n = which("node")
        if n: return n
        for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node", "C:/Program Files/nodejs/node.exe"):
            if Path(p).exists(): return p
        raise RuntimeError("node not found")

    @staticmethod
    def _gun(u):
        u = u.rstrip("/"); return u if u.endswith("/gun") else u + "/gun"

    def __repr__(self): return f"<HyperClient {self.relay_url}/{self.root} [{self.discovery}]>"


class _NB:
    def __init__(self, c, p): self._c = c; self._p = p; self._d = {}
    def write(self, **f): self._d.update(f); return self
    def commit(self): self._c.write(self._p, **self._d); return self

__all__ = ["HyperClient", "Action"]