from __future__ import annotations

import time

from . import netguard, providers
from .config import Config, cache_dir
from .model import SCHEMA, Snapshot, account_json, roll_forward
from .providers import write_json

USABLE = ("ok", "stale", "vpn_off")


def build_state(cfg: Config, now: int | None = None) -> dict:
    now = now or int(time.time())
    netguard.last_check = None
    results = []
    for acc in cfg.accounts:
        try:
            snap = providers.get(acc.provider).collect(acc, cfg, now)
        except Exception as e:  # one broken account must not hide the others
            snap = Snapshot(status="error", message=f"{type(e).__name__}: {e}")
        _age(snap, cfg, now)
        snap.limits = [roll_forward(lim, now) for lim in snap.limits]
        results.append((acc, snap))

    recommended = _recommended(results)
    return {
        "schema": SCHEMA,
        "generated_at": now,
        "network": {
            "enabled": cfg.network.active,
            "interface": cfg.network.require_interface,
            "vpn_ok": netguard.last_check,  # only checks made during this run
        },
        "accounts": [account_json(acc, snap, acc.id in recommended) for acc, snap in results],
    }


def write_state(state: dict) -> None:
    write_json(cache_dir() / "state.json", state)


def _age(snap: Snapshot, cfg: Config, now: int) -> None:
    if snap.status != "ok" or not snap.updated_at:
        return
    if now - snap.updated_at > cfg.stale_after_hours * 3600:
        snap.status = "stale"  # UIs show the age from updated_at themselves


def _recommended(results) -> set[str]:
    """Per provider with 2+ accounts: the usable account whose busiest limit is lowest."""
    by_provider: dict[str, list] = {}
    for acc, snap in results:
        by_provider.setdefault(acc.provider, []).append((acc, snap))
    picked = set()
    for accounts in by_provider.values():
        if len(accounts) < 2:
            continue
        usable = [(max(lim.used_percent for lim in s.limits), -(s.updated_at or 0), a.id)
                  for a, s in accounts if s.status in USABLE and s.limits]
        if usable:
            picked.add(min(usable)[2])
    return picked


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"
