# ai-limits

See how much of your AI subscriptions is left — Claude and Codex, any number of accounts — right
in the GNOME top bar.

```
                                   ✳ 42%   ◎ 19%   Oct 27 15:49
```

```
 Claude Max
   Session (5h)   ████░░░░░░  42%   ↻ 18:30 (in 2h10m)
   Week           ██░░░░░░░░  18%   ↻ Sun 19:00 (in 5d3h)
 Codex · personal                         ★ most headroom
   Week           ██░░░░░░░░  19%   ↻ Thu 12:00 (in 2d21h)
 Codex · work                             updated 8h ago
   Week           █████████░  91%   ↻ 18:00 (in 2h)
```

**Offline by default.** ai-limits reads only what Claude Code and Codex already saved on your
machine. It sends no requests and does not read your tokens unless you turn on network mode, and
network mode refuses to send anything unless the route goes through the VPN interface you name.

> Unofficial. Not affiliated with Anthropic or OpenAI. The local formats and the usage endpoint it
> reads are undocumented and may change.

## Install

```sh
uv tool install ai-limits        # or: pipx install ai-limits
ai-limits                       # check it sees your accounts
```

For the optional GNOME Shell 45–49 extension, install from a checkout (it is not in the wheel):

```sh
git clone https://github.com/jointsome0-lgtm/ai-limits.git
cd ai-limits
./gnome-extension/install.sh
```

### Platforms

Python **3.11–3.14**, with no runtime dependencies. On distributions with an older system Python
(e.g. Ubuntu 22.04 ships 3.10), use `uv tool install ai-limits`: uv fetches a suitable Python itself.
**Linux** supports the offline CLI,
Claude tap, and VPN-guarded network mode (`ip` from iproute2 is required for network mode).
**macOS and Windows** support the offline CLI and Claude tap; network mode refuses to run
there, before reading credentials or looking up the host. The GNOME extension is Linux-only.

Config and cache use `~/.config/ai-limits` and `~/.cache/ai-limits` on all platforms, unless
`XDG_CONFIG_HOME` / `XDG_CACHE_HOME` override them. On Windows, `~` is the user profile directory
and cache privacy relies on Windows ACLs; on POSIX, directories/files use 0700/0600.
Files and CLI streams use UTF-8. Reset times display in the local timezone; timestamps without
an explicit timezone are interpreted as UTC.

## Where the numbers come from

| Provider | Offline source | Network source (opt-in) |
|---|---|---|
| Codex | rate-limit snapshots in `~/.codex/sessions/**/rollout-*.jsonl`, updated whenever you use Codex | — |
| Claude | `rate_limits` Claude Code passes to its status line; saved by `ai-limits tap claude` | `api.anthropic.com/api/oauth/usage` (adds per-model weekly limits) |

When a window's reset time has passed, the limit is shown as `~0%` until fresh data arrives.

### Claude: add the tap to your status line

In `~/.claude/settings.json`:

```jsonc
"statusLine": { "type": "command", "command": "ai-limits tap claude --print" }
// already have a status line? pass stdin through to it:
"statusLine": { "type": "command", "command": "ai-limits tap claude | bash ~/.claude/statusline.sh" }
```

## Several accounts

Both CLIs keep each login in its own directory (`CLAUDE_CONFIG_DIR=…`, `CODEX_HOME=…`). List them in
`~/.config/ai-limits/config.toml`:

```toml
[[account]]
provider = "claude"
label    = "Claude Max"
home     = "~/.claude"

[[account]]
provider = "codex"
label    = "Codex · personal"
home     = "~/.codex"

[[account]]
provider = "codex"
label    = "Codex · work"
home     = "~/.codex-work"
```

Without a config file, `~/.claude` and `~/.codex` are picked up automatically. With two or more
accounts of one provider, the one with the most headroom gets a ★ and is the one shown in the panel.

## Network mode (Linux only, behind a VPN)

```toml
[network]
enabled = true
require_interface = "tun0"     # mandatory: no interface, no network
min_interval_seconds = 300
```

Before every request ai-limits:

1. checks the interface exists — if not, it stops before even a DNS lookup;
2. resolves the host and asks the kernel which interface the packet would leave by
   (`ip route get`, which honours policy routing as used by sing-box / xray / WireGuard);
3. sends an anonymous probe (no token, no cookies) to the same host and IP, e.g.
   `api.anthropic.com/v1/models`. Being the same domain, it takes the same routing rule in your VPN
   client, so a `direct` rule or a dead tunnel shows up here. Only the exact answer an allowed
   region gets for a missing token (`401 authentication_error`) passes; a region block (403), a
   timeout or anything unexpected stops here;
4. only then sends the real request, to exactly that IP, ignoring proxy environment variables.

Every socket is also bound to the interface (`SO_BINDTODEVICE`), so if the VPN drops between the
checks and the request, the kernel refuses to send it another way. Any failure means no request
with your token, and the panel shows `⏸` next to the last known numbers. Tokens are only read,
never refreshed, never put on a command line, and never written anywhere.

**What this cannot see:** what your VPN client does *inside* the tunnel. If its rules send
`anthropic.com` to a `direct` outbound (or fall back to direct when the proxy is down), traffic
enters `tun0` and still leaves from your real IP. The probe catches a region block, but the only
real guarantee is in your VPN config: route these domains to the proxy with no direct fallback,
or block them when the proxy is down (a kill switch).

## CLI

```
ai-limits               summary
ai-limits --version     installed version
ai-limits json          state for UIs (docs/state-schema.md), also written to ~/.cache/ai-limits/state.json
ai-limits accounts      configured / detected accounts
ai-limits tap claude    status line helper (see above)
```

## Development

```sh
python3 -m unittest discover tests
python3 -m ai_limits json
```

Releases go to PyPI through Trusted Publishing: run the **Build or publish to PyPI** workflow
(`publish.yml`) on `main` with mode `publish`; `build-only` just builds and checks the packages.
The PyPI publisher is owner `jointsome0-lgtm`, repository `ai-limits`, workflow `publish.yml`,
environment `pypi`. No API token is needed.

The GNOME extension only runs `ai-limits json` and draws the result; adding a provider means one
module in `ai_limits/providers/`.

## License

MIT
