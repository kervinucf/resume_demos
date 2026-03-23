"""
HyperClient — two flags, zero config.

    discovery   =  who you talk to     (local | lan | trusted)
    relay       =  your role           (auto | host | join)

    auto   →  browse mDNS for a relay. found one? join. nobody? become one.
    host   →  always start the relay. advertise on mDNS.
    join   →  browse mDNS for a relay. found one? join. nobody? fail.

    When discovery=lan, machines find each other via mDNS (_hyper._tcp.local).
    No IPs. No peer lists. No configuration. Two machines, same WiFi, done.

    pip install zeroconf

Examples:
    hc = HyperClient(root="chat")                                     # local, auto
    hc = HyperClient(root="chat", discovery="lan")                    # LAN, mDNS
    hc = HyperClient(root="chat", discovery="lan", relay="host")      # LAN, force host
    hc = HyperClient(root="chat", discovery="lan", relay="join")      # LAN, join only
    hc = HyperClient(root="chat", discovery="trusted",                # explicit peers
                      peers=["http://10.0.0.5:8765"])

    hc.connect()
    hc.mount("root/chat", html="...", js="...", layer=10)
    hc.write("root/chat", title="general")
    hc.clear()
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

MDNS_SERVICE = "_hyper._tcp.local."


# ----------------------------------------------------------------------
# mDNS
# ----------------------------------------------------------------------

def _mdns_available() -> bool:
    try:
        import zeroconf  # noqa: F401
        return True
    except ImportError:
        return False


def _mdns_advertise(machine_id: str, port: int, root: str) -> Any:
    from zeroconf import Zeroconf, ServiceInfo
    local_ip = _local_ip()
    info = ServiceInfo(
        MDNS_SERVICE,
        name=f"{machine_id}.{MDNS_SERVICE}",
        addresses=[socket.inet_aton(local_ip)],
        port=port,
        properties={b"root": root.encode(), b"machine": machine_id.encode()},
    )
    zc = Zeroconf()
    zc.register_service(info)
    log.info("mDNS: advertising %s at %s:%d", machine_id, local_ip, port)
    return zc, info


def _mdns_stop(handle: Any) -> None:
    if not handle:
        return
    zc, info = handle
    try:
        zc.unregister_service(info)
        zc.close()
    except Exception:
        pass


def _mdns_browse(timeout: float = 3.0, own_machine_id: str = "") -> Optional[str]:
    from zeroconf import Zeroconf, ServiceBrowser
    found = []

    class Listener:
        def add_service(self, zc, stype, name):
            info = zc.get_service_info(stype, name)
            if not info:
                return
            props = {k.decode(): v.decode() for k, v in (info.properties or {}).items()}
            if props.get("machine") == own_machine_id:
                return
            addrs = info.parsed_addresses()
            if addrs:
                found.append(f"http://{addrs[0]}:{info.port}")

        def remove_service(self, zc, stype, name):
            pass

        def update_service(self, zc, stype, name):
            pass

    zc = Zeroconf()
    _ = ServiceBrowser(zc, MDNS_SERVICE, Listener())
    deadline = time.time() + timeout
    while time.time() < deadline:
        if found:
            break
        time.sleep(0.1)
    zc.close()
    return found[0] if found else None


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        addr = s.getsockname()[0]
        s.close()
        return addr
    except OSError:
        return "127.0.0.1"


# ----------------------------------------------------------------------
# HyperClient
# ----------------------------------------------------------------------

class HyperClient:
    def __init__(
        self,
        relay_url: Optional[str] = None,
        root: str = "default",
        token: Optional[str] = None,
        relay_script: Optional[str] = None,
        discovery: str = "local",
        relay: str = "auto",
        peers: Optional[List[str]] = None,
        port: int = 8765,
        machine_name: Optional[str] = None,
    ):
        self.root = root
        self.token = token
        self.port = int(port)
        self.discovery = discovery.lower()
        self.relay_mode = relay.lower()
        self._proc = None
        self._hosting = False
        self._mdns_handle = None
        self._relay_script = relay_script or str(
            Path(__file__).resolve().parent / "src" / "relay.js"
        )

        self.machine_id = os.getenv("HYPER_MACHINE_ID") or self._make_machine_id()
        self.machine_name = machine_name or socket.gethostname()

        if relay_url:
            self.relay_url = relay_url.rstrip("/")
        else:
            self.relay_url = f"http://127.0.0.1:{self.port}"

        self._explicit_peers = [self._gun_url(p) for p in (peers or [])]
        self.peers: List[str] = []

    # ------------------------------------------------------------------
    # Connect
    # ------------------------------------------------------------------

    def connect(self) -> None:
        if self.relay_mode == "host":
            self._start_relay()
            return

        found = self._find_relay()

        if self.relay_mode == "join":
            if not found:
                raise RuntimeError(
                    "No relay found and relay='join'. "
                    "Use relay='auto' or relay='host'."
                )
            self._join(found)
            return

        if found:
            self._join(found)
        else:
            log.info("no relay found — promoting to host")
            self._start_relay()

    # backward compat
    def start_relay(self) -> None:
        self.connect()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def base(self):
        return f"{self.relay_url}/{self.root}"

    @property
    def gun_relay(self):
        return f"{self.relay_url}/gun"

    @property
    def is_hosting(self) -> bool:
        return self._hosting

    @property
    def browser_url(self) -> str:
        """URL you can open in a browser — uses LAN IP, not 127.0.0.1."""
        if self.discovery == "local":
            return f"http://127.0.0.1:{self.port}/{self.root}"
        return f"http://{_local_ip()}:{self.port}/{self.root}"

    # ------------------------------------------------------------------
    # Find / Join / Host
    # ------------------------------------------------------------------

    def _find_relay(self) -> Optional[str]:
        if self.discovery == "lan" and _mdns_available():
            log.info("mDNS: browsing for relays...")
            found = _mdns_browse(timeout=3.0, own_machine_id=self.machine_id)
            if found:
                log.info("mDNS: found relay at %s", found)
                return found
            log.info("mDNS: no relays found")

        for base in self._probe_candidates():
            if self._probe(base):
                return base
        return None

    def _join(self, relay_base: str) -> None:
        self.relay_url = relay_base
        self.peers = self._build_peers(relay_base)
        self._register()
        self._print_banner("joined")

    def _start_relay(self) -> None:
        if self._proc and self._proc.poll() is None:
            return

        bind = "127.0.0.1" if self.discovery == "local" else "0.0.0.0"
        self.peers = self._build_peers(f"http://127.0.0.1:{self.port}")

        env = os.environ.copy()
        env["PORT"] = str(self.port)
        env["HYPER_BIND_HOST"] = bind
        env["HYPER_MACHINE_ID"] = self.machine_id
        env["HYPER_MACHINE_NAME"] = self.machine_name
        env["HYPER_DISCOVERY"] = self.discovery
        env["HYPER_PEERS"] = json.dumps(self.peers)

        self._proc = subprocess.Popen(
            [self._find_node(), str(self._relay_script)],
            stdout=sys.stdout, stderr=sys.stderr, env=env,
        )
        atexit.register(self.stop)
        self._wait_healthy()

        self._hosting = True
        self.relay_url = f"http://127.0.0.1:{self.port}"

        if self.discovery == "lan" and _mdns_available():
            self._mdns_handle = _mdns_advertise(self.machine_id, self.port, self.root)

        self._register()
        self._print_banner("hosting")

    def _print_banner(self, role: str) -> None:
        log.info("━" * 50)
        log.info("  %s · %s · %s", role, self.discovery, self.machine_id)
        log.info("  ")
        log.info("  open in browser:")
        log.info("  %s", self.browser_url)
        log.info("  ")
        log.info("  peers: %s", self.peers)
        log.info("━" * 50)

    def stop(self) -> None:
        _mdns_stop(self._mdns_handle)
        self._mdns_handle = None
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self._proc = None
        self._hosting = False

    def stop_relay(self) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Peer list
    # ------------------------------------------------------------------

    def _build_peers(self, relay_base: str) -> List[str]:
        peers = [self._gun_url(relay_base)]

        if self.discovery in ("lan", "trusted"):
            local_ip = _local_ip()
            lan_peer = f"http://{local_ip}:{self.port}/gun"
            if lan_peer not in peers:
                peers.append(lan_peer)

        for p in self._explicit_peers:
            if p not in peers:
                peers.append(p)

        seen, out = set(), []
        for p in peers:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def _probe_candidates(self) -> List[str]:
        candidates = []
        for p in self._explicit_peers:
            base = p.rstrip("/")
            if base.endswith("/gun"):
                base = base[:-4]
            if base not in candidates:
                candidates.append(base)
        local = f"http://127.0.0.1:{self.port}"
        if local not in candidates:
            candidates.append(local)
        return candidates

    # ------------------------------------------------------------------
    # Display API
    # ------------------------------------------------------------------

    def mount(self, key: str, *, html: str = "", css: str = "", js: str = "",
              fixed: bool = False, layer: int = 0, **extra) -> bool:
        payload = {"html": html, "css": css, "js": js,
                   "fixed": fixed, "layer": layer, **extra}
        return self._put(f"scene/{key}", payload)

    def unmount(self, key: str) -> bool:
        return self._delete(f"scene/{key}")

    def write(self, path: str, **fields) -> bool:
        if not fields:
            return False
        clean = {}
        for k, v in fields.items():
            clean[k] = json.dumps(v) if isinstance(v, (dict, list)) else v
        return self._put(f"scene/{path}", clean)

    def remove(self, path: str) -> bool:
        return self._delete(f"scene/{path}")

    def clear(self) -> bool:
        return self._post("api/clear") is not None

    def snapshot(self) -> Dict[str, Any]:
        return self._get("api/snapshot") or {}

    def keys(self) -> List[str]:
        return self._get("api/keys") or []

    # ------------------------------------------------------------------
    # Machine registry
    # ------------------------------------------------------------------

    def _register(self) -> None:
        self.write(f"_machines/{self.machine_id}/info",
                   machine_id=self.machine_id, name=self.machine_name,
                   discovery=self.discovery, relay_mode=self.relay_mode,
                   hosting=self._hosting, peers=json.dumps(self.peers),
                   started_at=time.time())
        self.heartbeat()

    def heartbeat(self) -> None:
        self.write(f"_machines/{self.machine_id}/presence",
                   status="online", last_seen=time.time())

    def machines(self) -> Dict[str, Any]:
        snap = self.snapshot()
        out = {}
        for k, v in snap.items():
            if k.startswith("_machines/") and k.endswith("/info"):
                out[k.split("/")[1]] = v
        return out

    # ------------------------------------------------------------------
    # Runtime JS
    # ------------------------------------------------------------------

    def load_runtime_js(self) -> str:
        return f"""
