#!/usr/bin/env python3
# Relance une recherche sur les épisodes/films manquants qui sont pourtant
# déjà sortis.
#
# Pourquoi ce script existe : depuis Sonarr v3, NI Sonarr NI Radarr n'ont de
# tâche planifiée de recherche des manquants (leur /api/v3/system/task ne liste
# que RSS Sync, Refresh, Import List Sync...). Une release qui n'était pas
# publiée — ou qui l'était mais rejetée par le profil — au moment où l'épisode
# est passé dans le flux RSS n'est donc PLUS JAMAIS retentée : l'item reste
# manquant indéfiniment, sans erreur ni signal. Constaté le 2026-08-29 sur
# The Simpsons S37E13/E15, manquants depuis janvier/février alors qu'une
# release approuvée était disponible chez les indexeurs à l'instant du
# diagnostic, et sur Underground (1995), ajouté un mois plus tôt et jamais
# regrabé.
#
# Contrainte dominante : le quota des indexeurs. Une recherche « tous les
# manquants, chaque nuit » est exactement le motif de rafales qui a fait
# tomber C411 le 2026-07-28. D'où trois garde-fous, dans cet ordre :
#   1. cadence hebdomadaire (voir scripts/crontab), pas quotidienne ;
#   2. plafond dur d'items par exécution (MAX_SEARCHES_PER_RUN) ;
#   3. mémoire de la dernière recherche par item, dans un fichier d'état sous
#      DATA_ROOT — on ne repasse pas sur un item avant
#      MIN_RESEARCH_INTERVAL_DAYS, et on sert d'abord les plus anciennement
#      cherchés. Le plafond n'écarte donc jamais durablement les mêmes items :
#      la rotation finit par tous les couvrir, en étalant la charge.
#
# Ne cherche jamais :
#   - un item déjà dans la file de téléchargement (en cours, ou bloqué à
#     l'import comme les 5 titres débloqués à la main le 2026-08-29) — la
#     release est déjà trouvée, la rechercher ne ferait que consommer du quota
#     et risquer un doublon ;
#   - un film dont Radarr dit `isAvailable == False`, c'est-à-dire pas encore
#     sorti selon sa `minimumAvailability` (le catalogue en contient toujours
#     une dizaine : sorties à venir, ou sorties en salle sans sortie numérique).
#     Sonarr n'a pas besoin de l'équivalent, son endpoint `wanted/missing` ne
#     renvoie que des épisodes déjà diffusés.
#
# Best-effort par arr, comme scripts/apply-arr-overrides.py : un Sonarr
# injoignable n'empêche pas la passe Radarr. Sortie non nulle si l'un des deux
# a échoué, pour que la chaîne `&&` de cron n'écrive pas son marqueur de succès
# et que la carte « Tâches planifiées » du dashboard passe au rouge.
import argparse
import datetime
import json
import os
import shlex
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SONARR_CONTAINER = "arr-sonarr-1"
SONARR_URL = "http://localhost:8989/api/v3"
RADARR_CONTAINER = "arr-radarr-1"
RADARR_URL = "http://localhost:7878/api/v3"

# Fichier d'état de la rotation, sous DATA_ROOT et pas dans le repo : c'est de
# la donnée d'exécution propre à ce déploiement, pas de la configuration.
STATE_FILE_NAME = ".search-missing-state.json"

# Plafond volontairement bas. Chaque item déclenche une requête par indexeur
# activé (5 ici), donc 12 items ~ 60 requêtes par exécution hebdomadaire.
MAX_SEARCHES_PER_RUN = 12
# Un item cherché il y a moins de deux semaines ne l'est pas à nouveau : s'il
# est resté manquant, c'est qu'aucune release acceptable n'existe, et le stock
# des indexeurs ne bouge pas assez vite pour que réessayer plus tôt paie.
MIN_RESEARCH_INTERVAL_DAYS = 14


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


# Même mécanisme que scripts/apply-arr-overrides.py, dupliqué plutôt
# qu'importé : le nom de ce fichier-là n'est pas un identifiant Python valide,
# et chaque script du repo est autonome (voir scripts/provision.py). Tout
# correctif ici vaut pour les trois.
CURL_WRITE_OUT = "\n%{http_code}"


