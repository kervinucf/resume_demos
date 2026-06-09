import os
from pathlib import Path


def create_default_storage_directory():
    parent_dir = Path(__file__).parent.parent.parent.parent
    storage_path = parent_dir / "storage"

    storage_path.mkdir(parents=True, exist_ok=True)

    return storage_path

# ---------------------------------------------------------------------------
# Path + projection program
# ---------------------------------------------------------------------------

def projection(*fields: str) -> dict[str, str]:
    """link_projections where each index entry carries a field forward under its own name."""
    return {field: field for field in fields}


def dot(path: str | None) -> str:
    """Dotted form: strip wrappers, turn slashes into dots."""
    return str(path or "").strip().strip("/").strip(".").replace("/", ".")


def rel(path: str | None) -> str:
    """Relative slash form: strip wrappers, keep slashes as path separators."""
    return str(path or "").strip().strip("/").strip(".")


def under(root: str, path: str) -> str:
    """Dotted absolute path = root + relative path, without doubling the root."""
    root_d = dot(root)
    path_d = dot(path)

    if not root_d:
        return path_d
    if not path_d:
        return root_d
    if path_d == root_d or path_d.startswith(f"{root_d}."):
        return path_d

    return f"{root_d}.{path_d}"


def join_rel(*segments: str) -> str:
    """Join non-empty relative segments with '/'."""
    parts: list[str] = []
    for seg in segments:
        seg = rel(seg)
        if seg:
            parts.append(seg)
    return "/".join(parts)


THIS_FILE_PATH = Path(__file__).resolve().parent

def define_relay_script() -> None:
    os.environ["HYPER_RELAY_SCRIPT"] = str(
        (THIS_FILE_PATH / "../../../HyperCoreSDK/src/relay.js").resolve()
    )