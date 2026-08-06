# Service web FastAPI + gabarits Jinja2 (fragments rendus côté serveur,
# swap client via static/clearr.js — pas de HTMX vendoré, voir ce fichier).
# Chaque route recalcule l'état complet via core.load_full_state() plutôt que
# de garder un état en mémoire entre requêtes (contrairement à la TUI, qui ne
# recharge qu'une fois au démarrage) — voir CLAUDE.md "risque perf" : mesuré
# acceptable (<1s) à l'échelle de cette bibliothèque, à revoir si ça dérive.
import hashlib
import os
import urllib.parse

from fastapi import FastAPI, Form
from fastapi.requests import Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import core

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
templates = Jinja2Templates(directory=os.path.join(APP_DIR, "templates"))

app = FastAPI(title="clearr")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

DEFAULT_SORT = {"torrents": "AGE", "series": "TITRE", "films": "TITRE"}


def _compute_asset_version():
    # StaticFiles ne pose aucun Cache-Control — sans un paramètre qui change,
    # un navigateur qui a déjà chargé la page garde clearr.css/clearr.js en
    # cache indéfiniment après un `make update STACK=arr` (repéré le
    # 2026-08-01 : l'ancien design continuait de s'afficher après un rebuild
    # pourtant réussi côté conteneur). ?v=<hash> change dès qu'un des
    # fichiers statiques change, forçant un fetch — inclut aussi bootstrap.*
    # au cas où une future mise à jour de version les modifierait.
    digest = hashlib.sha256()
    for name in sorted(os.listdir(STATIC_DIR)):
        with open(os.path.join(STATIC_DIR, name), "rb") as f:
            digest.update(f.read())
    return digest.hexdigest()[:12]


ASSET_VERSION = _compute_asset_version()


# TransmissionClient.call lève RuntimeError quand transmission-vpn est
# injoignable (voir core.py) — la TUI l'attrape une seule fois en haut de
# tui.run() et affiche un message propre ; côté web chaque route peut la
# lever indépendamment (load_full_state() appelé par requête, voir plus
# haut), d'où ce handler global plutôt qu'un try/except répété partout.
@app.exception_handler(RuntimeError)
async def runtime_error_handler(request: Request, exc: RuntimeError):
    # Les routes /api/ (menu contextuel Kodi) ont un appelant qui parse du JSON :
    # lui renvoyer le fragment Bootstrap destiné au navigateur le ferait échouer
    # sur un message illisible plutôt que sur la vraie cause.
    if request.url.path.startswith("/api/"):
        return JSONResponse({"deleted": False, "message": f"Erreur : {exc}"}, status_code=503)
    return HTMLResponse(f'<div class="alert alert-danger m-3">Erreur : {exc}</div>', status_code=503)


def render(name, **ctx):
    return templates.env.get_template(name).render(**ctx)


@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


@app.get("/poster/{kind}/{arr_id}")
def poster(kind: str, arr_id: int):
    """Jaquette servie depuis le cache disque de Sonarr/Radarr (voir
    core.poster_file) — jamais un aller-retour vers thetvdb/tmdb, ni même vers
    l'API arr. 404 si le titre n'a pas de jaquette en cache : clearr.js n'a
    alors rien à afficher au survol, ce qui est le comportement voulu (pas de
    vignette cassée). Cache navigateur long : le contenu d'un
    MediaCover/<id>/poster-250.jpg ne change pas sans une action explicite dans
    Sonarr/Radarr."""
    path = core.poster_file(kind, arr_id) if kind in core.MEDIA_COVER_DIRS else None
    if not path:
        return Response(status_code=404)
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


# --- helpers d'affichage (équivalent web de ratio_color/col_label de tui.py) ---

def ratio_class(ratio):
    if ratio < 1.0:
        return "danger"
    if ratio < 3.0:
        return "warn"
    return "good"


def torrent_view(t, child=False, meta=None):
    return {
        "id": t["id"],
        # Série/film Sonarr/Radarr auquel ce torrent appartient (None si jamais
        # importé) — porte la jaquette et les liens, voir core.torrent_meta.
        "meta": meta,
        "name": t["name"],
        "bib": "" if child else ("✓" if t.get("_linked") else ""),
        "abs": "" if child else ("✓" if t.get("_missing") else ""),
        "abs_danger": (not child) and bool(t.get("_missing")),
        "age": core.human_age(t["addedDate"]),
        "size": core.human_size(t["totalSize"]),
        "ratio": f"{t['uploadRatio']:.2f}",
        "ratio_class": ratio_class(t["uploadRatio"]),
        "tracker": t.get("_tracker_name", "?"),
        # Hostnames bruts repliés sous "Autre" par core.tracker_display —
        # affichés en tooltip natif (title=), vide s'il n'y en a aucun.
        "tracker_others": t.get("_tracker_others", ""),
    }


def field_index(fields, label):
    for i, (name, _f) in enumerate(fields):
        if name == label:
            return i
    return 0


def sort_url(tab, key, current_key, current_reverse, filter_str):
    # Clic sur une colonne déjà active -> inverse le sens ; sur une autre
    # colonne -> bascule dessus en ascendant. Plus intuitif au clic qu'un
    # raccourci s/S séparé comme dans la TUI.
    reverse = "1" if (key == current_key and not current_reverse) else "0"
    q = urllib.parse.urlencode({"sort": key, "reverse": reverse, "filter": filter_str})
    return f"/tab/{tab}?{q}"


def build_columns(tab, fields, current_key, current_reverse, filter_str):
    return [
        {
            "key": key,
            "label": key,
            "active": key == current_key,
            "url": sort_url(tab, key, current_key, current_reverse, filter_str),
        }
        for key, _f in fields
    ]


