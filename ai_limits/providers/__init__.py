from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def read_json(path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def private_dir(path) -> Path:
    """Create a cache directory; restrict it to its owner on POSIX.

    Windows uses ACLs, not POSIX mode bits.
    """
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if hasattr(os, "getuid") and path.stat().st_uid == os.getuid():
        path.chmod(0o700)
    return path


def write_json(path, data: dict) -> None:
    """Atomic write through an exclusive temp file (0600 on POSIX, inherited ACLs on Windows)."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=private_dir(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, allow_nan=False)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def get(provider: str):
    from . import claude, codex, gemini

    return {"claude": claude, "codex": codex, "gemini": gemini}[provider]
