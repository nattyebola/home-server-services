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
#
# "season" passe par le même endpoint /series que "tvshow" : clearr résout
# toujours la SÉRIE (par ses ids externes), la saison n'étant qu'un filtre
# envoyé à côté. Kodi n'expose d'ailleurs aucun uniqueid exploitable sur une
# saison — il faut de toute façon remonter au tvshow parent, d'où les deux
# appels JSON-RPC de season_target().
KINDS = {
    "movie": ("film", "VideoLibrary.GetMovieDetails", "movieid", "moviedetails"),
    "tvshow": ("series", "VideoLibrary.GetTVShowDetails", "tvshowid", "tvshowdetails"),
    "season": ("series", "VideoLibrary.GetSeasonDetails", "seasonid", "seasondetails"),
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


def season_target(dbid):
    """(numéro de saison, dbid du tvshow parent) pour un clic sur une saison.

    Deux appels : Kodi ne met pas les uniqueid de la série sur ses saisons, et
    c'est la série que clearr doit résoudre. Le numéro de saison vient de Kodi
    (donc de Jellyfin, donc du nom de dossier écrit par Sonarr) ; clearr le
    revalide contre Sonarr et refuse une saison qu'il ne connaît pas, plutôt que
    d'en deviner une."""
    details = jsonrpc("VideoLibrary.GetSeasonDetails",
                      {"seasonid": dbid, "properties": ["season", "tvshowid"]})
    details = details.get("result", {}).get("seasondetails") or {}
    if "season" not in details or "tvshowid" not in details:
        return None, None
    return details["season"], details["tvshowid"]


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
    # Une saison a déjà été convertie en (numéro, tvshowid) par son appelant :
    # on lit ici la SÉRIE, seule porteuse des uniqueid dont clearr a besoin.
    lookup = "tvshow" if dbtype == "season" else dbtype
    _endpoint, method, id_param, result_key = KINDS[lookup]
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


def season_label(season):
    """Une ligne de la liste de sélection. Volontairement courte : la boîte
    multiselect de Kodi est étroite, un libellé long est tronqué au milieu."""
    parts = ["{} ép.".format(season.get("episodes", 0)), season.get("size", "?")]
    torrents = season.get("torrents", 0)
    if torrents:
        parts.append("{} torrent{}".format(torrents, "s" if torrents > 1 else ""))
    else:
        parts.append("sans torrent")
    return "S{:02d} — {}".format(season.get("number", 0), ", ".join(parts))


def choose_seasons(preview):
    """Saisons à supprimer, ou None si l'utilisateur annule.

    La liste vient de clearr (donc de Sonarr), jamais de la base locale de Kodi :
    celle-ci reflète Jellyfin, qui peut connaître des saisons que le serveur n'a
    pas — proposer de supprimer une saison inconnue du serveur ne mènerait qu'à
    un refus après confirmation.

    Une seule saison : pas de boîte, il n'y a rien à choisir.

    AUCUNE présélection : avec tout pré-coché (2026-08-30), cliquer sur la saison
    qu'on veut supprimer la décoche, et ce sont les autres qui partent. « Je
    coche ce que je veux supprimer » est le seul sens qui ne se retourne pas
    contre l'utilisateur."""
    seasons = preview.get("seasons") or []
    if len(seasons) <= 1:
        return [s["number"] for s in seasons]
    indexes = xbmcgui.Dialog().multiselect(
        "Cochez les saisons à supprimer",
        [season_label(s) for s in seasons])
    if indexes is None:
        return None
    return [seasons[i]["number"] for i in indexes]


def confirm(label, preview, dbtype, allow_purge):
    """-1 annulé, 0 supprimer, 1 purger.

    Trois boutons plutôt que deux : la purge (retrait de la série de Sonarr +
    exclusion de liste) est une action distincte, pas une case à cocher qu'on
    peut avoir laissée dans un état oublié. Elle n'est proposée que si TOUTES
    les saisons sont sélectionnées — une purge partielle laisserait les saisons
    gardées dans library/ sans plus aucun arr pour les revendiquer."""
    if dbtype == "movie":
        what = "ce film"
    elif dbtype == "season":
        what = "cette saison"
    else:
        what = "cette série"
    if preview.get("managed_by_arr"):
        scope = ("Torrents et fichiers seront supprimés. La série RESTE dans Sonarr : "
                 "une nouvelle saison sera téléchargée normalement."
                 if dbtype != "movie"
                 else "Torrents, fichiers et entrée Radarr seront supprimés.")
    else:
        scope = "Titre absent de Sonarr/Radarr : torrents et fichiers seront supprimés."
    message = "Supprimer {} ?\n\n[B]{}[/B]\n{}{}\n\n{}".format(
        what, label, preview.get("summary", ""), orphan_lines(preview), scope)

    if not allow_purge:
        return 0 if xbmcgui.Dialog().yesno(NAME, message, nolabel="Annuler",
                                           yeslabel="Supprimer") else -1
    # yesnocustom : -1 fermé, 0 « no », 1 « yes », 2 « custom ».
    choice = xbmcgui.Dialog().yesnocustom(
        NAME, message + "\n« Purger » retire aussi la série de Sonarr (plus rien ne reviendra).",
        customlabel="Purger", nolabel="Annuler", yeslabel="Supprimer")
    if choice == 1:
        return 0
    if choice == 2:
        return 1
    return -1


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
        notify("Cet élément n'est pas un film, une série ou une saison de la bibliothèque.",
               failed=True)
        return

    season_number = None
    lookup_id = int(dbid)
    if dbtype == "season":
        season_number, lookup_id = season_target(lookup_id)
        if season_number is None:
            notify("Saison introuvable dans la bibliothèque Kodi.", failed=True)
            return

    title, unique_ids, path = library_details(dbtype, lookup_id)
    label = title or xbmc.getInfoLabel("ListItem.Label") or "?"
    if season_number is not None:
        label = "{} — saison {}".format(label, season_number)
    # Mêmes clés que le corps attendu par clearr (DeleteTarget dans webapp.py) —
    # ce sont aussi les noms utilisés par Kodi dans son champ uniqueid.
    payload = {key: str(unique_ids.get(key, "")) for key in ("imdb", "tmdb", "tvdb")}
    payload["path"] = path
    if not any(payload.values()):
        xbmcgui.Dialog().ok(NAME, "Ni identifiant IMDb/TMDB/TVDB ni chemin de fichier pour « {} » : "
                                  "clearr ne peut pas retrouver ce titre.".format(label))
        return

    endpoint = KINDS[dbtype][0]
    if season_number is not None:
        payload["seasons"] = [season_number]
    # Prévisualisation avant de demander confirmation : la boîte de dialogue
    # annonce ce qui va réellement partir (nombre de torrents, taille) plutôt
    # que le seul titre. Elle sert aussi de garde — un titre que clearr ne sait
    # pas résoudre est signalé ICI, avant toute confirmation, plutôt qu'après un
    # « Supprimer » qui n'aurait rien supprimé.
    preview, error = fetch_preview(base_url, endpoint, payload)
    if error:
        xbmcgui.Dialog().ok(NAME, "{}\n\n[B]{}[/B]".format(error, label))
        return

    # Choix des saisons : seulement depuis une série gérée par Sonarr. Un clic
    # sur une saison a déjà désigné la sienne, et un titre hors arr n'a pas de
    # notion de saison (clearr renvoie alors une liste vide).
    available = [s["number"] for s in preview.get("seasons") or []]
    if dbtype == "tvshow" and preview.get("managed_by_arr") and available:
        chosen = choose_seasons(preview)
        if chosen is None:
            return
        if not chosen:
            notify("Aucune saison sélectionnée — rien n'a été supprimé.")
            return
        payload["seasons"] = chosen
        if sorted(chosen) != sorted(available):
            # La sélection a changé : le résumé de la première prévisualisation
            # ne décrit plus ce qui va partir. On le recalcule plutôt que
            # d'annoncer une taille et un nombre de torrents faux.
            preview, error = fetch_preview(base_url, endpoint, payload)
            if error:
                xbmcgui.Dialog().ok(NAME, "{}\n\n[B]{}[/B]".format(error, label))
                return

    # La purge n'a de sens que sur une SÉRIE entièrement sélectionnée : jamais sur
    # un film (Radarr retire déjà le titre de toute façon), jamais depuis une
    # saison (on n'en a désigné qu'une, purger emporterait les autres), jamais
    # sur un titre hors arr (rien à retirer d'un arr).
    allow_purge = (dbtype == "tvshow" and preview.get("managed_by_arr")
                   and sorted(payload.get("seasons") or available) == sorted(available))
    choice = confirm(label, preview, dbtype, allow_purge)
    if choice < 0:
        return
    if choice == 1:
        payload["purge"] = True
        payload.pop("seasons", None)

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
