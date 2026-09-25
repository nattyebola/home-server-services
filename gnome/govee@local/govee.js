// Client de l'API cloud Govee v2 (https://developer.govee.com) — on/off seulement.
// La clé API est lue dans le trousseau GNOME, jamais écrite sur disque en clair.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Secret from 'gi://Secret';
import Soup from 'gi://Soup?version=3.0';

Gio._promisify(Soup.Session.prototype, 'send_and_read_async');
Gio._promisify(Secret, 'password_lookup', 'password_lookup_finish');
Gio._promisify(Secret, 'password_store', 'password_store_finish');

const API_ROOT = 'https://openapi.api.govee.com/router/api/v1';
const ON_OFF = 'devices.capabilities.on_off';

// DONT_MATCH_NAME : retrouver la clé quel que soit l'outil qui l'a enregistrée.
const KEY_SCHEMA = Secret.Schema.new('org.gnome.shell.extensions.govee',
    Secret.SchemaFlags.DONT_MATCH_NAME, {service: Secret.SchemaAttributeType.STRING});
const KEY_ATTRS = {service: 'govee-api'};

export async function storeApiKey(key) {
    await Secret.password_store(KEY_SCHEMA, KEY_ATTRS, Secret.COLLECTION_DEFAULT,
        'Clé API Govee', key, null);
}

export class GoveeError extends Error {}

export class GoveeCloud {
    constructor() {
        this._session = new Soup.Session({timeout: 10});
        this._cancellable = new Gio.Cancellable();
        this._key = null;
    }

    async _apiKey() {
        this._key ??= await Secret.password_lookup(KEY_SCHEMA, KEY_ATTRS, this._cancellable);
        if (!this._key)
            throw new GoveeError('Clé API absente du trousseau');
        return this._key;
    }

    async _request(method, path, body) {
        const msg = Soup.Message.new(method, `${API_ROOT}${path}`);
        msg.request_headers.append('Govee-API-Key', await this._apiKey());
        if (body) {
            msg.set_request_body_from_bytes('application/json',
                new GLib.Bytes(new TextEncoder().encode(JSON.stringify(body))));
        }

        const started = GLib.get_monotonic_time();
        const bytes = await this._session.send_and_read_async(msg, GLib.PRIORITY_DEFAULT, this._cancellable);
        // status_code et pas get_status() : l'enum Soup.Status de GJS ne connaît
        // pas 429 et get_status() lève une exception au lieu de le renvoyer.
        const status = msg.status_code;
        const ms = Math.round((GLib.get_monotonic_time() - started) / 1000);
        const target = body?.payload?.sku ? ` ${body.payload.sku}` : '';
        const value = body?.payload?.capability ? ` value=${body.payload.capability.value}` : '';
        console.log(`govee: ${method} ${path}${target}${value} -> HTTP ${status} (${ms} ms)`);

        if (status === 401 || status === 403) {
            this._key = null; // relire le trousseau au prochain essai, la clé a pu changer
            throw new GoveeError('Clé API refusée');
        }
        if (status === 429)
            throw new GoveeError('Limite de requêtes Govee atteinte');

        let json;
        try {
            json = JSON.parse(new TextDecoder().decode(bytes.toArray()));
        } catch {
            throw new GoveeError(`Réponse illisible (HTTP ${status})`);
        }
        if (status !== 200 || json.code !== 200)
            throw new GoveeError(`Erreur Govee : ${json.message ?? json.msg ?? `HTTP ${status}`}`);
        return json;
    }

    // Appareils du compte qui savent s'allumer/s'éteindre : [{sku, device, name}]
    async devices() {
        const {data} = await this._request('GET', '/user/devices');
        return data
            // Les groupes de l'app (BaseGroup) acceptent un on/off mais n'ont pas
            // d'état lisible (/device/state répond « devices not exist »).
            .filter(d => d.sku !== 'BaseGroup' && d.capabilities?.some(c => c.type === ON_OFF))
            .map(d => ({sku: d.sku, device: d.device, name: d.deviceName || d.sku}));
    }

    // {online, on} ; online vaut true si l'API ne le précise pas.
    async state({sku, device}) {
        const {payload} = await this._request('POST', '/device/state',
            {requestId: GLib.uuid_string_random(), payload: {sku, device}});
        const caps = payload?.capabilities ?? [];
        const value = instance => caps.find(c => c.instance === instance)?.state?.value;
        return {online: value('online') !== false, on: value('powerSwitch') === 1};
    }

    async turn({sku, device}, on) {
        await this._request('POST', '/device/control', {
            requestId: GLib.uuid_string_random(),
            payload: {sku, device, capability: {type: ON_OFF, instance: 'powerSwitch', value: on ? 1 : 0}},
        });
    }

    destroy() {
        this._cancellable.cancel();
        this._session.abort();
    }
}

export function isCancelled(e) {
    return e instanceof GLib.Error && e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED);
}
