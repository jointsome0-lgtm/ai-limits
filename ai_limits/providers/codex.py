"""OpenAI Codex CLI (ChatGPT subscription).

Offline source ("sessions"): Codex writes the server's rate-limit snapshot into its session logs
(<home>/sessions/YYYY/MM/DD/rollout-*.jsonl, `event_msg` / `token_count` records). We read the
tail of the most recently modified logs and take the newest snapshot per limit_id.
No network source yet.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from ..model import AccountConfig, Limit, Snapshot, percent, to_epoch

SCAN_FILES = 8  # most recently modified logs to look at
TAIL_BYTES = 512 * 1024


def collect(acc: AccountConfig, cfg: Config, now: int) -> Snapshot:
    logs = _recent_logs(Path(acc.home))
    if not logs:
        return Snapshot(status="no_data", message="no Codex sessions yet")

    latest: dict[str, tuple[float, dict]] = {}  # limit_id -> (epoch with sub-seconds, rate_limits)
    for path in logs:
        for ts, rl in _snapshots(path):
            lid = rl.get("limit_id") or "codex"
            if lid not in latest or ts > latest[lid][0]:
                latest[lid] = (ts, rl)
    if not latest:
        return Snapshot(status="no_data", message="no rate-limit data in recent Codex sessions")

    # updated_at is the OLDEST snapshot used: nothing is dropped (an old 99 % still constrains the
    # account), but old numbers then make the whole account stale instead of passing as fresh
    snap = Snapshot(source="sessions", updated_at=int(min(ts for ts, _ in latest.values())))
    for lid, (ts, rl) in sorted(latest.items(), key=lambda kv: (kv[0] != "codex", kv[0])):
        snap.plan = snap.plan or rl.get("plan_type")
        for key in ("primary", "secondary"):
            w = rl.get(key)
            if isinstance(w, dict) and percent(w.get("used_percent")) is not None:
                snap.limits.append(_limit(lid, rl.get("limit_name"), w))
    return snap


def _limit(limit_id: str, limit_name: str | None, w: dict) -> Limit:
    minutes = w.get("window_minutes")
    minutes = minutes if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes > 0 else None
    base, label = {300: ("5h", "Session (5h)"), 10080: ("7d", "Week")}.get(
        minutes, (f"{minutes}m" if minutes else "?", _window_label(minutes)))
    if limit_id != "codex":
        name = limit_name or limit_id
        base, label = f"{base}:{limit_id}", f"{name} ({label.lower()})"
    return Limit(base, label, percent(w["used_percent"]), to_epoch(w.get("resets_at")), minutes)


def _window_label(minutes: int | None) -> str:
    if not minutes:
        return "Limit"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _recent_logs(home: Path) -> list[Path]:
    found = []
    for sub in ("sessions", "archived_sessions"):
        root = home / sub
        if not root.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if name.startswith("rollout-") and name.endswith(".jsonl"):
                    p = Path(dirpath, name)
                    try:
                        found.append((p.stat().st_mtime, p))
                    except OSError:
                        pass
    found.sort(reverse=True)
    return [p for _, p in found[:SCAN_FILES]]


def _parse_ts(value) -> float | None:
    """ISO-8601 with any precision/offset -> epoch float (string comparison gets these wrong)."""
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _snapshots(path: Path):
    """(epoch, rate_limits) for each record in the file's tail."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - TAIL_BYTES))
            chunk = f.read()
    except OSError:
        return
    lines = chunk.split(b"\n")
    if size > TAIL_BYTES:
        lines = lines[1:]  # first line is probably cut
    for line in lines:
        if b'"rate_limits"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        payload = rec.get("payload") if isinstance(rec, dict) else None
        rl = payload.get("rate_limits") if isinstance(payload, dict) else None
        ts = _parse_ts(rec.get("timestamp")) if isinstance(rl, dict) else None
        if ts is not None:
            yield ts, rl
