"""~/.config/ai-limits/config.toml, with auto-detection when no accounts are listed.

    stale_after_hours = 6

    [network]                      # off unless enabled AND require_interface is set
    enabled = false
    require_interface = "tun0"
    min_interval_seconds = 300

    [[account]]
    provider = "codex"
    label = "Codex · work"
    home = "~/.codex-work"

    [[account]]                    # gemini accounts are told apart by the agy sign-in email
    provider = "gemini"
    label = "Gemini Ultra x5"
    email = "me@gmail.com"
"""

from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .model import AccountConfig, normalise_home

PROVIDERS = ("claude", "codex", "gemini")
DEFAULT_HOMES = {  # provider: (env override or None, default home, default label)
    "claude": ("CLAUDE_CONFIG_DIR", "~/.claude", "Claude"),
    "codex": ("CODEX_HOME", "~/.codex", "Codex"),
    "gemini": (None, "~/.gemini/antigravity-cli", "Gemini"),
}


@dataclass
class NetworkConfig:
    enabled: bool = False
    require_interface: str | None = None
    min_interval_seconds: int = 300

    @property
    def active(self) -> bool:
        # without a required interface there is nothing to check against -> stay offline
        return self.enabled and bool(self.require_interface)


@dataclass
class Config:
    accounts: list[AccountConfig] = field(default_factory=list)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    stale_after_hours: float = 6.0
    path: Path | None = None


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "ai-limits"


def cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ai-limits"


class ConfigError(Exception):
    pass


def load(path: Path | None = None) -> Config:
    path = path or config_dir() / "config.toml"
    raw: dict = {}
    if path.exists():
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"{path}: {e}") from e

    net = raw.get("network", {})
    if not isinstance(net, dict):
        raise ConfigError(f"{path}: [network] must be a table")
    cfg = Config(
        network=NetworkConfig(
            # strict types: `enabled = "false"` must not switch the network on
            enabled=_typed(path, "network.enabled", net.get("enabled", False), bool),
            require_interface=_typed(path, "network.require_interface", net.get("require_interface"), str, None) or None,
            min_interval_seconds=_typed(path, "network.min_interval_seconds", net.get("min_interval_seconds", 300), int),
        ),
        stale_after_hours=float(_typed(path, "stale_after_hours", raw.get("stale_after_hours", 6), (int, float))),
        path=path if path.exists() else None,
    )
    if cfg.network.min_interval_seconds < 60:
        raise ConfigError(f"{path}: network.min_interval_seconds must be at least 60")
    if not (math.isfinite(cfg.stale_after_hours) and cfg.stale_after_hours > 0):
        raise ConfigError(f"{path}: stale_after_hours must be a positive number")

    accounts = raw.get("account", [])
    if not isinstance(accounts, list) or not all(isinstance(e, dict) for e in accounts):
        raise ConfigError(f"{path}: accounts must be [[account]] tables")
    for i, entry in enumerate(accounts):
        provider = entry.get("provider")
        if provider not in PROVIDERS:
            raise ConfigError(f"{path}: account #{i + 1}: provider must be one of {', '.join(PROVIDERS)}")
        env, default_home, default_label = DEFAULT_HOMES[provider]
        home = entry.get("home") or (env and os.environ.get(env)) or default_home
        email = _typed(path, "account.email", entry.get("email"), str, None) or None
        if email and provider != "gemini":
            raise ConfigError(f"{path}: account #{i + 1}: email is only used for gemini accounts")
        acc = AccountConfig(provider, _typed(path, "account.label", entry.get("label"), str, None) or default_label,
                            normalise_home(_typed(path, "account.home", home, str)), email)
        if any(a.id == acc.id for a in cfg.accounts):
            what = f"email {email}" if email else acc.home
            hint = " (give each gemini account its own email)" if provider == "gemini" and not email else ""
            raise ConfigError(f"{path}: account #{i + 1}: {provider} {what} is listed twice{hint}")
        cfg.accounts.append(acc)

    if not cfg.accounts:
        cfg.accounts = detect_accounts()
    return cfg


def _typed(path, key, value, types, *allowed):
    if value in allowed or (isinstance(value, types) and not (isinstance(value, bool) and types is not bool)):
        return value
    raise ConfigError(f"{path}: {key} has the wrong type ({type(value).__name__})")


def detect_accounts() -> list[AccountConfig]:
    found = []
    for provider, (env, default_home, label) in DEFAULT_HOMES.items():
        home = normalise_home((env and os.environ.get(env)) or default_home)
        if os.path.isdir(home):
            found.append(AccountConfig(provider, label, home))
    return found
