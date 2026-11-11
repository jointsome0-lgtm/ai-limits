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

function severity(percent) {
    return percent >= 90 ? 'ai-limits-critical' : percent >= 70 ? 'ai-limits-warning' : '';
}

function glyph(provider) {
    if (provider === 'claude')
        return '✳';
    if (provider === 'codex')
        return '◎';
    if (provider === 'gemini')
        return '✦';
    return Array.from(provider)[0].toUpperCase();
}

function duration(seconds, compact = false) {
    if (seconds <= 0)
        return 'now';
    const minutes = Math.ceil(seconds / 60);
    if (minutes < 60)
        return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24)
        return `${hours}h${!compact && minutes % 60 ? `${minutes % 60}m` : ''}`;
    const days = Math.floor(hours / 24);
    return `${days}d${!compact && hours % 24 ? `${hours % 24}h` : ''}`;
}

function age(timestamp, now) {
    if (!Number.isFinite(timestamp))
        return 'never';
    const seconds = Math.max(0, now - timestamp);
    if (seconds < 60)
        return 'just now';
    if (seconds < 3600)
        return `${Math.floor(seconds / 60)} min ago`;
    if (seconds < 86400)
        return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
}

function clock(timestamp, now) {
    const when = GLib.DateTime.new_from_unix_local(Math.floor(timestamp));
    const today = GLib.DateTime.new_from_unix_local(Math.floor(now));
    if (!when || !today)
        return null;
    return when.format(when.format('%F') === today.format('%F') ? '%H:%M' : '%a %H:%M');
}

function resetWhen(limit, now) {
    if (!Number.isFinite(limit.resets_at))
        return '';
    if (limit.resets_at <= now)
        return 'resetting';
    return clock(limit.resets_at, now) ?? '';
}