def query_string(sort, reverse, filter_str):
    return urllib.parse.urlencode({"sort": sort, "reverse": "1" if reverse else "0", "filter": filter_str})


# --- rendu des 3 onglets ---

def render_torrents_tab(sort, reverse, filter_str, message=None, message_kind="success"):
    state = core.load_full_state()
    all_torrents = state["all_torrents"]
    cross_seed_groups = state["cross_seed_groups"]
    cross_seed_child_ids = state["cross_seed_child_ids"]
    linked_ids = state["linked_ids"]
    missing_ids = state["missing_ids"]

    core.sort_items(all_torrents, core.SORT_FIELDS, field_index(core.SORT_FIELDS, sort), reverse)

    meta_index = core.build_arr_meta_index()

    def meta_of(torrent):
        return core.torrent_meta(torrent, state["library_index"], meta_index)

    top_level = [t for t in all_torrents if t["id"] not in cross_seed_child_ids]
    needle = filter_str.lower()
    groups = []
    for t in top_level:
        children = cross_seed_groups.get(t["id"], [])
        parent_match = needle in t["name"].lower()
        matching_children = [c for c in children if needle in c["name"].lower()]
        if needle and not parent_match and not matching_children:
            continue
        children_sorted = sorted(children, key=lambda c: c.get("_tracker_name", ""))
        groups.append({
            "parent": torrent_view(t, meta=meta_of(t)),
            "children": [torrent_view(c, child=True, meta=meta_of(c)) for c in children_sorted],
            "force_open": bool(needle and matching_children and not parent_match),
        })

    return render(
        "torrents_tab.html",
        sort=sort, reverse=reverse, filter_str=filter_str,
        qs=query_string(sort, reverse, filter_str),
        columns=build_columns("torrents", core.SORT_FIELDS, sort, reverse, filter_str),
        groups=groups,
        total=len(all_torrents), linked_count=len(linked_ids), missing_count=len(missing_ids),
        group_count=len(cross_seed_groups),
        message=message, message_kind=message_kind,
    )


def series_row(s):
    stats = s.get("statistics", {})
    seasons = s.get("seasons", [])
    monitored_seasons = sum(1 for se in seasons if se.get("monitored"))
    return {
        "id": s["id"],
        "monitored": bool(s.get("monitored")),
        "seasons": f"{monitored_seasons}/{len(seasons)}",
        "episodes": f"{stats.get('episodeFileCount', 0)}/{stats.get('totalEpisodeCount', 0)}",
        "size": core.human_size(stats.get("sizeOnDisk", 0)),
        "title": s["title"],
        "meta": core.item_meta("series", s),
    }


def film_row(m):
    return {
        "id": m["id"],
        "monitored": bool(m.get("monitored")),
        "has_file": bool(m.get("hasFile")),
        "year": m.get("year", ""),
        "size": core.human_size(m.get("sizeOnDisk", 0)),
        "title": m["title"],
        "meta": core.item_meta("film", m),
    }


# Les vues Séries et Films ne diffèrent que par ces 3 valeurs — d'où un seul
# render_arr_tab() plutôt que deux fonctions au squelette identique (factorisé
# le 2026-08-03 en y ajoutant les métadonnées, qui auraient sinon été écrites
# deux fois). La vue Torrents, elle, garde sa fonction propre : elle part de
# Transmission (load_full_state) et non d'un arr, et gère les groupes
# cross-seed.
ARR_TABS = {
    "series": {"fetch": core.fetch_series_list, "fields": core.SERIES_SORT_FIELDS, "row": series_row},
    "films": {"fetch": core.fetch_movies_list, "fields": core.FILMS_SORT_FIELDS, "row": film_row},
}


def render_arr_tab(tab, sort, reverse, filter_str, message=None, message_kind="success"):
    spec = ARR_TABS[tab]
    items = spec["fetch"]()
    selected = core.filter_by_title(items, filter_str)
    core.sort_items(selected, spec["fields"], field_index(spec["fields"], sort), reverse)
    return render(
        f"{tab}_tab.html",
        sort=sort, reverse=reverse, filter_str=filter_str,
        qs=query_string(sort, reverse, filter_str),
        columns=build_columns(tab, spec["fields"], sort, reverse, filter_str),
        rows=[spec["row"](i) for i in selected], total=len(items),
        message=message, message_kind=message_kind,
    )


@app.get("/", response_class=HTMLResponse)
def index():
    content = render_torrents_tab(DEFAULT_SORT["torrents"], False, "")
    return HTMLResponse(render("page.html", initial_content=content, v=ASSET_VERSION))


@app.get("/tab/torrents", response_class=HTMLResponse)
def tab_torrents(sort: str = DEFAULT_SORT["torrents"], reverse: str = "0", filter: str = ""):
    return HTMLResponse(render_torrents_tab(sort, reverse == "1", filter))


@app.get("/tab/series", response_class=HTMLResponse)
def tab_series(sort: str = DEFAULT_SORT["series"], reverse: str = "0", filter: str = ""):
    return HTMLResponse(render_arr_tab("series", sort, reverse == "1", filter))


@app.get("/tab/films", response_class=HTMLResponse)
def tab_films(sort: str = DEFAULT_SORT["films"], reverse: str = "0", filter: str = ""):
    return HTMLResponse(render_arr_tab("films", sort, reverse == "1", filter))


# --- purge groupée des torrents marqués ABS — routes STATIQUES, doivent être
# déclarées avant /torrents/{tid}/... : Starlette résout les routes dans
# l'ordre de déclaration, donc /torrents/{tid}/confirm capturerait sinon
# "purge-abs" comme une valeur de tid (404/422 au lieu d'atteindre cette
# route, piège rencontré en écrivant le smoke test). ---

