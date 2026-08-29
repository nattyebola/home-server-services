#!/usr/bin/env python3
# Débloque les téléchargements terminés que Sonarr/Radarr refusent d'importer
# tout seuls (état `importBlocked` / `importPending` dans la file).
#
# Pourquoi c'est un script et pas trois curl : le diagnostic demande de croiser
# /api/v3/queue (qui porte le MOTIF du blocage) avec
# /api/v3/manualimport?downloadId=… (qui porte les CANDIDATS et leurs rejets),
# et ces deux vues ne disent pas la même chose. Le 2026-08-29, 5 des 7 titres
# comptés « manquants » étaient en réalité téléchargés à 100 % et coincés là,
# invisibles hors de la file.
#
# Trois familles, que `list` sépare parce qu'elles n'appellent PAS la même
# décision :
#
#   importable — aucun rejet sur le candidat, cible (épisode/film) résolue.
#     Le blocage vient de la file, pas du fichier : typiquement « matched to
#     series/movie by ID, automatic import is not possible », c'est-à-dire une
#     release dont le titre ne se parse pas et que l'arr n'a rattachée que via
#     l'historique du grab. Import direct, sans risque.
#
#   à rattacher — le candidat revient avec `episodes: []` / `movie: null`,
#     l'arr ne sait pas à QUOI le fichier correspond. Cas récurrent ici : la
#     numérotation absolue des anime, que Sonarr ne remappe pas
#     (`One Piece S01E1172` pour S23E17). Demande une décision humaine, d'où
#     `assign` et non une devinette : se tromper de cible écrase le fichier
#     d'un autre épisode.
#
#   refusé — la cible est résolue MAIS le candidat porte de vrais rejets
#     (« Not a Custom Format upgrade », « was not found in the grabbed
#     release »). Ce sont des releases qu'on ne veut pas : un pack dont les
#     épisodes sont déjà là en mieux, un doublon. Jamais importées par `apply`
#     — la bonne suite est en général de les purger de la file.
#
# Best-effort par arr : un Sonarr injoignable n'empêche pas le diagnostic
# Radarr.
import argparse
import json
import os
import re
import shlex
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ARRS = {
    "sonarr": {
        "container": "arr-sonarr-1",
        "url": "http://localhost:8989/api/v3",
        "key": "SONARR_API_KEY",
        "queue_id_field": "episodeId",
        "target_key": "episodes",
        "title_key": "series",
    },
    "radarr": {
        "container": "arr-radarr-1",
        "url": "http://localhost:7878/api/v3",
        "key": "RADARR_API_KEY",
        "queue_id_field": "movieId",
        "target_key": "movie",
        "title_key": "movie",
    },
}

# Tags scène qui impliquent une piste ou un sous-titrage français. Servent
# uniquement à SIGNALER une détection de langue douteuse : sur
# `La.Vie.est.un.Miracle.2002.MULTI...`, Radarr a proposé « Vietnamese ». Une
# langue fausse part dans le nom du fichier importé et dans le `.nfo`.
FRENCH_TAGS = re.compile(r"\b(MULTI|MULTi|VFF|VFQ|VF|TRUEFRENCH|FRENCH|VOSTFR|SUBFRENCH)\b")

# Même mécanisme que scripts/apply-arr-overrides.py et
# scripts/search-missing.py, dupliqué plutôt qu'importé (chaque script du repo
# est autonome) : la clé API passe par STDIN, jamais dans l'argv d'un
# `docker exec`, lisible dans `ps` par n'importe quel processus local.
CURL_WRITE_OUT = "\n%{http_code}"


def load_env_file(path):
    values = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key] = value
    return values


def _curl(container, api_key, args, stdin=None):
    quoted = " ".join(shlex.quote(a) for a in args)
    script = f'IFS= read -r k; exec curl -s -w {shlex.quote(CURL_WRITE_OUT)} -H "X-Api-Key: $k" {quoted}'
    payload = api_key.encode() + b"\n" + (stdin or b"")
    res = subprocess.run(["docker", "exec", "-i", container, "sh", "-c", script],
                         input=payload, capture_output=True, timeout=120)
    if res.returncode != 0:
        raise RuntimeError(f"docker exec {container} a échoué — container arrêté ?")
    body, _, code = res.stdout.decode().rpartition("\n")
    return code.strip(), body.strip()