function resetIn(limit, now) {
    return Number.isFinite(limit.resets_at) && limit.resets_at > now
        ? `in ${duration(limit.resets_at - now)}` : '';
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

// Only say something when it needs attention; the footer already shows when data was refreshed.
function statusText(account, now) {
    const extra = account.message ? ` ${account.message}` : '';
    if (account.status === 'vpn_off')
        return `Paused: the VPN is off. Showing data from ${age(account.updated_at, now)}.${extra}`;
    if (account.status === 'error')
        return account.message || 'Unable to read account data.';
    if (account.status === 'no_data')
        return account.message || 'No data captured yet.';
    if (account.status === 'stale')
        return `Last updated ${age(account.updated_at, now)}, so these numbers may be out of date.${extra}`;
    return account.message || '';
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

    _renderPanel() {
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
            if (account?.status === 'error') {
                value = '!';
            } else if (account?.status !== 'no_data' && limit) {
                value = `${limit.estimated ? '~' : ''}${Math.round(limit.used_percent)}%`;
                color = severity(limit.used_percent);
                if (limit.used_percent >= 90 && Number.isFinite(limit.resets_at))
                    value += ` · ${duration(limit.resets_at - now, true)}`;
            }
            // offline data may still be fresh when only the network fetch is paused
            if (account?.status === 'vpn_off')
                value = value === '—' ? '⏸' : `${value} ⏸`;
            const dim = account?.status === 'stale' || this._notice.startsWith('Cached data');
            summaries.push({key: provider, text: `${glyph(provider)} ${value}`,
                style: `${color} ${dim ? 'ai-limits-stale' : ''}`});
        }
        if (!summaries.length)
            summaries.push({key: '', text: this._state ? 'AI —' : this._notice.startsWith('Loading') ? 'AI …' : 'AI !', style: ''});
        for (const [key, label] of this._chips) {
            if (!summaries.some(summary => summary.key === key)) {
                label.destroy();
                this._chips.delete(key);
            }
        }
        summaries.forEach((summary, index) => {
            let label = this._chips.get(summary.key);
            if (!label) {
                label = new St.Label({y_align: Clutter.ActorAlign.CENTER});
                this._chips.set(summary.key, label);
                this._panelBox.add_child(label);
            }
            this._panelBox.set_child_at_index(label, index);
            label.text = summary.text;
            label.style_class = `ai-limits-chip ${summary.style}`;
        });
        this._button.accessible_name = `AI Limits: ${summaries.map(item => item.text).join(', ')}`;
    }

    _label(text, style = '') {
        return new St.Label({text, style_class: style, y_align: Clutter.ActorAlign.CENTER});
    }

    _timeLabel(format, style = 'ai-limits-muted') {
        const label = this._label(format(Date.now() / 1000), style);
        this._timeLabels.push({label, format});
        return label;
    }

    _updateMenuTimes() {
        if (!this._button.menu.isOpen)
            return;
        const now = Date.now() / 1000;
        for (const {label, format} of this._timeLabels)
            label.text = format(now);
    }

    _info(text, style = 'ai-limits-muted') {
        const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        const label = this._label(text, style);
        // Bound long command errors; wrap them instead of widening the popup.
        label.clutter_text.line_wrap = true;
        label.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        label.add_style_class_name('ai-limits-notice');
        item.add_child(label);
        this._button.menu.addMenuItem(item);
    }

    _rebuildMenu() {
        this._clearMenu();
        if (this._notice)
            this._info(this._notice);
        const accounts = this._state?.accounts ?? [];
        if (this._state && !accounts.length)
            this._info('No accounts found. Configure accounts in ai-limits.');
        accounts.forEach((account, index) => {
            if (index)
                this._button.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
            const header = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
            header.add_style_class_name('ai-limits-head');
            const details = column({x_expand: true, style_class: 'ai-limits-account'});
            const title = new St.BoxLayout({style_class: 'ai-limits-title'});
            title.add_child(this._label(glyph(account.provider), 'ai-limits-glyph'));
            title.add_child(this._label(account.label, 'ai-limits-account-name'));
            if (account.plan)
                title.add_child(this._label(account.plan, 'ai-limits-plan'));
            if (account.recommended) {
                title.add_child(new St.Label({
                    text: '★ most headroom', style_class: 'ai-limits-pick',
                    x_expand: true, x_align: Clutter.ActorAlign.END, y_align: Clutter.ActorAlign.CENTER,
                }));
            }
            details.add_child(title);
            if (statusText(account, Date.now() / 1000)) {
                const status = this._timeLabel(now => statusText(account, now),
                    `ai-limits-status ai-limits-status-${account.status}`);
                status.add_style_class_name('ai-limits-notice');
                status.clutter_text.line_wrap = true;
                status.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
                details.add_child(status);
            }
            header.add_child(details);
            this._button.menu.addMenuItem(header);
            for (const limit of account.limits)
                this._addLimit(account, limit);
        });
        this._button.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const generated = this._state?.generated_at;
        for (const [title, callback, aside] of [
            ['Refresh', () => this._refresh(), now => Number.isFinite(generated)
                ? `updated ${age(generated, now)}` : 'not updated yet'],
            ['Settings', () => this.openPreferences(), null],
        ]) {
            const item = new PopupMenu.PopupMenuItem(title);
            if (aside) {
                const label = this._timeLabel(aside, 'ai-limits-aside');
                label.x_expand = true;
                label.x_align = Clutter.ActorAlign.END;
                item.add_child(label);
            }
            this._menuSignals.push([item, item.connect('activate', callback)]);
            this._button.menu.addMenuItem(item);
        }
    }

    _addLimit(account, limit) {
        const now = Date.now() / 1000;
        const level = severity(limit.used_percent);
        const row = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        row.add_style_class_name('ai-limits-limit');
        if (account.status === 'stale' || account.status === 'vpn_off')
            row.add_style_class_name('ai-limits-stale');
        row.add_child(this._label(limit.label, 'ai-limits-limit-name'));
        row.add_child(this._meter(limit, now, level));
        row.add_child(this._label(`${limit.estimated ? '~' : ''}${Math.round(limit.used_percent)}%`,
            `ai-limits-percent ${level}`));
        row.add_child(this._timeLabel(at => resetWhen(limit, at), 'ai-limits-when'));
        row.add_child(this._timeLabel(at => resetIn(limit, at), 'ai-limits-countdown'));
        this._button.menu.addMenuItem(row);

        const out = account.status === 'ok' ? runsOutAt(limit, now) : null;
        if (out !== null) {
            const hint = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
            hint.add_style_class_name('ai-limits-forecast-row');
            const text = this._label('', 'ai-limits-forecast');
            // our own text; the time is escaped anyway since it comes from the locale
            text.clutter_text.set_markup(`At this pace it runs out at <b>${
                GLib.markup_escape_text(clock(out, now) ?? '', -1)}</b>, before the reset.`);
            hint.add_child(text);
            this._button.menu.addMenuItem(hint);
        }
    }

    // Usage bar with a tick at how much of the window has passed: fill past the tick = faster than even pace.
    // Built from side-by-side segments (fill, empty, tick) rather than overlapping layers. Sizes are CSS px
    // (TRACK_WIDTH matches the stylesheet) so St applies HiDPI scaling; resizing from an allocation
    // handler would trigger relayout loops.
    _meter(limit, now, level) {
        const meter = new St.BoxLayout({style_class: 'ai-limits-meter', y_align: Clutter.ActorAlign.CENTER});
        const used = Math.round(TRACK_WIDTH * Math.min(1, Math.max(0, limit.used_percent / 100)));
        const passed = elapsed(limit, now);
        const tick = passed === null ? null
            : Math.min(TRACK_WIDTH - TICK_WIDTH, Math.round(TRACK_WIDTH * passed));
        const cuts = [...new Set([0, used, TRACK_WIDTH, ...(tick === null ? [] : [tick, tick + TICK_WIDTH])])]
            .sort((a, b) => a - b);
        const pieces = [];
        for (let i = 1; i < cuts.length; i++) {
            const [from, to] = [cuts[i - 1], cuts[i]];
            if (to > from)
                pieces.push({from, to, kind: from === tick ? 'tick' : to <= used ? 'fill' : 'track'});
        }
        pieces.forEach(({from, to, kind}, index) => {
            const classes = [kind === 'tick' ? 'ai-limits-tick'
                : kind === 'fill' ? `ai-limits-fill ${level}` : 'ai-limits-track'];
            if (kind === 'fill' && tick !== null && from >= tick)
                classes.push('ai-limits-ahead'); // used faster than an even pace
            if (kind !== 'tick') {
                // round only the outer ends of the bar, and the fill's end where it meets the empty track
                const first = index === 0 || pieces[index - 1].kind === 'tick';
                const last = index === pieces.length - 1 || pieces[index + 1].kind === 'tick';
                if (first && from === 0)
                    classes.push('ai-limits-start');
                if ((last && to === TRACK_WIDTH) || (kind === 'fill' && to === used))
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
