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

# mDNS service type — all hyper relays on the LAN advertise this
MDNS_SERVICE = "_hyper._tcp.local."


# ----------------------------------------------------------------------
# mDNS helpers — thin wrapper around zeroconf
# ----------------------------------------------------------------------

def _mdns_available() -> bool:
    try:
        import zeroconf  # noqa: F401
        return True
    except ImportError:
        return False


def _mdns_advertise(machine_id: str, port: int, root: str) -> Any:
    """Register this relay on mDNS. Returns (Zeroconf, ServiceInfo) to keep alive."""
    from zeroconf import Zeroconf, ServiceInfo

    local_ip = _local_ip()
    ip_bytes = socket.inet_aton(local_ip)

    info = ServiceInfo(
        MDNS_SERVICE,
        name=f"{machine_id}.{MDNS_SERVICE}",
        addresses=[ip_bytes],
        port=port,
        properties={
            b"root": root.encode(),
            b"machine": machine_id.encode(),
        },
    )

    zc = Zeroconf()
    zc.register_service(info)
    log.info("mDNS: advertising %s on %s:%d", machine_id, local_ip, port)
    return zc, info


def _mdns_stop(handle: Any) -> None:
    """Unregister from mDNS."""
    if not handle:
        return
    zc, info = handle
    try:
        zc.unregister_service(info)
        zc.close()
    except Exception:
        pass


def _mdns_browse(timeout: float = 3.0, own_machine_id: str = "") -> Optional[str]:
    """
    Browse mDNS for a live hyper relay.
    Returns the base URL (http://ip:port) of the first one found, or None.
    Skips our own advertisement.
    """
    from zeroconf import Zeroconf, ServiceBrowser

    found = []

    class Listener:
        def add_service(self, zc, stype, name):
            info = zc.get_service_info(stype, name)
            if not info:
                return
            # skip ourselves
            props = {k.decode(): v.decode() for k, v in (info.properties or {}).items()}
            if props.get("machine") == own_machine_id:
                return
            # extract IP
            addrs = info.parsed_addresses()
            if addrs:
                found.append(f"http://{addrs[0]}:{info.port}")

        def remove_service(self, zc, stype, name):
            pass

        def update_service(self, zc, stype, name):
            pass

    zc = Zeroconf()
    browser = ServiceBrowser(zc, MDNS_SERVICE, Listener())  # noqa: F841

    # wait up to timeout, return as soon as we find something
    deadline = time.time() + timeout
    while time.time() < deadline:
        if found:
            break
        time.sleep(0.1)

    zc.close()
    return found[0] if found else None


