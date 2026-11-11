import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const TRACK_WIDTH = 150; // px, matches .ai-limits-meter in stylesheet.css
const TICK_WIDTH = 2; // px, matches .ai-limits-tick
const TICK_GAP = 1; // px of background on each side of the tick, so it stays visible on the fill
const DAY = 86400;

function severity(percent) {
    return percent >= 90 ? 'ai-limits-critical' : percent >= 70 ? 'ai-limits-warning' : '';
}

const PROVIDER_NAMES = {claude: 'Claude', codex: 'Codex', gemini: 'Gemini'};

function providerName(provider) {
    return PROVIDER_NAMES[provider] ?? provider;
}

// Accounts the core can rank: they have numbers, even if not fresh ones (core.USABLE).
function usable(account) {
    return ['ok', 'stale', 'vpn_off'].includes(account.status) && account.limits.length > 0;
}

function duration(seconds, compact = false) {
    if (seconds <= 0)
        return 'now';
    const minutes = Math.ceil(seconds / 60);
    if (minutes < 60)
        return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24)
        return `${hours}h${!compact && minutes % 60 ? ` ${minutes % 60}m` : ''}`;
    const days = Math.floor(hours / 24);
    return `${days}d${!compact && hours % 24 ? ` ${hours % 24}h` : ''}`;
}

function age(timestamp, now) {
    if (!Number.isFinite(timestamp))
        return 'never';
    const seconds = Math.max(0, now - timestamp);
    if (seconds < 60)
        return 'just now';
    if (seconds < 3600)
        return `${Math.floor(seconds / 60)} min ago`;
    if (seconds < DAY)
        return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / DAY)}d ago`;
}

const WINDOW_NAMES = {60: 'Hour', 300: '5h', 1440: 'Day', 10080: 'Week'};

function windowName(minutes) {
    if (!Number.isFinite(minutes) || minutes <= 0)
        return '';
    if (WINDOW_NAMES[minutes])
        return WINDOW_NAMES[minutes];
    if (minutes % 1440 === 0)
        return `${minutes / 1440}d`;
    return minutes % 60 === 0 ? `${minutes / 60}h` : `${minutes}m`;
}

// Who a limit applies to and its window, for the two label columns. Scoped limits ("7d:fable")
// take the name from their label ("Fable (week)"); unscoped ones cover the provider's models.
function limitParts(account, limit) {
    const window = windowName(limit.window_minutes);
    if (!window)
        return {scope: limit.label, window: ''};
    if (String(limit.id ?? '').includes(':'))
        return {scope: limit.label.replace(/\s*\([^)]*\)\s*$/, '') || limit.label, window};
    return {scope: account.provider === 'gemini' ? 'Gemini' : 'All models', window};
}

// "Gemini Ultra x5" already says what "Google AI Ultra" says. A plan the label does not name
// stays visible: it is how a wrong signed-in account shows up.
function planText(account) {
    const plan = account.plan?.trim() ?? '';
    const tier = plan.split(/\s+/).pop()?.toLowerCase();
    const words = account.label.toLowerCase().split(/[^\p{L}\p{N}]+/u);
    return tier && words.includes(tier) ? '' : plan;
}

// Core messages are terse CLI text ("no data yet: add `x` to ..."): show them as sentences,
// with `command` spans in monospace. Everything from the core is escaped; only <tt> is ours.
function sentenceMarkup(text) {
    let line = String(text).trim();
    if (!line)
        return '';
    line = line[0].toUpperCase() + line.slice(1);
    if (!/[.!?)]$/.test(line))
        line += '.';
    return GLib.markup_escape_text(line, -1).replace(/`([^`]+)`/g, '<tt>$1</tt>');
}

function localTime(timestamp, format) {
    return GLib.DateTime.new_from_unix_local(Math.floor(timestamp))?.format(format) ?? '';
}

// One column for the reset: a countdown while it is less than a day away, then weekday and time.
function resetText(limit, now) {
    if (!Number.isFinite(limit.resets_at))
        return '';
    const left = limit.resets_at - now;
    if (left <= 0)
        return 'resetting';
    return left < DAY ? `in ${duration(left)}` : localTime(limit.resets_at, '%a %H:%M');
}

