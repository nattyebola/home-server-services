# Logique métier partagée par tui.py (TUI curses, `make clearr`), webapp.py
# (service web FastAPI/HTMX, clearr.${DOMAIN}) et cli.py (mode non-interactif
# delete-by-inode, utilisé par le skill anime-vf). Aucun des trois n'a de
# logique de matching/suppression en propre — tout passe par ici.
#
# Anciennement scripts/torrent-cleanup.py (TUI seule, tournait sur l'hôte via
# `docker exec <container> curl ...` pour atteindre Transmission/Sonarr/
# Radarr/Prowlarr). Ce module tourne maintenant DANS un conteneur (service
# `clearr`, arr/docker-compose.yml) qui rejoint directement les réseaux
# `vpn-internal` et `default` (arr) : plus de docker exec, de simples requêtes
# HTTP vers les noms de service Docker (transmission-vpn/sonarr/radarr/
# prowlarr). Monte ${DATA_ROOT}:/data_root comme sonarr/radarr (même mount
# unique, même raison : link() refuse de traverser deux montages Docker
# distincts même sur le même disque physique, voir CLAUDE.md) — core.py et
# arr partagent donc désormais le même référentiel de chemins, plus besoin de
# traduire host <-> conteneur arr (host_to_arr_path/arr_path_to_host de
# l'ancien script ont disparu).
#
# Marqueur 'M' (torrent dont le fichier a disparu du disque, ex. supprimé
# manuellement hors de cet outil) + purge groupée côté Transmission — ajouté
# le 2026-07-28 après un rattrapage cross-seed qui en a fait remonter 6 d'un
# coup, tous antérieurs à la stack arr.
#
# Arbre cross-seed : les torrents qui partagent au moins un fichier réel
# (même (dev, inode), voir build_cross_seed_groups) sont une seule et même
# release injectée sur plusieurs indexeurs par arr/cross-seed — regroupés
# sous un parent (le téléchargement d'origine, pas une entrée
# .cross-seed-links/) avec un compteur repliable plutôt que listés comme des
# torrents indépendants sans rapport apparent entre eux. Supprimer le parent
# supprime aussi ses enfants cross-seed (voir do_delete/apply_deletion) : une
# fois le téléchargement d'origine parti, ses cross-seeds n'ont plus rien à
# seeder. Supprimer un enfant seul ne touche ni au parent ni aux autres
# cross-seeds.
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

DATA_ROOT = "/data_root"
LIBRARY_ROOT = os.path.join(DATA_ROOT, "library")
TRANSMISSION_DATA_ROOT = os.path.join(DATA_ROOT, ".transmission", "data")
LOG_PATH = os.path.join(DATA_ROOT, ".clearr.log")

RPC_URL = "http://transmission-vpn:9091/transmission/rpc"

PROWLARR_URL = "http://prowlarr:9696"
PROWLARR_INDEXER_URL = PROWLARR_URL + "/api/v1/indexer"
PROWLARR_API_KEY = os.environ.get("PROWLARR_API_KEY")

RADARR_URL = "http://radarr:7878"
RADARR_API_KEY = os.environ.get("RADARR_API_KEY")

SONARR_URL = "http://sonarr:8989"
SONARR_API_KEY = os.environ.get("SONARR_API_KEY")

# Un seul fichier de log, pensé pour du debug/maintenance a posteriori : DEBUG
# = détail RPC/lookups (utile pour comprendre un comportement inattendu),
# INFO = actions normales (lancement, suppressions), WARNING = anomalie
# récupérée sans bloquer (fichier déjà absent, échec de suppression isolé),
# ERROR = échec qui empêche l'action demandée.
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("clearr")


def parse_tracker_aliases(raw):
    """TRACKER_ALIASES=domaine1=Nom1,domaine2=Nom1,... (arr/.env, voir
    .env.example) — trackers publics génériques qu'aucune API ne permet de
    rattacher à un indexeur (Prowlarr n'expose que les domaines du site,
    voir build_prowlarr_tracker_map, pas ceux du tracker BitTorrent
    lui-même embarqué dans les .torrent). Propre à ce déploiement (quels
    trackers publics tel ou tel indexeur utilise en pratique) : en config
    plutôt qu'en dur dans un script versionné sur un repo public."""
    aliases = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        domain, _, name = pair.partition("=")
        domain, name = domain.strip(), name.strip()
        if domain and name:
            aliases[domain] = name
    return aliases


TRACKER_ALIASES = parse_tracker_aliases(os.environ.get("TRACKER_ALIASES", ""))


def log_deletion(torrent, host_files, lib_matches, arr_plan):
    lines = [f"suppression torrent: {torrent['name']} (id={torrent['id']})"]
    lines.append(f"  transmission: {len(host_files)} fichier(s), {human_size(sum(s for _, s in host_files))}")
    for path, size in host_files:
        lines.append(f"    - {path} ({human_size(size)})")
    if lib_matches:
        lines.append(f"  library: {len(lib_matches)} fichier(s), {human_size(sum(s for _, s in lib_matches))}")
        for path, size in lib_matches:
            lines.append(f"    - {path} ({human_size(size)})")
    else:
        lines.append("  library: aucune correspondance (jamais importé, ou déjà supprimé)")
    if arr_plan:
        lines.append(f"  arr: {len(arr_plan)} action(s)")
        for action in arr_plan:
            lines.append(f"    - {action['description']}")
    logger.info("\n".join(lines))


