#!/usr/bin/env python3
# Remplace la release en place d'un film, d'un épisode ou d'une saison par une
# autre, choisie dans la recherche interactive de Sonarr/Radarr. Le choix de la
# release reste au skill (voir SKILL.md) : ce script trouve, liste, grabe et
# suit le téléchargement, il ne décide rien.
#
#   find TEXTE                         titres du catalogue proches de TEXTE
#   releases --movie-id N              fichier en place + releases numérotées
#   releases --series-id N --season S [--episode E]
#   grab N                             grabe la release n° N de la dernière liste
#   watch                              suit la file jusqu'à l'import ou au blocage
#
# Pourquoi un script : avec « jamais d'upgrade » (upgradeAllowed: false), TOUTE
# release d'un titre déjà présent revient rejetée. Il faut séparer les rejets
# qui ne tiennent qu'au fichier en place (un remplacement les lève) des vrais
# (langue, taille, 0 seeder), ce que l'UI ne fait pas.
import argparse
import difflib
import importlib.util
import json
import os
import re
import sys
import time
import unicodedata

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# Accès API de manual-import.py (clé par STDIN, docker exec), chargé comme
# sans-fichier charge search-missing.py : le nom du fichier n'est pas un
# identifiant Python valide.
spec = importlib.util.spec_from_file_location(
    "mi", os.path.join(REPO_ROOT, "scripts", "manual-import.py"))
mi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mi)
ARRS = mi.ARRS

# Rejets qui ne disent rien de la release elle-même, seulement qu'un fichier
# est déjà là (textes relevés le 2026-10-19 : « Existing file meets cutoff »,
# « Existing file on disk and Quality Profile 'Anime' does not allow
# upgrades », « Not an upgrade for existing … »). Le délai de 24 h aussi : un
# grab manuel ne l'attend pas.
FILE_ONLY_REJECTION = re.compile(
    r"existing file|does not allow upgrades|not an? .*upgrade|meets cutoff|delay", re.I)

# Rejet d'une release qui vise un autre épisode que celui demandé : la
# recherche par épisode remonte tous ceux de la série.
OTHER_TARGET_REJECTION = re.compile(r"wasn't requested", re.I)

# Repères d'épisode/saison retirés de la requête de `find` : « goblin ep 2 »
# doit chercher « goblin ». Un nombre seul reste (« dune 2 » est un titre).
EPISODE_MARKERS = re.compile(
    r"\b(?:s\d{1,2}(?:\s*e\d{1,4})?|\d{1,2}x\d{1,4}"
    r"|(?:ep|e|episode|épisode|saison|season)\s*\d{1,4})\b", re.I)

WATCH_POLL_SECONDS = 15

# Dernière liste de `releases`, relue par `grab` et `watch`. Le guid d'une
# release porte la clé API de l'indexeur (`…&apikey=…`) : il ne doit ni
# s'afficher ni passer en argument (argv lisible dans `ps`), d'où ce fichier
# en 600 et un simple numéro en ligne de commande.
CACHE_FILE = os.path.join(os.path.expanduser("~/.cache"), "remplacer-release", "derniere-recherche.json")


def save_cache(data):
    os.makedirs(os.path.dirname(CACHE_FILE), mode=0o700, exist_ok=True)
    fd = os.open(CACHE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False)


def load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        raise RuntimeError("aucune liste en cache : lancer `releases` d'abord")


def keys():
    env = mi.load_env_file(os.path.join(REPO_ROOT, "arr", ".env"))
    return {name: env.get(arr["key"]) for name, arr in ARRS.items()}


def get(arr_name, path):
    return mi.api_get(ARRS[arr_name], keys()[arr_name], path)