// Share of the current window that has already passed (0..1), or null when the window is unknown.
function elapsed(limit, now) {
    const windowSeconds = (limit.window_minutes ?? 0) * 60;
    if (!(windowSeconds > 0) || !Number.isFinite(limit.resets_at) || limit.resets_at <= now)
        return null;
    const left = limit.resets_at - now;
    return left > windowSeconds ? null : 1 - left / windowSeconds;
}

// When the limit runs out at the current pace, if that is clearly before the reset; otherwise null.
// Early in a window the pace says little, and running out minutes before the reset is not worth a line.
function runsOutAt(limit, now) {
    const passed = elapsed(limit, now);
    if (passed === null || passed < 0.2 || limit.estimated ||
        limit.used_percent < 5 || limit.used_percent >= 100)
        return null;
    const windowSeconds = limit.window_minutes * 60;
    const start = limit.resets_at - windowSeconds;
    const out = start + (now - start) * 100 / limit.used_percent;
    return out < limit.resets_at - windowSeconds * 0.05 ? out : null;
}

// The line under a limit that is on course to run out; the weekday comes from the locale, so escape it.
function forecastMarkup(out, now) {
    const left = out - now;
    if (left <= 0)
        return 'At this pace it runs out <b>now</b>.';
    const bold = text => `<b>${GLib.markup_escape_text(text, -1)}</b>`;
    const when = left < DAY ? `in ${bold(duration(left))}`
        : `on ${bold(localTime(out, '%a'))} at ${bold(localTime(out, '%H:%M'))}`;
    return `At this pace it runs out ${when}.`;
}

// Only say something when it needs attention; the footer already shows when data was refreshed.
function statusText(account, now) {
    const message = account.message ? sentenceMarkup(account.message) : '';
    const extra = message ? ` ${message}` : '';
    if (account.status === 'vpn_off')
        return `Paused: the VPN is off. Showing data from ${age(account.updated_at, now)}.${extra}`;
    if (account.status === 'error')
        return message || 'Unable to read account data.';
    if (account.status === 'no_data')
        return message || 'No data captured yet.';
    if (account.status === 'stale')
        return `Last updated ${age(account.updated_at, now)}, so these numbers may be out of date.${extra}`;
    return message;
}

function parsePayload(text) {
    const state = JSON.parse(text);
    if (state?.schema !== 1) {
        const error = new Error('Update ai-limits: this extension requires state schema 1.');
        error.name = 'SchemaVersionError';
        throw error;
    }
    // Validate before passing external data to St; never use markup from the core.
    if (!Number.isFinite(state.generated_at) || !Array.isArray(state.accounts))
        throw new Error('Invalid ai-limits state: expected generated_at and accounts.');
    for (const account of state.accounts) {
        if (!account || typeof account.provider !== 'string' || !account.provider ||
            typeof account.label !== 'string' || !Array.isArray(account.limits) ||
            !['ok', 'stale', 'no_data', 'vpn_off', 'error'].includes(account.status) ||
            !(account.updated_at === null || Number.isFinite(account.updated_at)) ||
            !(account.message == null || typeof account.message === 'string') ||
            !(account.plan == null || typeof account.plan === 'string'))
            throw new Error('Invalid ai-limits account.');
        for (const limit of account.limits) {
            if (!limit || typeof limit.label !== 'string' ||
                !Number.isFinite(limit.used_percent) || limit.used_percent < 0 ||
                !(limit.resets_at === null || Number.isFinite(limit.resets_at)))
                throw new Error('Invalid ai-limits limit.');
        }
    }
    return state;
}

function errorText(error) {
    return String(error.message ?? error).replace(/\s+/g, ' ').slice(0, 500);
}

// Prefer orientation where available (48+); vertical is the 45–47 API.
function column(properties = {}) {
    const direction = 'orientation' in St.BoxLayout.prototype
        ? {orientation: Clutter.Orientation.VERTICAL} : {vertical: true};
    return new St.BoxLayout({...direction, ...properties});
}

export default class AILimitsExtension extends Extension {
    enable() {
        this._enabled = true;
        this._settings = this.getSettings();
        this._settingsSignals = [];
        this._menuSignals = [];
        this._timeLabels = [];
        this._state = null;
        this._notice = 'Loading ai-limits…';
        this._stateKey = null;
        this._request = null;
        this._timer = 0;
        this._button = null;
        this._menuSignal = 0;
        this._placeButton();

        for (const key of ['panel-position', 'panel-index']) {
            this._settingsSignals.push(this._settings.connect(`changed::${key}`,
                () => this._placeButton()));
        }
        this._settingsSignals.push(this._settings.connect('changed::refresh-interval',
            () => this._schedule()));
        this._settingsSignals.push(this._settings.connect('changed::command', () => {
            this._cancelRequest();
            this._refresh();
        }));
        this._schedule();
        this._refresh();
    }