@app.get("/torrents/purge-abs/confirm", response_class=HTMLResponse)
def purge_confirm(sort: str = DEFAULT_SORT["torrents"], reverse: str = "0", filter: str = ""):
    state = core.load_full_state()
    missing_torrents = [t for t in state["all_torrents"] if t["id"] in state["missing_ids"]]
    return HTMLResponse(render(
        "confirm_bulk.html",
        torrents=[t["name"] for t in missing_torrents],
        sort=sort, reverse=reverse == "1", filter_str=filter,
    ))


@app.post("/torrents/purge-abs", response_class=HTMLResponse)
def purge_execute(sort: str = Form(DEFAULT_SORT["torrents"]), reverse: str = Form("0"), filter: str = Form("")):
    state = core.load_full_state()
    client = state["client"]
    all_torrents = state["all_torrents"]
    library_index = state["library_index"]
    linked_ids = state["linked_ids"]
    missing_ids = state["missing_ids"]
    cross_seed_groups = state["cross_seed_groups"]
    missing_torrents = [t for t in all_torrents if t["id"] in missing_ids]
    deleted, failed, skipped = 0, 0, 0
    for torrent in missing_torrents:
        current_ids = {t["id"] for t in all_torrents}
        if torrent["id"] not in current_ids:
            skipped += 1
            continue
        try:
            host_files = core.torrent_host_files(torrent)
            lib_matches = core.find_library_matches(host_files, library_index)
            arr_plan = core.plan_arr_actions(lib_matches)
            all_torrents, _freed = core.apply_deletion(client, torrent, host_files, lib_matches, arr_plan,
                                                         all_torrents, linked_ids, missing_ids, cross_seed_groups)
            deleted += 1
        except Exception as e:
            core.logger.error("échec de la purge de %r (id=%s) : %s", torrent["name"], torrent["id"], e)
            failed += 1
    message = f"Purge : {deleted} supprimé(s)"
    if skipped:
        message += f", {skipped} déjà supprimé(s) en cascade"
    if failed:
        message += f", {failed} échec(s) (voir {core.LOG_PATH})"
    kind = "danger" if failed else "success"
    return HTMLResponse(render_torrents_tab(sort, reverse == "1", filter, message=message, message_kind=kind))


# --- suppression d'un torrent (vue Torrents, et vue Films quand un torrent
# correspondant est trouvé — même gabarit confirm_torrent.html, réutilisé
# comme confirm_delete() l'est par les deux vues côté TUI) ---

# --- fiches détail ------------------------------------------------------------
#
# Ouvertes en cliquant le nom dans n'importe laquelle des 3 vues (voir le macro
# title_cell de templates/_meta.html). Un seul gabarit (details.html) alimenté
# par des paires libellé/valeur : chaque vue décide de SES sections, mais aucune
# ne redéclare la mise en forme. Toutes les valeurs sont déjà connues de clearr,
# aucune n'entraîne d'appel WAN — même contrainte que les jaquettes et les liens
# (voir CLAUDE.md), la seule requête ajoutée étant le nom du profil qualité,
# demandé à Sonarr/Radarr sur le réseau interne.

TORRENT_STATUS_LABELS = {
    0: "arrêté", 1: "en attente de vérification", 2: "vérification",
    3: "en attente de téléchargement", 4: "téléchargement",
    5: "en attente de seed", 6: "en seed 🌱",
}


def _yes_no(value):
    return "oui" if value else "non"


def _joined(values):
    return ", ".join(str(v) for v in values) if values else "—"


def _seed_limit_label(torrent):
    """Le mode décide LAQUELLE des limites s'applique — afficher la valeur seule
    induirait en erreur, un torrent en mode 0 portant souvent un seedRatioLimit
    résiduel qui ne sert à rien (constaté le 2026-08-06)."""
    mode = torrent.get("seedRatioMode")
    if mode == 1:
        return f"{torrent.get('seedRatioLimit')} (propre au torrent)"
    if mode == 2:
        return "aucune (seed sans fin)"
    return "limite globale de Transmission"


def _file_quality(arr_file):
    return ((arr_file.get("quality") or {}).get("quality") or {}).get("name") or "?"


def _file_languages(arr_file):
    return _joined([lang.get("name") for lang in arr_file.get("languages") or []])


def _distinct(values):
    """Valeurs distinctes en préservant l'ordre de première apparition —
    `sorted(set(...))` réordonnerait alphabétiquement des qualités/groupes qu'on
    lit plus naturellement dans l'ordre des fichiers."""
    return _joined(list(dict.fromkeys(v for v in values if v)))


def _media_rows(arr_file):
    """Section « Média » d'un fichier : ce que Sonarr/Radarr ont extrait du
    conteneur (mediaInfo). Absente si l'analyse n'a jamais tourné sur ce
    fichier — auquel cas la fiche saute simplement la section (voir
    details.html, qui n'affiche pas une section sans lignes)."""
    info = arr_file.get("mediaInfo") or {}
    if not info:
        return []
    video = " ".join(str(p) for p in (
        info.get("videoCodec"), f"{info['videoBitDepth']} bits" if info.get("videoBitDepth") else None,
        info.get("videoDynamicRangeType") or info.get("videoDynamicRange"),
        f"{info['videoFps']} fps" if info.get("videoFps") else None) if p)
    audio = " ".join(str(p) for p in (
        info.get("audioCodec"), info.get("audioChannels"),
        f"({info['audioLanguages']})" if info.get("audioLanguages") else None) if p)
    return [
        ("Durée", info.get("runTime") or "—"),
        ("Résolution", info.get("resolution") or "—"),
        ("Vidéo", video or "—"),
        ("Audio", audio or "—"),
        ("Sous-titres", info.get("subtitles") or "aucun"),
    ]


