import Gio from 'gi://Gio';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {GoveeCloud, GoveeError, isCancelled} from './govee.js';

// Appelée par le raccourci clavier (Paramètres → Clavier → raccourci perso) :
// gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell/Extensions/Govee --method org.gnome.Shell.Extensions.Govee.ToggleAll
const DBUS_PATH = '/org/gnome/Shell/Extensions/Govee';
const DBUS_IFACE = `<node>
  <interface name="org.gnome.Shell.Extensions.Govee">
    <method name="ToggleAll"/>
  </interface>
</node>`;

// PopupSwitchMenuItem ferme le menu à chaque bascule (sauf à la touche Espace) :
// on bascule sans remonter jusqu'à PopupBaseMenuItem.activate(), qui émet le
// signal `activate` sur lequel le menu se referme. Échap le ferme toujours.
const StickySwitchMenuItem = GObject.registerClass(
class StickySwitchMenuItem extends PopupMenu.PopupSwitchMenuItem {
    activate(_event) {
        if (this._switch.mapped)
            this.toggle();
    }
});

const GoveeIndicator = GObject.registerClass(
class GoveeIndicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.0, 'Govee');

        this._iconOff = Gio.icon_new_for_string(`${extension.path}/icons/lightbulb-symbolic.svg`);
        this._iconOn = Gio.icon_new_for_string(`${extension.path}/icons/lightbulb-on-symbolic.svg`);
        this._icon = new St.Icon({gicon: this._iconOff, style_class: 'system-status-icon'});
        this.add_child(this._icon);

        this._api = new GoveeCloud();
        this._devices = []; // [{sku, device, name, on, online, item, clicks}]
        this._error = null;
        this._loaded = false;
        this._syncing = false;
        this._refreshing = null; // promesse du rafraîchissement en cours
        this._switchingAll = false;
        this._shortcutBusy = false;

        // Interrupteur global, hors de _section : _rebuild() ne le recrée pas.
        this._allItem = new StickySwitchMenuItem('Tout', false);
        this._allItem.connect('toggled', (_item, state) => {
            if (!this._syncing)
                this._switchAll(state);
        });
        this.menu.addMenuItem(this._allItem);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._section = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._section);
        this._rebuild();

        this.menu.connect('open-state-changed', (_menu, open) => {
            if (open)
                this._refresh();
        });
        this._refresh();
    }

    // Un appel pendant un rafraîchissement attend celui-ci au lieu d'en lancer
    // un second : le raccourci a besoin de l'état à jour avant de basculer.
    _refresh() {
        this._refreshing ??= this._doRefresh().finally(() => {
            this._refreshing = null;
        });
        return this._refreshing;
    }

    // Le menu garde la dernière liste connue pendant la requête, pour ne pas
    // s'ouvrir vide ; il n'est reconstruit que si la liste ou l'erreur change.
    async _doRefresh() {
        try {
            const list = await this._api.devices();
            const ids = devs => devs.map(d => `${d.device}|${d.name}`).sort().join();
            if (!this._loaded || this._error || ids(list) !== ids(this._devices)) {
                this._devices = list.map(d => ({...d, on: false, online: true, item: null, clicks: 0}))
                    .sort((a, b) => a.name.localeCompare(b.name));
                this._error = null;
                this._loaded = true;
                this._rebuild();
            }
            await Promise.all(this._devices.map(async d => {
                // Un clic pendant la requête rend sa réponse périmée : l'état a
                // été lu AVANT la commande, l'appliquer remettrait l'interrupteur
                // (et l'ampoule) sur l'ancienne position.
                const clicks = d.clicks;
                try {
                    const state = await this._api.state(d);
                    if (d.clicks !== clicks)
                        return;
                    Object.assign(d, state);
                } catch (e) {
                    if (isCancelled(e))
                        throw e;
                    if (d.clicks !== clicks)
                        return;
                    d.online = false;
                    logError(e, `govee: état de ${d.name}`);
                }
                this._sync(d);
            }));
        } catch (e) {
            if (isCancelled(e))
                return;
            if (!(e instanceof GoveeError))
                logError(e, 'govee: rafraîchissement');
            this._error = e.message;
            this._rebuild();
        }
    }

    _rebuild() {
        this._section.removeAll();

        if (this._error) {
            this._addInfo(this._error);
            if (this._error.startsWith('Clé API'))
                this._addInfo('Lancer set-api-key.sh dans le dossier de l\'extension');
        } else if (this._devices.length === 0) {
            this._addInfo(this._loaded ? 'Aucun appareil sur le compte' : 'Chargement…');
        }

        for (const d of this._devices) {
            d.item = new StickySwitchMenuItem(d.name, d.on);
            d.item.connect('toggled', (_item, state) => {
                // setToggleState() émet aussi `toggled` (le signal suit
                // notify::state du Switch) : sans ce garde, chaque resynchro
                // depuis l'API renverrait une commande à l'appareil.
                if (!this._syncing)
                    this._toggle(d, state);
            });
            this._section.addMenuItem(d.item);
            this._sync(d);
        }
        this._updateSummary();
    }

    // Majorité des appareils joignables ; une égalité compte comme allumé.
    _majorityOn() {
        const online = this._devices.filter(d => d.online);
        return online.length > 0 && 2 * online.filter(d => d.on).length >= online.length;
    }

    // Ampoule pleine dès qu'au moins un appareil joignable est allumé ;
    // interrupteur global sur l'état majoritaire.
    _updateSummary() {
        const anyOn = this._devices.some(d => d.online && d.on);
        this._icon.gicon = anyOn ? this._iconOn : this._iconOff;
        this._allItem.setSensitive(this._devices.some(d => d.online));
        // Pendant une bascule globale, chaque réponse ferait osciller
        // l'interrupteur global au gré de la majorité intermédiaire.
        if (!this._switchingAll)
            this._setSwitch(this._allItem, this._majorityOn());
    }

    // setToggleState() émet `toggled` : le garde évite de renvoyer une commande.
    _setSwitch(item, on) {
        this._syncing = true;
        try {
            item.setToggleState(on);
        } finally {
            this._syncing = false;
        }
    }

    _addInfo(text) {
        this._section.addMenuItem(new PopupMenu.PopupMenuItem(text, {reactive: false}));
    }

    _sync(d) {
        // Une réponse peut arriver pour un appareil d'une liste déjà remplacée,
        // dont l'item a été détruit par _rebuild().
        if (!d.item || !this._devices.includes(d))
            return;
        this._setSwitch(d.item, d.on);
        d.item.setSensitive(d.online);
        this._updateSummary();
        d.item.label.text = d.online ? d.name : `${d.name} (hors ligne)`;
    }

    // Renvoie le message d'erreur, null si la commande est passée.
    async _toggle(d, on, {notify = true} = {}) {
        console.log(`govee: clic ${d.name} (${d.sku}) -> ${on ? 'on' : 'off'}`);
        d.clicks++;
        let error = null;
        try {
            await this._api.turn(d, on);
            d.on = on;
        } catch (e) {
            if (isCancelled(e))
                return null;
            logError(e, `govee: bascule de ${d.name}`);
            error = e.message;
            if (notify)
                Main.notifyError(`Govee : ${d.name}`, error);
        }
        // En cas d'échec, remet l'interrupteur sur l'état réel.
        this._sync(d);
        return error;
    }

    // Commande envoyée à tous les appareils joignables, même ceux déjà dans
    // l'état voulu : hors menu ouvert, l'état connu peut dater. Une seule
    // notification pour l'ensemble des échecs.
    async _switchAll(on) {
        if (this._switchingAll)
            return;
        this._switchingAll = true;
        const targets = this._devices.filter(d => d.online);
        console.log(`govee: clic tout -> ${on ? 'on' : 'off'} (${targets.length} appareils)`);
        let failed;
        try {
            for (const d of targets)
                this._setSwitch(d.item, on);
            const errors = await Promise.all(targets.map(d => this._toggle(d, on, {notify: false})));
            failed = targets.map((d, i) => errors[i] && `${d.name} : ${errors[i]}`).filter(Boolean);
        } finally {
            this._switchingAll = false;
        }
        this._updateSummary();
        if (failed.length > 0)
            Main.notifyError('Govee', failed.join('\n'));
    }

    // Raccourci clavier : relit l'état (le menu est fermé, il peut dater),
    // puis bascule vers l'inverse de la majorité. Un appui pendant qu'une
    // bascule est en cours est ignoré.
    async toggleAllFromShortcut() {
        if (this._shortcutBusy)
            return;
        this._shortcutBusy = true;
        try {
            await this._refresh();
            if (!this._devices.some(d => d.online)) {
                this._osd(this._iconOff, this._error ?? 'Aucun appareil joignable');
                return;
            }
            const on = !this._majorityOn();
            this._osd(on ? this._iconOn : this._iconOff, on ? 'Tout allumer' : 'Tout éteindre');
            await this._switchAll(on);
        } catch (e) {
            logError(e, 'govee: raccourci');
        } finally {
            this._shortcutBusy = false;
        }
    }

    // Retour visuel du raccourci, menu fermé (même bulle que le volume).
    // API interne de GNOME Shell : un échec ne doit pas empêcher la bascule.
    _osd(icon, label) {
        try {
            Main.osdWindowManager.showAll(icon, label);
        } catch (e) {
            logError(e, 'govee: bulle OSD');
        }
    }

    destroy() {
        this._api.destroy();
        super.destroy();
    }
});

export default class GoveeExtension extends Extension {
    enable() {
        this._indicator = new GoveeIndicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);

        // Pas de valeur de retour : gdbus rend la main tout de suite, la
        // bascule se poursuit en arrière-plan.
        this._dbus = Gio.DBusExportedObject.wrapJSObject(DBUS_IFACE, {
            ToggleAll: () => {
                this._indicator.toggleAllFromShortcut();
            },
        });
        this._dbus.export(Gio.DBus.session, DBUS_PATH);
    }

    disable() {
        this._dbus?.unexport();
        this._dbus = null;
        this._indicator?.destroy();
        this._indicator = null;
    }
}
