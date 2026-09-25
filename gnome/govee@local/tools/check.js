// Test hors GNOME Shell : liste les appareils du compte et leur état.
import GLib from 'gi://GLib';
import {GoveeCloud} from '../govee.js';

const api = new GoveeCloud();
const loop = GLib.MainLoop.new(null, false);
(async () => {
    for (const d of await api.devices())
        print(d.name, d.sku, JSON.stringify(await api.state(d)));
})().catch(e => printerr(`Erreur : ${e.message}`)).finally(() => loop.quit());
loop.run();