def _torrent_details_context(torrent, state):
    host_files = core.torrent_host_files(torrent)
    lib_matches = core.find_library_matches(host_files, state["library_index"])
    meta = core.torrent_meta(torrent, state["library_index"], core.build_arr_meta_index())
    children = state["cross_seed_groups"].get(torrent["id"], [])
    trackers = core.tracker_host(torrent)
    return dict(
        title=torrent["name"],
        poster=meta["poster"] if meta else None,
        links=meta["links"] if meta else [],
        overview=None,
        sections=[
            {"title": "Torrent", "rows": [
                ("Statut", TORRENT_STATUS_LABELS.get(torrent.get("status"), "?")),
                ("Taille", core.human_size(torrent["totalSize"])),
                ("Avancement", f"{torrent.get('percentDone', 0) * 100:.1f} %"),
                ("Ajouté", f"il y a {core.human_age(torrent['addedDate'])}"),
                ("Dossier", torrent.get("downloadDir", "—")),
            ]},
            {"title": "Partage", "rows": [
                ("Ratio", f"{torrent['uploadRatio']:.2f}"),
                ("Limite de ratio", _seed_limit_label(torrent)),
                ("Tracker", torrent.get("_tracker_name", "?")),
                ("Hôtes d'annonce", trackers.replace(",", ", ") if trackers != "?" else "—"),
            ]},
            {"title": "Rattachement", "rows": [
                ("Titre arr", meta["title"] if meta else "aucun (jamais importé)"),
                ("Cross-seed", f"{len(children)} torrent(s) rattaché(s)" if children
                               else ("injecté par cross-seed" if core.is_cross_seed_entry(torrent) else "non")),
            ]},
        ],
        lists=[
            {"title": "Fichiers Transmission",
             "items": [{"name": os.path.basename(p), "size": core.human_size(s)} for p, s in host_files]},
            {"title": "Fichiers bibliothèque",
             "items": [{"name": p.replace(core.LIBRARY_ROOT, "library"), "size": core.human_size(s)}
                       for p, s in lib_matches]},
            {"title": "Torrents cross-seedés",
             "items": [{"name": c["name"], "size": c.get("_tracker_name", "")} for c in children]},
        ],
    )


def _series_details_context(series):
    stats = series.get("statistics", {})
    seasons = series.get("seasons", [])
    profiles = core.quality_profile_names("series")
    # Trié par chemin : c'est l'ordre saison/épisode, celui dans lequel on les
    # cherche des yeux — l'ordre de l'API est celui des ids, donc celui des
    # imports (E03 avant E01 sur cette bibliothèque).
    files = sorted(core.fetch_episode_files(series["id"]), key=lambda f: f.get("relativePath", ""))
    return dict(
        title=series["title"],
        poster=f"/poster/series/{series['id']}" if core.poster_file("series", series["id"]) else None,
        links=core.item_meta("series", series)["links"],
        overview=series.get("overview"),
        sections=[
            {"title": "Série", "rows": [
                ("Année", series.get("year", "—")),
                ("Statut", series.get("status", "—") + (" (terminée)" if series.get("ended") else "")),
                ("Diffuseur", series.get("network") or "—"),
                ("Type", series.get("seriesType", "—")),
                ("Durée", f"{series['runtime']} min" if series.get("runtime") else "—"),
                ("Genres", _joined(series.get("genres"))),
                ("Langue d'origine", (series.get("originalLanguage") or {}).get("name", "—")),
            ]},
            {"title": "Bibliothèque", "rows": [
                ("Chemin", series.get("path", "—")),
                ("Suivie", _yes_no(series.get("monitored"))),
                ("Profil qualité", profiles.get(series.get("qualityProfileId"), series.get("qualityProfileId", "—"))),
                ("Saisons", f"{sum(1 for s in seasons if s.get('monitored'))} suivie(s) / {len(seasons)}"),
                ("Épisodes", f"{stats.get('episodeFileCount', 0)} sur disque / "
                             f"{stats.get('totalEpisodeCount', 0)} au total"),
                ("Taille", core.human_size(stats.get("sizeOnDisk", 0))),
                ("Prochaine diffusion", (series.get("nextAiring") or "—")[:10]),
            ]},
            # Agrégé plutôt que détaillé par fichier : une série a autant de
            # mediaInfo que d'épisodes, les empiler rendrait la fiche
            # illisible. Les valeurs distinctes suffisent à repérer un
            # mélange (deux groupes, deux qualités, une VF au milieu de VO).
            {"title": "Fichiers", "rows": [
                ("Nombre", len(files)),
                ("Taille totale", core.human_size(sum(f.get("size", 0) for f in files))),
                ("Qualités", _distinct(_file_quality(f) for f in files)),
                ("Groupes", _distinct(f.get("releaseGroup") for f in files)),
                ("Langues", _distinct(lang.get("name") for f in files
                                      for lang in f.get("languages") or [])),
                ("Codecs vidéo", _distinct((f.get("mediaInfo") or {}).get("videoCodec") for f in files)),
                ("Résolutions", _distinct((f.get("mediaInfo") or {}).get("resolution") for f in files)),
            ] if files else []},
            {"title": "Identifiants", "rows": [
                ("IMDb", series.get("imdbId") or "—"),
                ("TVDB", series.get("tvdbId") or "—"),
                ("TMDB", series.get("tmdbId") or "—"),
            ]},
        ],
        lists=[
            {"title": "Saisons",
             "items": [{"name": f"Saison {s['seasonNumber']}"
                                + ("" if s.get("monitored") else " (non suivie)"),
                        "size": core.human_size((s.get("statistics") or {}).get("sizeOnDisk", 0))}
                       for s in seasons]},
            {"title": "Fichiers",
             "items": [{"name": f.get("relativePath", "?"),
                        "size": f"{core.human_size(f.get('size', 0))} — {_file_quality(f)}"}
                       for f in files]},
        ],
    )