    disable() {
        this._enabled = false;
        if (this._timer)
            GLib.Source.remove(this._timer);
        this._timer = 0;
        for (const id of this._settingsSignals)
            this._settings.disconnect(id);
        this._cancelRequest();
        this._destroyButton();
        this._settingsSignals = null;
        this._settings = null;
        this._state = null;
        this._stateKey = null;
        this._notice = null;
        this._menuSignals = null;
        this._timeLabels = null;
    }

    _schedule() {
        if (this._timer)
            GLib.Source.remove(this._timer);
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT,
            Math.max(15, this._settings.get_int('refresh-interval')), () => {
                this._renderPanel();
                this._updateMenuTimes();
                this._refresh();
                return GLib.SOURCE_CONTINUE;
            });
    }

    _cancelRequest() {
        const request = this._request;
        this._request = null;
        if (!request)
            return;
        if (request.timeout)
            GLib.Source.remove(request.timeout);
        request.timeout = 0;
        request.cancellable.cancel();
        request.commandCancellable.cancel();
        // Cancelling communicate only cancels I/O, not the child process.
        if (request.process?.get_identifier())
            request.process.force_exit();
    }

    async _refresh() {
        if (!this._enabled || this._request)
            return;
        const request = {
            cancellable: new Gio.Cancellable(),
            commandCancellable: new Gio.Cancellable(),
            process: null,
            timeout: 0,
            timedOut: false,
        };
        this._request = request;
        const current = () => this._enabled && this._request === request;
        try {
            let state;
            let notice = '';
            try {
                state = parsePayload(await this._runCommand(request));
            } catch (error) {
                if (!current())
                    return;
                if (error.name === 'SchemaVersionError')
                    throw error;
                const commandError = errorText(error);
                try {
                    state = parsePayload(await this._readCache(request.cancellable));
                    notice = `Cached data · ${commandError}`;
                } catch (cacheError) {
                    if (cacheError.name === 'SchemaVersionError')
                        throw cacheError;
                    throw new Error(`ai-limits unavailable / not installed. Install: pipx install ai-limits. ${commandError} · Cache: ${errorText(cacheError)}`);
                }
            }
            if (current())
                this._setState(state, notice);
        } catch (error) {
            if (current())
                this._setState(null, errorText(error));
        } finally {
            if (request.timeout)
                GLib.Source.remove(request.timeout);
            request.timeout = 0;
            request.process = null;
            if (current()) {
                this._request = null;
                this._renderPanel();
                this._updateMenuTimes();
            }
        }
    }

    _runCommand(request) {
        const [parsed, argv] = GLib.shell_parse_argv(this._settings.get_string('command'));
        if (!parsed || !argv.length)
            throw new Error('Set a command in Settings (default: ai-limits json).');
        if (!argv[0].includes('/') && !GLib.find_program_in_path(argv[0]))
            argv[0] = GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', argv[0]]);
        request.process = Gio.Subprocess.new(argv,
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        request.timeout = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 30, () => {
            request.timeout = 0;
            request.timedOut = true;
            request.commandCancellable.cancel();
            if (request.process?.get_identifier())
                request.process.force_exit();
            return GLib.SOURCE_REMOVE;
        });
        return new Promise((resolve, reject) => {
            request.process.communicate_utf8_async(null, request.commandCancellable,
                (process, result) => {
                    if (request.timeout)
                        GLib.Source.remove(request.timeout);
                    request.timeout = 0;
                    try {
                        const [, stdout, stderr] = process.communicate_utf8_finish(result);
                        if (!process.get_successful())
                            throw new Error(stderr.trim() || 'ai-limits command failed.');
                        resolve(stdout);
                    } catch (error) {
                        reject(request.timedOut ? new Error('ai-limits command timed out after 30s.') : error);
                    } finally {
                        request.process = null;
                    }
                });
        });
    }

    _readCache(cancellable) {
        const path = GLib.build_filenamev([GLib.get_user_cache_dir(), 'ai-limits', 'state.json']);
        return new Promise((resolve, reject) => {
            Gio.File.new_for_path(path).load_contents_async(cancellable, (file, result) => {
                try {
                    const [, bytes] = file.load_contents_finish(result);
                    resolve(new TextDecoder('utf-8', {fatal: true}).decode(bytes));
                } catch (error) {
                    reject(error);
                }
            });
        });
    }

    _setState(state, notice) {
        const key = JSON.stringify([state, notice]);
        if (key === this._stateKey)
            return;
        this._stateKey = key;
        this._state = state;
        this._notice = notice;
        this._renderPanel();
        this._rebuildMenu();
    }

    _placeButton() {
        const wasOpen = this._button?.menu.isOpen;
        this._destroyButton();
        this._button = new PanelMenu.Button(0.5, 'AI Limits');
        this._chips = new Map();
        this._panelBox = new St.BoxLayout({style_class: 'panel-status-menu-box ai-limits-panel'});
        this._button.add_child(this._panelBox);
        this._menuSignal = this._button.menu.connect('open-state-changed', (_menu, open) => {
            if (open) {
                this._renderPanel();
                this._rebuildMenu();
            }
        });
        // Recreate through the public panel API, avoiding private panel box fields.
        Main.panel.addToStatusArea(this.uuid, this._button,
            this._settings.get_int('panel-index'), this._settings.get_string('panel-position'));
        this._renderPanel();
        this._rebuildMenu();
        if (wasOpen)
            this._button.menu.open();
    }

    _clearMenu() {
        for (const [object, id] of this._menuSignals)
            object.disconnect(id);
        this._menuSignals = [];
        this._timeLabels = [];
        this._button?.menu.removeAll();
    }

    _destroyButton() {
        this._clearMenu();
        if (this._menuSignal)
            this._button.menu.disconnect(this._menuSignal);
        this._menuSignal = 0;
        this._button?.destroy();
        this._button = null;
        this._panelBox = null;
        this._chips = null;
    }

    // St CSS has neither opacity nor currentColor, so muted text and status colours are fixed colours,
    // with a second set for light surfaces (Yaru's light menus; the panel stays dark). Tag the
    // container when the theme draws dark text on it.
    _markShade(actor) {
        if (!actor?.get_stage())
            return;
        const color = actor.get_theme_node().get_foreground_color();
        const light = 0.299 * color.red + 0.587 * color.green + 0.114 * color.blue < 128;
        if (light)
            actor.add_style_class_name('ai-limits-on-light');
        else
            actor.remove_style_class_name('ai-limits-on-light');
    }

    _renderPanel() {
        this._markShade(this._panelBox);
        const providers = new Map();
        for (const account of this._state?.accounts ?? []) {
            if (!providers.has(account.provider))
                providers.set(account.provider, []);
            providers.get(account.provider).push(account);
        }
        const summaries = [];
        const now = Date.now() / 1000;
        for (const [provider, accounts] of providers) {
            const account = accounts.find(item => item.recommended === true) ??
                (accounts.length === 1 ? accounts[0] : null);
            const limit = account?.limits.reduce((max, item) =>
                !max || item.used_percent > max.used_percent ? item : max, null);
            let value = '—';
            let color = '';
            let note = '';
            if (account?.status === 'error') {
                value = '!';
            } else if (account?.status !== 'no_data' && limit) {
                value = `${limit.estimated ? '~' : ''}${Math.round(limit.used_percent)}%`;
                // amber/red outrank blue: they already say "look at this"
                const ahead = account.status === 'ok' &&
                    account.limits.some(item => runsOutAt(item, now) !== null);
                color = severity(limit.used_percent) || (ahead ? 'ai-limits-ahead' : '');
                if (ahead)
                    note = ', on course to run out before the reset';
                if (limit.used_percent >= 90 && Number.isFinite(limit.resets_at))
                    value += ` · ${duration(limit.resets_at - now, true)}`;
            }
            // offline data may still be fresh when only the network fetch is paused
            const paused = account?.status === 'vpn_off';
            if (paused)
                note += ', paused while the VPN is off';
            const dim = account?.status === 'stale' || this._notice.startsWith('Cached data');
            summaries.push({key: provider, text: value, note, paused, style: dim ? 'ai-limits-tertiary' : color});
        }
        if (!summaries.length)
            summaries.push({key: '', text: this._state ? 'AI —' : this._notice.startsWith('Loading') ? 'AI …' : 'AI !', style: ''});
        for (const [key, chip] of this._chips) {
            if (!summaries.some(summary => summary.key === key)) {
                chip.box.destroy();
                this._chips.delete(key);
            }
        }
        summaries.forEach((summary, index) => {
            let chip = this._chips.get(summary.key);
            if (!chip) {
                // colour classes go on the box; the icon and the label inherit them
                const box = new St.BoxLayout({y_align: Clutter.ActorAlign.CENTER});
                if (summary.key)
                    box.add_child(this._icon(summary.key, 'ai-limits-chip-icon'));
                const label = new St.Label({y_align: Clutter.ActorAlign.CENTER});
                box.add_child(label);
                const pause = new St.Icon({icon_name: 'media-playback-pause-symbolic',
                    style_class: 'ai-limits-chip-icon', y_align: Clutter.ActorAlign.CENTER});
                box.add_child(pause);
                chip = {box, label, pause};
                this._chips.set(summary.key, chip);
                this._panelBox.add_child(box);
            }
            this._panelBox.set_child_at_index(chip.box, index);
            chip.label.text = summary.text;
            chip.pause.visible = summary.paused === true;
            chip.box.style_class = `ai-limits-chip ${summary.style}`;
        });
        this._button.accessible_name = `AI Limits: ${summaries.map(item =>
            `${item.key ? `${providerName(item.key)} ` : ''}${item.text}${item.note ?? ''}`).join('; ')}`;
    }

    // Symbolic SVG from icons/<provider>-symbolic.svg, recoloured like text (so amber/red/blue apply);
    // unknown providers get their initial. The name comes from the core, so it is checked before use as a path.
    _icon(provider, styleClass) {
        if (/^[a-z0-9_-]+$/.test(provider)) {
            const file = this.dir.get_child('icons').get_child(`${provider}-symbolic.svg`);
            if (file.query_exists(null)) {
                return new St.Icon({gicon: new Gio.FileIcon({file}), style_class: styleClass,
                    y_align: Clutter.ActorAlign.CENTER});
            }
        }
        return this._label(Array.from(provider)[0]?.toUpperCase() ?? '?', `${styleClass} ai-limits-letter`);
    }

    _label(text, style = '') {
        return new St.Label({text, style_class: style, y_align: Clutter.ActorAlign.CENTER});
    }

    _wrap(label) {
        // Bound long messages; wrap them instead of widening the popup.
        label.clutter_text.line_wrap = true;
        label.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        return label;
    }

    _timeLabel(format, style = '', markup = false) {
        const label = this._label('', style);
        const set = text => markup ? label.clutter_text.set_markup(text) : label.set_text(text);
        set(format(Date.now() / 1000));
        this._timeLabels.push({set, format});
        return label;
    }

    _updateMenuTimes() {
        if (!this._button.menu.isOpen)
            return;
        const now = Date.now() / 1000;
        for (const {set, format} of this._timeLabels)
            set(format(now));
    }

    // Accounts, limits and notes are plain widgets rather than menu items, and without the
    // .popup-menu-item class: St marks widgets that take no input :insensitive, and the theme greys
    // out the text of insensitive menu items.
    _block(...children) {
        const block = column({style_class: 'ai-limits-block'});
        for (const child of children)
            block.add_child(child);
        return block;
    }

    _rebuildMenu() {
        this._clearMenu();
        const menu = this._button.menu;
        this._markShade(menu.box);
        const section = new PopupMenu.PopupMenuSection();
        menu.addMenuItem(section);
        if (this._notice)
            section.box.add_child(this._block(this._wrap(this._label(this._notice, 'ai-limits-notice ai-limits-secondary'))));
        const accounts = this._state?.accounts ?? [];
        if (this._state && !accounts.length)
            section.box.add_child(this._block(this._label('No accounts found. Configure accounts in ai-limits.', 'ai-limits-secondary')));
        if (accounts.some(account => account.limits.length))
            section.box.add_child(this._block(this._columnTitles()));
        const choices = new Map();
        for (const account of accounts.filter(usable))
            choices.set(account.provider, (choices.get(account.provider) ?? 0) + 1);
        accounts.forEach((account, index) => {
            // a line between providers; accounts of the same provider stay together
            if (index && accounts[index - 1].provider !== account.provider)
                section.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
            section.box.add_child(this._account(account,
                account.recommended === true && choices.get(account.provider) > 1));
        });
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        const generated = this._state?.generated_at;
        const refresh = new PopupMenu.PopupMenuItem('Refresh');
        const aside = this._timeLabel(now => this._request ? 'refreshing…'
            : Number.isFinite(generated) ? `updated ${age(generated, now)}` : 'not updated yet',
        'ai-limits-aside ai-limits-secondary');
        aside.x_expand = true;
        aside.x_align = Clutter.ActorAlign.END;
        refresh.add_child(aside);
        // Keep the menu open: the new numbers replace the old ones in place.
        refresh.activate = () => {
            this._refresh();
            this._updateMenuTimes();
        };
        menu.addMenuItem(refresh);
        const settings = new PopupMenu.PopupMenuItem('Settings');
        this._menuSignals.push([settings, settings.connect('activate', () => this.openPreferences())]);
        menu.addMenuItem(settings);
    }

    // The first cell of a row: which models and which window ("All models  5h"). One fixed-width box,
    // so a limit without a known window can use its whole width for the name.
    _names(scope = '', window = '', tone = '') {
        const cell = new St.BoxLayout({style_class: 'ai-limits-names'});
        if (!window) {
            cell.add_child(new St.Label({text: scope, style_class: `ai-limits-scope-only ${tone}`,
                x_expand: true, y_align: Clutter.ActorAlign.CENTER}));
            return cell;
        }
        cell.add_child(this._label(scope, `ai-limits-scope ${tone}`));
        cell.add_child(this._label(window, `ai-limits-window ${tone || 'ai-limits-secondary'}`));
        return cell;
    }

    // Column titles above the first account, aligned with the limit rows.
    _columnTitles() {
        const row = new St.BoxLayout({style_class: 'ai-limits-row ai-limits-titles'});
        row.add_child(this._names());
        const legend = new St.BoxLayout({style_class: 'ai-limits-legend'});
        legend.add_child(new St.Widget({style_class: 'ai-limits-tick ai-limits-legend-tick',
            y_align: Clutter.ActorAlign.CENTER}));
        legend.add_child(this._label('time passed', 'ai-limits-column-title ai-limits-tertiary'));
        row.add_child(legend);
        row.add_child(this._label('used', 'ai-limits-percent ai-limits-column-title ai-limits-tertiary'));
        row.add_child(this._label('resets', 'ai-limits-reset ai-limits-column-title ai-limits-tertiary'));
        return row;
    }

    _account(account, pick) {
        const now = Date.now() / 1000;
        const title = new St.BoxLayout({style_class: 'ai-limits-title'});
        title.add_child(this._icon(account.provider, 'ai-limits-glyph'));
        title.add_child(this._label(account.label, 'ai-limits-account-name'));
        const plan = planText(account);
        if (plan)
            title.add_child(this._label(plan, 'ai-limits-plan ai-limits-secondary'));
        if (pick) {
            title.add_child(new St.Label({
                text: '★ most room left', style_class: 'ai-limits-pick ai-limits-secondary',
                x_expand: true, x_align: Clutter.ActorAlign.END, y_align: Clutter.ActorAlign.CENTER,
            }));
        }
        const block = this._block(title);
        if (statusText(account, now)) {
            const tone = account.status === 'error' ? 'ai-limits-critical' : 'ai-limits-secondary';
            block.add_child(this._wrap(this._timeLabel(at => statusText(account, at),
                `ai-limits-status ${tone}`, true)));
        }
        let scope = null;
        for (const limit of account.limits) {
            const parts = limitParts(account, limit);
            const out = account.status === 'ok' ? runsOutAt(limit, now) : null;
            block.add_child(this._limitRow(account, limit, parts, parts.scope !== scope, out, now));
            scope = parts.scope;
            if (out !== null)
                block.add_child(this._forecastRow(limit, out));
        }
        return block;
    }

    _limitRow(account, limit, parts, firstInScope, out, now) {
        const stale = account.status === 'stale' || account.status === 'vpn_off';
        const unused = Math.round(limit.used_percent) === 0;
        // old numbers are shown, but without the colours that ask for attention
        const tone = stale ? 'ai-limits-dim' : severity(limit.used_percent) || (out !== null ? 'ai-limits-ahead' : '');
        const quiet = stale || unused ? 'ai-limits-tertiary' : 'ai-limits-secondary';
        const percent = `${limit.estimated ? '~' : ''}${Math.round(limit.used_percent)}%`;
        const row = new St.BoxLayout({style_class: 'ai-limits-row'});
        // the scope is written once per group; rows below it leave that part empty
        row.add_child(this._names(firstInScope || !parts.window ? parts.scope : '', parts.window,
            stale ? 'ai-limits-tertiary' : ''));
        row.add_child(this._meter(limit, now, tone));
        row.add_child(this._label(percent, `ai-limits-percent ${tone === 'ai-limits-dim' || (!tone && unused)
            ? 'ai-limits-tertiary' : tone}`));
        row.add_child(this._timeLabel(at => resetText(limit, at), `ai-limits-reset ${quiet}`));
        const reset = resetText(limit, now);
        row.accessible_name = `${account.label}, ${parts.scope}${parts.window ? ` ${parts.window}` : ''}: ` +
            `${percent} used${reset ? `, resets ${reset}` : ''}` +
            `${out !== null ? ', on course to run out before the reset' : ''}`;
        return row;
    }

    // A note right under the limit it is about, starting at the bar so the scope column stays clear.
    _forecastRow(limit, out) {
        const row = new St.BoxLayout({style_class: 'ai-limits-row ai-limits-forecast-row'});
        row.add_child(this._names());
        row.add_child(this._timeLabel(now => forecastMarkup(out, now),
            `ai-limits-forecast ${severity(limit.used_percent) || 'ai-limits-ahead'}`, true));
        return row;
    }

    // Usage bar with a tick at how much of the window has passed: fill past the tick = faster than even pace.
    // Built from side-by-side segments (fill, empty, gap, tick) rather than overlapping layers. Sizes are
    // CSS px (TRACK_WIDTH matches the stylesheet) so St applies HiDPI scaling; resizing from an
    // allocation handler would trigger relayout loops.
    _meter(limit, now, tone) {
        const meter = new St.BoxLayout({style_class: 'ai-limits-meter', y_align: Clutter.ActorAlign.CENTER});
        const used = Math.round(TRACK_WIDTH * Math.min(1, Math.max(0, limit.used_percent / 100)));
        const passed = elapsed(limit, now);
        const tick = passed === null ? null
            : Math.min(TRACK_WIDTH - TICK_WIDTH, Math.round(TRACK_WIDTH * passed));
        const gapFrom = tick === null ? null : Math.max(0, tick - TICK_GAP);
        const gapTo = tick === null ? null : Math.min(TRACK_WIDTH, tick + TICK_WIDTH + TICK_GAP);
        const cuts = [...new Set([0, used, TRACK_WIDTH,
            ...(tick === null ? [] : [gapFrom, tick, tick + TICK_WIDTH, gapTo])])].sort((a, b) => a - b);
        const pieces = [];
        for (let i = 1; i < cuts.length; i++) {
            const [from, to] = [cuts[i - 1], cuts[i]];
            if (to <= from)
                continue;
            const kind = from === tick ? 'tick'
                : tick !== null && from >= gapFrom && to <= gapTo ? 'gap'
                    : to <= used ? 'fill' : 'track';
            pieces.push({from, to, kind});
        }
        pieces.forEach(({from, to, kind}, index) => {
            const classes = [{tick: 'ai-limits-tick', gap: 'ai-limits-gap', fill: `ai-limits-fill ${tone}`,
                track: 'ai-limits-track'}[kind]];
            if (kind === 'fill' || kind === 'track') {
                // round only the bar's outer ends; cuts at the tick and between fill and track stay square
                const outer = neighbour => !neighbour || neighbour.kind === 'tick' || neighbour.kind === 'gap';
                if (from === 0 && outer(pieces[index - 1]))
                    classes.push('ai-limits-start');
                if (to === TRACK_WIDTH && outer(pieces[index + 1]))
                    classes.push('ai-limits-end');
            }
            meter.add_child(new St.Widget({
                style_class: classes.join(' '), style: `width: ${to - from}px;`,
                y_align: Clutter.ActorAlign.CENTER,
            }));
        });
        return meter;
    }
}