def normalize(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def release_key(title):
    """Titre de release réduit pour reconnaître la MÊME release sous deux
    graphies (points ou espaces, suffixe d'indexeur entre parenthèses).
    REPACK/PROPER font partie de la clé : c'est justement une autre release."""
    title = re.sub(r"\s*\((?:[^()]*,){1,}[^()]*\)\s*$", "", title or "")
    return normalize(title)


def similarity(query, title):
    q, t = normalize(query), normalize(title)
    if not q or not t:
        return 0.0
    ratio = difflib.SequenceMatcher(None, q, t).ratio()
    # Une requête courte (« goblin ») contenue dans le titre doit gagner sur un
    # titre de longueur voisine mais sans rapport.
    words = q.split()
    contained = sum(w in t.split() for w in words) / len(words)
    return max(ratio, 0.5 + 0.5 * contained if contained == 1 else contained * 0.8)


def cmd_find(args):
    query = EPISODE_MARKERS.sub(" ", args.text)
    rows = []
    for s in get("sonarr", "/series"):
        titles = [s["title"]] + [a["title"] for a in s.get("alternateTitles") or []]
        stats = s.get("statistics") or {}
        rows.append((max(similarity(query, t) for t in titles), "sonarr", s["id"],
                     f"{s['title']} ({s.get('year')}) [{s['seriesType']}] "
                     f"{stats.get('episodeFileCount', 0)} fichier(s), saisons "
                     + ",".join(str(x["seasonNumber"]) for x in s["seasons"] if x["seasonNumber"] > 0)))
    for m in get("radarr", "/movie"):
        titles = [m["title"], m.get("originalTitle")] + [a["title"] for a in m.get("alternateTitles") or []]
        rows.append((max(similarity(query, t) for t in titles if t), "radarr", m["id"],
                     f"{m['title']} ({m.get('year')}) "
                     + ("fichier présent" if m.get("hasFile") else "SANS fichier")))
    rows.sort(key=lambda r: -r[0])
    for score, arr_name, item_id, label in rows[:args.limit]:
        print(f"{score:.2f}  {arr_name} id={item_id}  {label}")
    return 0


def origin_download_id(arr_name, history_path, scene_name):
    """Hash du torrent dont vient le fichier en place : dernier import dont le
    titre source est le sceneName du fichier. Sert à dire à l'utilisateur quel
    torrent continue de seeder sans fichier en bibliothèque après le remplacement."""
    records = get(arr_name, history_path)
    records = records.get("records", records) if isinstance(records, dict) else records
    for r in sorted(records, key=lambda r: r["date"], reverse=True):
        if r["eventType"] == "downloadFolderImported" and r.get("sourceTitle") == scene_name:
            return r.get("downloadId")
    return None


def current_files(args):
    """-> (arr, fichiers en place [dict], chemin de recherche interactive)."""
    if args.movie_id:
        movie = get("radarr", f"/movie/{args.movie_id}")
        files = [movie["movieFile"]] if movie.get("movieFile") else []
        for f in files:
            f["_target"] = f"{movie['title']} ({movie.get('year')})"
            f["_origin"] = origin_download_id(
                "radarr", f"/history/movie?movieId={args.movie_id}", f.get("sceneName"))
        return "radarr", files, f"/release?movieId={args.movie_id}"
    episodes = [e for e in get("sonarr", f"/episode?seriesId={args.series_id}&seasonNumber={args.season}")
                if args.episode is None or e["episodeNumber"] == args.episode]
    if not episodes:
        raise RuntimeError(f"aucun épisode S{args.season:02d}"
                           + (f"E{args.episode:02d}" if args.episode else "") + " dans cette série")
    by_file = {}
    for e in episodes:
        if e.get("episodeFileId"):
            by_file.setdefault(e["episodeFileId"], []).append(e)
    files = []
    for file_id, eps in sorted(by_file.items()):
        f = get("sonarr", f"/episodefile/{file_id}")
        f["_target"] = ", ".join(f"S{e['seasonNumber']:02d}E{e['episodeNumber']:02d} id={e['id']}" for e in eps)
        f["_origin"] = origin_download_id(
            "sonarr", f"/history/series?seriesId={args.series_id}", f.get("sceneName"))
        files.append(f)
    if args.episode is not None:
        path = f"/release?episodeId={episodes[0]['id']}"
    else:
        # Par saison : seule cette recherche remonte les packs (voir sans-fichier).
        path = f"/release?seriesId={args.series_id}&seasonNumber={args.season}"
    return "sonarr", files, path


def classify(release, current_keys, season_mode):
    if release_key(release["title"]) in current_keys:
        return "identique"
    if season_mode and not release.get("fullSeason"):
        return "hors cible"  # épisode isolé dans une recherche de saison
    if not season_mode and release.get("fullSeason"):
        return "hors cible"  # pack : remplacerait toute la saison
    rejections = release.get("rejections") or []
    if any(OTHER_TARGET_REJECTION.search(r) for r in rejections):
        return "hors cible"
    real = [r for r in rejections if not FILE_ONLY_REJECTION.search(r)]
    return "rejetée" if real else "candidate"


def size(n):
    return f"{(n or 0) / 1e9:.2f} Go"


def languages(item):
    return "+".join(l["name"] for l in item.get("languages") or []) or "?"


def cmd_releases(args):
    arr_name, files, search_path = current_files(args)
    season_mode = arr_name == "sonarr" and args.episode is None
    print(f"== En place ({arr_name})")
    if not files:
        print("  aucun fichier : rien à remplacer, voir le skill sans-fichier")
    for f in files:
        print(f"  {f['_target']} | {f.get('sceneName') or f.get('relativePath')}\n"
              f"    {f['quality']['quality']['name']} {languages(f)} score={f.get('customFormatScore')} "
              f"{size(f.get('size'))} torrent={f['_origin'] or '?'}")
    current_keys = {release_key(f.get("sceneName")) for f in files if f.get("sceneName")}
    releases = get(arr_name, search_path)
    for r in releases:
        r["_kind"] = classify(r, current_keys, season_mode)
    order = {"candidate": 0, "rejetée": 1, "identique": 2, "hors cible": 3}
    releases.sort(key=lambda r: (order[r["_kind"]], -(r.get("customFormatScore") or 0),
                                 -(r.get("seeders") or 0)))
    save_cache({"arr": arr_name, "movieId": args.movie_id, "seriesId": args.series_id,
                "searchedAt": time.time(),
                "releases": [{k: r.get(k) for k in ("title", "guid", "indexerId")} for r in releases]})
    print(f"\n== {len(releases)} release(s)")
    for n, r in enumerate(releases, 1):
        if r["_kind"] == "hors cible" and not args.all:
            continue
        line = (f"#{n} [{r['_kind']}] score={r.get('customFormatScore')} seed={r.get('seeders')} "
                f"{size(r.get('size'))} {languages(r)} [{r.get('indexer')}]\n"
                f"    {r['title']}")
        if r["_kind"] == "rejetée":
            real = [x for x in r["rejections"] if not FILE_ONLY_REJECTION.search(x)]
            line += "\n    rejet : " + "; ".join(x[:110] for x in real)
        print(line)
    return 0


# L'arr ne garde les résultats d'une recherche interactive que 30 min : au-delà,
# POST /release répond 404 sur le guid.
RELEASE_CACHE_SECONDS = 30 * 60


def cmd_grab(args):
    cache = load_cache()
    if time.time() - cache["searchedAt"] > RELEASE_CACHE_SECONDS:
        raise RuntimeError("liste de plus de 30 min, l'arr l'a oubliée : relancer `releases`")
    if not 1 <= args.n <= len(cache["releases"]):
        raise RuntimeError(f"pas de release n° {args.n}")
    release = cache["releases"][args.n - 1]
    payload = {"guid": release["guid"], "indexerId": release["indexerId"]}
    # movieId rattache au film une release dont Radarr ne reconnaît pas le titre.
    if cache["movieId"]:
        payload["movieId"] = cache["movieId"]
    if args.episode_ids:
        payload["seriesId"] = cache["seriesId"]
        payload["episodeIds"] = args.episode_ids
    mi.api_post(ARRS[cache["arr"]], keys()[cache["arr"]], "/release", payload)
    cache["grabbed"] = release["title"]
    save_cache(cache)
    print(f"grab envoyé : {release['title']}")
    return 0


def cmd_watch(args):
    cache = load_cache()
    if not cache.get("grabbed"):
        raise RuntimeError("rien de grabé depuis la dernière liste")
    arr_name = cache["arr"]
    arr = ARRS[arr_name]
    wanted = release_key(cache["grabbed"])
    deadline = time.monotonic() + args.timeout
    seen = None
    while True:
        queue = get(arr_name, f"/queue?page=1&pageSize=1000&{arr['unknown_param']}")
        records = [r for r in queue.get("records", []) if release_key(r.get("title")) == wanted]
        if records:
            seen = records[0]
            r = seen
            messages = "; ".join(m for s in r.get("statusMessages") or [] for m in s.get("messages") or [])
            state = r.get("trackedDownloadState")
            print(f"{time.strftime('%H:%M:%S')} {state} reste={size(r.get('sizeleft'))} "
                  f"downloadId={r.get('downloadId')} {messages}", flush=True)
            if state in ("importBlocked", "importPending", "failedPending"):
                print("BLOQUÉ : voir SKILL.md, étape 5")
                return 2
        elif seen:
            print("sorti de la file : vérifier l'import (SKILL.md, étape 6)")
            return 0
        else:
            print(f"{time.strftime('%H:%M:%S')} pas encore dans la file", flush=True)
        if time.monotonic() > deadline:
            print("délai dépassé, toujours en cours")
            return 3
        time.sleep(WATCH_POLL_SECONDS)


def main():
    parser = argparse.ArgumentParser(description="Remplace la release en place par une autre.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("find")
    p.add_argument("text")
    p.add_argument("--limit", type=int, default=6)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("releases")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--movie-id", type=int)
    g.add_argument("--series-id", type=int)
    p.add_argument("--season", type=int)
    p.add_argument("--episode", type=int)
    p.add_argument("--all", action="store_true", help="montrer aussi les releases hors cible")
    p.set_defaults(func=cmd_releases)

    p = sub.add_parser("grab")
    p.add_argument("n", type=int, help="numéro affiché par `releases`")
    p.add_argument("--episode-ids", type=lambda s: [int(x) for x in s.split(",")],
                   help="sonarr : force les épisodes visés (titre mal numéroté)")
    p.set_defaults(func=cmd_grab)

    p = sub.add_parser("watch")
    p.add_argument("--timeout", type=int, default=540, help="secondes (défaut 9 min)")
    p.set_defaults(func=cmd_watch)

    args = parser.parse_args()
    if args.command == "releases" and args.series_id and args.season is None:
        parser.error("--season est requis avec --series-id")
    try:
        return args.func(args)
    except RuntimeError as e:
        print(f"ERREUR {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