def _film_details_context(movie):
    movie_file = movie.get("movieFile") or {}
    quality = ((movie_file.get("quality") or {}).get("quality") or {}).get("name")
    profiles = core.quality_profile_names("film")
    return dict(
        title=movie["title"],
        poster=f"/poster/film/{movie['id']}" if core.poster_file("film", movie["id"]) else None,
        links=core.item_meta("film", movie)["links"],
        overview=movie.get("overview"),
        sections=[
            {"title": "Film", "rows": [
                ("Année", movie.get("year", "—")),
                ("Titre original", movie.get("originalTitle") or "—"),
                ("Statut", movie.get("status", "—")),
                ("Studio", movie.get("studio") or "—"),
                ("Durée", f"{movie['runtime']} min" if movie.get("runtime") else "—"),
                ("Genres", _joined(movie.get("genres"))),
                ("Collection", (movie.get("collection") or {}).get("title", "—")),
            ]},
            {"title": "Bibliothèque", "rows": [
                ("Chemin", movie.get("path", "—")),
                ("Suivi", _yes_no(movie.get("monitored"))),
                ("Fichier présent", _yes_no(movie.get("hasFile"))),
                ("Profil qualité", profiles.get(movie.get("qualityProfileId"), movie.get("qualityProfileId", "—"))),
            ]},
            # Un film n'a qu'un fichier : contrairement à une série, son détail
            # tient dans la fiche sans rien agréger.
            {"title": "Fichier", "rows": [
                ("Nom", movie_file.get("relativePath", "—")),
                ("Taille", core.human_size(movie.get("sizeOnDisk", 0))),
                ("Qualité", quality or "—"),
                ("Édition", movie_file.get("edition") or "—"),
                ("Groupe", movie_file.get("releaseGroup") or "—"),
                ("Langues", _file_languages(movie_file)),
                ("Importé le", (movie_file.get("dateAdded") or "—")[:10]),
                # Pas de custom formats ici : contrairement à l'episodefile de
                # Sonarr, le movieFile imbriqué dans l'objet film de Radarr ne
                # porte ni customFormats ni customFormatScore (vérifié le
                # 2026-08-06) — les afficher ne donnerait que des tirets.
                ("Nom de release", movie_file.get("sceneName") or "—"),
            ] if movie_file else []},
            {"title": "Média", "rows": _media_rows(movie_file)},
            {"title": "Identifiants", "rows": [
                ("IMDb", movie.get("imdbId") or "—"),
                ("TMDB", movie.get("tmdbId") or "—"),
            ]},
        ],
        lists=[],
    )


@app.get("/torrents/{tid}/details", response_class=HTMLResponse)
def torrent_details(tid: int):
    state = core.load_full_state()
    torrent = next((t for t in state["all_torrents"] if t["id"] == tid), None)
    if not torrent:
        return HTMLResponse("<p>Torrent introuvable (déjà supprimé ?). Fermez et rafraîchissez.</p>")
    return HTMLResponse(render("details.html", **_torrent_details_context(torrent, state)))


@app.get("/series/{sid}/details", response_class=HTMLResponse)
def series_details(sid: int):
    series = core.find_series_by_id(sid)
    if not series:
        return HTMLResponse("<p>Série introuvable. Fermez et rafraîchissez.</p>")
    return HTMLResponse(render("details.html", **_series_details_context(series)))


@app.get("/films/{mid}/details", response_class=HTMLResponse)
def film_details(mid: int):
    movie = core.find_movie_by_id(mid)
    if not movie:
        return HTMLResponse("<p>Film introuvable. Fermez et rafraîchissez.</p>")
    return HTMLResponse(render("details.html", **_film_details_context(movie)))


def _torrent_confirm_context(torrent, state, sort, reverse, filter_str, post_url):
    host_files = core.torrent_host_files(torrent)
    lib_matches = core.find_library_matches(host_files, state["library_index"])
    arr_plan = core.plan_arr_actions(lib_matches)
    dependents = state["cross_seed_groups"].get(torrent["id"], [])
    return dict(
        torrent_name=torrent["name"],
        dependents=[{"name": d["name"], "tracker": d.get("_tracker_name", "?")} for d in dependents],
        host_files=[{"name": os.path.basename(p), "size": core.human_size(s)} for p, s in host_files],
        host_files_size=core.human_size(sum(s for _, s in host_files)),
        lib_matches=[{"name": p.replace(core.LIBRARY_ROOT, "library"), "size": core.human_size(s)}
                     for p, s in lib_matches],
        lib_matches_size=core.human_size(sum(s for _, s in lib_matches)),
        arr_plan=[a["description"] for a in arr_plan],
        # host_files seul, pas + lib_matches : find_library_matches() ne renvoie
        # que des fichiers library/ hardlinkés (même inode) à un fichier de
        # host_files, les additionner compte deux fois les mêmes octets
        # physiques (repéré le 2026-08-01, cf. core.apply_deletion).
        total_size=core.human_size(sum(s for _, s in host_files)),
        sort=sort, reverse=reverse, filter_str=filter_str,
        post_url=post_url, target="#tab-content",
    )


@app.get("/torrents/{tid}/confirm", response_class=HTMLResponse)
def torrent_confirm(tid: int, sort: str = DEFAULT_SORT["torrents"], reverse: str = "0", filter: str = ""):
    state = core.load_full_state()
    torrent = next((t for t in state["all_torrents"] if t["id"] == tid), None)
    if not torrent:
        return HTMLResponse("<p>Torrent introuvable (déjà supprimé ?). Fermez et rafraîchissez.</p>")
    ctx = _torrent_confirm_context(torrent, state, sort, reverse == "1", filter, f"/torrents/{tid}/delete")
    return HTMLResponse(render("confirm_torrent.html", **ctx))


