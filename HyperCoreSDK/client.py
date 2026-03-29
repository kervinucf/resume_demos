from __future__ import annotations

import atexit
import base64
import json
import logging
import mimetypes
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
from typing import Any, Dict, Iterator, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

MDNS_TYPE = "_hyper._tcp.local."


class Action(dict):
    def __init__(self, name: str, fields: dict):
        super().__init__(fields)
        self.name = name

    def __repr__(self):
        return f"Action({self.name!r}, {dict(self)})"


class LiveBind(str):
    pass


def _mdns_ok():
    try:
        import zeroconf  # noqa: F401
        return True
    except ImportError:
        return False


def _mdns_advertise(mid, port, root):
    from zeroconf import ServiceInfo, Zeroconf

    ip = _local_ip()
    info = ServiceInfo(
        MDNS_TYPE,
        f"{mid}.{MDNS_TYPE}",
        addresses=[socket.inet_aton(ip)],
        port=port,
        properties={b"root": root.encode(), b"machine": mid.encode()},
    )
    zc = Zeroconf()
    zc.register_service(info)
    log.info("mDNS: advertising %s at %s:%d", mid, ip, port)
    return zc, info


def _mdns_stop(h):
    if not h:
        return
    try:
        h[0].unregister_service(h[1])
        h[0].close()
    except Exception:
        pass


def _mdns_browse(timeout=3.0, own=""):
    from zeroconf import ServiceBrowser, Zeroconf

    found = []

    class L:
        def add_service(self, zc, st, name):
            info = zc.get_service_info(st, name)
            if not info:
                return
            p = {k.decode(): v.decode() for k, v in (info.properties or {}).items()}
            if p.get("machine") == own:
                return
            addrs = info.parsed_addresses()
            if addrs:
                found.append(f"http://{addrs[0]}:{info.port}")

        def remove_service(self, *a):
            pass

        def update_service(self, *a):
            pass

    zc = Zeroconf()
    ServiceBrowser(zc, MDNS_TYPE, L())
    deadline = time.time() + timeout
    while time.time() < deadline:
        if found:
            break
        time.sleep(0.1)
    zc.close()
    return found


def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        addr = s.getsockname()[0]
        s.close()
        return addr
    except Exception:
        return "127.0.0.1"


class _SSESubscription:
    def __init__(self, url: str, timeout: float = 3600.0):
        self.url = url
        self.timeout = timeout
        self._resp = None
        self._closed = False

    def close(self):
        self._closed = True
        if self._resp is not None:
            try:
                self._resp.close()
            except Exception:
                pass
            self._resp = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        req = urllib.request.Request(
            self.url,
            method="GET",
            headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
        )
        self._resp = urllib.request.urlopen(req, timeout=self.timeout)

        event_name = "message"
        event_id = None
        data_lines: List[str] = []

        try:
            while not self._closed:
                line = self._resp.readline()
                if not line:
                    break

                text = line.decode("utf-8", "replace").rstrip("\r\n")

                if text == "":
                    if data_lines:
                        raw = "\n".join(data_lines)
                        try:
                            payload = json.loads(raw)
                        except Exception:
                            payload = raw

                        evt: Dict[str, Any] = {
                            "event": event_name or "message",
                            "id": event_id,
                            "data": payload,
                        }
                        if isinstance(payload, dict):
                            evt.update(payload)
                        yield evt

                    event_name = "message"
                    event_id = None
                    data_lines = []
                    continue

                if text.startswith(":"):
                    continue
                if text.startswith("event:"):
                    event_name = text[6:].strip() or "message"
                    continue
                if text.startswith("id:"):
                    event_id = text[3:].strip()
                    continue
                if text.startswith("data:"):
                    data_lines.append(text[5:].lstrip())
                    continue
        finally:
            self.close()


