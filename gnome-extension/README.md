# AI Limits — GNOME Shell extension

Local Claude / Codex / Gemini usage in the top bar, with a menu for every account.
Targets GNOME Shell **45–49**, on Debian/Ubuntu, Fedora, Arch, or other GNOME distributions.
No JavaScript build step; the installer compiles the settings schema.

```sh
./gnome-extension/install.sh
# First install on Wayland: log out and back in, then:
gnome-extensions enable ai-limits@jointsome0-lgtm.github.io
gnome-extensions prefs ai-limits@jointsome0-lgtm.github.io
```

The installer respects `XDG_DATA_HOME` (normally `~/.local/share`). It compiles
the schema in the installed copy, without enabling the extension or installing
the Python core. Log out/in after code updates too; toggling an extension does
not reliably reload its imported modules.

Requires Bash, coreutils (`install`), and `glib-compile-schemas` (Debian/Ubuntu:
`libglib2.0-bin`; Fedora: `glib2`; Arch: `glib2`). The optional `--enable` flag also
requires `gsettings`, Python 3, and the `org.gnome.shell` settings schema.

## Behaviour

- Runs `ai-limits json` asynchronously every 60 seconds (configurable, ≥15).
  Bare executables missing from Shell's PATH are also tried in `~/.local/bin`.
  A 30-second timeout prevents hung commands from blocking future refreshes.
- If the command fails or its JSON is malformed, reads
  `$XDG_CACHE_HOME/ai-limits/state.json` (normally `~/.cache/ai-limits/state.json`)
  asynchronously. Cached chips are dimmed and the menu shows the command error.
  Unsupported schemas display an update message, not a misleading summary.
- Does not make network requests or open credentials. Only the configured core
  command may use network mode, according to its own configuration.
- Each provider chip shows the recommended account's highest usage, or the
  only account's highest usage. Multiple accounts without a recommendation
  show `—`; the extension does not guess which account to recommend.
- Usage is monochrome below 70%, amber at 70%, red at 90%. Critical chips
  include a compact reset countdown. `⏸` means VPN paused, `!` error, `—` no
  data; stale accounts are dim. All accounts remain visible in the menu.
- In the menu each limit has a bar with a tick at how much of its window has
  passed (when the window length is known). Fill past the tick, in blue, is
  usage ahead of an even pace. When that pace would use the limit up clearly
  before the reset, a line under the row says when. An account's status line
  appears only when something needs attention (stale, VPN paused, error, no data).
- Countdown/status labels update in place on refresh. Menu rows rebuild only
  on changed data, opening the menu, or changing the panel placement.
  Past resets say “resetting”; usage estimation remains the core's job.
- Preferences apply position/index live. Use the command row's Apply button
  to save a command. Parsing supports quoting, but **not** shell expansion,
  `~`, `$VARIABLES`, pipelines, or shell operators.

## Fixture / manual checks

Set **Command** in preferences to:

```text
cat /absolute/path/to/ai-limits/tests/fixtures/state-sample.json
```

Expect `✳ 42%   ◎ 19%`, three account sections, a recommended personal Codex
account, and a dimmed critical 91% work account. The fixture's timestamps are
fixed; reset text depends on the current clock and local timezone.

On a real Shell session, also check left/center/right placement and index,
preferences saving, repeated disable/enable, missing-command cache fallback,
missing cache, malformed/unsupported JSON, VPN/no-data/error statuses, and
disabling during a long-running command. No live Shell is needed for:

```sh
ext=gnome-extension/ai-limits@jointsome0-lgtm.github.io
glib-compile-schemas --strict --dry-run "$ext/schemas"
python3 -m json.tool "$ext/metadata.json" >/dev/null
node --input-type=module --check < "$ext/extension.js"
node --input-type=module --check < "$ext/prefs.js"
bash -n gnome-extension/install.sh
```

Compatibility: ESM and Extension/ExtensionPreferences APIs require Shell 45+;
Adw.SpinRow requires libadwaita 1.4+ (GNOME 45+). The layout helper uses
`orientation` where available and the older `vertical` property otherwise.
Static checks do not verify Shell rendering, themes, HiDPI, or runtime behaviour
across all five Shell versions; those still require desktop smoke testing.

GNOME [50](https://gjs.guide/extensions/upgrading/gnome-shell-50.html) has no relevant
entry-point changes. GNOME [51](https://gjs.guide/extensions/upgrading/gnome-shell-51.html)
removes `vertical` and rejects async `disable()`; this extension already feature-detects
`orientation` and has a synchronous `disable()`. No further source-level incompatibility
was found, but 50/51 are deliberately not advertised in `shell-version` until desktop
smoke tests confirm rendering, preferences, and lifecycle behaviour.