@app.post("/torrents/{tid}/delete", response_class=HTMLResponse)
def torrent_delete(tid: int, sort: str = Form(DEFAULT_SORT["torrents"]), reverse: str = Form("0"),
                    filter: str = Form("")):
    state = core.load_full_state()
    torrent = next((t for t in state["all_torrents"] if t["id"] == tid), None)
    if not torrent:
        return HTMLResponse(render_torrents_tab(sort, reverse == "1", filter,
                                                  message="Torrent déjà supprimé.", message_kind="warning"))
    host_files = core.torrent_host_files(torrent)
    lib_matches = core.find_library_matches(host_files, state["library_index"])
    arr_plan = core.plan_arr_actions(lib_matches)
    try:
        _remaining, freed = core.apply_deletion(state["client"], torrent, host_files, lib_matches, arr_plan,
                                                  state["all_torrents"], state["linked_ids"], state["missing_ids"],
                                                  state["cross_seed_groups"])
        message = f"Supprimé : {torrent['name']} ({core.human_size(freed)} libéré(s))"
        kind = "success"
    except Exception as e:
        core.logger.error("échec de la suppression de %r : %s", torrent["name"], e)
        message, kind = f"ÉCHEC (voir {core.LOG_PATH}) : {e}", "danger"
    return HTMLResponse(render_torrents_tab(sort, reverse == "1", filter, message=message, message_kind=kind))


# --- suppression d'un titre entier, partagée entre les routes web (vues
# Séries/Films) et les routes /api/ (menu contextuel Kodi) : la seule différence
# entre les deux est la façon de rapporter le résultat, pas ce qui est supprimé.
# Ces deux helpers lèvent sur échec, chaque appelant décidant du rendu. ---

def _delete_series(series, state):
    matched = core.find_series_torrents(state["all_torrents"], state["library_index"],
                                         state["cross_seed_child_ids"], series["path"])
    core.execute_delete_series(state["client"], series, matched, state["all_torrents"],
                                state["cross_seed_groups"], state["linked_ids"], state["missing_ids"])
    return f"Série supprimée : {series['title']}"


def _delete_movie(movie, state):
    movie_path = movie["movieFile"]["path"] if movie.get("hasFile") else None
    torrent = core.find_movie_torrent(state["all_torrents"], state["cross_seed_child_ids"], movie_path) \
        if movie_path else None
    if torrent:
        host_files = core.torrent_host_files(torrent)
        lib_matches = core.find_library_matches(host_files, state["library_index"])
        arr_plan = core.plan_arr_actions(lib_matches)
        _remaining, freed = core.apply_deletion(state["client"], torrent, host_files, lib_matches, arr_plan,
                                                  state["all_torrents"], state["linked_ids"],
                                                  state["missing_ids"], state["cross_seed_groups"])
        return f"Film supprimé : {movie['title']} ({core.human_size(freed)} libéré(s))"
    # Jamais téléchargé, ou fichier orphelin hors suivi : c'est Radarr qui
    # supprime son propre fichier (voir core.execute_delete_movie_no_torrent).
    core.execute_delete_movie_no_torrent(movie)
    return f"Film supprimé : {movie['title']}"


# --- suppression d'une série entière (vue Séries) ---

@app.get("/series/{sid}/confirm", response_class=HTMLResponse)
def series_confirm(sid: int, sort: str = DEFAULT_SORT["series"], reverse: str = "0", filter: str = ""):
    series = core.find_series_by_id(sid)
    if not series:
        return HTMLResponse("<p>Série introuvable. Fermez et rafraîchissez.</p>")
    state = core.load_full_state()
    matched = core.find_series_torrents(state["all_torrents"], state["library_index"],
                                         state["cross_seed_child_ids"], series["path"])
    total_files = sum(len(hf) for _t, hf, _lm in matched)
    total_size = sum(s for _t, hf, _lm in matched for _p, s in hf)
    # Fichiers du dossier qu'aucun torrent ne couvre : execute_delete_series les
    # supprime (cleanup_orphan_files) mais l'écran de confirmation ne les
    # annonçait pas, donc promettait moins que ce qu'il fait.
    orphans = core.series_orphan_files(matched, series["path"])
    return HTMLResponse(render(
        "confirm_series.html",
        series_id=sid, series_title=series["title"],
        torrents=[{"name": t["name"], "seeding": core.is_seeding(t)} for t, _hf, _lm in matched],
        total_files=total_files, total_size=core.human_size(total_size),
        orphans=[{"name": os.path.basename(p), "size": core.human_size(s)} for p, s in orphans],
        orphans_size=core.human_size(sum(s for _p, s in orphans)),
        sort=sort, reverse=reverse == "1", filter_str=filter,
    ))


@app.post("/series/{sid}/delete", response_class=HTMLResponse)
def series_delete(sid: int, sort: str = Form(DEFAULT_SORT["series"]), reverse: str = Form("0"),
                   filter: str = Form("")):
    series = core.find_series_by_id(sid)
    if not series:
        return HTMLResponse(render_arr_tab("series", sort, reverse == "1", filter,
                                               message="Série déjà supprimée.", message_kind="warning"))
    state = core.load_full_state()
    try:
        message = _delete_series(series, state)
        kind = "success"
    except Exception as e:
        core.logger.error("échec de la suppression de la série %r : %s", series["title"], e)
        message, kind = f"ÉCHEC (voir {core.LOG_PATH}) : {e}", "danger"
    return HTMLResponse(render_arr_tab("series", sort, reverse == "1", filter, message=message, message_kind=kind))


# --- suppression d'un film (vue Films) ---

