# `ai-limits json` output (schema 1)

The core (`ai-limits`, Python) prints this JSON on stdout and also writes it to
`$XDG_CACHE_HOME/ai-limits/state.json`. UIs (the GNOME extension, scripts) only
read it — they never touch credentials or the network themselves.

```jsonc
{
  "schema": 1,
  "generated_at": 1793107651,          // epoch seconds
  "network": {
    "enabled": false,                  // network mode switched on in config
    "interface": "tun0",               // required VPN interface, null if not configured
    "vpn_ok": null                     // true/false when checked this run, null when not checked
  },
  "accounts": [
    {
      "id": "codex-1a2b3c4d5e6f",      // stable: provider + hash of home dir (gemini with `email`: hash of the email)
      "provider": "codex",             // "claude" | "codex" | "gemini"
      "label": "Codex · personal",
      "home": "/home/user/.codex",
      "plan": "pro",                   // null if unknown
      "source": "sessions",            // "tap" | "sessions" | "network" | null
      "updated_at": 1793107000,        // when the data was captured, null if never
      "status": "ok",                  // see below
      "message": null,                 // human-readable detail for non-ok status
      "recommended": true,             // most headroom among this provider's usable accounts; only set when the provider has 2+ accounts
      "limits": [
        {
          "id": "7d",                  // "5h" | "7d" | "7d:<model>" | "1d" | other provider ids
          "label": "Week",
          "used_percent": 64.0,        // 0..100 (may exceed 100)
          "resets_at": 1793599717,     // epoch seconds, null if unknown
          "window_minutes": 10080,     // null if unknown
          "estimated": false           // true: resets_at already passed, so used_percent was set to 0 locally
        }
      ]
    }
  ]
}
```

## `status`

| value     | meaning                                                         | UI hint                     |
|-----------|-----------------------------------------------------------------|-----------------------------|
| `ok`      | data is fresh enough                                            | normal                      |
| `stale`   | data is older than the stale threshold (default 6 h)           | dim, show "updated N ago"   |
| `no_data` | account exists but nothing captured yet                         | "—", show `message`         |
| `vpn_off` | network mode wanted to fetch but the VPN check failed (no request was sent) | "⏸ VPN off"   |
| `error`   | anything else (unreadable files, bad JSON…)                     | "!" + `message`             |

`stale` / `vpn_off` accounts still carry their last known `limits`.

## Panel summary rule (for UIs)

For each provider: take the `recommended` account (or the only account), and show
the highest `used_percent` among its limits, plus its `resets_at` countdown when
≥ 90 %. Thresholds: < 70 normal (monochrome), 70–89 warning, ≥ 90 critical.
