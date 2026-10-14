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
# Saison entière manquante = recherche de SAISON, pas d'épisodes : une série
# sortie d'un bloc n'existe souvent qu'en packs, que la recherche d'un épisode
# ne remonte pas (Man vs. Baby, 2026-10-14 : 1 résultat par épisode, 25 pour
# la saison). Voir sonarr_candidates().
#
# Best-effort par arr, comme scripts/apply-arr-overrides.py : un Sonarr
# injoignable n'empêche pas la passe Radarr. Sortie non nulle si l'un des deux
# a échoué — y compris quand la commande de recherche, acceptée (201), finit en
# `failed` côté arr (voir wait_command()) — pour que la chaîne `&&` de cron
# n'écrive pas son marqueur de succès et que la carte « Tâches planifiées » du
# dashboard passe au rouge.
import argparse
import datetime
import json
import os
import shlex
import subprocess
import sys
import time

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


# Relecture de l'état d'une commande après son POST : un 201 veut seulement
# dire « mise en file », pas « recherche faite ». Délai court et borné — une
# recherche de 12 items sur 5 indexeurs dure couramment plus longtemps, et ce
# script n'a pas à attendre la fin : il doit seulement attraper l'échec
# immédiat (indexeurs tous désactivés, exception au démarrage de la commande).
COMMAND_WATCH_SECONDS = 60
COMMAND_POLL_SECONDS = 3
# États Servarr qui disent que la commande n'a pas fait son travail.
# `orphaned` = l'arr a redémarré pendant son exécution, elle n'ira pas au bout.
COMMAND_FAILED_STATES = ("failed", "aborted", "cancelled", "orphaned")


def wait_command(container, base_url, api_key, command, sleep=time.sleep, clock=time.monotonic):
    """Relit GET /command/{id} jusqu'à un état terminal ou COMMAND_WATCH_SECONDS.
    Renvoie (état, message). Encore `queued`/`started` au bout du délai =
    considérée acceptée : attendre la fin bloquerait cron plusieurs minutes pour
    un échec tardif rare, et l'item simplement repassera dans la rotation après
    MIN_RESEARCH_INTERVAL_DAYS s'il est toujours manquant."""
    command_id = (command or {}).get("id")
    if command_id is None:
        raise RuntimeError(f"POST /command : réponse sans id — {str(command)[:200]}")
    status = command.get("status")
    deadline = clock() + COMMAND_WATCH_SECONDS
    while status not in ("completed", *COMMAND_FAILED_STATES) and clock() < deadline:
        sleep(COMMAND_POLL_SECONDS)
        command = api_get(container, base_url, api_key, f"/command/{command_id}")
        status = command.get("status")
    return status, command.get("message") or command.get("exception") or ""


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


def last_searched(seen, ids):
    """Date de dernière recherche d'un candidat. Un candidat « saison » couvre
    plusieurs épisodes : un seul jamais cherché (ou date illisible) suffit à le
    rendre prioritaire, sinon on retient la plus ancienne."""
    dates = []
    for key in ids:
        try:
            dates.append(datetime.date.fromisoformat(seen[str(key)]))
        except (KeyError, ValueError):
            return datetime.date.min
    return min(dates)


def pick_candidates(items, seen, today, limit):
    """Ordonne les candidats du moins récemment cherché au plus récent (jamais
    cherché d'abord), écarte ceux vus il y a moins de MIN_RESEARCH_INTERVAL_DAYS
    et tronque au plafond. Un candidat est (ids, libellé, saison) : `ids` sont
    les clés du fichier d'état qu'il couvre, `saison` vaut (seriesId, n° de
    saison) pour une recherche de saison, None sinon. Renvoie (à_chercher,
    nb_écartés_trop_récents)."""
    ready, skipped = [], 0
    for item in items:
        last = last_searched(seen, item[0])
        if last != datetime.date.min and (today - last).days < MIN_RESEARCH_INTERVAL_DAYS:
            skipped += 1
            continue
        ready.append((last, item))
    ready.sort(key=lambda t: t[0])
    return [item for _, item in ready[:limit]], skipped


def queued_ids(container, base_url, api_key, id_field, unknown_param):
    """Ids présents dans la file de téléchargement, quel qu'en soit l'état :
    un `importBlocked` compte autant qu'un `downloading`, la release est
    trouvée dans les deux cas. `unknown_param` : le paramètre « inclure les
    entrées orphelines » n'a pas le même nom d'un arr à l'autre
    (`includeUnknownSeriesItems` / `includeUnknownMovieItems`) — la variante
    Radarr était passée aussi à Sonarr, qui l'ignorait sans erreur."""
    queue = api_get(container, base_url, api_key,
                    f"/queue?page=1&pageSize=1000&{unknown_param}")
    ids = set()
    for record in queue.get("records", []):
        value = record.get(id_field)
        if value:
            ids.add(value)
    return ids