@app.get("/films/{mid}/confirm", response_class=HTMLResponse)
def film_confirm(mid: int, sort: str = DEFAULT_SORT["films"], reverse: str = "0", filter: str = ""):
    movie = core.find_movie_by_id(mid)
    if not movie:
        return HTMLResponse("<p>Film introuvable. Fermez et rafraîchissez.</p>")
    state = core.load_full_state()
    movie_path = movie["movieFile"]["path"] if movie.get("hasFile") else None
    torrent = core.find_movie_torrent(state["all_torrents"], state["cross_seed_child_ids"], movie_path) \
        if movie_path else None
    if torrent:
        ctx = _torrent_confirm_context(torrent, state, sort, reverse == "1", filter, f"/films/{mid}/delete")
        return HTMLResponse(render("confirm_torrent.html", **ctx))
    return HTMLResponse(render(
        "confirm_movie_no_torrent.html",
        movie_id=mid, movie_title=movie["title"], has_file=bool(movie.get("hasFile")),
        size=core.human_size(movie.get("sizeOnDisk", 0)),
        sort=sort, reverse=reverse == "1", filter_str=filter,
    ))


@app.post("/films/{mid}/delete", response_class=HTMLResponse)
def film_delete(mid: int, sort: str = Form(DEFAULT_SORT["films"]), reverse: str = Form("0"), filter: str = Form("")):
    movie = core.find_movie_by_id(mid)
    if not movie:
        return HTMLResponse(render_arr_tab("films", sort, reverse == "1", filter,
                                              message="Film déjà supprimé.", message_kind="warning"))
    state = core.load_full_state()
    try:
        message = _delete_movie(movie, state)
        kind = "success"
    except Exception as e:
        core.logger.error("échec de la suppression du film %r : %s", movie["title"], e)
        message, kind = f"ÉCHEC (voir {core.LOG_PATH}) : {e}", "danger"
    return HTMLResponse(render_arr_tab("films", sort, reverse == "1", filter, message=message, message_kind=kind))


# --- API JSON : prévisualisation et suppression d'un titre --------------------
#
# Consommée par l'addon de menu contextuel Kodi (kodi/context.clearr, entrée
# « Supprimer avec clearr »). Deux modes de résolution, essayés dans cet ordre :
#
# 1. Par id externe (IMDb/TVDB/TMDB) côté Sonarr/Radarr — un client media ne
#    connaît pas les ids arr, seulement ceux des bases publiques, d'où
#    core.find_*_by_external_ids. Supprime alors exactement ce que suppriment
#    les vues Séries/Films (mêmes helpers _delete_series/_delete_movie), pas une
#    variante allégée.
# 2. Par chemin, pour un titre récupéré à la main qui n'est dans aucun arr
#    (ajouté le 2026-08-06) : Jellyfin sert aussi completed/ comme bibliothèque,
#    donc Kodi affiche des titres que l'étape 1 ne peut pas résoudre. Voir la
#    section "Titres hors Sonarr/Radarr" de core.py pour le pourquoi du chemin
#    plutôt que des ids, et pourquoi ce repli s'interdit library/.
#
# Chaque mode a sa route de prévisualisation (/api/preview/*), appelée par
# l'addon avant sa demande de confirmation : sans elle, la boîte de dialogue
# Kodi ne pourrait annoncer que le titre, jamais ce qui va réellement partir.
#
# Aucune authentification, comme le reste de clearr : le service est LAN-only
# (middleware arr-lan-only, arr/docker-compose.yml) et son UI web expose déjà
# les mêmes suppressions en POST sans jeton — ces routes n'ajoutent donc pas une
# classe d'exposition nouvelle. À revoir en même temps que celle de l'UI si le
# besoin d'un jeton apparaît, pas séparément.
#
# Kodi ne retire PAS l'item de sa propre base à la réponse : c'est jellyfin-kodi
# qui le fait quand Jellyfin lui pousse l'événement de suppression (Sonarr/
# Radarr notifient Jellyfin dès le retrait du titre, déclencheurs onSeriesDelete/
# onMovieDelete activés le 2026-08-05). Voir kodi/README.md pour la chaîne
# complète et pourquoi le retrait local a été écarté.

class DeleteTarget(BaseModel):
    imdb: str = ""
    tvdb: str = ""
    tmdb: str = ""
    # Chemin du fichier (film) ou du dossier (série) tel que le voit le client,
    # facultatif : il ne sert que quand les ids ne résolvent rien côté arr.
    path: str = ""


class _NotFound(Exception):
    """Résolution impossible — porte le message destiné à l'utilisateur Kodi."""


def _resolve_target(target, find, kind_label):
    """(mode, objet) où mode vaut "arr" (objet = titre Sonarr/Radarr) ou
    "media_path" (objet = chemin réel sous /data_root). Lève _NotFound sinon."""
    item = find(target.model_dump())
    if item:
        return "arr", item

    known = ", ".join(f"{k}={v}" for k, v in target.model_dump().items() if v and k != "path") \
        or "aucun id fourni"
    if not target.path:
        raise _NotFound(f"{kind_label} introuvable côté arr ({known}).")

    resolved = core.resolve_media_path(target.path)
    if not resolved:
        raise _NotFound(f"{kind_label} introuvable côté arr ({known}), et son chemin "
                        f"n'a pas pu être retrouvé sur le disque du serveur.")
    if core.is_arr_managed_path(resolved):
        # Le titre vit dans library/ : soit Sonarr/Radarr le suit sous d'autres
        # ids, soit ses ids sont ambigus (voir core._find_by_external_ids).
        # Supprimer ses fichiers sans retirer son entrée arr le ferait
        # re-télécharger — on renvoie donc l'utilisateur vers l'UI web.
        raise _NotFound(f"{kind_label} géré par Sonarr/Radarr mais non résolu par ses "
                        f"identifiants ({known}) : à supprimer depuis l'interface web de clearr.")
    return "media_path", resolved


