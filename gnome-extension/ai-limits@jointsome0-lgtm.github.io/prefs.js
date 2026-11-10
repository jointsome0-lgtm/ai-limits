import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';

import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

export default class AILimitsPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();
        const page = new Adw.PreferencesPage({title: 'AI Limits', icon_name: 'view-statistics-symbolic'});
        const group = new Adw.PreferencesGroup({title: 'Top bar'});
        page.add(group);
        window.add(page);

        const refresh = new Adw.SpinRow({
            title: 'Refresh interval',
            subtitle: 'Seconds between core reads (minimum 15)',
            adjustment: new Gtk.Adjustment({lower: 15, upper: 86400, step_increment: 15, page_increment: 60}),
            digits: 0,
            numeric: true,
        });
        settings.bind('refresh-interval', refresh, 'value', Gio.SettingsBindFlags.DEFAULT);
        group.add(refresh);

        const positions = ['left', 'center', 'right'];
        const position = new Adw.ComboRow({
            title: 'Panel position',
            model: Gtk.StringList.new(['Left', 'Center', 'Right']),
            selected: positions.indexOf(settings.get_string('panel-position')),
        });
        group.add(position);
        const positionSignal = position.connect('notify::selected', () => {
            const value = positions[position.selected];
            if (value !== undefined && settings.get_string('panel-position') !== value)
                settings.set_string('panel-position', value);
        });
        const settingsSignal = settings.connect('changed::panel-position', () => {
            position.selected = positions.indexOf(settings.get_string('panel-position'));
        });

        const index = new Adw.SpinRow({
            title: 'Panel index',
            subtitle: 'Position within the selected section; 0 is first',
            adjustment: new Gtk.Adjustment({lower: 0, upper: 100, step_increment: 1, page_increment: 5}),
            digits: 0,
            numeric: true,
        });
        settings.bind('panel-index', index, 'value', Gio.SettingsBindFlags.DEFAULT);
        group.add(index);

        const source = new Adw.PreferencesGroup({
            title: 'Local data source',
            description: 'ai-limits never sends network requests unless you enable network mode in ~/.config/ai-limits/config.toml',
        });
        page.add(source);
        const command = new Adw.EntryRow({
            title: 'Command',
            text: settings.get_string('command'),
            show_apply_button: true,
        });
        source.add(command);
        source.add(new Adw.ActionRow({
            title: 'Default: ai-limits json',
            subtitle: 'Apply to save. Arguments support quotes, not shell expansion or pipelines. For a fixture: cat /absolute/path/state-sample.json',
        }));
        // Applying explicitly avoids launching partial commands while typing.
        const commandSignal = command.connect('apply', () => {
            settings.set_string('command', command.text.trim());
        });
        const commandSettingsSignal = settings.connect('changed::command', () => {
            command.text = settings.get_string('command');
        });

        const closeSignal = window.connect('close-request', () => {
            settings.disconnect(settingsSignal);
            settings.disconnect(commandSettingsSignal);
            position.disconnect(positionSignal);
            command.disconnect(commandSignal);
            Gio.Settings.unbind(refresh, 'value');
            Gio.Settings.unbind(index, 'value');
            window.disconnect(closeSignal);
            return false;
        });
    }
}
