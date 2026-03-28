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
from typing import Any, Dict, Iterator, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

MDNS_TYPE = "_hyper._tcp.local."


class Action(dict):
    """A browser action. Has a .name and field access via [] or .get()."""

    def __init__(self, name: str, fields: dict):
        super().__init__(fields)
        self.name = name

    def __repr__(self):
        return f"Action({self.name!r}, {dict(self)})"


def _mdns_ok():
    try:
        import zeroconf  # noqa: F401
        return True
    except ImportError:
        return False


def _mdns_advertise(mid, port, root):
    from zeroconf import Zeroconf, ServiceInfo

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
    from zeroconf import Zeroconf, ServiceBrowser

    found = []

    class L:
        def add_service(self, zc, st, name):
            info = zc.get_service_info(st, name)
            if not info:
                return
            p = {k.decode(): v.decode() for k, v in (info.properties or {}).items()}
            if p.get("machine") == own:
                return
            a = info.parsed_addresses()
            if a:
                found.append(f"http://{a[0]}:{info.port}")

        def remove_service(self, *a):
            pass

        def update_service(self, *a):
            pass

    zc = Zeroconf()
    ServiceBrowser(zc, MDNS_TYPE, L())
    dl = time.time() + timeout
    while time.time() < dl:
        if found:
            break
        time.sleep(0.1)
    zc.close()
    return found


def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        a = s.getsockname()[0]
        s.close()
        return a
    except Exception:
        return "127.0.0.1"


