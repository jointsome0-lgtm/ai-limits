"""Data model shared by providers and the output (see docs/state-schema.md)."""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime

SCHEMA = 1


@dataclass
class Limit:
    id: str  # "5h" | "7d" | "7d:<model>" | ...
    label: str
    used_percent: float
    resets_at: int | None = None
    window_minutes: int | None = None
    estimated: bool = False


@dataclass
class AccountConfig:
    provider: str  # "claude" | "codex"
    label: str
    home: str  # absolute, normalised

    @property
    def id(self) -> str:
        return f"{self.provider}-{home_key(self.home)}"


@dataclass
class Snapshot:
    """What a provider found for one account."""

    limits: list[Limit] = field(default_factory=list)
    source: str | None = None  # "tap" | "sessions" | "network"
    updated_at: int | None = None
    plan: str | None = None
    status: str = "ok"  # "ok" | "stale" | "no_data" | "vpn_off" | "error"
    message: str | None = None


def home_key(home: str) -> str:
    """Stable 12-char key for a config dir; the statusline tap uses the same one."""
    return hashlib.sha1(home.encode()).hexdigest()[:12]


def normalise_home(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def percent(value) -> float | None:
    """A usable percentage (finite, >= 0) or None; rejects NaN/inf/strings so the JSON stays valid."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def to_epoch(value) -> int | None:
    """Epoch seconds/milliseconds or ISO-8601 -> epoch seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return None
        return int(value / 1000) if value > 1e11 else int(value)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def roll_forward(limit: Limit, now: int) -> Limit:
    """A window that already reset is at 0 % now; move resets_at to the next window if we know its length."""
    if limit.resets_at is None or limit.resets_at > now:
        return limit
    limit.used_percent = 0.0
    limit.estimated = True
    if limit.window_minutes and limit.window_minutes > 0:
        step = limit.window_minutes * 60
        limit.resets_at += ((now - limit.resets_at) // step + 1) * step
    else:
        limit.resets_at = None
    return limit


def account_json(acc: AccountConfig, snap: Snapshot, recommended: bool) -> dict:
    return {
        "id": acc.id,
        "provider": acc.provider,
        "label": acc.label,
        "home": acc.home,
        "plan": snap.plan,
        "source": snap.source,
        "updated_at": snap.updated_at,
        "status": snap.status,
        "message": snap.message,
        "recommended": recommended,
        "limits": [asdict(lim) for lim in snap.limits],
    }
