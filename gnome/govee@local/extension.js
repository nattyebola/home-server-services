import Gio from 'gi://Gio';
import GObject from 'gi://GObject';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {GoveeCloud, GoveeError, isCancelled} from './govee.js';

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

        this.add_child(new St.Icon({
            gicon: Gio.icon_new_for_string(`${extension.path}/icons/lightbulb-symbolic.svg`),
            style_class: 'system-status-icon',
        }));

        this._api = new GoveeCloud();
        this._devices = []; // [{sku, device, name, on, online, item}]
        this._error = null;
        this._loaded = false;
        this._refreshing = false;
        this._syncing = false;

        this._section = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._section);
        this._rebuild();

        this.menu.connect('open-state-changed', (_menu, open) => {
            if (open)
                this._refresh();
        });
        this._refresh();
    }

    // Le menu garde la dernière liste connue pendant la requête, pour ne pas
    // s'ouvrir vide ; il n'est reconstruit que si la liste ou l'erreur change.
    async _refresh() {
        if (this._refreshing)
            return;
        this._refreshing = true;
        try {
            const list = await this._api.devices();
            const ids = devs => devs.map(d => `${d.device}|${d.name}`).sort().join();
            if (!this._loaded || this._error || ids(list) !== ids(this._devices)) {
                this._devices = list.map(d => ({...d, on: false, online: true, item: null}))
                    .sort((a, b) => a.name.localeCompare(b.name));
                this._error = null;
                this._loaded = true;
                this._rebuild();
            }
            await Promise.all(this._devices.map(async d => {
                try {
                    Object.assign(d, await this._api.state(d));
                } catch (e) {
                    if (isCancelled(e))
                        throw e;
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
        } finally {
            this._refreshing = false;
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
    }

    _addInfo(text) {
        this._section.addMenuItem(new PopupMenu.PopupMenuItem(text, {reactive: false}));
    }

    _sync(d) {
        // Une réponse peut arriver pour un appareil d'une liste déjà remplacée,
        // dont l'item a été détruit par _rebuild().
        if (!d.item || !this._devices.includes(d))
            return;
        this._syncing = true;
        try {
            d.item.setToggleState(d.on);
        } finally {
            this._syncing = false;
        }
        d.item.setSensitive(d.online);
        d.item.label.text = d.online ? d.name : `${d.name} (hors ligne)`;
    }

    async _toggle(d, on) {
        console.log(`govee: clic ${d.name} (${d.sku}) -> ${on ? 'on' : 'off'}`);
        try {
            await this._api.turn(d, on);
            d.on = on;
        } catch (e) {
            if (isCancelled(e))
                return;
            logError(e, `govee: bascule de ${d.name}`);
            Main.notifyError(`Govee : ${d.name}`, e.message);
        }
        // En cas d'échec, remet l'interrupteur sur l'état réel.
        this._sync(d);
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
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
