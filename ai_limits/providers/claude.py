"""Claude Code (claude.ai subscription).

Offline source ("tap"): Claude Code passes `rate_limits` to the status line command on stdin;
`ai-limits tap claude` (or a custom status line) saves it to
$XDG_CACHE_HOME/ai-limits/taps/claude-<home_key>.json. Nothing is read from the credentials file.

Network source (opt-in): the OAuth usage endpoint used by Claude Code's /usage. Undocumented and
may change. It also reports per-model weekly limits. The token is only read, never refreshed:
refreshing it here would race with Claude Code's own refresh.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from dataclasses import asdict

from .. import netguard
from ..config import Config, cache_dir
from ..model import AccountConfig, Limit, Snapshot, home_key, percent, to_epoch
from . import private_dir, read_json, write_json

USAGE_HOST = "api.anthropic.com"
USAGE_PATH = "/api/oauth/usage"
# what an allowed region gets without a token; /v1/models because the usage path rate-limits anonymous calls
PROBE_EXPECT = ("/v1/models", 401, "authentication_error")
TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/=-]{20,4096}")


def tap_path(acc: AccountConfig):
    return cache_dir() / "taps" / f"claude-{home_key(acc.home)}.json"


def write_tap(home: str, status_input: dict, now: int) -> bool:
    rate_limits = status_input.get("rate_limits")
    if not rate_limits:
        return False
    if not isinstance(rate_limits, dict):
        return False
    kept = {key: {"used_percentage": w.get("used_percentage"), "resets_at": w.get("resets_at")}
            for key, w in rate_limits.items() if key in ("five_hour", "seven_day") and isinstance(w, dict)}
    write_json(cache_dir() / "taps" / f"claude-{home_key(home)}.json",
               {"home": home, "captured_at": now, "rate_limits": kept})
    return True


def collect(acc: AccountConfig, cfg: Config, now: int) -> Snapshot:
    tap = _from_tap(acc)
    net = _from_network(acc, cfg, now) if cfg.network.active else None

    if tap is None and net is None:
        return Snapshot(status="no_data", message="no data yet: add `ai-limits tap claude` to your status line")
    sources = sorted((s for s in (tap, net) if s is not None and s.limits),
                     key=lambda s: s.updated_at or 0, reverse=True)
    if not sources:  # network failed and nothing offline
        return net if net is not None else tap
    # Merge by limit id, newest source first; an older source only adds limits the newer one lacks
    # (e.g. per-model weeks). Nothing is dropped (a 99 % week still constrains the account even if old),
    # but updated_at is the oldest contributing source, so old numbers make the account stale, not fresh.
    merged = Snapshot(source=sources[0].source, plan=net.plan if net else None)
    for src in sources:
        known = {lim.id for lim in merged.limits}
        extra = [lim for lim in src.limits if lim.id not in known]
        if extra:
            merged.limits += extra
            merged.updated_at = min(filter(None, (merged.updated_at, src.updated_at)), default=None)
    merged.limits.sort(key=lambda lim: ({"5h": 0, "7d": 1}.get(lim.id, 2), lim.id))
    if net is not None and net.status != "ok":  # keep the network problem visible next to offline data
        merged.status, merged.message = net.status, net.message
    return merged


def _from_tap(acc: AccountConfig) -> Snapshot | None:
    data = read_json(tap_path(acc))
    if not data:
        return None
    rl = data.get("rate_limits") or {}
    limits = []
    for key, lid, label, window in (("five_hour", "5h", "Session (5h)", 300), ("seven_day", "7d", "Week", 10080)):
        w = rl.get(key)
        used = percent(w.get("used_percentage")) if isinstance(w, dict) else None
        if used is not None:
            limits.append(Limit(lid, label, used, to_epoch(w.get("resets_at")), window))
    return Snapshot(limits=limits, source="tap", updated_at=to_epoch(data.get("captured_at")))


def _from_network(acc: AccountConfig, cfg: Config, now: int) -> Snapshot:
    """Cached network result, refreshed at most every min_interval_seconds (successful or not)."""
    net_dir = private_dir(cache_dir() / "net")
    cache_file = net_dir / f"{acc.id}.json"
    with os.fdopen(os.open(net_dir / f"{acc.id}.lock", os.O_WRONLY | os.O_CREAT, 0o600), "w") as lock:
        try:  # one fetcher per account across processes (extension, status line, terminal)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _cached_snapshot(read_json(cache_file) or {})
        return _fetch_locked(acc, cfg, now, cache_file)


def _cached_snapshot(cache: dict) -> Snapshot:
    snap = Snapshot(limits=_cached_limits(cache.get("limits")), source="network",
                    updated_at=to_epoch(cache.get("fetched_at")), plan=cache.get("plan"))
    snap.status, snap.message = cache.get("status", "ok"), cache.get("message")
    return snap


def _fetch_locked(acc: AccountConfig, cfg: Config, now: int, cache_file) -> Snapshot:
    cache = read_json(cache_file) or {}
    snap = _cached_snapshot(cache)
    if now - (to_epoch(cache.get("attempted_at")) or 0) < cfg.network.min_interval_seconds:
        return snap
    # record the attempt before any network activity, so a killed run still counts against the interval
    cache["attempted_at"] = now
    write_json(cache_file, cache)

    status, message = "ok", None
    creds = read_json(f"{acc.home}/.credentials.json") or {}
    oauth = creds.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    expires = to_epoch(oauth.get("expiresAt"))
    if not token:
        status, message = "error", "not logged in (no OAuth token)"
    elif not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        status, message = "error", "OAuth token has an unexpected format"  # never echo it
    elif expires and expires <= now:
        status, message = "stale", "token expired: run `claude` once to refresh it"
    else:
        try:
            usage = netguard.get_json_authenticated(USAGE_HOST, USAGE_PATH, {
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
            }, cfg.network.require_interface, probe_expect=PROBE_EXPECT)
            snap = _parse_usage(usage, now)
            snap.plan = oauth.get("subscriptionType")
            # cache only the parsed limits: no unknown fields, nothing that could break strict JSON
            cache.update(limits=[asdict(lim) for lim in snap.limits], fetched_at=now, plan=snap.plan)
        except netguard.VpnCheckFailed as e:
            status, message = "vpn_off", f"network check failed, request not sent ({e})"
        except Exception as e:  # anything else after the token was handed over: never let its text through raw
            status, message = "error", f"usage request failed: {_scrub(e, token)}"

    cache.update(attempted_at=now, status=status, message=message)
    write_json(cache_file, cache)
    snap.status, snap.message = status, message
    return snap


def _cached_limits(items) -> list[Limit]:
    limits = []
    for item in items if isinstance(items, list) else []:
        try:
            lim = Limit(**item)
        except TypeError:
            continue
        if percent(lim.used_percent) is not None:
            limits.append(lim)
    return limits


def _scrub(error: Exception, token: str) -> str:
    """Error text safe to store and show: no token, bounded length."""
    return str(error).replace(token, "<token>")[:200] or type(error).__name__


def _parse_usage(usage: dict, fetched_at: int | None) -> Snapshot:
    limits = []
    for item in usage.get("limits") or []:
        if not isinstance(item, dict):
            continue
        kind, pct = item.get("kind"), percent(item.get("percent"))
        if pct is None:
            continue
        resets = to_epoch(item.get("resets_at"))
        if kind == "session":
            limits.append(Limit("5h", "Session (5h)", pct, resets, 300))
        elif kind == "weekly_all":
            limits.append(Limit("7d", "Week", pct, resets, 10080))
        elif kind == "weekly_scoped":
            scope = item.get("scope") or {}
            name = (scope.get("model") or {}).get("display_name") or scope.get("surface") or "scoped"
            limits.append(Limit(f"7d:{name.lower()}", f"{name} (week)", pct, resets, 10080))
    if not limits:  # older response shape
        for key, lid, label, window in (("five_hour", "5h", "Session (5h)", 300), ("seven_day", "7d", "Week", 10080)):
            w = usage.get(key)
            used = percent(w.get("utilization")) if isinstance(w, dict) else None
            if used is not None:
                limits.append(Limit(lid, label, used, to_epoch(w.get("resets_at")), window))
    return Snapshot(limits=limits, source="network", updated_at=fetched_at)


def tap_from_stdin(home: str, raw: str) -> None:
    try:
        data = json.loads(raw)
    except ValueError:
        return
    if isinstance(data, dict):
        write_tap(home, data, int(time.time()))
