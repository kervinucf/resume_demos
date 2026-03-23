"""
HyperClient — two flags, dead simple.

    discovery   =  who you talk to     (local | lan | trusted)
    relay       =  your role           (auto | host | join)

    auto   →  look for an existing relay. if nobody's home, become the relay.
    host   →  always start the relay. you ARE the server.
    join   →  never start a relay. just connect. fail if nobody's there.

Examples:
    hc = HyperClient(root="chat")                                     # local, auto
    hc = HyperClient(root="chat", discovery="lan")                    # LAN, auto-promote
    hc = HyperClient(root="chat", discovery="lan", relay="host")      # LAN, always host
    hc = HyperClient(root="chat", discovery="lan", relay="join")      # LAN, join only
    hc = HyperClient(root="chat", discovery="trusted",                # explicit peers
                      peers=["http://10.0.0.5:8765"])

    hc.connect()
    hc.mount("root/chat", html="...", js="...", layer=10)
    hc.write("root/chat", title="general")
    snap = hc.snapshot()
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
        self._relay_script = relay_script or str(
            Path(__file__).resolve().parent / "src" / "relay.js"
        )

        # machine identity
        self.machine_id = os.getenv("HYPER_MACHINE_ID") or self._make_machine_id()
        self.machine_name = machine_name or socket.gethostname()

        # where we send HTTP — always 127.0.0.1 (connectable on every OS)
        if relay_url:
            self.relay_url = relay_url.rstrip("/")
        else:
            self.relay_url = f"http://127.0.0.1:{self.port}"

        # who Gun peers with
        self.peers = self._resolve_peers(peers or [])

    # ------------------------------------------------------------------
    # The one method you call
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """
        Connect to the network.

            auto  →  try peers first, if nobody's there, become the relay
            host  →  start the relay, period
            join  →  connect to an existing relay, fail if none found
        """
        if self.relay_mode == "host":
            self._start_relay()
            return

        if self.relay_mode == "join":
            if not self._peer_is_alive():
                raise RuntimeError(
                    f"No relay found at {self.relay_url} and relay='join' — "
                    f"nothing to connect to. Use relay='auto' or relay='host'."
                )
            log.info("joined existing relay at %s", self.relay_url)
            self._register()
            return

        # auto — try to find someone, otherwise become the someone
        if self._peer_is_alive():
            log.info("found existing relay at %s — joining", self.relay_url)
            self._register()
        else:
            log.info("no relay found — promoting to host")
            self._start_relay()

    # backward compat alias
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
    # Discovery — who we talk to
    # ------------------------------------------------------------------

    def _resolve_peers(self, explicit: List[str]) -> List[str]:
        if self.discovery == "trusted":
            return [self._gun_url(p) for p in explicit]

        if self.discovery == "lan":
            hosts = self._lan_hosts()
            return [f"http://{h}:{self.port}/gun" for h in hosts]

        # local
        return [f"http://127.0.0.1:{self.port}/gun"]

    def _lan_hosts(self) -> List[str]:
        found: List[str] = []

        env = os.getenv("HYPER_ADVERTISE_HOST")
        if env:
            found.append(env)

        hostname = socket.gethostname()
        if hostname:
            found.append(hostname)
            found.append(f"{hostname}.local")

        try:
            for addr in socket.gethostbyname_ex(hostname)[2]:
                if self._is_private(addr):
                    found.append(addr)
        except OSError:
            pass

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            addr = s.getsockname()[0]
            s.close()
            if self._is_private(addr):
                found.append(addr)
        except OSError:
            pass

        seen, out = set(), []
        for h in found:
            h = h.strip()
            if h and h not in seen:
                seen.add(h)
                out.append(h)
        return out or ["127.0.0.1"]

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
    # Relay process management
    # ------------------------------------------------------------------

    def _start_relay(self) -> None:
        if self._proc and self._proc.poll() is None:
            return

        env = os.environ.copy()
        env["PORT"] = str(self.port)
        env["HYPER_MACHINE_ID"] = self.machine_id
        env["HYPER_MACHINE_NAME"] = self.machine_name
        env["HYPER_DISCOVERY"] = self.discovery
        env["HYPER_PEERS"] = json.dumps(self.peers)
        env["HYPER_BIND_HOST"] = "127.0.0.1" if self.discovery == "local" else "0.0.0.0"

        self._proc = subprocess.Popen(
            [self._find_node(), str(self._relay_script)],
            stdout=sys.stdout, stderr=sys.stderr, env=env,
        )
        atexit.register(self.stop_relay)
        self._wait_healthy()
        self._hosting = True
        self._register()

        log.info("━" * 50)
        log.info("  hosting %s/%s", self.relay_url, self.root)
        log.info("  discovery: %s   relay: %s", self.discovery, self.relay_mode)
        log.info("  machine: %s", self.machine_id)
        log.info("  peers: %s", self.peers)
        log.info("━" * 50)

    def stop_relay(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self._proc = None
        self._hosting = False

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

    def _peer_is_alive(self, timeout: float = 2.0) -> bool:
        """Can we reach an existing relay?"""
        try:
            req = urllib.request.Request(self.relay_url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                return bool(data.get("relay"))
        except Exception:
            pass

        # fallback: raw socket check (Gun sometimes eats the root route)
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
            s.close()
            return True
        except OSError:
            return False

    def _wait_healthy(self, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._peer_is_alive(timeout=1):
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