def _preview_arr_series(series, state):
    matched = core.find_series_torrents(state["all_torrents"], state["library_index"],
                                         state["cross_seed_child_ids"], series["path"])
    # Mêmes fichiers résiduels que ceux annoncés par l'écran web (voir
    # series_confirm) : execute_delete_series les supprime aussi, l'addon Kodi
    # doit donc pouvoir les nommer avant de demander confirmation.
    orphans = core.series_orphan_files(matched, series["path"])
    return {
        "title": series["title"],
        "torrents": len(matched),
        "seeding": sum(1 for t, _hf, _lm in matched if core.is_seeding(t)),
        "files": sum(len(host_files) for _t, host_files, _lm in matched) + len(orphans),
        "orphan_files": len(orphans),
        "orphans": [{"name": os.path.basename(p), "size": core.human_size(s)} for p, s in orphans],
        "size_bytes": sum(s for _t, host_files, _lm in matched for _p, s in host_files)
                      + sum(s for _p, s in orphans),
    }


def _preview_arr_movie(movie, state):
    movie_path = movie["movieFile"]["path"] if movie.get("hasFile") else None
    torrent = core.find_movie_torrent(state["all_torrents"], state["cross_seed_child_ids"], movie_path) \
        if movie_path else None
    host_files = core.torrent_host_files(torrent) if torrent else []
    return {
        "title": movie["title"],
        "torrents": 1 if torrent else 0,
        "seeding": 1 if torrent and core.is_seeding(torrent) else 0,
        "files": len(host_files) or (1 if movie.get("hasFile") else 0),
        # Pas d'orphelins ici : contrairement à une série, la suppression d'un
        # film ne balaie pas son dossier (voir _delete_movie) — soit un torrent
        # le couvre, soit c'est Radarr qui supprime son propre fichier.
        "orphan_files": 0,
        "orphans": [],
        "size_bytes": sum(s for _p, s in host_files) or movie.get("sizeOnDisk", 0),
    }


def _summary(preview, managed_by_arr):
    """Résumé d'une ligne affiché dans la boîte de confirmation de Kodi."""
    parts = []
    if preview["torrents"]:
        # 🌱 : combien de ces torrents partagent encore. Un torrent arrêté part
        # aussi, mais son retrait ne coûte rien au ratio — d'où la distinction.
        seeding = preview.get("seeding", 0)
        suffix = f" ({seeding} 🌱)" if seeding else ""
        parts.append(f"{preview['torrents']} torrent"
                     + ("s" if preview["torrents"] > 1 else "") + suffix)
    if preview.get("orphan_files"):
        parts.append(f"{preview['orphan_files']} fichier"
                     + ("s" if preview["orphan_files"] > 1 else "") + " sans torrent")
    if not parts:
        parts.append("aucun torrent" if managed_by_arr else "aucun fichier")
    return ", ".join(parts) + f" — {core.human_size(preview['size_bytes'])}"


def _api_preview(target, find, preview_arr, kind_label):
    try:
        mode, resolved = _resolve_target(target, find, kind_label)
    except _NotFound as e:
        return JSONResponse({"found": False, "message": str(e)}, status_code=404)
    state = core.load_full_state()
    if mode == "arr":
        preview = preview_arr(resolved, state)
    else:
        preview = core.plan_media_path_deletion(state, resolved)
        preview.pop("matched")  # non sérialisable, et sans intérêt pour l'appelant
        preview.pop("target")
    return {"found": True, "managed_by_arr": mode == "arr", "summary": _summary(preview, mode == "arr"),
            **preview}


def _delete_media_path(resolved, state):
    plan = core.plan_media_path_deletion(state, resolved)
    _remaining, freed, deleted, failed = core.execute_delete_media_path(
        state["client"], plan, state["all_torrents"], state["cross_seed_groups"],
        state["linked_ids"], state["missing_ids"])
    suffix = f", {failed} échec(s)" if failed else ""
    return f"Supprimé : {plan['title']} ({deleted} torrent(s){suffix}, {core.human_size(freed)} libéré(s))"


def _api_delete(target, find, delete_arr, kind_label):
    try:
        mode, resolved = _resolve_target(target, find, kind_label)
    except _NotFound as e:
        return JSONResponse({"deleted": False, "message": str(e)}, status_code=404)
    state = core.load_full_state()
    title = resolved["title"] if mode == "arr" else os.path.basename(resolved.rstrip("/"))
    try:
        message = delete_arr(resolved, state) if mode == "arr" else _delete_media_path(resolved, state)
    except Exception as e:
        core.logger.error("API : échec de la suppression de %r : %s", title, e)
        return JSONResponse(
            {"deleted": False, "title": title, "message": f"ÉCHEC (voir {core.LOG_PATH}) : {e}"},
            status_code=500,
        )
    core.logger.info("API : %s", message)
    return {"deleted": True, "title": title, "message": message}


@app.post("/api/preview/series")
def api_preview_series(target: DeleteTarget):
    return _api_preview(target, core.find_series_by_external_ids, _preview_arr_series, "Série")


@app.post("/api/preview/film")
def api_preview_film(target: DeleteTarget):
    return _api_preview(target, core.find_movie_by_external_ids, _preview_arr_movie, "Film")


@app.post("/api/delete/series")
def api_delete_series(target: DeleteTarget):
    return _api_delete(target, core.find_series_by_external_ids, _delete_series, "Série")


@app.post("/api/delete/film")
def api_delete_film(target: DeleteTarget):
    return _api_delete(target, core.find_movie_by_external_ids, _delete_movie, "Film")
