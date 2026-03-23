"""
HyperClient — two flags, zero config.

    discovery   =  who you talk to     (local | lan | trusted)
    relay       =  your role           (auto | host | join)

Architecture:
    Every machine runs its own relay. Relays peer with each other via Gun.
    The browser always opens http://localhost:8765/{root} — never a LAN IP.
    Gun syncs data between relays in the background.
    No cross-machine HTTP from the browser. No HTTPS redirect problems.

    discovery=local    →  standalone, no peering
    discovery=lan      →  mDNS finds other relays, Gun peers with them
    discovery=trusted  →  explicit peer list, Gun peers with them

    relay=auto   →  always start a relay. if LAN, also discover and peer.
    relay=host   →  same as auto (every machine hosts).
    relay=join   →  find a peer first, fail if nobody's there, then start locally.

    pip install zeroconf

Examples:
    hc = HyperClient(root="chat")                                     # local
    hc = HyperClient(root="chat", discovery="lan")                    # LAN
    hc = HyperClient(root="chat", discovery="trusted",
                      peers=["http://10.0.0.5:8765"])                 # explicit

    hc.connect()
    # prints:  open in browser: http://localhost:8765/chat
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


def _mdns_browse(timeout: float = 3.0, own_machine_id: str = "") -> List[str]:
    """Browse mDNS for hyper relays. Returns list of base URLs found."""
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
    return found


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
        self._mdns_handle = None
        self._relay_script = relay_script or str(
            Path(__file__).resolve().parent / "src" / "relay.js"
        )

        self.machine_id = os.getenv("HYPER_MACHINE_ID") or self._make_machine_id()
        self.machine_name = machine_name or socket.gethostname()

        # always talk to our own local relay
        self.relay_url = relay_url.rstrip("/") if relay_url else f"http://127.0.0.1:{self.port}"

        # explicit peers (for trusted mode)
        self._explicit_peers = [self._gun_url(p) for p in (peers or [])]

        # filled during connect()
        self.peers: List[str] = []

    # ------------------------------------------------------------------
    # Connect
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """
        1. Discover peers (mDNS for LAN, explicit for trusted, none for local)
        2. Start our own relay, configured to peer with whatever we found
        3. Advertise ourselves on mDNS so others can find us
        4. Browser opens http://localhost:{port}/{root} — always local
        """
        # discover who else is out there
        remote_peers = self._discover_peers()

        # build the full peer list (our LAN IP + discovered peers + explicit)
        self.peers = self._build_peers(remote_peers)

        # if relay=join, at least one remote peer must exist
        if self.relay_mode == "join" and not remote_peers:
            raise RuntimeError(
                "No peers found and relay='join'. "
                "Use relay='auto' or start another machine first."
            )

        # start our own relay
        self._start_relay()

        # advertise on mDNS
        if self.discovery == "lan" and _mdns_available():
            self._mdns_handle = _mdns_advertise(self.machine_id, self.port, self.root)

        self._register()
        self._print_banner(remote_peers)

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
    def browser_url(self) -> str:
        return f"http://localhost:{self.port}/{self.root}"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _discover_peers(self) -> List[str]:
        """Find remote relays. Returns list of base URLs."""
        if self.discovery == "local":
            return []

        if self.discovery == "trusted":
            # just return explicit peers, no scanning
            return [p.rstrip("/").replace("/gun", "") for p in self._explicit_peers]

        # LAN — use mDNS
        if _mdns_available():
            log.info("mDNS: scanning for peers...")
            found = _mdns_browse(timeout=3.0, own_machine_id=self.machine_id)
            if found:
                log.info("mDNS: found %d peer(s): %s", len(found), found)
            else:
                log.info("mDNS: no peers found (we'll be the first)")
            return found
        else:
            log.warning("zeroconf not installed — pip install zeroconf")
            return []

    def _build_peers(self, remote_peers: List[str]) -> List[str]:
        """Build Gun peer URLs: our LAN IP + discovered remotes + explicit."""
        peers = []

        # our own relay (localhost)
        peers.append(f"http://127.0.0.1:{self.port}/gun")

        # our LAN IP (so other relays and browsers on LAN can reach us)
        if self.discovery in ("lan", "trusted"):
            lan = f"http://{_local_ip()}:{self.port}/gun"
            if lan not in peers:
                peers.append(lan)

        # discovered remote relays
        for base in remote_peers:
            gun_url = self._gun_url(base)
            if gun_url not in peers:
                peers.append(gun_url)

        # explicit peers
        for p in self._explicit_peers:
            if p not in peers:
                peers.append(p)

        return peers

    # ------------------------------------------------------------------
    # Relay
    # ------------------------------------------------------------------

    def _start_relay(self) -> None:
        if self._proc and self._proc.poll() is None:
            return

        bind = "127.0.0.1" if self.discovery == "local" else "0.0.0.0"

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

    def _print_banner(self, remote_peers: List[str]) -> None:
        role = "peered" if remote_peers else "standalone"
        log.info("")
        log.info("━" * 50)
        log.info("  %s · %s · %s", self.discovery, role, self.machine_id[:20])
        log.info("")
        log.info("  open in browser:")
        log.info("  \033[1m%s\033[0m", self.browser_url)
        log.info("")
        if remote_peers:
            log.info("  peered with: %s", [p.replace("http://", "") for p in remote_peers])
        log.info("━" * 50)
        log.info("")

    def stop(self) -> None:
        _mdns_stop(self._mdns_handle)
        self._mdns_handle = None
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self._proc = None

    def stop_relay(self) -> None:
        self.stop()

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
                   discovery=self.discovery, peers=json.dumps(self.peers),
                   started_at=time.time())

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
  window.$gun=Gun({{peers:{json.dumps(self.peers)}}});
  window.$root=window.$gun.get({json.dumps(self.root)});
  window.$scene=window.$root.get('scene');
  window.$hyper={{machine:{json.dumps(self.machine_id)},root:{json.dumps(self.root)}}};
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
            if ch.isspace(): i += 1; continue
            if ch == "[":
                end = layout.index("]", i)
                name = layout[i+1:end].strip()
                key = f"{base}~{name}" if row_mode else f"{base}/{name}"
                out.append((name.upper(), key)); i = end + 1; continue
            if ch == "~": row_mode = True; i += 1; continue
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

    def _get(self, p): return self._req(p, "GET")
    def _post(self, p, data=None): return self._req(p, "POST", data)

    def _put(self, p, data):
        r = self._req(p, "PUT", data, write=True)
        return r is not None and (r.get("ok", False) if isinstance(r, dict) else True)

    def _delete(self, p):
        return self._req(p, "DELETE", write=True) is not None

    def _req(self, path, method="GET", data=None, write=False):
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

    def _probe(self, base_url, timeout=2.0):
        try:
            req = urllib.request.Request(base_url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return bool(json.loads(resp.read()).get("relay"))
        except Exception:
            pass
        try:
            p = urllib.parse.urlparse(base_url)
            s = socket.create_connection((p.hostname or "127.0.0.1", p.port or self.port), timeout=timeout)
            s.close()
            return True
        except OSError:
            return False

    def _wait_healthy(self, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._probe(f"http://127.0.0.1:{self.port}", timeout=1):
                return
            time.sleep(0.3)
        raise RuntimeError(f"Relay not ready after {timeout}s")

    def _find_node(self):
        from shutil import which
        node = which("node")
        if node: return node
        for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node",
                   "C:/Program Files/nodejs/node.exe"):
            if Path(p).exists(): return p
        raise RuntimeError("node not found in PATH")

    @staticmethod
    def _make_machine_id():
        return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _gun_url(url):
        url = url.rstrip("/")
        return url if url.endswith("/gun") else url + "/gun"

    def __repr__(self):
        return f"<HyperClient {self.relay_url}/{self.root} [{self.discovery}]>"


class _NodeBuilder:
    def __init__(self, client, path):
        self._c = client
        self._p = path
        self._d = {}

    def write(self, **fields):
        self._d.update(fields)
        return self

    def commit(self):
        self._c.write(self._p, **self._d)
        return self


__all__ = ["HyperClient"]