def api_get(arr, api_key, path):
    code, body = _curl(arr["container"], api_key, [f"{arr['url']}{path}"])
    if not code.startswith("2"):
        raise RuntimeError(f"GET {path} : HTTP {code} — {body[:200] or 'réponse vide'}")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"GET {path} : réponse illisible — {body[:200]!r}")


def api_post(arr, api_key, path, obj):
    code, body = _curl(arr["container"], api_key,
                       ["-X", "POST", "-H", "Content-Type: application/json",
                        "--data", "@-", f"{arr['url']}{path}"],
                       stdin=json.dumps(obj).encode())
    if not code.startswith("2"):
        raise RuntimeError(f"POST {path} : HTTP {code} — {body[:300] or 'réponse vide'}")
    return json.loads(body) if body else None


def stuck_queue_records(arr, api_key):
    """Entrées de la file dont le téléchargement est fini mais l'import non.
    `importPending` compte autant qu'`importBlocked` : l'arr y range aussi ce
    qu'il a renoncé à rattacher, et ça ne se débloque pas tout seul."""
    queue = api_get(arr, api_key, "/queue?page=1&pageSize=1000"
                                  "&includeSeries=true&includeMovie=true&includeUnknownMovieItems=true")
    return [r for r in queue.get("records", [])
            if r.get("trackedDownloadState") in ("importBlocked", "importPending")]


def queue_reasons(record):
    """Motifs affichés par la file — c'est là, et pas sur le candidat, que se
    lit « matched by ID, automatic import is not possible »."""
    reasons = []
    for message in record.get("statusMessages") or []:
        reasons.extend(message.get("messages") or [])
    if record.get("errorMessage"):
        reasons.append(record["errorMessage"])
    # Dédup en préservant l'ordre : un pack répète le même motif par fichier.
    seen, unique = set(), []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            unique.append(reason)
    return unique


def classify(arr, candidate):
    """-> ("importable" | "assign" | "refuse", raisons)"""
    rejections = [r.get("reason", str(r)) for r in candidate.get("rejections") or []]
    target = candidate.get(arr["target_key"])
    resolved = bool(target) if arr["target_key"] == "movie" else bool(target)
    if not resolved:
        return "assign", rejections
    if rejections:
        return "refuse", rejections
    return "importable", []


def suspicious_language(candidate):
    """Vrai si le nom du fichier annonce du français que la détection n'a pas
    vu. Purement indicatif — l'appelant décide."""
    name = os.path.basename(candidate.get("path", ""))
    if not FRENCH_TAGS.search(name):
        return False
    return not any(l.get("name") == "French" for l in candidate.get("languages") or [])


def describe_target(arr, candidate):
    if arr["target_key"] == "movie":
        movie = candidate.get("movie") or {}
        return f"{movie.get('title', '?')} ({movie.get('year', '?')})" if movie else "— non résolu —"
    episodes = candidate.get("episodes") or []
    if not episodes:
        return "— non résolu —"
    series = (candidate.get("series") or {}).get("title", "?")
    return series + " " + ", ".join(f"S{e['seasonNumber']:02d}E{e['episodeNumber']:02d}" for e in episodes)


def collect(arr, api_key, only_download_id=None):
    """-> liste de dicts, un par candidat de fichier, enrichis du contexte file."""
    rows = []
    for record in stuck_queue_records(arr, api_key):
        download_id = record.get("downloadId")
        if only_download_id and download_id != only_download_id:
            continue
        # `title_key` est vide sur un « unknown movie/series item » — la file ne
        # sait pas à quoi rattacher le téléchargement. Le titre de la release est
        # alors la seule accroche lisible pour l'utilisateur.
        title = (record.get(arr["title_key"]) or {}).get("title") or record.get("title", "?")
        reasons = queue_reasons(record)
        try:
            candidates = api_get(arr, api_key,
                                 f"/manualimport?downloadId={download_id}&filterExistingFiles=false")
        except RuntimeError as e:
            rows.append({"downloadId": download_id, "title": title, "kind": "erreur",
                         "path": None, "target": None, "reasons": [str(e)], "queue_reasons": reasons})
            continue
        for candidate in candidates:
            kind, rejections = classify(arr, candidate)
            rows.append({
                "downloadId": download_id,
                "queueId": record.get("id"),
                "title": title,
                "kind": kind,
                "path": candidate.get("path"),
                "target": describe_target(arr, candidate),
                "targetIds": ([e["id"] for e in candidate.get("episodes") or []]
                              if arr["target_key"] == "episodes"
                              else (candidate.get("movie") or {}).get("id")),
                "quality": ((candidate.get("quality") or {}).get("quality") or {}).get("name"),
                "languages": [l.get("name") for l in candidate.get("languages") or []],
                "suspiciousLanguage": suspicious_language(candidate),
                "reasons": rejections,
                "queue_reasons": reasons,
                "_candidate": candidate,
            })
    return rows


