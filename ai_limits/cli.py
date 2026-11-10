"""ai-limits: AI subscription usage limits, offline by default.

  ai-limits                 human-readable summary
  ai-limits json            state JSON (docs/state-schema.md), also saved to ~/.cache/ai-limits/state.json
  ai-limits accounts        configured / detected accounts
  ai-limits tap claude      for Claude Code's statusLine: saves rate_limits from stdin, passes stdin through
  ai-limits tap claude --print   same, but prints a short status line instead of passing stdin through
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

from . import config
from .core import build_state, format_duration, write_state
from .model import normalise_home
from .providers import claude

DIM, RESET = "\033[2m", "\033[0m"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "show"

    if cmd in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if cmd == "tap":
        return _tap(args[1:])
    if cmd not in ("show", "json", "accounts") or len(args) > 1:
        # before loading config or providers: a typo must never trigger collection (or network)
        print(f"ai-limits: unknown command {' '.join(args)!r} (see --help)", file=sys.stderr)
        return 2

    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(f"ai-limits: {e}", file=sys.stderr)
        return 2

    if cmd == "accounts":
        print(f"config: {cfg.path or '(none, auto-detected)'}")
        net = cfg.network
        print(f"network: {'on via ' + net.require_interface if net.active else 'off'}")
        for acc in cfg.accounts:
            print(f"  {acc.id:22} {acc.provider:7} {acc.label!r:24} {acc.home}")
        return 0

    state = build_state(cfg)
    write_state(state)
    if cmd == "json":
        json.dump(state, sys.stdout, ensure_ascii=False, indent=1, allow_nan=False)
        print()
    else:
        _show(state, color=sys.stdout.isatty())
    return 0


def _tap(args: list[str]) -> int:
    if not args or args[0] != "claude":
        print("usage: ai-limits tap claude [--print]", file=sys.stderr)
        return 2
    raw = sys.stdin.read()
    claude.tap_from_stdin(normalise_home(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"), raw)
    if "--print" in args:
        try:
            rl = json.loads(raw).get("rate_limits") or {}
        except (ValueError, AttributeError):
            rl = {}
        parts = [f"{label} {rl[key]['used_percentage']:.0f}%" for key, label in (("five_hour", "5h"), ("seven_day", "7d"))
                 if isinstance(rl.get(key), dict) and rl[key].get("used_percentage") is not None]
        print(" · ".join(parts) or "no limit data yet")
    else:
        sys.stdout.write(raw)
    return 0


def _show(state: dict, color: bool) -> None:
    dim, reset = (DIM, RESET) if color else ("", "")
    now = int(time.time())
    if not state["accounts"]:
        print("No accounts found (no ~/.claude or ~/.codex). See ~/.config/ai-limits/config.toml.")
    for acc in state["accounts"]:
        head = acc["label"] + (f" {dim}{acc['plan']}{reset}" if acc["plan"] else "")
        if acc["recommended"]:
            head += "  ★ most headroom"
        if acc["status"] == "stale" and acc["updated_at"]:
            head += f"  {dim}[updated {format_duration(now - acc['updated_at'])} ago]{reset}"
        elif acc["status"] != "ok":
            head += f"  {dim}[{acc['status']}{': ' + acc['message'] if acc['message'] else ''}]{reset}"
        print(head)
        for lim in acc["limits"]:
            pct = f"{'~' if lim['estimated'] else ''}{lim['used_percent']:.0f}%"
            bar = _bar(lim["used_percent"])
            print(f"  {lim['label']:16} {bar} {pct:>5}  {dim}{_reset_text(lim['resets_at'], now)}{reset}")
    net = state["network"]
    print(f"{dim}network: {'on via ' + net['interface'] if net['enabled'] else 'off (offline mode)'}{reset}")


def _bar(pct: float, width: int = 12) -> str:
    filled = round(min(max(pct, 0), 100) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _reset_text(resets_at: int | None, now: int) -> str:
    if not resets_at:
        return ""
    when = datetime.fromtimestamp(resets_at)
    same_day = when.date() == datetime.fromtimestamp(now).date()
    return f"↻ {when.strftime('%H:%M' if same_day else '%a %H:%M')} (in {format_duration(resets_at - now)})"