class _NodeRef:
    def __init__(self, client: "HyperClient", path: str):
        self._client = client
        self._path = path

    @property
    def path(self) -> str:
        return self._client.dot(self._path)

    def write(
        self,
        *,
        data: Optional[Dict[str, Any]] = None,
        html: Optional[str] = None,
        css: Optional[str] = None,
        js: Optional[str] = None,
        file: Any = None,
        **extra: Any,
    ) -> bool:
        payload: Dict[str, Any] = {}
        if data is not None:
            payload["data"] = data
        if html is not None:
            payload["html"] = html
        if css is not None:
            payload["css"] = css
        if js is not None:
            payload["js"] = js
        if file is not None:
            payload["file"] = self._client._normalize_file(file)
        if extra:
            payload.update(extra)
        return self._client.write_path(self.path, payload, full_path=True)

    def read(self):
        return self._client.read_path(self.path, full_path=True)

    def stream(self, timeout: float = 3600.0) -> _SSESubscription:
        return self._client.stream_path(self.path, full_path=True, timeout=timeout)

    def stream_url(self, params: Optional[Dict[str, Any]] = None) -> str:
        if params:
            return self._client.render_url(self.path, params=params, full_path=True)
        return self._client.stream_url(self.path, full_path=True)

    def render_url(self, params: Optional[Dict[str, Any]] = None) -> str:
        return self._client.render_url(self.path, params=params, full_path=True)

    def events_url(self, params: Optional[Dict[str, Any]] = None) -> str:
        return self._client.events_url(self.path, params=params, full_path=True)

    def download_url(self) -> str:
        return self._client.download_url(self.path, full_path=True)

    def download(self) -> bytes:
        return self._client.download_path(self.path, full_path=True)

    def tree(self):
        return self._client.tree_path(self.path, full_path=True)

    def search(self, q: str, limit: int = 50):
        return self._client.search_path(self.path, q=q, limit=limit, full_path=True)

    def delete(self) -> bool:
        return self._client.delete_path(self.path, full_path=True)

    def __repr__(self) -> str:
        return f"<NodeRef {self.path}>"


