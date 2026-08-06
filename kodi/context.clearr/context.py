# Entrée de menu contextuel Kodi « Supprimer avec clearr » (cf. addon.xml).
#
# Ne supprime rien lui-même et ne touche PAS à la base vidéo de Kodi : il envoie
# les ids externes ET le chemin du titre sélectionné à l'API de clearr (POST
# /api/preview/{film,series} pour annoncer ce qui va partir, puis /api/delete/…),
# qui supprime torrents + fichiers + entrée Sonarr/Radarr le cas échéant. Le
# chemin est ce qui permet de traiter aussi les titres récupérés à la main,
# absents de Sonarr/Radarr : Jellyfin sert completed/ comme bibliothèque, Kodi
# les affiche donc au même titre que le reste, mais aucun id externe ne les
# retrouve côté arr. C'est ensuite jellyfin-kodi qui retire l'item de Kodi, quand
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
    """Titre + ids externes + chemin connus de Kodi pour cet item.

    Passe par JSON-RPC plutôt que par les InfoLabels ListItem.UniqueID(imdb)/
    ListItem.IMDBNumber : ceux-ci ne remontent que l'id désigné par défaut,
    alors que jellyfin-kodi en écrit plusieurs (les ProviderIds de Jellyfin) et
    qu'on veut pouvoir retomber sur tvdb/tmdb quand imdb manque.

    `file` est le fichier du film / le dossier de la série. Il n'est exploitable
    que parce que jellyfin-kodi est en chemins directs (useDirectPaths) : en
    mode addon, Kodi ne connaîtrait qu'une URL plugin://, que clearr ne saurait
    pas rattacher à un fichier. Le préfixe, lui, peut différer de celui du
    serveur — clearr résout par suffixe, voir core.resolve_media_path."""
    _endpoint, method, id_param, result_key = KINDS[dbtype]
    response = jsonrpc(method, {id_param: dbid, "properties": ["title", "uniqueid", "file"]})
    details = response.get("result", {}).get(result_key) or {}
    return details.get("title", ""), details.get("uniqueid") or {}, details.get("file", "")


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


def fetch_preview(base_url, endpoint, payload):
    """(prévisualisation, None) ou (None, message d'erreur). Rendu court
    (quelques centaines de ms côté clearr, qui recalcule tout son état) mais pas
    instantané : une boîte de progression évite l'impression d'un clic sans
    effet sur le menu contextuel."""
    progress = xbmcgui.DialogProgressBG()
    progress.create(NAME, "Analyse…")
    try:
        return post_json("{}/api/preview/{}".format(base_url, endpoint), payload), None
    except urllib.error.HTTPError as exc:
        return None, error_message(exc)
    except Exception as exc:
        return None, "clearr injoignable : {}".format(exc)
    finally:
        progress.close()


# Nombre de fichiers sans torrent nommés dans la boîte de confirmation. La
# boîte yesno de Kodi ne défile pas : au-delà, le texte déborderait sous les
# boutons et le résumé lui-même deviendrait illisible.
MAX_ORPHANS_LISTED = 5


def orphan_lines(preview):
    """Fichiers que clearr supprimera sans qu'aucun torrent ne les couvre —
    typiquement des épisodes dont Sonarr a retiré le torrent du client une fois
    son ratio atteint. Le résumé n'en donne que le compte ; les nommer ici évite
    une taille annoncée qu'on ne saurait rattacher à rien. Rien n'est affiché
    quand il n'y en a pas, le cas courant."""
    orphans = preview.get("orphans") or []
    if not orphans:
        return ""
    shown = orphans[:MAX_ORPHANS_LISTED]
    lines = ["", "Sans torrent (supprimés aussi) :"]
    lines += ["  {} ({})".format(f.get("name", "?"), f.get("size", "?")) for f in shown]
    if len(orphans) > len(shown):
        lines.append("  … et {} de plus".format(len(orphans) - len(shown)))
    return "\n".join(lines)


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

    title, unique_ids, path = library_details(dbtype, int(dbid))
    label = title or xbmc.getInfoLabel("ListItem.Label") or "?"
    # Mêmes clés que le corps attendu par clearr (DeleteTarget dans webapp.py) —
    # ce sont aussi les noms utilisés par Kodi dans son champ uniqueid.
    payload = {key: str(unique_ids.get(key, "")) for key in ("imdb", "tmdb", "tvdb")}
    payload["path"] = path
    if not any(payload.values()):
        xbmcgui.Dialog().ok(NAME, "Ni identifiant IMDb/TMDB/TVDB ni chemin de fichier pour « {} » : "
                                  "clearr ne peut pas retrouver ce titre.".format(label))
        return

    endpoint = KINDS[dbtype][0]
    # Prévisualisation avant de demander confirmation : la boîte de dialogue
    # annonce ce qui va réellement partir (nombre de torrents, taille) plutôt
    # que le seul titre. Elle sert aussi de garde — un titre que clearr ne sait
    # pas résoudre est signalé ICI, avant toute confirmation, plutôt qu'après un
    # « Supprimer » qui n'aurait rien supprimé.
    preview, error = fetch_preview(base_url, endpoint, payload)
    if error:
        xbmcgui.Dialog().ok(NAME, "{}\n\n[B]{}[/B]".format(error, label))
        return

    what = "ce film" if dbtype == "movie" else "cette série et toutes ses saisons"
    scope = ("Torrents, fichiers de la bibliothèque et entrée Sonarr/Radarr seront supprimés."
             if preview.get("managed_by_arr")
             else "Titre absent de Sonarr/Radarr : torrents et fichiers seront supprimés.")
    if not xbmcgui.Dialog().yesno(
            NAME,
            "Supprimer {} ?\n\n[B]{}[/B]\n{}{}\n\n{}".format(
                what, label, preview.get("summary", ""), orphan_lines(preview), scope),
            nolabel="Annuler", yeslabel="Supprimer"):
        return

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