def _curl(container, api_key, args, stdin=None):
    """Renvoie (code_http, corps). La clé API passe par STDIN, jamais en
    argument : l'argv d'un `docker exec` est lisible dans `ps` par n'importe
    quel processus local."""
    quoted = " ".join(shlex.quote(a) for a in args)
    script = f'IFS= read -r k; exec curl -s -w {shlex.quote(CURL_WRITE_OUT)} -H "X-Api-Key: $k" {quoted}'
    payload = api_key.encode() + b"\n" + (stdin or b"")
    res = subprocess.run(["docker", "exec", "-i", container, "sh", "-c", script],
                         input=payload, capture_output=True, timeout=120)
    if res.returncode != 0:
        raise RuntimeError(f"docker exec {container} a échoué — container arrêté ?")
    body, _, code = res.stdout.decode().rpartition("\n")
    return code.strip(), body.strip()


def api_get(container, base_url, api_key, path):
    code, body = _curl(container, api_key, [f"{base_url}{path}"])
    if not code.startswith("2"):
        raise RuntimeError(f"GET {path} : HTTP {code} — {body[:200] or 'réponse vide'}")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"GET {path} : réponse illisible — {body[:200]!r}")


def api_command(container, base_url, api_key, payload):
    code, body = _curl(container, api_key,
                       ["-X", "POST", "-H", "Content-Type: application/json",
                        "--data", "@-", f"{base_url}/command"],
                       stdin=json.dumps(payload).encode())
    if not code.startswith("2"):
        raise RuntimeError(f"POST /command : HTTP {code} — {body[:300] or 'réponse vide'}")
    return json.loads(body) if body else None


