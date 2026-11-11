"""Google Gemini subscription (Google AI Pro / Ultra), as seen by the Antigravity CLI (`agy`).

Offline source ("tap"): agy passes `quota` (bucket id -> remaining_fraction, reset_time, ...),
`plan_tier` and `email` to its status line command on stdin
(https://antigravity.google/docs/cli/statusline). `ai-limits tap gemini` saves the quota to
$XDG_CACHE_HOME/ai-limits/taps/gemini-<key>.json, where <key> is a hash of the signed-in email,
so several Google accounts used from one agy install are kept apart. The email itself is not
stored. No network source.
"""

from __future__ import annotations

import json
import re
import time

from ..config import Config, cache_dir
from ..model import AccountConfig, Limit, Snapshot, home_key, percent, to_epoch
from . import read_json, write_json

# window by a word in the bucket id ("gemini-weekly" -> 7 days): (word, limit id, label, short label, minutes)
WINDOWS = (("weekly", "7d", "Week", "week", 10080), ("daily", "1d", "Day", "day", 1440),
           ("5h", "5h", "Session (5h)", "5h", 300), ("hourly", "1h", "Hour", "hour", 60))
# other prefixes agy uses for buckets; "3p" = third-party models in agy (Claude, GPT-OSS)
NAMES = {"3p": "Third-party"}
ANY_ACCOUNT = "any"  # tap key when agy did not report an email


def email_key(email: str | None) -> str:
    return home_key(email.strip().lower()) if email else ANY_ACCOUNT


def tap_path(key: str):
    return cache_dir() / "taps" / f"gemini-{key}.json"


def write_tap(status_input: dict, now: int) -> bool:
    quota = status_input.get("quota")
    if not isinstance(quota, dict) or not quota:
        return False
    kept = {}
    for bucket, q in quota.items():
        if not isinstance(bucket, str) or not isinstance(q, dict):
            continue
        kept[bucket[:64]] = {k: q.get(k) for k in ("remaining_fraction", "reset_time", "reset_in_seconds", "disabled")}
    if not kept:
        return False
    email = status_input.get("email")
    plan = status_input.get("plan_tier")
    write_json(tap_path(email_key(email if isinstance(email, str) else None)), {
        "captured_at": now,
        "plan": plan[:64] if isinstance(plan, str) else None,
        "quota": kept,
    })
    return True


def collect(acc: AccountConfig, cfg: Config, now: int) -> Snapshot:
    data = read_json(tap_path(email_key(acc.email))) if acc.email else _newest_tap()
    if not data:
        hint = "add `ai-limits tap gemini` to the agy status line"
        if acc.email:
            hint += f" and use agy signed in as {acc.email}"
        return Snapshot(status="no_data", message=f"no data yet: {hint}")
    captured = to_epoch(data.get("captured_at"))
    snap = Snapshot(source="tap", updated_at=captured,
                    plan=data.get("plan") if isinstance(data.get("plan"), str) else None)
    snap.limits = _limits(data.get("quota"), captured)
    if not snap.limits:
        snap.status, snap.message = "no_data", "agy reported no quota with a percentage"
    return snap


def parse_quota(status_input: dict, now: int | None = None) -> list[Limit]:
    """Limits straight from a status-line payload (for `tap gemini --print`)."""
    return _limits(status_input.get("quota"), now or int(time.time()))


def _limits(quota, captured: int | None) -> list[Limit]:
    limits = []
    for bucket, q in sorted(quota.items()) if isinstance(quota, dict) else ():
        lim = _limit(bucket, q, captured) if isinstance(bucket, str) and isinstance(q, dict) else None
        if lim is not None:
            limits.append(lim)
    # Gemini's own limits first (like Claude: session, week), then third-party / other buckets
    return sorted(limits, key=lambda lim: (":" in lim.id, lim.window_minutes or 0, lim.id))


def _limit(bucket: str, q: dict, captured: int | None) -> Limit | None:
    if q.get("disabled") is True:
        return None
    left = percent(q.get("remaining_fraction"))
    if left is None:  # remaining_amount alone has no total to compare against
        return None
    used = max(0.0, (1.0 - left) * 100)
    resets = to_epoch(q.get("reset_time"))
    secs = q.get("reset_in_seconds")
    if resets is None and captured and percent(secs) is not None:
        resets = captured + int(secs)

    name = re.sub(r"^gemini[-_ ]*", "", bucket, flags=re.I)
    for word, lid, label, short, minutes in WINDOWS:
        if word in name.lower():
            rest = re.sub(rf"[-_ ]*{word}[-_ ]*", " ", name, flags=re.I).strip()
            if rest:  # e.g. "3p-weekly" -> "Third-party (week)", "gemini-pro-daily" -> "Pro (day)"
                pretty = NAMES.get(rest.lower()) or rest.replace("-", " ").replace("_", " ").title()
                return Limit(f"{lid}:{rest.lower()}", f"{pretty} ({short})", used, resets, minutes)
            return Limit(lid, label, used, resets, minutes)
    label = re.sub(r"[-_]+", " ", name or bucket).strip().capitalize() or "Limit"
    return Limit(bucket, label, used, resets, None)


def _newest_tap() -> dict | None:
    best = None
    root = cache_dir() / "taps"
    for path in root.glob("gemini-*.json") if root.is_dir() else ():
        data = read_json(path)
        if data and (best is None or (to_epoch(data.get("captured_at")) or 0) > (to_epoch(best.get("captured_at")) or 0)):
            best = data
    return best


def tap_from_stdin(raw: str) -> dict | None:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if isinstance(data, dict):
        write_tap(data, int(time.time()))
        return data
    return None
