from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def read_json(path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def private_dir(path) -> Path:
    """Create (0700) or tighten a directory we own; caches hold account details."""
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid == os.getuid():
        path.chmod(0o700)
    return path


def write_json(path, data: dict) -> None:
    """Atomic write, 0600, through an exclusively created temp file (no symlink/pre-created file games)."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=private_dir(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, allow_nan=False)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def get(provider: str):
    from . import claude, codex

    return {"claude": claude, "codex": codex}[provider]
