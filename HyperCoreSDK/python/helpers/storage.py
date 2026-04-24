from pathlib import Path


def create_default_storage_directory():
    parent_dir = Path(__file__).parent.parent.parent.parent
    storage_path = parent_dir / "storage"

    storage_path.mkdir(parents=True, exist_ok=True)

    return storage_path