def sonarr_candidates(api_key):
    """Épisodes monitored, déjà diffusés, sans fichier — l'endpoint
    `wanted/missing` applique lui-même le filtre « déjà diffusé ».

    Une saison entièrement diffusée, sans aucun fichier, dont il manque au
    moins deux épisodes devient UN candidat « saison » (SeasonSearch) : c'est le
    cas où seuls des packs existent, et une requête par indexeur au lieu d'une
    par épisode. La recherche de saison remonte aussi les épisodes isolés. Une
    saison en cours de diffusion reste épisode par épisode : un pack n'y
    passerait pas (Sonarr refuse un pack dont tout n'est pas diffusé)."""
    missing = api_get(SONARR_CONTAINER, SONARR_URL, api_key,
                      "/wanted/missing?page=1&pageSize=1000&monitored=true&includeSeries=true")
    in_queue = queued_ids(SONARR_CONTAINER, SONARR_URL, api_key, "episodeId",
                          "includeUnknownSeriesItems=true")
    # `includeSeries` ne porte pas les statistiques des saisons, d'où /series.
    season_stats = {(s["id"], season["seasonNumber"]): season.get("statistics") or {}
                    for s in api_get(SONARR_CONTAINER, SONARR_URL, api_key, "/series")
                    for season in s.get("seasons", [])}
    by_season = {}
    for episode in missing.get("records", []):
        if episode["id"] in in_queue:
            continue
        by_season.setdefault((episode["seriesId"], episode["seasonNumber"]), []).append(episode)
    items = []
    for season, episodes in by_season.items():
        title = (episodes[0].get("series") or {}).get("title", "?")
        stats = season_stats.get(season, {})
        if (len(episodes) >= 2 and stats.get("episodeFileCount", 1) == 0
                and not stats.get("nextAiring")):
            items.append(([e["id"] for e in episodes],
                          f"{title} S{season[1]:02d} (saison, {len(episodes)} ép.)", season))
            continue
        for e in episodes:
            items.append(([e["id"]], f"{title} S{e['seasonNumber']:02d}E{e['episodeNumber']:02d}", None))
    return items


def radarr_candidates(api_key):
    missing = api_get(RADARR_CONTAINER, RADARR_URL, api_key,
                      "/wanted/missing?page=1&pageSize=1000&monitored=true")
    in_queue = queued_ids(RADARR_CONTAINER, RADARR_URL, api_key, "movieId",
                          "includeUnknownMovieItems=true")
    items = []
    for movie in missing.get("records", []):
        # Le seul filtre que Radarr n'applique pas lui-même : sans lui on
        # chercherait chaque semaine des films dont la sortie numérique est
        # dans plusieurs mois.
        if not movie.get("isAvailable"):
            continue
        if movie["id"] in in_queue:
            continue
        items.append(([movie["id"]], f"{movie.get('title', '?')} ({movie.get('year', '?')})", None))
    return items


def run_arr(name, container, base_url, api_key, candidates, command_name,
            ids_field, state, today, limit, dry_run):
    """Renvoie (lignes de rapport, nb d'items réellement cherchés, erreur ou
    None). L'erreur est rendue plutôt que levée pour que les lignes du rapport
    (ce qui était visé) restent imprimées à côté."""
    seen = state.setdefault(name, {})
    picked, skipped = pick_candidates(candidates, seen, today, limit)
    lines = [f"{name} : {len(candidates)} manquant(s) éligible(s), "
             f"{skipped} écarté(s) (cherché(s) il y a moins de {MIN_RESEARCH_INTERVAL_DAYS} j), "
             f"{len(picked)} recherché(s)"]
    for _, label, _ in picked:
        lines.append(f"  - {label}")
    if not picked:
        return lines, 0, None
    if dry_run:
        lines.append("  (dry-run : aucune recherche envoyée)")
        return lines, 0, None
    # Les items simples partent en une seule commande ; chaque saison a la
    # sienne (SeasonSearch ne prend qu'une saison).
    batches = []
    singles = [item for item in picked if item[2] is None]
    if singles:
        batches.append(({"name": command_name, ids_field: [i for item in singles for i in item[0]]}, singles))
    for item in picked:
        if item[2] is not None:
            batches.append(({"name": "SeasonSearch", "seriesId": item[2][0],
                             "seasonNumber": item[2][1]}, [item]))
    searched, errors = 0, []
    for payload, items in batches:
        command = api_command(container, base_url, api_key, payload)
        status, message = wait_command(container, base_url, api_key, command)
        lines.append(f"  commande {payload['name']} {command.get('id')} : {status}")
        # Échec = items NON marqués : les marquer les aurait sortis de la rotation
        # 14 jours pour une recherche qui n'a jamais eu lieu.
        if status in COMMAND_FAILED_STATES:
            errors.append(f"commande {payload['name']} {command.get('id')} : {status} — {message[:200] or 'sans message'}")
            continue
        for ids, _, _ in items:
            for key in ids:
                seen[str(key)] = today.isoformat()
        searched += len(items)
    # Purge des ids qui ne sont plus manquants (importés, ou titre retiré) :
    # sans ça le fichier d'état grossit indéfiniment et garde des dates qui ne
    # correspondent plus à rien.
    still_missing = {str(key) for ids, _, _ in candidates for key in ids}
    state[name] = {k: v for k, v in seen.items() if k in still_missing}
    return lines, searched, "; ".join(errors) or None


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
            lines, count, error = run_arr(name, container, base_url, api_key,
                                          collect(api_key), command_name, ids_field,
                                          state, today, args.limit, args.dry_run)
            print("\n".join(lines))
            searched += count
            if error:
                errors.append(f"{name} : {error}")
        except Exception as e:
            errors.append(f"{name} : {e}")

    if not args.dry_run and searched:
        save_state(data_root, state)

    for error in errors:
        print(f"ERREUR {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
