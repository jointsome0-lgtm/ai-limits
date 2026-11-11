import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const TRACK_WIDTH = 110; // px, matches .ai-limits-track in stylesheet.css

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

function resetText(timestamp, now) {
    if (!Number.isFinite(timestamp))
        return 'Reset unknown';
    if (timestamp <= now)
        return '↻ reset due';
    const reset = GLib.DateTime.new_from_unix_local(Math.floor(timestamp));
    const today = GLib.DateTime.new_from_unix_local(Math.floor(now));
    if (!reset || !today)
        return 'Reset unknown';
    const format = reset.format('%F') === today.format('%F') ? '%H:%M' : '%a %H:%M';
    return `↻ ${reset.format(format)} (in ${duration(timestamp - now)})`;
}

function statusText(account, now) {
    const updated = `updated ${age(account.updated_at, now)}`;
    if (account.status === 'vpn_off')
        return `VPN off — paused · ${updated}${account.message ? ` · ${account.message}` : ''}`;
    if (account.status === 'error')
        return account.message || 'Unable to read account data';
    if (account.status === 'no_data')
        return account.message || 'No data captured yet';
    return `${updated}${account.message ? ` · ${account.message}` : ''}`;
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
            const details = column({x_expand: true, style_class: 'ai-limits-account'});
            const title = new St.BoxLayout({style_class: 'ai-limits-title'});
            title.add_child(this._label(`${glyph(account.provider)} ${account.label}`, 'ai-limits-account-name'));
            if (account.plan)
                title.add_child(this._label(account.plan, 'ai-limits-muted'));
            details.add_child(title);
            const status = this._timeLabel(now => statusText(account, now));
            status.add_style_class_name('ai-limits-notice');
            status.clutter_text.line_wrap = true;
            status.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
            details.add_child(status);
            if (account.recommended)
                details.add_child(this._label('★ most headroom', 'ai-limits-muted'));
            header.add_child(details);
            this._button.menu.addMenuItem(header);
            for (const limit of account.limits)
                this._addLimit(account, limit);
        });
        this._button.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const footer = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        const generated = this._state?.generated_at;
        footer.add_child(this._timeLabel(now => Number.isFinite(generated)
            ? `Updated ${age(generated, now)}` : 'Not updated yet'));
        this._button.menu.addMenuItem(footer);
        for (const [title, callback] of [
            ['Refresh', () => this._refresh()],
            ['Settings', () => this.openPreferences()],
        ]) {
            const item = new PopupMenu.PopupMenuItem(title);
            this._menuSignals.push([item, item.connect('activate', callback)]);
            this._button.menu.addMenuItem(item);
        }
    }

    _addLimit(account, limit) {
        const row = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        row.add_style_class_name('ai-limits-limit');
        if (account.status === 'stale' || account.status === 'vpn_off')
            row.add_style_class_name('ai-limits-stale');
        const name = this._label(limit.label, 'ai-limits-limit-name');
        row.add_child(name);
        // A box (not St.Bin, which centres its child) so the fill starts at the left edge.
        // Width in CSS px (same TRACK_WIDTH as the stylesheet) so St applies HiDPI scaling;
        // resizing from an allocation handler would trigger relayout loops.
        const bar = new St.BoxLayout({style_class: 'ai-limits-track', y_align: Clutter.ActorAlign.CENTER});
        const fraction = Math.min(1, Math.max(0, limit.used_percent / 100));
        bar.add_child(new St.Widget({
            style_class: `ai-limits-fill ${severity(limit.used_percent)}`,
            style: `width: ${Math.round(TRACK_WIDTH * fraction)}px;`,
        }));
        row.add_child(bar);
        row.add_child(this._label(`${limit.estimated ? '~' : ''}${Math.round(limit.used_percent)}%`,
            `ai-limits-percent ${severity(limit.used_percent)}`));
        row.add_child(this._timeLabel(now => resetText(limit.resets_at, now)));
        this._button.menu.addMenuItem(row);
    }
}