def load_state(data_root):
    """L'état est un confort d'étalement, pas une source de vérité : un fichier
    illisible ou absent repart de zéro (tout est candidat) plutôt que de faire
    échouer la passe."""
    if not data_root:
        return {}
    try:
        with open(os.path.join(data_root, STATE_FILE_NAME)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(data_root, state):
    if not data_root:
        return
    path = os.path.join(data_root, STATE_FILE_NAME)
    # mktemp + rename : le fichier est relu par la passe suivante, une écriture
    # interrompue le rendrait illisible (donc rotation perdue).
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def pick_candidates(items, seen, today, limit):
    """Ordonne les candidats du moins récemment cherché au plus récent (jamais
    cherché d'abord), écarte ceux vus il y a moins de MIN_RESEARCH_INTERVAL_DAYS
    et tronque au plafond. Renvoie (à_chercher, nb_écartés_trop_récents)."""
    ready, skipped = [], 0
    for key, label in items:
        last = seen.get(str(key))
        if last is None:
            ready.append((datetime.date.min, key, label))
            continue
        try:
            last_date = datetime.date.fromisoformat(last)
        except ValueError:
            ready.append((datetime.date.min, key, label))
            continue
        if (today - last_date).days < MIN_RESEARCH_INTERVAL_DAYS:
            skipped += 1
            continue
        ready.append((last_date, key, label))
    ready.sort(key=lambda t: t[0])
    return [(k, lbl) for _, k, lbl in ready[:limit]], skipped


def queued_ids(container, base_url, api_key, id_field):
    """Ids présents dans la file de téléchargement, quel qu'en soit l'état :
    un `importBlocked` compte autant qu'un `downloading`, la release est
    trouvée dans les deux cas."""
    queue = api_get(container, base_url, api_key,
                    "/queue?page=1&pageSize=1000&includeUnknownMovieItems=true")
    ids = set()
    for record in queue.get("records", []):
        value = record.get(id_field)
        if value:
            ids.add(value)
    return ids


def sonarr_candidates(api_key):
    """Épisodes monitored, déjà diffusés, sans fichier — l'endpoint
    `wanted/missing` applique lui-même le filtre « déjà diffusé »."""
    missing = api_get(SONARR_CONTAINER, SONARR_URL, api_key,
                      "/wanted/missing?page=1&pageSize=1000&monitored=true&includeSeries=true")
    in_queue = queued_ids(SONARR_CONTAINER, SONARR_URL, api_key, "episodeId")
    items = []
    for episode in missing.get("records", []):
        if episode["id"] in in_queue:
            continue
        series = episode.get("series") or {}
        items.append((episode["id"],
                      f"{series.get('title', '?')} S{episode['seasonNumber']:02d}E{episode['episodeNumber']:02d}"))
    return items


def radarr_candidates(api_key):
    missing = api_get(RADARR_CONTAINER, RADARR_URL, api_key,
                      "/wanted/missing?page=1&pageSize=1000&monitored=true")
    in_queue = queued_ids(RADARR_CONTAINER, RADARR_URL, api_key, "movieId")
    items = []
    for movie in missing.get("records", []):
        # Le seul filtre que Radarr n'applique pas lui-même : sans lui on
        # chercherait chaque semaine des films dont la sortie numérique est
        # dans plusieurs mois.
        if not movie.get("isAvailable"):
            continue
        if movie["id"] in in_queue:
            continue
        items.append((movie["id"], f"{movie.get('title', '?')} ({movie.get('year', '?')})"))
    return items


def run_arr(name, container, base_url, api_key, candidates, command_name,
            ids_field, state, today, limit, dry_run):
    """Renvoie (lignes de rapport, nb d'items réellement cherchés)."""
    seen = state.setdefault(name, {})
    picked, skipped = pick_candidates(candidates, seen, today, limit)
    lines = [f"{name} : {len(candidates)} manquant(s) éligible(s), "
             f"{skipped} écarté(s) (cherché(s) il y a moins de {MIN_RESEARCH_INTERVAL_DAYS} j), "
             f"{len(picked)} recherché(s)"]
    for _, label in picked:
        lines.append(f"  - {label}")
    if not picked:
        return lines, 0
    if dry_run:
        lines.append("  (dry-run : aucune recherche envoyée)")
        return lines, 0
    api_command(container, base_url, api_key,
                {"name": command_name, ids_field: [key for key, _ in picked]})
    for key, _ in picked:
        seen[str(key)] = today.isoformat()
    # Purge des ids qui ne sont plus manquants (importés, ou titre retiré) :
    # sans ça le fichier d'état grossit indéfiniment et garde des dates qui ne
    # correspondent plus à rien.
    still_missing = {str(key) for key, _ in candidates}
    state[name] = {k: v for k, v in seen.items() if k in still_missing}
    return lines, len(picked)


def main():
    parser = argparse.ArgumentParser(description="Relance une recherche sur les épisodes/films manquants déjà sortis.")
    parser.add_argument("--dry-run", action="store_true",
                        help="liste ce qui serait cherché sans rien envoyer aux indexeurs")
    parser.add_argument("--limit", type=int, default=MAX_SEARCHES_PER_RUN,
                        help=f"plafond d'items par arr et par exécution (défaut : {MAX_SEARCHES_PER_RUN})")
    args = parser.parse_args()

    print(f"=== {datetime.datetime.now().isoformat(timespec='seconds')} ===")
    arr_env = load_env_file(os.path.join(REPO_ROOT, "arr", ".env"))
    data_root = load_env_file(os.path.join(REPO_ROOT, ".env.shared")).get("DATA_ROOT")
    state = load_state(data_root)
    today = datetime.date.today()

    errors = []
    searched = 0
    for name, container, base_url, key_name, collect, command_name, ids_field in (
        ("sonarr", SONARR_CONTAINER, SONARR_URL, "SONARR_API_KEY",
         sonarr_candidates, "EpisodeSearch", "episodeIds"),
        ("radarr", RADARR_CONTAINER, RADARR_URL, "RADARR_API_KEY",
         radarr_candidates, "MoviesSearch", "movieIds"),
    ):
        api_key = arr_env.get(key_name)
        if not api_key:
            errors.append(f"{name} : {key_name} absent de arr/.env (voir `make api-keys`)")
            continue
        try:
            lines, count = run_arr(name, container, base_url, api_key,
                                   collect(api_key), command_name, ids_field,
                                   state, today, args.limit, args.dry_run)
            print("\n".join(lines))
            searched += count
        except Exception as e:
            errors.append(f"{name} : {e}")

    if not args.dry_run and searched:
        save_state(data_root, state)

    for error in errors:
        print(f"ERREUR {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