class _SSESubscription:
    def __init__(self, client: "HyperClient", dot_path: str, timeout: float = 3600.0):
        self._client = client
        self.dot_path = dot_path
        self.timeout = timeout
        self._resp = None
        self._closed = False

    @property
    def url(self) -> str:
        return self._client.events_url(self.dot_path, full_path=True)

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
        headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
        req = urllib.request.Request(self.url, method="GET", headers=headers)
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
        self.relay_url = (relay_url.rstrip("/") if relay_url else f"http://127.0.0.1:{self.port}")
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

        self._register()

        r = "peered" if remote else "standalone"
        log.info("")
        log.info("━" * 50)
        log.info("  %s · %s · %s", self.discovery, r, self.machine_id[:20])
        log.info("")
        log.info("  open in browser:")
        log.info("  \033[1mhttp://localhost:%d/%s\033[0m", self.port, self.root)
        log.info("")
        if remote:
            log.info("  peered with: %s", remote)
        log.info("━" * 50)
        log.info("")

    def start_relay(self):
        self.connect()

    @property
    def base(self):
        return f"{self.relay_url}/{self.root}"

    @property
    def gun_relay(self):
        return f"{self.relay_url}/gun"

    @property
    def browser_url(self):
        return f"http://localhost:{self.port}/{self.root}"

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
            lp = f"http://{_local_ip()}:{self.port}/gun"
            if lp not in peers:
                peers.append(lp)

        for base in remote:
            g = self._gun(base)
            if g not in peers:
                peers.append(g)

        for x in self._explicit:
            if x not in peers:
                peers.append(x)

        return peers

    def _relay_probe_urls(self) -> List[str]:
        return [
            f"http://127.0.0.1:{self.port}/{self.root}/api/snapshot",
            f"http://127.0.0.1:{self.port}",
        ]

    def _relay_ready(self, timeout=1.0) -> bool:
        for url in self._relay_probe_urls():
            if self._probe(url, timeout=timeout):
                return True
        return False

    def _start_relay(self):
        if self._proc and self._proc.poll() is None:
            return

        # Reuse an already-running local relay on this port.
        if self._relay_ready(timeout=0.75):
            log.info("relay already running on port %d; reusing it", self.port)
            self._proc = None
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

    # ------------------------------------------------------------------
    # Scene convenience layer
    # ------------------------------------------------------------------

    def mount(self, key, *, html="", css="", js="", fixed=False, layer=0, **kw):
        return self._put(
            f"scene/{key}",
            {"html": html, "css": css, "js": js, "fixed": fixed, "layer": layer, **kw},
        )

    def unmount(self, key):
        return self._delete(f"scene/{key}")

    def write(self, path, **fields):
        if not fields:
            return False

        c = {}
        for k, v in fields.items():
            c[k] = json.dumps(v) if isinstance(v, (dict, list)) else v

        return self._put(f"scene/{path}", c)

    def read(self, path):
        return self._get(f"scene/{path}") or {}

    def remove(self, path):
        return self._delete(f"scene/{path}")

    def clear(self):
        return self._post("api/clear") is not None

    def snapshot(self):
        return self._get("api/snapshot") or {}

    def keys(self):
        return self._get("api/keys") or []

    # ------------------------------------------------------------------
    # Dot-path API
    # ------------------------------------------------------------------

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

        return f"{self.root}.{p}" if p else self.root

    def path_url(self, path: str, full_path: bool = False) -> str:
        dp = self.dot(path, full_path=full_path)
        return f"{self.relay_url}/{urllib.parse.quote(dp, safe='.')}"

    def connect_url(self, path: str, full_path: bool = False) -> str:
        dp = self.dot(path, full_path=full_path)
        return f"{self.relay_url}/{urllib.parse.quote(dp, safe='.')}._connect"

    def events_url(self, path: str, full_path: bool = False) -> str:
        dp = self.dot(path, full_path=full_path)
        return f"{self.relay_url}/{urllib.parse.quote(dp, safe='.')}._events"

    def get_path(self, path: str, *, full_path: bool = False):
        return self._req_url(self.path_url(path, full_path=full_path), "GET")

    def put_path(self, path: str, data: Dict[str, Any], *, full_path: bool = False) -> bool:
        r = self._req_url(self.path_url(path, full_path=full_path), "PUT", data=data, write=True)
        return r is not None and (r.get("ok", False) if isinstance(r, dict) else True)

    def delete_path(self, path: str, *, full_path: bool = False) -> bool:
        return self._req_url(self.path_url(path, full_path=full_path), "DELETE", write=True) is not None

    def multi_get(self, *paths: str, full_path: bool = False):
        if not paths:
            return {}

        dps = [self.dot(p, full_path=full_path) for p in paths]
        base = self.path_url(dps[0], full_path=True)

        if len(dps) == 1:
            return self._req_url(base, "GET")

        qs = urllib.parse.urlencode([("also", p) for p in dps[1:]], doseq=True)
        return self._req_url(base + "?" + qs, "GET")

    def subscribe(self, path: str, *, full_path: bool = False, timeout: float = 3600.0) -> _SSESubscription:
        return _SSESubscription(self, self.dot(path, full_path=full_path), timeout=timeout)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

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
                lines.append(
                    f'  el_{submit}.onkeydown=function(e){{if(e.key==="Enter"){fn_name}();}};'
                )

        lines.append("})();")
        return "\n".join(lines)

    def actions(self):
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
        self.write(
            f"_machines/{self.machine_id}/info",
            machine_id=self.machine_id,
            name=self.machine_name,
            discovery=self.discovery,
            peers=json.dumps(self.peers),
            t=time.time(),
        )

    def heartbeat(self):
        self.write(f"_machines/{self.machine_id}/presence", status="online", t=time.time())

    def machines(self):
        s = self.snapshot()
        o = {}
        for k, v in s.items():
            if k.startswith("_machines/") and k.endswith("/info"):
                o[k.split("/")[1]] = v
        return o

    # ------------------------------------------------------------------
    # Runtime helpers
    # ------------------------------------------------------------------

    def load_runtime_js(self):
        return (
            "(function(){if(typeof Gun==='undefined')return;"
            f"window.$gun=Gun({{peers:{json.dumps(self.peers)}}});"
            f"window.$root=window.$gun.get({json.dumps(self.root)});"
            "window.$scene=window.$root.get('scene')})()"
        )

    def runtime_script_tag(self):
        return f'<script src="{self.gun_relay}/gun.js"></script>'

    def runtime_bootstrap_tag(self):
        return "<script>" + self.load_runtime_js() + "</script>"

    def runtime_tags(self):
        return self.runtime_script_tag() + "\n" + self.runtime_bootstrap_tag()

    @staticmethod
    def expand_layout(base, layout):
        out, rm, i = [], False, 0
        while i < len(layout):
            c = layout[i]
            if c.isspace():
                i += 1
                continue
            if c == "[":
                e = layout.index("]", i)
                n = layout[i + 1:e].strip()
                out.append((n.upper(), f"{base}~{n}" if rm else f"{base}/{n}"))
                i = e + 1
                continue
            if c == "~":
                rm = True
                i += 1
                continue
            i += 1
        return out

    @staticmethod
    def links(*r):
        return json.dumps([{"rel": a, "href": b} for a, b in r])

    def wait(self, s):
        time.sleep(s)

    def node(self, p):
        return _NB(self, p)

    # ------------------------------------------------------------------
    # HTTP internals
    # ------------------------------------------------------------------

    def _h(self, w=False, accept: Optional[str] = None):
        h = {"Content-Type": "application/json"}
        if accept:
            h["Accept"] = accept
        if w and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _get(self, p):
        return self._req(p, "GET")

    def _post(self, p, d=None):
        return self._req(p, "POST", d)

    def _put(self, p, d):
        r = self._req(p, "PUT", d, True)
        return r is not None and (r.get("ok", False) if isinstance(r, dict) else True)

    def _delete(self, p):
        return self._req(p, "DELETE", write=True) is not None

    def _decode_response(self, resp):
        raw = resp.read()
        if not raw:
            return {}

        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            return json.loads(raw)

        try:
            text = raw.decode("utf-8")
        except Exception:
            text = raw.decode("utf-8", "replace")

        if text and text[:1] in "[{":
            try:
                return json.loads(text)
            except Exception:
                pass

        return text

    def _req(self, path, method="GET", data=None, write=False):
        return self._req_url(f"{self.base}/{path}", method=method, data=data, write=write)

    def _req_url(self, url, method="GET", data=None, write=False, accept: Optional[str] = None):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, method=method, headers=self._h(write, accept=accept))

        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return self._decode_response(resp)
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                detail = str(e)
            log.error("%s %s → HTTP %s %s", method, url, e.code, detail)
            return None
        except Exception as e:
            log.error("%s %s → %s", method, url, e)
            return None

    def _probe(self, url, timeout=2.0):
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return 200 <= getattr(r, "status", 200) < 300
        except Exception:
            pass

        try:
            p = urllib.parse.urlparse(url)
            s = socket.create_connection((p.hostname or "127.0.0.1", p.port or self.port), timeout=timeout)
            s.close()
            return True
        except Exception:
            return False

    def _wait(self, timeout=10.0):
        dl = time.time() + timeout

        while time.time() < dl:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(f"Relay exited early with code {self._proc.returncode}")

            if self._relay_ready(timeout=1.0):
                return

            time.sleep(0.3)

        raise RuntimeError(f"Relay not ready after {timeout}s")

    def _find_node(self):
        from shutil import which

        n = which("node")
        if n:
            return n

        for p in (
            "/opt/homebrew/bin/node",
            "/usr/local/bin/node",
            "C:/Program Files/nodejs/node.exe",
        ):
            if Path(p).exists():
                return p

        raise RuntimeError("node not found")

    @staticmethod
    def _gun(u):
        u = u.rstrip("/")
        return u if u.endswith("/gun") else u + "/gun"

    def __repr__(self):
        return f"<HyperClient {self.relay_url}/{self.root} [{self.discovery}]>"


class _NB:
    def __init__(self, c, p):
        self._c = c
        self._p = p
        self._d = {}

    def write(self, **f):
        self._d.update(f)
        return self

    def commit(self):
        self._c.write(self._p, **self._d)
        return self


__all__ = ["HyperClient", "Action"]