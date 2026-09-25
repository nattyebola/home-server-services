// Lit la clé sur stdin et l'enregistre dans le trousseau GNOME.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GioUnix from 'gi://GioUnix';
import {storeApiKey} from '../govee.js';

const stdin = new Gio.DataInputStream({base_stream: new GioUnix.InputStream({fd: 0})});
const [line] = stdin.read_line_utf8(null);
const key = line?.trim();
if (!key) {
    printerr('Clé vide, rien enregistré.');
    imports.system.exit(1);
}
const loop = GLib.MainLoop.new(null, false);
storeApiKey(key)
    .then(() => print('Clé enregistrée dans le trousseau.'))
    .catch(e => { printerr(`Échec : ${e.message}`); imports.system.exit(1); })
    .finally(() => loop.quit());
loop.run();