class TransmissionClient:
    def __init__(self):
        self.session_id = None

    def call(self, method, arguments=None):
        logger.debug("RPC %s(%s)", method, arguments)
        payload = json.dumps({"method": method, "arguments": arguments or {}}).encode()
        headers = {"Content-Type": "application/json"}
        if self.session_id:
            headers["X-Transmission-Session-Id"] = self.session_id
        req = urllib.request.Request(RPC_URL, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 409:
                session_id = e.headers.get("X-Transmission-Session-Id")
                if not session_id:
                    logger.error("RPC %s: 409 sans X-Transmission-Session-Id", method)
                    raise RuntimeError(f"Transmission RPC 409 sans session id sur {method}")
                self.session_id = session_id
                logger.debug("nouveau X-Transmission-Session-Id obtenu, on rejoue %s", method)
                return self.call(method, arguments)
            logger.error("RPC %s: HTTP %s", method, e.code)
            raise RuntimeError(f"Transmission RPC HTTP {e.code} sur {method}")
        except (urllib.error.URLError, OSError) as e:
            logger.error("RPC %s: transmission-vpn injoignable (%s) — container arrêté ?", method, e)
            raise RuntimeError(f"transmission-vpn injoignable sur {method} : {e}")
        if not body.strip():
            logger.error("RPC %s: réponse vide", method)
            raise RuntimeError(f"réponse Transmission vide sur {method}")
        data = json.loads(body)
        if data.get("result") != "success":
            logger.error("RPC %s: erreur Transmission: %s", method, data)
            raise RuntimeError(f"Transmission RPC error: {data}")
        return data["arguments"]

    def list_torrents(self):
        fields = ["id", "name", "addedDate", "downloadDir", "totalSize",
                  "files", "trackerStats", "percentDone", "uploadRatio"]
        torrents = self.call("torrent-get", {"fields": fields})["torrents"]
        logger.info("liste torrents récupérée : %d torrents", len(torrents))
        return torrents

    def remove_torrent(self, torrent_id):
        self.call("torrent-remove", {"ids": [torrent_id], "delete-local-data": True})
        logger.info("torrent id=%s supprimé côté Transmission (delete-local-data)", torrent_id)


def container_path_to_host(container_path):
    if not container_path.startswith("/data"):
        raise ValueError(f"chemin inattendu (hors /data): {container_path}")
    return container_path.replace("/data", TRANSMISSION_DATA_ROOT, 1)


def tracker_host(torrent):
    stats = torrent.get("trackerStats") or []
    if not stats:
        return "?"
    hosts = []
    for t in stats:
        host = urllib.parse.urlparse(t.get("announce", "")).hostname
        if host and host not in hosts:
            hosts.append(host)
    return ",".join(hosts) if hosts else "?"


def base_domain(hostname):
    """Domaine de base (2 derniers labels : www.yggreborn.org -> yggreborn.org,
    tracker.yggreborn.org -> yggreborn.org) — heuristique suffisante ici (pas
    de TLD à deux parties type .co.uk chez les indexeurs concernés), pas une
    vraie public-suffix-list. Nécessaire car le domaine d'annonce BitTorrent
    réel et le(s) domaine(s) de site listés par Prowlarr (indexerUrls/
    legacyUrls) sont souvent des sous-domaines FRÈRES du même domaine de
    base, pas l'un suffixe de l'autre : `tracker.yggreborn.org` (annonce) vs
    `www.yggreborn.org` (indexerUrls) — un simple `hostname.endswith(domain)`
    échoue alors qu'ils dérivent bien du même indexeur."""
    parts = hostname.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else hostname


def _http_json(method, url, api_key=None, json_body=None, timeout=15):
    headers = {}
    if api_key:
        headers["X-Api-Key"] = api_key
    data = None
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def build_prowlarr_tracker_map():
    """domaine de base (ex. tr4ker.net) -> nom d'indexeur Prowlarr (ex.
    TR4KER). Si le tracker BT utilise un domaine de base sans rapport avec
    le(s) site(s) Prowlarr (mutualisé entre plusieurs indexeurs — ex. les
    trackers publics génériques que Nyaa.si ajoute en plus du sien,
    open.stealth.si/opentrackr.org/exodus.desync.com/tracker.torrent.eu.org,
    aucun n'appartenant à Nyaa en propre), aucun match possible : on retombe
    sur le hostname brut plutôt que d'inventer un nom — cf. base_domain()."""
    if not PROWLARR_API_KEY:
        logger.warning("PROWLARR_API_KEY absente — noms de tracker non résolus")
        return {}
    try:
        raw = _http_json("GET", PROWLARR_INDEXER_URL, api_key=PROWLARR_API_KEY, timeout=15)
    except (urllib.error.URLError, OSError) as e:
        logger.warning("Prowlarr injoignable (%s) — noms de tracker non résolus", e)
        return {}
    try:
        indexers = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("réponse Prowlarr illisible — noms de tracker non résolus")
        return {}
    domain_map = {}
    for idx in indexers:
        name = idx.get("name")
        if not name:
            continue
        for url in (idx.get("indexerUrls") or []) + (idx.get("legacyUrls") or []):
            host = urllib.parse.urlparse(url).hostname
            if host:
                domain_map[base_domain(host.lower())] = name
    logger.info("noms de tracker Prowlarr chargés : %d indexeur(s), %d domaine(s)",
                len(indexers), len(domain_map))
    return domain_map


def resolve_tracker_name(hostname, tracker_map):
    """Renvoie (nom, officiel) — officiel=True si le hostname se rattache à
    un indexeur réellement configuré dans Prowlarr, False s'il retombe sur le
    hostname brut (tracker public embarqué dans le .torrent, pas un indexeur
    qu'on interroge nous-mêmes) : voir tracker_display, qui replie tous les
    non-officiels sous un seul libellé."""
    # TRACKER_ALIASES (arr/.env) d'abord : couvre les trackers publics
    # génériques qu'aucune API ne permet de rattacher à un indexeur (voir
    # parse_tracker_aliases). Matché en exact, jamais via base_domain — un
    # domaine public à 2 labels type eu.org serait un bien pire faux positif
    # que l'inverse.
    if hostname in TRACKER_ALIASES:
        return TRACKER_ALIASES[hostname], True
    name = tracker_map.get(base_domain(hostname))
    if name:
        return name, True
    return hostname, False


OTHER_TRACKER_LABEL = "Autre"


def tracker_display(torrent, tracker_map):
    """Renvoie (libellé, hostnames_non_officiels) pour la colonne TRACKER.

    Tout host non rattaché à un indexeur Prowlarr est replié sous un seul
    "Autre" plutôt que listé tel quel : une release peut embarquer une
    vingtaine de trackers publics de secours en plus du sien (constaté le
    2026-08-02 sur un post YggReborn ancien, 24 hosts d'annonce), ce qui
    débordait complètement la colonne — en TUI comme en web. Le détail reste
    accessible : les hostnames bruts sont renvoyés à part pour alimenter un
    tooltip côté web (`title=` natif — pas un tooltip Bootstrap, qui
    nécessiterait Popper, non vendoré, voir CLAUDE.md)."""
    hosts = tracker_host(torrent)
    if hosts == "?":
        return "?", ""
    # dict.fromkeys plutôt qu'un set : dédup en préservant l'ordre. Via
    # TRACKER_ALIASES, plusieurs hosts d'un même torrent peuvent résoudre
    # vers le même nom (ex. les 5 trackers publics de Nyaa) — sans ça, la
    # colonne affichait "Nyaa.si,Nyaa.si,Nyaa.si,Nyaa.si,Nyaa.si".
    names, others = [], []
    for host in hosts.split(","):
        name, official = resolve_tracker_name(host, tracker_map)
        (names if official else others).append(name)
    labels = list(dict.fromkeys(names))
    others = list(dict.fromkeys(others))
    if others:
        labels.append(OTHER_TRACKER_LABEL)
    return ",".join(labels), ",".join(others)


def arr_api(base_url, api_key, method, path, params=None, json_body=None):
    """GET/PUT/DELETE générique vers Sonarr/Radarr, HTTP direct (réseau
    `default` du projet arr, cf. commentaire d'en-tête). Renvoie None sur
    tout échec (clé absente, service injoignable, timeout, JSON illisible) —
    chaque appelant doit dégrader proprement plutôt que planter (best-effort,
    comme build_prowlarr_tracker_map)."""
    if not api_key:
        return None
    url = base_url + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        raw = _http_json(method, url, api_key=api_key, json_body=json_body, timeout=15)
    except urllib.error.HTTPError as e:
        logger.warning("arr_api %s %s: HTTP %s", method, path, e.code)
        return None
    except (urllib.error.URLError, OSError) as e:
        logger.warning("arr_api %s %s: injoignable (%s)", method, path, e)
        return None
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("arr_api %s %s: réponse illisible: %r", method, path, raw[:300])
        return None


def plan_radarr_deletion(paths):
    """paths: set de chemins (vus par les conteneurs arr, identiques aux
    chemins internes de clearr — même mount /data_root) déjà identifiés comme
    présents dans library/. Renvoie (plan, chemins_matchés) — les chemins
    matchés sont retirés du set par l'appelant avant de tenter le matching
    Sonarr (un fichier ne peut être qu'un film OU un épisode)."""
    if not RADARR_API_KEY or not paths:
        return [], set()
    movies = arr_api(RADARR_URL, RADARR_API_KEY, "GET", "/api/v3/movie")
    if not movies:
        return [], set()
    plan, matched = [], set()
    for m in movies:
        mf = m.get("movieFile")
        if mf and mf.get("path") in paths:
            matched.add(mf["path"])
            plan.append({
                "description": f'Radarr : "{m["title"]}" retiré complètement (+ exclusion de liste)',
                "kind": "radarr_delete",
                "movie_id": m["id"],
                "title": m["title"],
            })
    return plan, matched


def plan_sonarr_unmonitor(paths):
    """Regroupe par série (préfixe de series.path), puis par saison. Une
    saison n'est désactivée en bloc que si TOUS ses fichiers connus de
    Sonarr sont dans ce qu'on supprime ET qu'elle est terminée (aucun
    épisode restant à venir) — sinon on désactive juste les épisodes
    concernés, pour ne pas couper une saison en cours de diffusion."""
    if not SONARR_API_KEY or not paths:
        return []
    series_list = arr_api(SONARR_URL, SONARR_API_KEY, "GET", "/api/v3/series")
    if not series_list:
        return []
    plan = []
    for series in series_list:
        prefix = series["path"] + "/"
        matched_paths = {p for p in paths if p.startswith(prefix)}
        if not matched_paths:
            continue
        episodefiles = arr_api(SONARR_URL, SONARR_API_KEY, "GET", "/api/v3/episodefile",
                                params={"seriesId": series["id"]}) or []
        matched_ef_ids = {ef["id"] for ef in episodefiles if ef["path"] in matched_paths}
        if not matched_ef_ids:
            continue
        episodes = arr_api(SONARR_URL, SONARR_API_KEY, "GET", "/api/v3/episode",
                            params={"seriesId": series["id"]}) or []
        by_season = {}
        for ef in episodefiles:
            by_season.setdefault(ef["seasonNumber"], set()).add(ef["id"])
        touched_by_season = {}
        for e in episodes:
            if e.get("episodeFileId") in matched_ef_ids:
                touched_by_season.setdefault(e["seasonNumber"], []).append(e)
        season_stats = {s["seasonNumber"]: s["statistics"] for s in series["seasons"]}
        for season_number, touched_eps in touched_by_season.items():
            stats = season_stats.get(season_number, {})
            season_complete = (stats.get("totalEpisodeCount", 0) > 0
                                and stats.get("totalEpisodeCount") == stats.get("episodeCount"))
            season_fully_deleted = by_season.get(season_number, set()) <= matched_ef_ids
            if season_complete and season_fully_deleted:
                plan.append({
                    "description": f'Sonarr : "{series["title"]}" saison {season_number} — '
                                    f"monitoring désactivé (série toujours suivie)",
                    "kind": "sonarr_season",
                    "series_id": series["id"],
                    "season_number": season_number,
                })
            else:
                plan.append({
                    "description": f'Sonarr : "{series["title"]}" — {len(touched_eps)} épisode(s) '
                                    f"retiré(s) du monitoring (saison {season_number} toujours suivie)",
                    "kind": "sonarr_episodes",
                    "episode_ids": [e["id"] for e in touched_eps],
                })
    return plan


def plan_arr_actions(lib_matches):
    """Point d'entrée : chemins déjà trouvés dans library/ (find_library_matches)
    -> plan d'actions Sonarr/Radarr. Best-effort : une clé API absente ou une
    instance injoignable réduit juste le plan, ne bloque jamais la suppression
    des fichiers elle-même."""
    paths = {p for p, _size in lib_matches}
    radarr_plan, matched = plan_radarr_deletion(paths)
    remaining = paths - matched
    sonarr_plan = plan_sonarr_unmonitor(remaining)
    return radarr_plan + sonarr_plan


def execute_arr_plan(plan):
    for action in plan:
        try:
            if action["kind"] == "radarr_delete":
                arr_api(RADARR_URL, RADARR_API_KEY, "DELETE",
                        f"/api/v3/movie/{action['movie_id']}",
                        params={"deleteFiles": "false", "addImportExclusion": "true"})
                logger.info("Radarr: film retiré id=%s (%s)", action["movie_id"], action["title"])
            elif action["kind"] == "sonarr_season":
                series = arr_api(SONARR_URL, SONARR_API_KEY, "GET",
                                  f"/api/v3/series/{action['series_id']}")
                if not series:
                    logger.warning("sonarr_season: série id=%s introuvable au moment d'exécuter", action["series_id"])
                    continue
                for season in series["seasons"]:
                    if season["seasonNumber"] == action["season_number"]:
                        season["monitored"] = False
                arr_api(SONARR_URL, SONARR_API_KEY, "PUT",
                        f"/api/v3/series/{action['series_id']}", json_body=series)
                logger.info("Sonarr: série id=%s saison %s désactivée", action["series_id"], action["season_number"])
            elif action["kind"] == "sonarr_episodes":
                arr_api(SONARR_URL, SONARR_API_KEY, "PUT", "/api/v3/episode/monitor",
                        json_body={"episodeIds": action["episode_ids"], "monitored": False})
                logger.info("Sonarr: épisodes désactivés ids=%s", action["episode_ids"])
        except Exception as e:
            logger.warning("échec d'exécution de l'action arr %r : %s", action.get("description"), e)


# --- Vues Séries/Films : suppression d'un titre entier d'un coup (torrents +
# fichiers + retrait complet de Sonarr/Radarr), par opposition à la vue
# Torrents qui ne touche qu'un torrent (et, pour Sonarr, seulement l'épisode/
# la saison correspondante, voir plan_sonarr_unmonitor). Une série "qui
# n'intéresse plus" doit disparaître complètement, peu importe si une saison
# est encore en cours de diffusion.

def fetch_series_list():
    series = arr_api(SONARR_URL, SONARR_API_KEY, "GET", "/api/v3/series")
    if not series:
        return []
    series.sort(key=lambda s: s["title"].lower())
    return series


def fetch_movies_list():
    movies = arr_api(RADARR_URL, RADARR_API_KEY, "GET", "/api/v3/movie")
    if not movies:
        return []
    movies.sort(key=lambda m: m["title"].lower())
    return movies


def find_series_torrents(all_torrents, library_index, cross_seed_child_ids, series_path):
    """Torrents top-level (hors enfants cross-seed, cascadés avec leur parent
    — voir build_cross_seed_groups) ayant au moins un fichier library/ sous le
    dossier de la série. Même préfixe que plan_sonarr_unmonitor, mais sans sa
    condition "saison terminée" : ici on veut tout supprimer, peu importe
    l'état de diffusion."""
    prefix = series_path + "/"
    result = []
    for t in all_torrents:
        if t["id"] in cross_seed_child_ids:
            continue
        host_files = torrent_host_files(t)
        lib_matches = find_library_matches(host_files, library_index)
        if any(p.startswith(prefix) for p, _s in lib_matches):
            result.append((t, host_files, lib_matches))
    return result


def find_movie_torrent(all_torrents, cross_seed_child_ids, movie_path):
    """Torrent top-level dont un fichier correspond au fichier du film (même
    (dev, inode) via resolved_stat(), donc cross-seed symlinks compris). Un
    film n'a qu'un seul fichier : au plus un torrent top-level peut en être la
    source, build_cross_seed_groups ayant déjà regroupé tout ce qui partage
    cet inode sous un même parent."""
    target_stat = resolved_stat(movie_path)
    if target_stat is None:
        return None
    target = (target_stat.st_dev, target_stat.st_ino)
    for t in all_torrents:
        if t["id"] in cross_seed_child_ids:
            continue
        for path, _size in torrent_host_files(t):
            st = resolved_stat(path)
            if st and (st.st_dev, st.st_ino) == target:
                return t
    return None


def bulk_delete_torrents(client, matched, all_torrents, linked_ids, missing_ids, cross_seed_groups):
    """Supprime une liste de torrents top-level (matched: [(torrent, host_files,
    lib_matches), ...]) sans plan Sonarr/Radarr par-torrent (arr_plan=[]) — une
    action arr globale (retrait complet de la série/du film) suit de toute
    façon juste après, un plan désactivation-épisode par-torrent serait
    redondant. Ignore un torrent déjà supprimé en cascade (cross-seed d'un
    autre élément de `matched` traité plus tôt). Un échec par torrent (ex.
    Transmission injoignable un instant) est capturé individuellement plutôt
    que de laisser apply_deletion() remonter l'exception et interrompre le
    reste du lot — un torrent en échec ne doit jamais bloquer les suivants, ni
    empêcher le nettoyage des fichiers résiduels/le retrait Sonarr-Radarr qui
    suit dans execute_delete_series()."""
    freed = 0
    deleted = 0
    failed = 0
    for torrent, host_files, lib_matches in matched:
        current_ids = {t["id"] for t in all_torrents}
        if torrent["id"] not in current_ids:
            continue
        try:
            all_torrents, f = apply_deletion(client, torrent, host_files, lib_matches, [], all_torrents,
                                              linked_ids, missing_ids, cross_seed_groups)
        except Exception as e:
            logger.error("échec de la suppression groupée de %r (id=%s) : %s", torrent["name"], torrent["id"], e)
            failed += 1
            continue
        freed += f
        deleted += 1
    return all_torrents, freed, deleted, failed


def cleanup_orphan_series_files(series_path):
    """Supprime tout fichier restant sous le dossier de la série après le
    passage de bulk_delete_torrents — un fichier sans torrent Transmission
    correspondant (retiré par un autre moyen, ou jamais suivi) resterait
    sinon orphelin malgré la promesse "tous les épisodes téléchargés"."""
    removed = 0
    if not os.path.isdir(series_path):
        return removed
    for root, _dirs, files in os.walk(series_path):
        for name in files:
            path = os.path.join(root, name)
            try:
                os.remove(path)
                removed += 1
                logger.info("fichier orphelin de série supprimé : %s", path)
                prune_empty_dirs(path)
            except OSError as e:
                logger.warning("échec de suppression du fichier orphelin %s : %s", path, e)
    return removed


def execute_delete_series(client, series, matched, all_torrents, cross_seed_groups, linked_ids, missing_ids):
    """Supprime tous les torrents connus de la série (matched, déjà calculé par
    l'appelant pour l'écran de confirmation — pas recalculé ici, ça évite de
    rescanner tous les torrents une deuxième fois pour le même résultat) + les
    fichiers résiduels de son dossier, puis retire la série de Sonarr
    (deleteFiles=false : les fichiers ont déjà été supprimés ici même, comme
    pour un film via plan_radarr_deletion) avec exclusion de liste pour ne
    jamais la voir revenir via un import list sync."""
    host_path = series["path"]  # même mount /data_root que Sonarr, aucune traduction nécessaire
    all_torrents, freed, deleted, failed = bulk_delete_torrents(client, matched, all_torrents, linked_ids,
                                                                 missing_ids, cross_seed_groups)
    orphan_removed = cleanup_orphan_series_files(host_path)
    arr_api(SONARR_URL, SONARR_API_KEY, "DELETE", f"/api/v3/series/{series['id']}",
            params={"deleteFiles": "false", "addImportListExclusion": "true"})
    logger.info("Sonarr: série %r (id=%s) retirée complètement (+ exclusion) — %d torrent(s) supprimé(s), "
                "%d échec(s), %d fichier(s) orphelin(s)",
                series["title"], series["id"], deleted, failed, orphan_removed)
    return all_torrents, freed, deleted, failed


def execute_delete_movie_no_torrent(movie):
    """Chemin de repli quand find_movie_torrent() ne trouve aucun torrent
    (jamais téléchargé, ou fichier orphelin hors suivi Transmission) : Radarr
    supprime lui-même son fichier (deleteFiles=true) puisqu'aucun torrent ne
    s'en charge ici, contrairement au chemin normal où plan_radarr_deletion
    laisse toujours deleteFiles=false (le fichier est déjà retiré par ce
    script via lib_matches)."""
    arr_api(RADARR_URL, RADARR_API_KEY, "DELETE", f"/api/v3/movie/{movie['id']}",
            params={"deleteFiles": "true" if movie.get("hasFile") else "false", "addImportExclusion": "true"})
    logger.info("Radarr: film %r (id=%s) retiré complètement (+ exclusion), sans torrent correspondant",
                movie["title"], movie["id"])


# Champs de tri disponibles (vue Torrents). BIB/ABS (bibliothèque/absent)
# lisent des attributs précalculés (_linked/_missing, posés une seule fois au
# chargement en même temps que linked_ids/missing_ids) plutôt que de refaire
# un lookup dans ces sets ici : un lambda de SORT_FIELDS ne reçoit que le
# torrent, pas les sets externes.
SORT_FIELDS = [
    ("BIB", lambda t: t["_linked"]),
    ("ABS", lambda t: t["_missing"]),
    ("AGE", lambda t: t["addedDate"]),
    ("TAILLE", lambda t: t["totalSize"]),
    ("RATIO", lambda t: t["uploadRatio"]),
    ("TRACKER", lambda t: t.get("_tracker_name", "")),
    ("NOM", lambda t: t["name"].lower()),
]

# Mêmes conventions que SORT_FIELDS — même ordre que les colonnes affichées.
# Défaut sur TITRE (dernier champ) pour préserver le tri alphabétique déjà
# posé par fetch_series_list()/fetch_movies_list() tant que l'utilisateur n'a
# pas encore trié lui-même.
SERIES_SORT_FIELDS = [
    ("MON", lambda s: bool(s.get("monitored"))),
    ("SAISONS", lambda s: sum(1 for se in s.get("seasons", []) if se.get("monitored"))),
    ("EPISODES", lambda s: s.get("statistics", {}).get("episodeFileCount", 0)),
    ("TAILLE", lambda s: s.get("statistics", {}).get("sizeOnDisk", 0)),
    ("TITRE", lambda s: s["title"].lower()),
]

FILMS_SORT_FIELDS = [
    ("MON", lambda m: bool(m.get("monitored"))),
    ("FICH", lambda m: bool(m.get("hasFile"))),
    ("ANNEE", lambda m: m.get("year", 0)),
    ("TAILLE", lambda m: m.get("sizeOnDisk", 0)),
    ("TITRE", lambda m: m["title"].lower()),
]

VIEWS = ["torrents", "series", "films"]
VIEW_LABELS = {"torrents": "Torrents", "series": "Séries", "films": "Films"}


def sort_items(items, fields, sort_idx, reverse):
    """Générique — utilisée avec SORT_FIELDS/SERIES_SORT_FIELDS/FILMS_SORT_FIELDS :
    même mécanisme de tri dans les trois vues, seule la liste de champs
    triables change."""
    _label, key_func = fields[sort_idx]
    items.sort(key=key_func, reverse=reverse)


def filter_by_title(items, filter_str):
    needle = filter_str.lower()
    if not needle:
        return items
    return [i for i in items if needle in i["title"].lower()]


def human_age(added_ts):
    days = int((time.time() - added_ts) // 86400)
    if days < 1:
        return "auj."
    if days < 31:
        return f"{days}j"
    if days < 365:
        return f"{days // 30}mois"
    return f"{days // 365}an{'s' if days // 365 > 1 else ''}"


def human_size(nbytes):
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if nbytes < 1024 or unit == "To":
            return f"{nbytes:.1f}{unit}" if unit != "o" else f"{int(nbytes)}{unit}"
        nbytes /= 1024


def torrent_host_files(torrent):
    download_dir = torrent["downloadDir"]
    paths = []
    for f in torrent.get("files", []):
        container_path = os.path.join(download_dir, f["name"])
        try:
            paths.append((container_path_to_host(container_path), f["length"]))
        except ValueError as e:
            # Torrent déplacé manuellement hors /data (ex. via Transmission
            # lui-même) — on l'ignore plutôt que de faire planter tout
            # l'outil pour un seul torrent.
            logger.warning("torrent %r (id=%s) : %s — fichier ignoré",
                            torrent.get("name"), torrent.get("id"), e)
    return paths


def build_library_index():
    """(device, inode) -> chemin, pour tout fichier sous library/ — construit
    une seule fois par appel plutôt qu'à chaque torrent (évite de re-walker
    library/ pour chacun des ~140 torrents)."""
    index = {}
    for root, _dirs, files in os.walk(LIBRARY_ROOT):
        for name in files:
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError as e:
                logger.warning("stat impossible sur %s pendant l'indexation de library/ : %s", path, e)
                continue
            index[(st.st_dev, st.st_ino)] = path
    logger.debug("index library/ construit : %d fichier(s)", len(index))
    return index


def find_library_matches(host_files, library_index):
    matches = []
    for path, _size in host_files:
        st = resolved_stat(path)
        if st is None:
            logger.warning("fichier attendu absent du disque (déjà déplacé/supprimé ?) : %s", path)
            continue
        hit = library_index.get((st.st_dev, st.st_ino))
        if hit:
            matches.append((hit, st.st_size))
    logger.debug("find_library_matches: %d fichier(s) torrent -> %d correspondance(s) library/",
                 len(host_files), len(matches))
    return matches


def resolved_stat(path):
    """os.stat() qui sait traverser un symlink cross-seed vers un chemin
    absolu /data/... — cross-seed (linkType symlink par défaut) crée ses
    liens dans .cross-seed-links/<tracker>/... en pointant vers le chemin
    tel que VU PAR LE CONTENEUR TRANSMISSION (/data/completed/...), pas le
    chemin utilisé par clearr en interne. Un os.stat() nu suit le lien avec
    la racine de CE conteneur, qui n'a pas de /data : le fichier semble
    absent alors qu'il existe très bien. Renvoie None si le fichier (ou sa
    cible réelle) n'existe pas."""
    target = path
    if os.path.islink(path):
        link_target = os.readlink(path)
        if link_target.startswith("/data"):
            link_target = container_path_to_host(link_target)
        elif not os.path.isabs(link_target):
            link_target = os.path.join(os.path.dirname(path), link_target)
        target = link_target
    try:
        return os.stat(target)
    except OSError:
        return None


def find_torrent_by_inode(client, dev, ino):
    """Sens inverse de find_library_matches() : part d'un (dev, inode) déjà
    connu — capturé sur un fichier library/ avant qu'il soit remplacé par un
    import Sonarr/Radarr, voir cli.delete_by_inode() — et cherche le torrent
    Transmission dont un des fichiers y résout (même resolved_stat() que le
    reste du module, donc même gestion des symlinks cross-seed). Renvoie le
    torrent (dict) ou None."""
    for torrent in client.list_torrents():
        for path, _size in torrent_host_files(torrent):
            st = resolved_stat(path)
            if st and (st.st_dev, st.st_ino) == (dev, ino):
                return torrent
    return None


def analyze_torrent_files(host_files, library_index):
    """Une seule passe resolved_stat() par torrent, qui alimente à la fois les
    marqueurs affichés (évite de stat chaque fichier deux fois) et le
    regroupement cross-seed (build_cross_seed_groups, via les inodes
    renvoyés) : linked=True si au moins un fichier a une correspondance
    library/ (hardlink), missing=True si AUCUN fichier du torrent n'existe
    plus sur disque — le cas Transmission "No data found!" (torrent
    orphelin). inodes = (dev, inode) de chaque fichier réellement présent —
    un torrent cross-seedé et l'original dont il dérive partagent ces mêmes
    inodes malgré deux entrées Transmission distinctes."""
    if not host_files:
        return False, False, set()
    any_exists = False
    linked = False
    inodes = set()
    for path, _size in host_files:
        st = resolved_stat(path)
        if st is None:
            continue
        any_exists = True
        inodes.add((st.st_dev, st.st_ino))
        if (st.st_dev, st.st_ino) in library_index:
            linked = True
    return linked, not any_exists, inodes


def is_cross_seed_entry(torrent):
    """True si ce torrent est une entrée injectée par arr/cross-seed (pas le
    téléchargement d'origine) — son downloadDir vit sous le sous-dossier
    dédié .cross-seed-links/<tracker>/... plutôt que directement sous
    /data/completed. Sert à choisir le "parent" d'un groupe cross-seed
    (build_cross_seed_groups) : le téléchargement réel, pas l'un des
    symlinks pointant dessus."""
    return "/.cross-seed-links/" in torrent.get("downloadDir", "")


def build_cross_seed_groups(all_torrents):
    """Regroupe les torrents qui partagent au moins un fichier réel (même
    (dev, inode), voir analyze_torrent_files/t["_inodes"]) — un torrent
    cross-seedé (arr/cross-seed, linkType symlink par défaut) pointe vers les
    mêmes données qu'un torrent déjà présent sur un autre indexeur : même
    contenu, deux entrées Transmission distinctes qui n'apparaissent
    autrement reliées par rien dans la liste. Union-find sur les inodes
    partagés plutôt qu'une comparaison torrent x torrent (O(n²)). N'a besoin
    d'aucun nouveau stat — les inodes sont déjà mis en cache sur chaque
    torrent (t["_inodes"]) au chargement, donc peut être rappelée à bas coût
    après chaque suppression pour refléter les groupes restants. Renvoie
    (groups, child_ids) : groups = {parent_id: [torrent_enfant, ...]}
    (uniquement les groupes de taille >= 2), child_ids = ids à retirer du
    niveau racine de l'arbre (déjà représentés sous leur parent, voir
    build_tree)."""
    parent_of = {t["id"]: t["id"] for t in all_torrents}

    def find(x):
        while parent_of[x] != x:
            parent_of[x] = parent_of[parent_of[x]]
            x = parent_of[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent_of[ra] = rb

    inode_owner = {}
    for t in all_torrents:
        for inode in t.get("_inodes", ()):
            if inode in inode_owner:
                union(inode_owner[inode], t["id"])
            else:
                inode_owner[inode] = t["id"]

    clusters = {}
    for t in all_torrents:
        clusters.setdefault(find(t["id"]), []).append(t)

    groups, child_ids = {}, set()
    for members in clusters.values():
        if len(members) < 2:
            continue
        # Le parent est le téléchargement d'origine (pas une entrée
        # .cross-seed-links/) s'il en reste un ; sinon (original déjà
        # supprimé) le plus ancien du groupe fait office de parent, pour
        # toujours avoir une racine plutôt que de casser le groupement.
        originals = [t for t in members if not is_cross_seed_entry(t)]
        parent = originals[0] if originals else min(members, key=lambda t: t["addedDate"])
        children = [t for t in members if t["id"] != parent["id"]]
        groups[parent["id"]] = children
        child_ids.update(t["id"] for t in children)
    return groups, child_ids


def build_tree(top_level, cross_seed_groups, expanded_ids, filter_str):
    """Aplatit les groupes cross-seed en lignes affichables/navigables : une
    ligne racine par torrent parent (depth=0), suivie de ses enfants
    (depth=1, un par cross-seed) si le groupe est déplié (expanded_ids) — ou
    si le filtre ne matche qu'un enfant, auquel cas le groupe est forcé
    ouvert pour révéler ce match plutôt que de le masquer silencieusement.
    top_level : torrents dont l'id n'est pas dans child_ids de
    build_cross_seed_groups (déjà filtré par l'appelant, un enfant ne doit
    apparaître qu'une fois, sous son parent)."""
    needle = filter_str.lower()
    rows = []
    for t in top_level:
        children = cross_seed_groups.get(t["id"], [])
        parent_match = needle in t["name"].lower()
        matching_children = [c for c in children if needle in c["name"].lower()]
        if needle and not parent_match and not matching_children:
            continue
        rows.append({"torrent": t, "depth": 0, "child_count": len(children), "parent_id": None})
        if children and (t["id"] in expanded_ids or (needle and matching_children and not parent_match)):
            for c in sorted(children, key=lambda c: c.get("_tracker_name", "")):
                rows.append({"torrent": c, "depth": 1, "child_count": 0, "parent_id": t["id"]})
    return rows


def prune_empty_dirs(path):
    d = os.path.dirname(path)
    while d and os.path.commonpath([d, LIBRARY_ROOT]) == LIBRARY_ROOT and d != LIBRARY_ROOT:
        try:
            os.rmdir(d)
            logger.debug("dossier vide supprimé : %s", d)
        except OSError:
            break
        d = os.path.dirname(d)


def do_delete(client, torrent, host_files, lib_matches, arr_plan, dependents=()):
    log_deletion(torrent, host_files, lib_matches, arr_plan)
    client.remove_torrent(torrent["id"])
    for path, _size in lib_matches:
        try:
            os.remove(path)
            logger.info("fichier library supprimé : %s", path)
            prune_empty_dirs(path)
        except OSError as e:
            logger.warning("échec de suppression du fichier library %s : %s", path, e)
    execute_arr_plan(arr_plan)
    # Un torrent cross-seedé (dependents, voir build_cross_seed_groups) partage
    # l'inode de ce torrent : une fois ce dernier supprimé, ses cross-seeds
    # n'ont plus rien à seeder (données orphelines) — on les supprime avec
    # lui plutôt que de laisser des entrées mortes dans Transmission.
    # remove_torrent(delete-local-data=True) suffit pour le "lien logique" :
    # le fichier d'un enfant est un symlink (.cross-seed-links/<tracker>/...,
    # cf. is_cross_seed_entry) — Transmission appelle unlink() dessus comme
    # sur n'importe quel fichier, ce qui retire le symlink lui-même sans
    # toucher sa cible (déjà supprimée juste au-dessus). Pas de
    # find_library_matches/plan_arr_actions rejoués pour ces enfants : même
    # inode que le parent, donc mêmes correspondances library/ et même plan
    # Sonarr/Radarr déjà traités ci-dessus — les rejouer serait redondant.
    for child in dependents:
        try:
            client.remove_torrent(child["id"])
            logger.info("cross-seed: torrent enfant %r (id=%s) supprimé avec son parent %r (id=%s)",
                        child["name"], child["id"], torrent["name"], torrent["id"])
        except Exception as e:
            logger.warning("échec de suppression du cross-seed enfant %r (id=%s) : %s",
                            child["name"], child["id"], e)


def apply_deletion(client, torrent, host_files, lib_matches, arr_plan, all_torrents, linked_ids, missing_ids,
                    cross_seed_groups):
    """Effectue la suppression et renvoie (nouvelle liste all_torrents, octets
    libérés) — factorise ce que les différents points d'entrée (confirmé,
    direct, purge groupée) ont en commun une fois host_files/lib_matches/
    arr_plan connus. Si torrent est le parent d'un groupe cross-seed, ses
    enfants sont supprimés avec lui (voir do_delete) ; freed ne compte que
    les octets du parent — les enfants ne libèrent aucun espace disque
    supplémentaire, ce sont des symlinks vers les mêmes fichiers. Même
    principe pour lib_matches (repéré le 2026-08-01, "52.0Go" affiché pour
    un film de 26.0Go réels) : find_library_matches() ne renvoie QUE des
    fichiers library/ qui sont un hardlink (même inode) d'un fichier de
    host_files — additionner les deux comptait deux fois les mêmes octets
    physiques. host_files seul donne déjà la taille réelle du torrent."""
    dependents = cross_seed_groups.get(torrent["id"], [])
    do_delete(client, torrent, host_files, lib_matches, arr_plan, dependents)
    removed_ids = {torrent["id"]} | {c["id"] for c in dependents}
    remaining = [t for t in all_torrents if t["id"] not in removed_ids]
    linked_ids.difference_update(removed_ids)
    missing_ids.difference_update(removed_ids)
    freed = sum(s for _, s in host_files)
    return remaining, freed


def load_full_state():
    """Point d'entrée unique utilisé par webapp.py (recalcul complet à chaque
    requête, voir CLAUDE.md "risque perf") et par tui.py (une seule fois au
    démarrage) : récupère les torrents Transmission, résout les noms de
    tracker, indexe library/, calcule les marqueurs BIB/ABS et les groupes
    cross-seed. Renvoie un dict prêt à consommer par les deux frontends."""
    client = TransmissionClient()
    all_torrents = client.list_torrents()

    tracker_map = build_prowlarr_tracker_map()
    for t in all_torrents:
        t["_tracker_name"], t["_tracker_others"] = tracker_display(t, tracker_map)

    library_index = build_library_index()
    linked_ids, missing_ids = set(), set()
    for t in all_torrents:
        linked, missing, inodes = analyze_torrent_files(torrent_host_files(t), library_index)
        t["_linked"] = linked
        t["_missing"] = missing
        t["_inodes"] = inodes
        if linked:
            linked_ids.add(t["id"])
        if missing:
            missing_ids.add(t["id"])

    cross_seed_groups, cross_seed_child_ids = build_cross_seed_groups(all_torrents)
    logger.info("état recalculé : %d torrents, %d avec fichier(s) bibliothèque, %d fichier manquant, "
                "%d groupe(s) cross-seed", len(all_torrents), len(linked_ids), len(missing_ids),
                len(cross_seed_groups))

    return {
        "client": client,
        "all_torrents": all_torrents,
        "library_index": library_index,
        "linked_ids": linked_ids,
        "missing_ids": missing_ids,
        "cross_seed_groups": cross_seed_groups,
        "cross_seed_child_ids": cross_seed_child_ids,
    }