class HyperClient:
    def __init__(
        self,
        relay_url=None,
        root="default",
        token=None,
        relay_script=None,
        discovery="local",
        relay="auto",
        peers=None,
        port=8765,
        machine_name=None,
    ):
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
        self.relay_url = relay_url.rstrip("/") if relay_url else f"http://127.0.0.1:{self.port}"
        self._explicit = [self._gun(p) for p in (peers or [])]
        self.peers: List[str] = []

    def connect(self):
        remote = self._discover()
        self.peers = self._build_peers(remote)
        if self.relay_mode == "join" and not remote:
            raise RuntimeError("No peers found and relay='join'.")

        self._start_relay()

        if self.discovery == "lan" and _mdns_ok():
            self._mdns = _mdns_advertise(self.machine_id, self.port, self.root)

        mode = "peered" if remote else "standalone"
        log.info("")
        log.info("━" * 50)
        log.info("  %s · %s · %s", self.discovery, mode, self.machine_id[:20])
        log.info("")
        log.info("  open in browser:")
        log.info("  \033[1mhttp://localhost:%d/\033[0m", self.port)
        log.info("")
        if remote:
            log.info("  peered with: %s", remote)
        log.info("━" * 50)
        log.info("")

    def start_relay(self):
        self.connect()

    def stop(self):
        _mdns_stop(self._mdns)
        self._mdns = None
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self._proc = None

    def stop_relay(self):
        self.stop()

    @property
    def base(self):
        return f"{self.relay_url}/{self.root}"

    @property
    def gun_relay(self):
        return f"{self.relay_url}/gun"

    @property
    def browser_url(self):
        return f"http://localhost:{self.port}/{self.root}"

    def at(self, path: str = "") -> _NodeRef:
        return _NodeRef(self, path)

    def bind(self, path: str, *, full_path: bool = False) -> LiveBind:
        return LiveBind(f"${self.dot(path, full_path=full_path)}")

    def object_bind(self, path: str, *, full_path: bool = False) -> LiveBind:
        return LiveBind(f"@{self.dot(path, full_path=full_path)}")

    def dot(self, path: str, full_path: bool = False) -> str:
        p = str(path or "").strip()
        if not p:
            return self.root
        if p.startswith("/"):
            p = p[1:]
        p = p.replace("/", ".")
        if full_path or p == self.root or p.startswith(self.root + "."):
            return p
        if p.startswith("scene."):
            p = p[len("scene."):]
        return f"{self.root}.{p}"

    def path_url(self, path: str, full_path: bool = False) -> str:
        dp = self.dot(path, full_path=full_path)
        return f"{self.relay_url}/{urllib.parse.quote(dp, safe='.')}"

    def stream_url(self, path: str, full_path: bool = False) -> str:
        return self.path_url(path, full_path=full_path) + ".stream"

    def render_url(self, path: str, params: Optional[Dict[str, Any]] = None, *, full_path: bool = False) -> str:
        base = self.stream_url(path, full_path=full_path)
        if not params:
            return base
        enc = urllib.parse.urlencode(self._encode_params(params), doseq=True)
        return base if not enc else f"{base}?{enc}"

    def events_url(self, path: str, params: Optional[Dict[str, Any]] = None, *, full_path: bool = False) -> str:
        base = self.path_url(path, full_path=full_path) + ".events"
        if not params:
            return base
        enc = urllib.parse.urlencode(self._encode_params(params), doseq=True)
        return base if not enc else f"{base}?{enc}"

    def download_url(self, path: str, full_path: bool = False) -> str:
        return self.path_url(path, full_path=full_path) + ".download"

    def tree_url(self, path: str, full_path: bool = False) -> str:
        return self.path_url(path, full_path=full_path) + ".tree"

    def search_url(self, path: str, q: str, limit: int = 50, full_path: bool = False) -> str:
        base = self.path_url(path, full_path=full_path) + ".search"
        return base + "?" + urllib.parse.urlencode({"q": q, "limit": limit})

    def read_path(self, path: str, *, full_path: bool = False):
        return self._req_url(self.path_url(path, full_path=full_path), "GET")

    def write_path(self, path: str, payload: Dict[str, Any], *, full_path: bool = False) -> bool:
        result = self._req_url(self.path_url(path, full_path=full_path), "PUT", data=payload)
        return result is not None and (result.get("ok", False) if isinstance(result, dict) else True)

    def delete_path(self, path: str, *, full_path: bool = False) -> bool:
        return self._req_url(self.path_url(path, full_path=full_path), "DELETE") is not None

    def stream_path(self, path: str, *, full_path: bool = False, timeout: float = 3600.0) -> _SSESubscription:
        return _SSESubscription(self.events_url(path, full_path=full_path), timeout=timeout)

    def download_path(self, path: str, *, full_path: bool = False) -> bytes:
        req = urllib.request.Request(self.download_url(path, full_path=full_path), method="GET", headers=self._headers(False))
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

    def tree_path(self, path: str, *, full_path: bool = False):
        return self._req_url(self.tree_url(path, full_path=full_path), "GET")

    def search_path(self, path: str, q: str, limit: int = 50, *, full_path: bool = False):
        return self._req_url(self.search_url(path, q=q, limit=limit, full_path=full_path), "GET")

    def write(self, path, **fields):
        return self.write_path(path, fields)

    def read(self, path):
        return self.read_path(path) or {}

    def remove(self, path):
        return self.delete_path(path)

    def subscribe(self, path: str, *, full_path: bool = False, timeout: float = 3600.0) -> _SSESubscription:
        return self.stream_path(path, full_path=full_path, timeout=timeout)

    def clear(self):
        return False

    def snapshot(self):
        return {}

    def keys(self):
        return []

    @staticmethod
    def actions_js(**action_defs):
        lines = ["(function(){"]
        all_ids = set()
        for _name, defn in action_defs.items():
            for fid in defn.get("fields", []):
                all_ids.add(fid)
            all_ids.add(defn["trigger"])

        guard_id = list(action_defs.values())[0]["trigger"]
        for eid in sorted(all_ids):
            lines.append(f'  var el_{eid}=document.getElementById("{eid}");')
        lines.append(f'  if(!el_{guard_id}||el_{guard_id}.dataset.on)return;')
        lines.append(f'  el_{guard_id}.dataset.on=1;')

        for name, defn in action_defs.items():
            fields = defn.get("fields", [])
            trigger = defn["trigger"]
            submit = defn.get("submit")
            fn_name = f"do_{name}"
            field_reads = ", ".join(f'{fid}:el_{fid}.value.trim()' for fid in fields)

            lines.append(f'  function {fn_name}(){{')
            if submit:
                lines.append(f'    if(!el_{submit}.value.trim())return;')
            lines.append(f'    action({{_action:"{name}",{field_reads}}});')
            if submit:
                lines.append(f'    el_{submit}.value="";')
            lines.append('  }')
            lines.append(f'  el_{trigger}.onclick={fn_name};')
            if submit:
                lines.append(f'  el_{submit}.onkeydown=function(e){{if(e.key==="Enter"){{{fn_name}();}}}};')

        lines.append("})();")
        return "\n".join(lines)

    def actions(self):
        return []

    def mount(self, key, *, html="", css="", js="", fixed=False, layer=0, data=None, **kw):
        payload = {"html": html, "css": css, "js": js, "fixed": fixed, "layer": layer, **kw}
        if data is not None:
            payload["data"] = data
        return self.write_path(key, payload)

    def _encode_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k, v in (params or {}).items():
            if isinstance(v, LiveBind):
                out[k] = str(v)
            elif isinstance(v, (dict, list, tuple, bool, int, float)):
                out[k] = json.dumps(v)
            elif v is None:
                out[k] = ""
            else:
                out[k] = str(v)
        return out

    def _discover(self):
        if self.discovery == "local":
            return []
        if self.discovery == "trusted":
            return [p.rstrip("/").replace("/gun", "") for p in self._explicit]
        if _mdns_ok():
            log.info("mDNS: scanning...")
            found = _mdns_browse(timeout=3.0, own=self.machine_id)
            if found:
                log.info("mDNS: found %d peer(s): %s", len(found), found)
            else:
                log.info("mDNS: no peers (we'll be first)")
            return found
        log.warning("zeroconf not installed — pip install zeroconf")
        return []

    def _build_peers(self, remote):
        peers = [f"http://127.0.0.1:{self.port}/gun"]
        if self.discovery in ("lan", "trusted"):
            local_peer = f"http://{_local_ip()}:{self.port}/gun"
            if local_peer not in peers:
                peers.append(local_peer)
        for base in remote:
            gun_peer = self._gun(base)
            if gun_peer not in peers:
                peers.append(gun_peer)
        for explicit in self._explicit:
            if explicit not in peers:
                peers.append(explicit)
        return peers

    def _start_relay(self):
        if self._proc and self._proc.poll() is None:
            return
        env = os.environ.copy()
        env["PORT"] = str(self.port)
        env["HYPER_BIND_HOST"] = "127.0.0.1" if self.discovery == "local" else "0.0.0.0"
        env["HYPER_PEERS"] = json.dumps(self.peers)
        self._proc = subprocess.Popen(
            [self._find_node(), str(self._relay_script)],
            stdout=sys.stdout,
            stderr=sys.stderr,
            env=env,
        )
        atexit.register(self.stop)
        self._wait()

    def _wait(self):
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if self._proc and self._proc.poll() is not None:
                raise RuntimeError(f"Relay exited early with code {self._proc.returncode}")
            try:
                self._req_url(f"{self.relay_url}/?json=1", "GET")
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("Timed out waiting for relay")

    def _find_node(self):
        for name in ("node", "nodejs"):
            path = shutil.which(name) if 'shutil' in globals() else None
            if path:
                return path
        # delayed import so module still loads in limited envs
        import shutil
        for name in ("node", "nodejs"):
            path = shutil.which(name)
            if path:
                return path
        raise RuntimeError("Node.js not found on PATH")

    def _gun(self, base: str) -> str:
        b = str(base or "").rstrip("/")
        return b if b.endswith("/gun") else b + "/gun"

    def _headers(self, auth=False):
        headers = {"Accept": "application/json"}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _normalize_file(self, file: Any):
        if file is None:
            return None
        if isinstance(file, (bytes, bytearray)):
            return {
                "name": "upload.bin",
                "type": "application/octet-stream",
                "encoding": "base64",
                "data": base64.b64encode(bytes(file)).decode("ascii"),
            }
        if isinstance(file, str) and os.path.exists(file):
            data = Path(file).read_bytes()
            mime = mimetypes.guess_type(file)[0] or "application/octet-stream"
            return {
                "name": Path(file).name,
                "type": mime,
                "encoding": "base64",
                "data": base64.b64encode(data).decode("ascii"),
            }
        return file

    def _req_url(self, url: str, method: str, data: Optional[Dict[str, Any]] = None, auth: bool = False):
        body = None
        headers = self._headers(auth)
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, method=method, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
                if not raw:
                    return None
                ctype = resp.headers.get("Content-Type", "")
                if "application/json" in ctype:
                    return json.loads(raw)
                try:
                    return json.loads(raw)
                except Exception:
                    return raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except Exception:
                payload = raw
            raise RuntimeError(f"HTTP {e.code}: {payload}") from e

