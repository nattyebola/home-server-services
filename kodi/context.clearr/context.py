# Entrée de menu contextuel Kodi « Supprimer avec clearr » (cf. addon.xml).
#
# Ne supprime rien lui-même et ne touche PAS à la base vidéo de Kodi : il envoie
# les ids externes du titre sélectionné à l'API de clearr (POST /api/delete/
# {film,series}), qui supprime torrents + fichiers library/ + entrée Sonarr/
# Radarr. C'est ensuite jellyfin-kodi qui retire l'item de la base Kodi, quand
# Jellyfin lui pousse l'événement de suppression — voir README.md de ce dossier
# pour la chaîne complète (mesurée : ~1 min 38 s), pourquoi un retrait local
# (VideoLibrary.RemoveMovie) a été écarté, et pourquoi il n'y a aucun
# rafraîchissement de vue à la fin.
import json
import urllib.error
import urllib.request

import xbmc
import xbmcaddon
import xbmcgui

ADDON = xbmcaddon.Addon()
NAME = ADDON.getAddonInfo("name")

# ListItem.DBTYPE -> (endpoint clearr, méthode JSON-RPC, nom du paramètre d'id,
# clé du résultat). Les autres types sont déjà exclus par le <visible> de
# addon.xml, ce dict fait aussi office de garde-fou côté script.
KINDS = {
    "movie": ("film", "VideoLibrary.GetMovieDetails", "movieid", "moviedetails"),
    "tvshow": ("series", "VideoLibrary.GetTVShowDetails", "tvshowid", "tvshowdetails"),
}

# Une suppression de série enchaîne plusieurs torrents et plusieurs appels arr :
# large, mais borné (sinon un clearr bloqué laisse la boîte de progression
# indéfiniment à l'écran).
TIMEOUT_SECONDS = 180

# Durée d'affichage des notifications Kodi. Volontairement courtes : la
# suppression côté serveur est déjà finie quand elles s'affichent, le message
# n'est qu'un accusé de réception. L'erreur reste plus longue, elle porte une
# information à lire (raison de l'échec).
NOTIFICATION_MS = 2500
NOTIFICATION_ERROR_MS = 5000


def jsonrpc(method, params):
    request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return json.loads(xbmc.executeJSONRPC(json.dumps(request)))


def library_details(dbtype, dbid):
    """Titre + tous les ids externes connus de Kodi pour cet item.

    Passe par JSON-RPC plutôt que par les InfoLabels ListItem.UniqueID(imdb)/
    ListItem.IMDBNumber : ceux-ci ne remontent que l'id désigné par défaut,
    alors que jellyfin-kodi en écrit plusieurs (les ProviderIds de Jellyfin) et
    qu'on veut pouvoir retomber sur tvdb/tmdb quand imdb manque."""
    _endpoint, method, id_param, result_key = KINDS[dbtype]
    response = jsonrpc(method, {id_param: dbid, "properties": ["title", "uniqueid"]})
    details = response.get("result", {}).get(result_key) or {}
    return details.get("title", ""), details.get("uniqueid") or {}


def post_json(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def error_message(exc):
    """clearr répond en JSON même sur 404/500/503 (message lisible dans le champ
    `message`, voir webapp.py) — on l'affiche tel quel plutôt qu'un code HTTP nu.
    Repli sur le code pour tout le reste (422 de FastAPI, page d'erreur Traefik
    si la requête n'atteint jamais clearr...)."""
    try:
        body = json.loads(exc.read().decode("utf-8"))
        if isinstance(body, dict) and body.get("message"):
            return body["message"]
    except Exception:
        pass
    return "clearr a répondu HTTP {}.".format(exc.code)


def notify(message, failed=False):
    xbmcgui.Dialog().notification(
        NAME, message,
        xbmcgui.NOTIFICATION_ERROR if failed else xbmcgui.NOTIFICATION_INFO,
        NOTIFICATION_ERROR_MS if failed else NOTIFICATION_MS,
    )


def main():
    base_url = ADDON.getSetting("clearr_url").strip().rstrip("/")
    if not base_url:
        xbmcgui.Dialog().ok(NAME, "L'URL de clearr n'est pas renseignée.\n\n"
                                  "Indiquez-la dans les paramètres de l'extension "
                                  "(par exemple https://clearr.exemple.org).")
        ADDON.openSettings()
        return

    dbtype = xbmc.getInfoLabel("ListItem.DBTYPE")
    dbid = xbmc.getInfoLabel("ListItem.DBID")
    if dbtype not in KINDS or not dbid:
        notify("Cet élément n'est pas un film ou une série de la bibliothèque.", failed=True)
        return

    title, unique_ids = library_details(dbtype, int(dbid))
    label = title or xbmc.getInfoLabel("ListItem.Label") or "?"
    # Mêmes clés que le corps attendu par clearr (ExternalIds dans webapp.py) —
    # ce sont aussi les noms utilisés par Kodi dans son champ uniqueid.
    payload = {key: str(unique_ids.get(key, "")) for key in ("imdb", "tmdb", "tvdb")}
    if not any(payload.values()):
        xbmcgui.Dialog().ok(NAME, "Aucun identifiant IMDb, TMDB ou TVDB pour « {} » : "
                                  "clearr ne peut pas retrouver ce titre côté Sonarr/Radarr.".format(label))
        return

    what = "ce film" if dbtype == "movie" else "cette série et toutes ses saisons"
    if not xbmcgui.Dialog().yesno(
            NAME,
            "Supprimer {} ?\n\n[B]{}[/B]\n\n"
            "Torrents, fichiers de la bibliothèque et entrée Sonarr/Radarr "
            "seront supprimés.".format(what, label),
            nolabel="Annuler", yeslabel="Supprimer"):
        return

    endpoint = KINDS[dbtype][0]
    progress = xbmcgui.DialogProgressBG()
    progress.create(NAME, "Suppression de {}…".format(label))
    try:
        result = post_json("{}/api/delete/{}".format(base_url, endpoint), payload)
        message, failed = result.get("message", "Supprimé."), False
    except urllib.error.HTTPError as exc:
        message, failed = error_message(exc), True
    except Exception as exc:
        # URLError (DNS, refus de connexion, TLS), timeout, réponse non-JSON.
        message, failed = "clearr injoignable : {}".format(exc), True
    finally:
        progress.close()

    notify(message, failed=failed)
    # Pas de Container.Refresh : la ligne ne peut de toute façon pas disparaître
    # dans la foulée. Mesuré le 2026-08-05 sur une vraie suppression de série,
    # 1 min 38 s entre la réponse de clearr et le retrait effectif de la base
    # Kodi — dont 65 s de LibraryMonitorDelay côté Jellyfin (60 s, appliqué AUSSI
    # aux mises à jour signalées par Sonarr/Radarr, vérifié en appelant
    # Library/Media/Updated à la main) puis ~30 s de KodiSyncQueue et de
    # jellyfin-kodi. Un rafraîchissement immédiat rerendait donc la même liste ;
    # la ligne part d'elle-même quand jellyfin-kodi retire l'item.


main()