(function(){{
  'use strict';
  if(typeof Gun==='undefined'){{console.error('[hyper] Gun not loaded');return;}}
  var PEERS={json.dumps(self.peers)};
  var ROOT={json.dumps(self.root)};
  var MACHINE={json.dumps(self.machine_id)};
  window.$gun=Gun({{peers:PEERS}});
  window.$root=window.$gun.get(ROOT);
  window.$scene=window.$root.get('scene');
  window.$hyper={{relay:{json.dumps(self.gun_relay)},root:ROOT,machine:MACHINE,
    scene:window.$scene,gun:window.$gun}};
}})();
""".strip()

    def runtime_script_tag(self) -> str:
        return f'<script src="{self.gun_relay}/gun.js"></script>'

    def runtime_bootstrap_tag(self) -> str:
        return "<script>" + self.load_runtime_js() + "</script>"

    def runtime_tags(self) -> str:
        return self.runtime_script_tag() + "\n" + self.runtime_bootstrap_tag()

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def expand_layout(base: str, layout: str) -> list[tuple[str, str]]:
        out, row_mode, i = [], False, 0
        while i < len(layout):
            ch = layout[i]
            if ch.isspace():
                i += 1; continue
            if ch == "[":
                end = layout.index("]", i)
                name = layout[i+1:end].strip()
                key = f"{base}~{name}" if row_mode else f"{base}/{name}"
                out.append((name.upper(), key))
                i = end + 1; continue
            if ch == "~":
                row_mode = True; i += 1; continue
            i += 1
        return out

    @staticmethod
    def links(*resources):
        return json.dumps([{"rel": r, "href": h} for r, h in resources])

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)

    def node(self, path: str):
        return _NodeBuilder(self, path)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _headers(self, write=False):
        h = {"Content-Type": "application/json"}
        if write and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _get(self, path: str):
        return self._req(path, "GET")

    def _post(self, path: str, data=None):
        return self._req(path, "POST", data)

    def _put(self, path: str, data: dict) -> bool:
        r = self._req(path, "PUT", data, write=True)
        return r is not None and (r.get("ok", False) if isinstance(r, dict) else True)

    def _delete(self, path: str) -> bool:
        return self._req(path, "DELETE", write=True) is not None

    def _req(self, path: str, method: str = "GET", data=None, write=False):
        url = f"{self.base}/{path}"
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, method=method,
                                     headers=self._headers(write))
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except Exception as e:
            log.error("%s %s → %s", method, path, e)
            return None

    def _probe(self, base_url: str, timeout: float = 2.0) -> bool:
        try:
            req = urllib.request.Request(base_url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                return bool(data.get("relay"))
        except Exception:
            pass
        try:
            parsed = urllib.parse.urlparse(base_url)
            s = socket.create_connection(
                (parsed.hostname or "127.0.0.1", parsed.port or self.port), timeout=timeout)
            s.close()
            return True
        except OSError:
            return False

    def _wait_healthy(self, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._probe(f"http://127.0.0.1:{self.port}", timeout=1):
                return
            time.sleep(0.3)
        raise RuntimeError(f"Relay not ready after {timeout}s")

    def _find_node(self) -> str:
        from shutil import which
        node = which("node")
        if node:
            return node
        for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node",
                   "C:/Program Files/nodejs/node.exe"):
            if Path(p).exists():
                return p
        raise RuntimeError("node not found in PATH")

    @staticmethod
    def _make_machine_id() -> str:
        return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _gun_url(url: str) -> str:
        url = url.rstrip("/")
        return url if url.endswith("/gun") else url + "/gun"

    @staticmethod
    def _is_private(addr: str) -> bool:
        return (addr.startswith("10.") or addr.startswith("192.168.")
                or addr.startswith("172.") or addr.startswith("169.254."))

    def __repr__(self):
        role = "hosting" if self._hosting else "joined"
        return f"<HyperClient {self.relay_url}/{self.root} [{self.discovery}/{role}]>"


class _NodeBuilder:
    def __init__(self, client: HyperClient, path: str):
        self._c = client
        self._p = path
        self._d: Dict[str, Any] = {}

    def write(self, **fields):
        self._d.update(fields)
        return self

    def commit(self):
        self._c.write(self._p, **self._d)
        return self


__all__ = ["HyperClient"]