def _local_ip() -> str:
    """Best-effort: find the LAN IP of this machine."""
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
        # --- two flags ---
        discovery: str = "local",       # local | lan | trusted
        relay: str = "auto",            # auto | host | join
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

        # machine identity
        self.machine_id = os.getenv("HYPER_MACHINE_ID") or self._make_machine_id()
        self.machine_name = machine_name or socket.gethostname()

        # where we send HTTP — always connectable
        if relay_url:
            self.relay_url = relay_url.rstrip("/")
        else:
            self.relay_url = f"http://127.0.0.1:{self.port}"

        # explicit peers (for trusted mode, or as extra hints for lan)
        self._explicit_peers = [self._gun_url(p) for p in (peers or [])]

        # peers gets populated during connect()
        self.peers: List[str] = []

    # ------------------------------------------------------------------
    # The one method you call
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """
        auto  →  look for a relay (mDNS on LAN, direct probe otherwise).
                  found? join. nobody? become the relay.
        host  →  start the relay. advertise on mDNS if LAN.
        join  →  find a relay or die.
        """
        if self.relay_mode == "host":
            self._start_relay()
            return

        # try to find an existing relay
        found = self._find_relay()

        if self.relay_mode == "join":
            if not found:
                raise RuntimeError(
                    "No relay found and relay='join'. "
                    "Use relay='auto' or relay='host'."
                )
            self._join(found)
            return

        # auto
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

    # ------------------------------------------------------------------
    # Find / Join / Host
    # ------------------------------------------------------------------

    def _find_relay(self) -> Optional[str]:
        """
        Try to find a live relay. Returns base URL or None.

        LAN      →  mDNS browse, then fall back to probing explicit peers
        trusted  →  probe explicit peers
        local    →  probe localhost
        """
        # 1. mDNS (LAN only)
        if self.discovery == "lan" and _mdns_available():
            log.info("mDNS: browsing for relays...")
            found = _mdns_browse(timeout=3.0, own_machine_id=self.machine_id)
            if found:
                log.info("mDNS: found relay at %s", found)
                return found
            log.info("mDNS: no relays found")

        # 2. probe explicit peers + localhost
        candidates = self._probe_candidates()
        for base in candidates:
            if self._probe(base):
                return base

        return None

    def _join(self, relay_base: str) -> None:
        """Point all HTTP at the found relay and register."""
        self.relay_url = relay_base
        self.peers = self._build_peers(relay_base)
        log.info("━" * 50)
        log.info("  joined %s/%s", self.relay_url, self.root)
        log.info("  discovery: %s   relay: joined", self.discovery)
        log.info("  machine: %s", self.machine_id)
        log.info("  peers: %s", self.peers)
        log.info("━" * 50)
        self._register()

    def _start_relay(self) -> None:
        if self._proc and self._proc.poll() is None:
            return

        bind = "127.0.0.1" if self.discovery == "local" else "0.0.0.0"

        env = os.environ.copy()
        env["PORT"] = str(self.port)
        env["HYPER_MACHINE_ID"] = self.machine_id
        env["HYPER_MACHINE_NAME"] = self.machine_name
        env["HYPER_DISCOVERY"] = self.discovery
        env["HYPER_BIND_HOST"] = bind

        self._proc = subprocess.Popen(
            [self._find_node(), str(self._relay_script)],
            stdout=sys.stdout, stderr=sys.stderr, env=env,
        )
        atexit.register(self.stop)
        self._wait_healthy()

        self._hosting = True
        self.relay_url = f"http://127.0.0.1:{self.port}"
        self.peers = self._build_peers(self.relay_url)

        # advertise on mDNS so other machines can find us
        if self.discovery == "lan" and _mdns_available():
            self._mdns_handle = _mdns_advertise(self.machine_id, self.port, self.root)

        self._register()

        log.info("━" * 50)
        log.info("  hosting %s/%s", self.relay_url, self.root)
        log.info("  discovery: %s   relay: host", self.discovery)
        log.info("  machine: %s", self.machine_id)
        log.info("  peers: %s", self.peers)
        log.info("━" * 50)

    def stop(self) -> None:
        _mdns_stop(self._mdns_handle)
        self._mdns_handle = None
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self._proc = None
        self._hosting = False

    # alias
    def stop_relay(self) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Peer list construction
    # ------------------------------------------------------------------

    def _build_peers(self, relay_base: str) -> List[str]:
        """
        Build the Gun peer list from the relay we're connected to,
        plus our own LAN IPs (so browsers on the same machine work),
        plus any explicit peers.
        """
        peers: List[str] = []

        # the relay we're actually talking to
        peers.append(self._gun_url(relay_base))

        # our own LAN IPs (for browser connections)
        if self.discovery == "lan":
            local_ip = _local_ip()
            peers.append(f"http://{local_ip}:{self.port}/gun")

        # explicit extras
        for p in self._explicit_peers:
            if p not in peers:
                peers.append(p)

        # dedupe
        seen, out = set(), []
        for p in peers:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def _probe_candidates(self) -> List[str]:
        """Base URLs to probe (no mDNS, just direct checks)."""
        candidates = []

        # explicit peers
        for p in self._explicit_peers:
            base = p.rstrip("/")
            if base.endswith("/gun"):
                base = base[:-4]
            if base not in candidates:
                candidates.append(base)

        # localhost
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
        info = {
            "machine_id": self.machine_id,
            "name": self.machine_name,
            "discovery": self.discovery,
            "relay_mode": self.relay_mode,
            "hosting": self._hosting,
            "peers": json.dumps(self.peers),
            "started_at": time.time(),
        }
        self.write(f"_machines/{self.machine_id}/info", **info)
        self.heartbeat()

    def heartbeat(self) -> None:
        self.write(f"_machines/{self.machine_id}/presence",
                   status="online", last_seen=time.time())

    def machines(self) -> Dict[str, Any]:
        snap = self.snapshot()
        out = {}
        for k, v in snap.items():
            if k.startswith("_machines/") and k.endswith("/info"):
                mid = k.split("/")[1]
                out[mid] = v
        return out

    # ------------------------------------------------------------------
    # Runtime JS
    # ------------------------------------------------------------------

    def load_runtime_js(self) -> str:
        return f"""
(function(){{
  'use strict';
  if(typeof Gun==='undefined'){{
    console.error('[hyper] Gun not loaded');return;
  }}
  const PEERS={json.dumps(self.peers)};
  const ROOT={json.dumps(self.root)};
  const MACHINE={json.dumps(self.machine_id)};
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
        out = []
        row_mode = False
        i = 0
        while i < len(layout):
            ch = layout[i]
            if ch.isspace():
                i += 1
                continue
            if ch == "[":
                end = layout.index("]", i)
                name = layout[i + 1:end].strip()
                key = f"{base}~{name}" if row_mode else f"{base}/{name}"
                out.append((name.upper(), key))
                i = end + 1
                continue
            if ch == "~":
                row_mode = True
                i += 1
                continue
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
        req = urllib.request.Request(
            url, data=body, method=method, headers=self._headers(write),
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except Exception as e:
            log.error("%s %s → %s", method, path, e)
            return None

    def _probe(self, base_url: str, timeout: float = 2.0) -> bool:
        """Check if a relay is alive at this base URL."""
        try:
            req = urllib.request.Request(base_url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                return bool(data.get("relay"))
        except Exception:
            pass
        try:
            parsed = urllib.parse.urlparse(base_url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or self.port
            s = socket.create_connection((host, port), timeout=timeout)
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
        if not url.endswith("/gun"):
            url += "/gun"
        return url

    @staticmethod
    def _is_private(addr: str) -> bool:
        return (addr.startswith("10.") or addr.startswith("192.168.")
                or addr.startswith("172.") or addr.startswith("169.254."))

    def __repr__(self):
        role = "hosting" if self._hosting else "joined"
        return f"<HyperClient {self.relay_url}/{self.root} [{self.discovery}/{role}]>"


class _NodeBuilder:
    """Fluent builder for graph writes."""
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