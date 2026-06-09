from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


STATE_FILENAME = "loader_state.json"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def utc_stamp() -> str:
    now = utc_now()
    stamp = now.strftime("%Y%m%dT%H%M%S%f")
    suffix = uuid4().hex[:8]
    return f"{stamp}_{suffix}"


def state_file_path(DATA_DIR: str | None = None) -> Path:
    if DATA_DIR:
        root = Path(DATA_DIR).expanduser().resolve()
    else:
        root = Path.cwd()

    root.mkdir(parents=True, exist_ok=True)
    return root / STATE_FILENAME


def _load_state_file(DATA_DIR: str | None = None) -> dict[str, list[dict[str, Any]]]:
    path = state_file_path(DATA_DIR)

    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        print(f"[state] warning: corrupt state file ignored: {path}", flush=True)
        return {}

    if not isinstance(data, dict):
        return {}

    clean: dict[str, list[dict[str, Any]]] = {}

    for name, records in data.items():
        if not isinstance(name, str):
            continue

        if not isinstance(records, list):
            continue

        clean[name] = [
            record
            for record in records
            if isinstance(record, dict)
        ]

    return clean


def _save_state_file(
    state: dict[str, list[dict[str, Any]]],
    DATA_DIR: str | None = None,
) -> None:
    path = state_file_path(DATA_DIR)
    tmp = path.with_suffix(f".{utc_stamp()}.tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(
            state,
            f,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )

    os.replace(tmp, path)


def get_loader_runs(
    name: str,
    DATA_DIR: str | None = None,
) -> list[dict[str, Any]]:
    state = _load_state_file(DATA_DIR)
    runs = list(state.get(name, []))

    print(f"[state] read file {name} hits={len(runs)}", flush=True)

    return runs


def get_loader_state(
    name: str,
    DATA_DIR: str | None = None,
) -> dict[str, Any]:
    runs = get_loader_runs(
        name=name,
        DATA_DIR=DATA_DIR,
    )

    latest: dict[str, Any] = {}

    for record in runs:
        if not latest:
            latest = record
            continue

        if str(record.get("last_run_at") or "") > str(latest.get("last_run_at") or ""):
            latest = record

    return latest


def save_loader_state(
    name: str,
    data: dict[str, Any],
    DATA_DIR: str | None = None,
) -> None:
    state = _load_state_file(DATA_DIR)

    record = {
        "id": utc_stamp(),
        "name": name,
        **data,
    }

    state.setdefault(name, []).append(record)

    print(f"[state] write file {name}", flush=True)

    _save_state_file(
        state,
        DATA_DIR=DATA_DIR,
    )


def mark_ran(
    name: str,
    DATA_DIR: str | None = None,
    **extra: Any,
) -> None:
    save_loader_state(
        name=name,
        DATA_DIR=DATA_DIR,
        data={
            "last_run_at": utc_now_iso(),
            **extra,
        },
    )


def is_due(
    name: str,
    wait_seconds: int,
    DATA_DIR: str | None = None,
) -> bool:
    state = get_loader_state(
        name=name,
        DATA_DIR=DATA_DIR,
    )

    value = state.get("last_run_at")

    if not value:
        return True

    last = datetime.fromisoformat(str(value))
    now = utc_now()

    return (now - last).total_seconds() >= wait_seconds