def build_file(arr, candidate, download_id, target_ids=None, languages=None):
    entry = {
        "path": candidate["path"],
        "quality": candidate["quality"],
        "languages": languages if languages is not None else candidate["languages"],
        "releaseGroup": candidate.get("releaseGroup"),
        "downloadId": download_id,
    }
    if arr["target_key"] == "movie":
        entry["movieId"] = target_ids if target_ids is not None else candidate["movie"]["id"]
    else:
        entry["seriesId"] = candidate["series"]["id"]
        entry["episodeIds"] = (target_ids if target_ids is not None
                               else [e["id"] for e in candidate["episodes"]])
    return entry


def resolve_language(arr, api_key, name):
    for language in api_get(arr, api_key, "/language"):
        if language.get("name", "").lower() == name.lower():
            return [{"id": language["id"], "name": language["name"]}]
    raise RuntimeError(f"langue inconnue de {arr['container']} : {name!r}")


def print_rows(rows, arr_name):
    order = {"importable": 0, "assign": 1, "refuse": 2, "erreur": 3}
    labels = {"importable": "IMPORTABLE", "assign": "À RATTACHER",
              "refuse": "REFUSÉ", "erreur": "ERREUR"}
    if not rows:
        print(f"{arr_name} : rien en attente d'import manuel")
        return
    print(f"{arr_name} : {len(rows)} fichier(s) en attente")
    for row in sorted(rows, key=lambda r: order.get(r["kind"], 9)):
        print(f"  [{labels[row['kind']]}] {row['title']} — {row['target']}")
        print(f"      downloadId={row['downloadId']} queueId={row.get('queueId')}")
        if row["path"]:
            print(f"      {os.path.basename(row['path'])}")
        if row.get("quality"):
            print(f"      qualité={row['quality']} langue(s)={','.join(row['languages']) or '—'}"
                  + ("  ⚠ langue douteuse (tag FR dans le nom)" if row["suspiciousLanguage"] else ""))
        for reason in row["reasons"]:
            print(f"      rejet : {reason}")
        for reason in row["queue_reasons"][:2]:
            print(f"      file  : {reason}")


def cmd_list(args, env):
    payload = {}
    errors = []
    for name, arr in ARRS.items():
        api_key = env.get(arr["key"])
        if not api_key:
            errors.append(f"{name} : {arr['key']} absent de arr/.env (voir `make api-keys`)")
            continue
        try:
            rows = collect(arr, api_key, args.download_id)
        except Exception as e:
            errors.append(f"{name} : {e}")
            continue
        payload[name] = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
        if not args.json:
            print_rows(rows, name)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    for error in errors:
        print(f"ERREUR {error}", file=sys.stderr)
    return 1 if errors else 0


def cmd_apply(args, env):
    """N'importe QUE la catégorie « importable ». Les deux autres demandent une
    décision : les avaler ici reviendrait à écraser un épisode au hasard ou à
    importer un pack explicitement refusé."""
    errors, imported = [], 0
    for name, arr in ARRS.items():
        api_key = env.get(arr["key"])
        if not api_key:
            continue
        try:
            rows = [r for r in collect(arr, api_key, args.download_id) if r["kind"] == "importable"]
        except Exception as e:
            errors.append(f"{name} : {e}")
            continue
        if not rows:
            continue
        files = [build_file(arr, r["_candidate"], r["downloadId"]) for r in rows]
        print(f"{name} : {len(files)} fichier(s) à importer")
        for row in rows:
            print(f"  - {row['title']} — {row['target']}"
                  + ("   ⚠ langue détectée douteuse, voir `assign --language`" if row["suspiciousLanguage"] else ""))
        if args.dry_run:
            print("  (dry-run : rien envoyé)")
            continue
        try:
            result = api_post(arr, api_key, "/command",
                              {"name": "ManualImport", "importMode": "auto", "files": files})
            print(f"  commande {result.get('id')} : {result.get('status')}")
            imported += len(files)
        except Exception as e:
            errors.append(f"{name} : {e}")
    # Vaut aussi en dry-run : un silence complet se lit comme un échec du
    # script, pas comme « rien de la famille 1 en attente ».
    if not imported and not errors:
        print("rien à importer sans ambiguïté — voir `list` pour les autres familles")
    for error in errors:
        print(f"ERREUR {error}", file=sys.stderr)
    return 1 if errors else 0


