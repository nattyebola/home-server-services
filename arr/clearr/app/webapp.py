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
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

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
    return HTMLResponse(render(
        "confirm_series.html",
        series_id=sid, series_title=series["title"],
        torrents=[t["name"] for t, _hf, _lm in matched],
        total_files=total_files, total_size=core.human_size(total_size),
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
    matched = core.find_series_torrents(state["all_torrents"], state["library_index"],
                                         state["cross_seed_child_ids"], series["path"])
    try:
        core.execute_delete_series(state["client"], series, matched, state["all_torrents"],
                                    state["cross_seed_groups"], state["linked_ids"], state["missing_ids"])
        message = f"Série supprimée : {series['title']}"
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
            message = f"Film supprimé : {movie['title']} ({core.human_size(freed)} libéré(s))"
        else:
            core.execute_delete_movie_no_torrent(movie)
            message = f"Film supprimé : {movie['title']}"
        kind = "success"
    except Exception as e:
        core.logger.error("échec de la suppression du film %r : %s", movie["title"], e)
        message, kind = f"ÉCHEC (voir {core.LOG_PATH}) : {e}", "danger"
    return HTMLResponse(render_arr_tab("films", sort, reverse == "1", filter, message=message, message_kind=kind))
