# HyperCoreSDK/python/node.py
from __future__ import annotations

from dataclasses import fields, asdict
from typing import Any, Optional

from HyperCoreSDK.python.client import HyperClient


class HyperCoreNode:
    """
    Base for domain dataclasses that live in the hypergraph.

    Subclass with @dataclass(frozen=True), declare your fields, and you get
    commit() and read_out() for free. Nothing else is prescribed.
    """

    _metadata: dict = {}

    @classmethod
    def properties(cls) -> list[str]:
        try:
            return [f.name for f in fields(cls)]
        except TypeError:
            return []

    def to_kv(self) -> dict[str, Any]:
        return asdict(self)

    def commit(
        self,
        hc: HyperClient,
        sub_paths: list[str],
        *,
        links: dict[str, str] | None = None,
        embed: dict[str, Any] | None = None,
        actions: dict[str, Any] | None = None,
    ) -> None:
        """
        Write this node to `{hc.root}/{sub_paths...}`.

        links   — cross-path references → exposed as _links
        embed   — nodes/paths → exposed as _embedded
        actions — HAL action specs alongside the defaults
        """
        path = "/".join(str(p).strip("/") for p in sub_paths if str(p).strip())

        payload: dict[str, Any] = {"data": self.to_kv()}

        meta = getattr(self, "_metadata", None)
        if isinstance(meta, dict) and meta:
            for k, v in meta.items():
                if k not in ("data", "links", "embed", "actions"):
                    payload[k] = v

        if links:
            payload["links"] = {str(k): str(v) for k, v in links.items() if v}
        if embed:
            payload["embed"] = embed
        if actions:
            payload["actions"] = actions

        hc.write(f"{hc.root}/{path}", data=payload)

    @classmethod
    def read_out(
        cls,
        hc: HyperClient,
        sub_paths: list[str],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """
        Fetch the node at `{hc.root}/{sub_paths...}` and split it into
        (clean_fields, metadata) where clean_fields matches the dataclass
        signature. Returns ({}, {}) if the node doesn't exist.
        """
        path = "/".join(str(p).strip("/") for p in sub_paths if str(p).strip())
        response = hc.read(f"{hc.root}/{path}")

        if not isinstance(response, dict):
            return {}, {}

        raw = response.get("data")
        if not isinstance(raw, dict):
            return {}, {}

        allowed = set(cls.properties())
        clean = {k: v for k, v in raw.items() if k in allowed}

        meta: dict[str, Any] = {k: v for k, v in raw.items() if k not in allowed}
        for envelope_key in ("_state", "_links", "_actions", "_embedded"):
            if envelope_key in response:
                meta[envelope_key] = response[envelope_key]

        return clean, meta


# ---------------------------------------------------------------------------
# Back-references
# ---------------------------------------------------------------------------

def announce_ref(
    hc: HyperClient,
    *,
    at_path: str,
    rel: str,
    target_path: str,
    kind: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """
    Write a back-reference so a reader of `at_path` discovers that something
    lives at `target_path`.

        announce_ref(hc,
            at_path="geo.locations.nyc-5128581",
            rel="weather",
            target_path="weather.current.nyc-5128581",
            kind="weather-current")

    Creates `geo.locations.nyc-5128581.refs.weather` with:
        data:  {rel, target, kind, ...extra}
        links: {target: target_path}

    Accretes under the source node without modifying it. Because the relay
    emits _embedded.children on the parent, a subscriber to at_path sees the
    new ref appear live the moment it's written.

    `rel` is the name under refs/. If you write the same rel twice, the
    second write replaces the first — each rel is a singleton pointer.
    Use `rel="weather.2026-04-19T15Z"` for history.
    """
    rel = str(rel).strip().strip(".").replace("/", ".")
    if not rel:
        raise ValueError("announce_ref requires a non-empty `rel`")

    data: dict[str, Any] = {"rel": rel, "target": target_path}
    if kind:
        data["kind"] = kind
    if extra:
        for k, v in extra.items():
            if k not in data:
                data[k] = v

    at_dot = str(at_path).strip().strip("/").replace("/", ".")
    ref_path = f"{at_dot}.refs.{rel}"

    hc.write(ref_path, {
        "data": data,
        "links": {"target": target_path},
    })