def cmd_assign(args, env):
    """Force la cible d'un fichier que l'arr n'a pas su rattacher."""
    arr = ARRS[args.arr]
    api_key = env.get(arr["key"])
    if not api_key:
        print(f"ERREUR {arr['key']} absent de arr/.env", file=sys.stderr)
        return 1
    candidates = api_get(arr, api_key,
                         f"/manualimport?downloadId={args.download_id}&filterExistingFiles=false")
    if args.path:
        candidates = [c for c in candidates if os.path.basename(c["path"]) == os.path.basename(args.path)]
    if len(candidates) != 1:
        print(f"ERREUR {len(candidates)} candidat(s) pour ce downloadId — préciser --path",
              file=sys.stderr)
        for c in candidates:
            print(f"  {os.path.basename(c['path'])}", file=sys.stderr)
        return 1
    candidate = candidates[0]
    languages = resolve_language(arr, api_key, args.language) if args.language else None
    target = args.movie_id if arr["target_key"] == "movie" else args.episode_ids
    if target is None:
        print("ERREUR --episode-ids (sonarr) ou --movie-id (radarr) est requis", file=sys.stderr)
        return 1
    # seriesId vient du candidat : Sonarr le résout via l'historique du grab même
    # quand il échoue à trouver l'épisode. Absent = rien à quoi rattacher.
    if arr["target_key"] == "episodes" and not (candidate.get("series") or {}).get("id"):
        print("ERREUR série non résolue sur ce candidat — import manuel via l'UI",
              file=sys.stderr)
        return 1
    entry = build_file(arr, candidate, args.download_id, target_ids=target, languages=languages)
    print(f"{os.path.basename(candidate['path'])} -> {arr['target_key']}={target}"
          + (f" langue={args.language}" if args.language else ""))
    if args.dry_run:
        print("(dry-run : rien envoyé)")
        return 0
    result = api_post(arr, api_key, "/command",
                      {"name": "ManualImport", "importMode": "auto", "files": [entry]})
    print(f"commande {result.get('id')} : {result.get('status')}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Débloque les téléchargements terminés que Sonarr/Radarr refusent d'importer seuls.")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="diagnostique ce qui est coincé (défaut)")
    p_list.add_argument("--download-id", help="ne regarder que ce téléchargement")
    p_list.add_argument("--json", action="store_true", help="sortie machine")
    p_list.set_defaults(func=cmd_list)

    p_apply = sub.add_parser("apply", help="importe les fichiers sans rejet ni ambiguïté")
    p_apply.add_argument("--download-id", help="ne traiter que ce téléchargement")
    p_apply.add_argument("--dry-run", action="store_true")
    p_apply.set_defaults(func=cmd_apply)

    p_assign = sub.add_parser("assign", help="force la cible d'un fichier non rattaché")
    p_assign.add_argument("arr", choices=sorted(ARRS))
    p_assign.add_argument("--download-id", required=True)
    p_assign.add_argument("--episode-ids", type=lambda s: [int(x) for x in s.split(",")],
                          help="sonarr : ids d'épisodes, séparés par des virgules")
    p_assign.add_argument("--movie-id", type=int, help="radarr : id du film")
    p_assign.add_argument("--path", help="nom du fichier si le téléchargement en contient plusieurs")
    p_assign.add_argument("--language", help="force la langue (ex. French) si la détection est fausse")
    p_assign.add_argument("--dry-run", action="store_true")
    p_assign.set_defaults(func=cmd_assign)

    args = parser.parse_args()
    if not args.command:
        args = parser.parse_args(["list"])
    env = load_env_file(os.path.join(REPO_ROOT, "arr", ".env"))
    return args.func(args, env)


if __name__ == "__main__":
    sys.exit(main())
