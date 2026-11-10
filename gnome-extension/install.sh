#!/usr/bin/env bash
set -euo pipefail

uuid='ai-limits@jointsome0-lgtm.github.io'
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source_dir="$script_dir/$uuid"
destination="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$uuid"
enable=0
if [ "$#" -gt 1 ]; then
    printf 'usage: %s [--enable]\n' "$0" >&2
    exit 2
fi
case "${1:-}" in
    --enable) enable=1 ;;
    '') ;;
    *) printf 'usage: %s [--enable]\n' "$0" >&2; exit 2 ;;
esac

if ! command -v glib-compile-schemas >/dev/null 2>&1; then
    printf '%s\n' \
        'Missing glib-compile-schemas. Install the GLib tools for your distribution:' \
        '  Debian/Ubuntu: sudo apt install libglib2.0-bin' \
        '  Fedora: sudo dnf install glib2' \
        '  Arch: sudo pacman -S glib2' >&2
    exit 1
fi

if [ "$enable" = 1 ]; then
    for tool in gsettings python3; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            printf 'Missing %s (required for --enable).\n' "$tool" >&2
            exit 1
        fi
    done
    # Fail before installing anything when the session lacks GNOME Shell's settings schema.
    gsettings get org.gnome.shell enabled-extensions >/dev/null
    gsettings get org.gnome.shell disabled-extensions >/dev/null
fi

# Check before copying; settings are only touched with --enable.
glib-compile-schemas --strict --dry-run "$source_dir/schemas"
mkdir -p -- "$destination/schemas"
for file in metadata.json extension.js prefs.js stylesheet.css; do
    install -m 644 -- "$source_dir/$file" "$destination/$file"
done
install -m 644 -- "$source_dir/schemas/org.gnome.shell.extensions.ai-limits.gschema.xml" "$destination/schemas/"
glib-compile-schemas --strict "$destination/schemas"

printf 'Installed to %s\n' "$destination"

if [ "$enable" = 1 ]; then
    # Add to enabled-extensions (keeping everything already there) and drop it from disabled-extensions,
    # so Shell turns it on at the next login. Works even before Shell has discovered the extension.
    edit_list() {  # edit_list <key> add|remove
        local current
        current=$(gsettings get org.gnome.shell "$1")
        gsettings set org.gnome.shell "$1" "$(python3 -c '
import ast, sys
items = [] if sys.argv[1].startswith("@as") else ast.literal_eval(sys.argv[1])
items = [i for i in items if i != sys.argv[3]] + ([sys.argv[3]] if sys.argv[2] == "add" else [])
print(repr(items))' "$current" "$2" "$uuid")"
    }
    edit_list enabled-extensions add
    edit_list disabled-extensions remove
    printf '%s\n' 'Enabled: it will appear in the top bar after the next login (on X11: Alt+F2, r).'
else
    printf '%s\n' \
        'On Wayland, log out and back in after a first install so Shell discovers the extension.' \
        "Enable:   \"$script_dir/install.sh\" --enable   (or: gnome-extensions enable $uuid after re-login)"
fi
printf '%s\n' \
    'After updating its code, log out and back in to load the new JavaScript.' \
    "Settings: gnome-extensions prefs $uuid" \
    'Install the core separately if needed: uv tool install ai-limits (or pipx)'
