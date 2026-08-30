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

# /data_root est le montage réel en service (voir arr/docker-compose.yml) et
# reste le défaut. L'override par variable d'environnement n'existe que pour les
# tests (arr/clearr/tests/), qui doivent pouvoir faire pointer tout le module
# vers un répertoire jetable : sans lui, aucun des chemins destructifs n'était
# testable ailleurs que contre la vraie bibliothèque. Ne pas la poser en
# production — les chemins renvoyés par les API arr sont en /data_root, et un
# préfixe différent ne matcherait plus rien.
DATA_ROOT = os.environ.get("CLEARR_DATA_ROOT", "/data_root")
LIBRARY_ROOT = os.path.join(DATA_ROOT, "library")
TRANSMISSION_DATA_ROOT = os.path.join(DATA_ROOT, ".transmission", "data")
# Racine des téléchargements terminés. Aussi montée comme bibliothèque Jellyfin
# (jellyfin/docker-compose.override.yml) : c'est là que vivent les films/séries
# récupérés à la main, hors de tout suivi Sonarr/Radarr — voir la section
# "Titres hors Sonarr/Radarr" plus bas.
COMPLETED_ROOT = os.path.join(TRANSMISSION_DATA_ROOT, "completed")
LOG_PATH = os.path.join(DATA_ROOT, ".clearr.log")

# Fichiers que Sonarr/Radarr écrivent À CÔTÉ d'un média qu'ils gèrent
# (metadata writer .nfo activé le 2026-08-06, jaquettes, sous-titres) : ils
# n'apparaissent dans aucun episodefile/movieFile et aucun torrent ne les
# couvre par un hardlink, donc un balayage de library/ les compterait tous
# orphelins (241 .nfo au moment de l'ajout). Sous le dossier d'un titre encore
# connu d'un arr, ils sont considérés couverts par lui — ailleurs ils restent
# des résidus à part entière (voir library_orphan_files).
SIDECAR_EXTENSIONS = (".nfo", ".srt", ".sub", ".idx", ".ass", ".ssa", ".vtt",
                      ".jpg", ".jpeg", ".png", ".webp", ".tbn")

# Statuts RPC Transmission correspondant à un torrent qui partage encore
# (5 = en attente de seed, 6 = seed). Tout le reste (0 arrêté, 1-2 vérification,
# 3-4 téléchargement) ne partage pas — distinction affichée dans les écrans de
# confirmation, un torrent arrêté n'apportant plus rien à personne.
SEEDING_STATUSES = (5, 6)

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
# DEBUG par défaut faisait ~95 Ko/jour (une ligne par appel RPC et par lookup),
# soit ~35 Mo/an sans rotation — et surtout une trace d'erreur noyée au milieu.
# INFO par défaut, DEBUG à la demande via CLEARR_LOG_LEVEL quand on diagnostique
# vraiment : la valeur utile au quotidien n'est pas celle utile une fois par
# trimestre. La rotation est par ailleurs assurée par scripts/logrotate.conf.
logging.basicConfig(
    filename=LOG_PATH,
    level=getattr(logging, os.environ.get("CLEARR_LOG_LEVEL", "INFO").upper(), logging.INFO),
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
        # `status` sert uniquement aux écrans de confirmation, pour distinguer un
        # torrent encore en seed d'un torrent arrêté (voir SEEDING_STATUSES) —
        # sans lui, la modale ne saurait pas dire ce qui va cesser d'être partagé.
        # seedRatioLimit/Mode : posés par Sonarr/Radarr au grab sur les indexeurs
        # publics (voir PUBLIC_INDEXER_SEED_RATIO dans apply-arr-overrides.py),
        # donc ce qui décide quand un torrent cessera de partager — affiché dans
        # la fiche détail, où « ratio 1.90 » seul ne dit pas s'il reste du chemin.
        fields = ["id", "name", "addedDate", "downloadDir", "totalSize",
                  "files", "trackerStats", "percentDone", "uploadRatio", "status",
                  "seedRatioLimit", "seedRatioMode"]
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


def alias_for(hostname):
    """Nom d'indexeur déclaré dans TRACKER_ALIASES pour ce host, ou None.

    Deux formes de clé, testées dans cet ordre :
    - **host exact** (`tracker.opentrackr.org=Nyaa.si`) — seule forme
      acceptable pour un tracker public mutualisé, dont le domaine de base
      ne dit rien de l'indexeur qui l'embarque (`eu.org` pour
      `tracker.torrent.eu.org` : un domaine partagé par d'innombrables sites
      sans rapport) ;
    - **domaine de base** (`c411.tw=C411`) — couvre d'un coup tous ses
      sous-domaines présents et à venir (`tk.c411.tw`), pour un domaine qui
      appartient EN PROPRE à l'indexeur. Nécessaire parce que Prowlarr ne
      connaît que le(s) domaine(s) du site (`c411.org`), jamais le domaine
      distinct sur lequel le tracker annonce.
    L'exact l'emporte : une clé host reste prioritaire sur une clé domaine
    qui la recouvrirait."""
    if hostname in TRACKER_ALIASES:
        return TRACKER_ALIASES[hostname]
    return TRACKER_ALIASES.get(base_domain(hostname))


def resolve_tracker_name(hostname, tracker_map):
    """Renvoie (nom, officiel) — officiel=True si le hostname se rattache à
    un indexeur réellement configuré dans Prowlarr, False s'il retombe sur le
    hostname brut (tracker public embarqué dans le .torrent, pas un indexeur
    qu'on interroge nous-mêmes) : voir tracker_display, qui replie tous les
    non-officiels sous un seul libellé."""
    # TRACKER_ALIASES (arr/.env) d'abord : couvre ce qu'aucune API ne permet
    # de rattacher à un indexeur — trackers publics génériques ET domaine
    # d'annonce propre à un indexeur mais distinct du domaine de son site
    # (voir alias_for pour les deux formes de clé).
    name = alias_for(hostname)
    if name:
        return name, True
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


def arr_write(base_url, api_key, method, path, params=None, json_body=None, what=""):
    """Écriture arr dont on VÉRIFIE le résultat, contrairement à arr_api() qui
    est best-effort et rend None sur tout échec sans que l'appelant s'en
    aperçoive. Renvoie True/False.

    Repéré le 2026-08-09 : les écritures des chemins de suppression jetaient
    leur retour puis loguaient un succès inconditionnel. Une série pouvait donc
    être effacée du disque, affichée « supprimée » en vert et journalisée comme
    retirée, tout en restant suivie par Sonarr — donc re-téléchargée
    intégralement à la recherche suivante, ce que le retrait avec exclusion
    existe précisément pour empêcher. arr_api reste best-effort pour les
    LECTURES, où dégrader est le bon comportement ; une écriture, non."""
    if arr_api(base_url, api_key, method, path, params=params, json_body=json_body) is None:
        logger.error("écriture arr ÉCHOUÉE (%s %s)%s — état arr inchangé",
                     method, path, f" : {what}" if what else "")
        return False
    return True


def execute_arr_plan(plan):
    """Renvoie le nombre d'actions arr qui ont échoué — l'appelant doit le dire
    à l'utilisateur plutôt que d'annoncer une suppression complète."""
    arr_failed = 0
    for action in plan:
        try:
            if action["kind"] == "radarr_delete":
                if arr_write(RADARR_URL, RADARR_API_KEY, "DELETE",
                             f"/api/v3/movie/{action['movie_id']}",
                             params={"deleteFiles": "false", "addImportExclusion": "true"},
                             what=f"retrait du film {action['title']!r}"):
                    logger.info("Radarr: film retiré id=%s (%s)", action["movie_id"], action["title"])
                else:
                    arr_failed += 1
            elif action["kind"] == "sonarr_season":
                series = arr_api(SONARR_URL, SONARR_API_KEY, "GET",
                                  f"/api/v3/series/{action['series_id']}")
                if not series:
                    logger.warning("sonarr_season: série id=%s introuvable au moment d'exécuter", action["series_id"])
                    arr_failed += 1
                    continue
                for season in series["seasons"]:
                    if season["seasonNumber"] == action["season_number"]:
                        season["monitored"] = False
                if arr_write(SONARR_URL, SONARR_API_KEY, "PUT",
                             f"/api/v3/series/{action['series_id']}", json_body=series,
                             what=f"désactivation de la saison {action['season_number']}"):
                    logger.info("Sonarr: série id=%s saison %s désactivée",
                                action["series_id"], action["season_number"])
                else:
                    arr_failed += 1
            elif action["kind"] == "sonarr_episodes":
                if arr_write(SONARR_URL, SONARR_API_KEY, "PUT", "/api/v3/episode/monitor",
                             json_body={"episodeIds": action["episode_ids"], "monitored": False},
                             what=f"désactivation des épisodes {action['episode_ids']}"):
                    logger.info("Sonarr: épisodes désactivés ids=%s", action["episode_ids"])
                else:
                    arr_failed += 1
        except Exception as e:
            logger.warning("échec d'exécution de l'action arr %r : %s", action.get("description"), e)
            arr_failed += 1
    return arr_failed


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


def fetch_episode_files(series_id):
    """Fichiers importés d'une série (chemin, taille, qualité, groupe, langues,
    mediaInfo). L'objet série ne porte que des compteurs agrégés dans
    `statistics` — le détail par fichier demande cet appel séparé, fait
    seulement à l'ouverture d'une fiche détail, jamais au rendu d'un onglet."""
    return arr_api(SONARR_URL, SONARR_API_KEY, "GET", "/api/v3/episodefile",
                   params={"seriesId": series_id}) or []


def quality_profile_names(kind):
    """id de profil qualité -> nom. Les objets série/film ne portent que
    `qualityProfileId` ; la fiche détail veut le nom, d'où cet appel séparé.
    Best-effort comme tout arr_api : un dict vide fait juste retomber la fiche
    sur l'id brut."""
    url, key = (SONARR_URL, SONARR_API_KEY) if kind == "series" else (RADARR_URL, RADARR_API_KEY)
    profiles = arr_api(url, key, "GET", "/api/v3/qualityprofile") or []
    return {p["id"]: p["name"] for p in profiles}


def find_series_by_id(series_id):
    return next((s for s in fetch_series_list() if s["id"] == series_id), None)


def find_movie_by_id(movie_id):
    return next((m for m in fetch_movies_list() if m["id"] == movie_id), None)


# --- Résolution par id externe (IMDb/TVDB/TMDB) -------------------------------
#
# Utilisé par les routes /api/delete/* de webapp.py, donc par l'addon de menu
# contextuel Kodi (kodi/context.clearr) : un client media ne connaît pas les ids
# Sonarr/Radarr, seulement les ids des bases publiques — que jellyfin-kodi
# recopie dans la base Kodi depuis les ProviderIds de Jellyfin. Ces ids sont
# déjà dans les objets arr (mêmes champs que external_links plus bas, vérifiés
# présents sur toute la bibliothèque le 2026-08-03), donc pas plus d'appel WAN
# ici qu'ailleurs.

def _same_external_id(wanted, actual):
    # str() des deux côtés : Sonarr/Radarr renvoient tvdbId/tmdbId en entier
    # alors que Kodi les stocke en chaîne. casefold pour les "tt..." d'IMDb.
    if not wanted or not actual:
        return False
    return str(wanted).strip().casefold() == str(actual).strip().casefold()


def _find_by_external_ids(items, ids, candidates):
    """`candidates` = ((clé côté Kodi, clé côté arr), ...) dans l'ordre de
    priorité. IMDb d'abord : c'est le seul id commun à Sonarr et Radarr, et
    celui que Jellyfin renseigne le plus fidèlement — un tvdbId/tmdbId peut
    venir d'une identification approximative côté Jellyfin.

    Renvoie None si aucun id ne matche, mais AUSSI si un id matche plusieurs
    titres : l'appelant supprime des fichiers, mieux vaut ne rien faire et
    laisser l'utilisateur trancher dans l'UI web que de deviner."""
    for kodi_key, arr_key in candidates:
        wanted = ids.get(kodi_key)
        if not wanted:
            continue
        matches = [i for i in items if _same_external_id(wanted, i.get(arr_key))]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning("id externe %s=%r ambigu : %d titres correspondent (%s) — abandon",
                           kodi_key, wanted, len(matches), ", ".join(m["title"] for m in matches))
            return None
    return None


def find_series_by_external_ids(ids):
    return _find_by_external_ids(fetch_series_list(), ids,
                                 (("imdb", "imdbId"), ("tvdb", "tvdbId"), ("tmdb", "tmdbId")))


def find_movie_by_external_ids(ids):
    # Pas de tvdb côté Radarr : ses films n'ont que imdbId/tmdbId.
    return _find_by_external_ids(fetch_movies_list(), ids,
                                 (("imdb", "imdbId"), ("tmdb", "tmdbId")))


# --- Métadonnées d'affichage (jaquette + liens), partagées par les 3 vues -----
#
# Aucun appel WAN côté serveur, volontairement (demandé explicitement le
# 2026-08-03) : les jaquettes sortent du cache disque de Sonarr/Radarr et les
# ids externes (imdbId/tvdbId/tmdbId) sont déjà dans les objets renvoyés par
# leur API, donc rien à aller chercher chez thetvdb/tmdb. Seuls les liens
# eux-mêmes sortent — et c'est le navigateur qui les suit, sur clic.

# Sonarr/Radarr téléchargent la jaquette à l'ajout du titre et la gardent sous
# leur dossier de config, déjà visible par clearr via le mount unique
# ${DATA_ROOT}:/data_root (mêmes chemins que dans arr/docker-compose.yml, cf.
# commentaire d'en-tête) : on sert donc le fichier tel quel, sans même passer
# par un proxy HTTP vers sonarr/radarr.
MEDIA_COVER_DIRS = {
    "series": os.path.join(DATA_ROOT, ".arr", "sonarr", "config", "MediaCover"),
    "film": os.path.join(DATA_ROOT, ".arr", "radarr", "config", "MediaCover"),
}
# Du plus léger au plus lourd : poster-250 (~20 Ko) suffit largement pour une
# vignette au survol, les autres ne servent que de repli si le cache d'un titre
# est incomplet (rien ne garantit que les 3 variantes soient générées).
POSTER_CANDIDATES = ("poster-250.jpg", "poster-500.jpg", "poster.jpg")

# Les UI Sonarr/Radarr vivent sur des sous-domaines de ${DOMAIN} (LAN-only via
# arr-lan-only, comme clearr lui-même). DOMAIN vient de .env.shared, donc
# injecté explicitement dans l'environnement du conteneur (`environment:` dans
# arr/docker-compose.yml, env_file ne chargeant que arr/.env) — absent = pas
# de lien arr affiché, le reste fonctionne quand même.
DOMAIN = os.environ.get("DOMAIN", "")


def poster_file(kind, arr_id):
    """Chemin disque de la jaquette d'une série/d'un film, ou None si le cache
    Sonarr/Radarr n'en a pas (titre tout juste ajouté, cache purgé...). `arr_id`
    est passé par int() : c'est ce qui garantit qu'aucune valeur venue de l'URL
    ne puisse remonter hors de MediaCover/."""
    directory = MEDIA_COVER_DIRS.get(kind)
    if not directory:
        return None
    for name in POSTER_CANDIDATES:
        path = os.path.join(directory, str(int(arr_id)), name)
        if os.path.exists(path):
            return path
    return None


def external_links(kind, item):
    """Liens vers les bases publiques, construits depuis les ids déjà présents
    dans l'objet arr. TVDB est adressé par son endpoint /dereferrer/series/<id>
    (redirection officielle par id) : Sonarr expose tvdbId, pas le slug du
    site."""
    links = []
    if item.get("imdbId"):
        links.append({"label": "IMDb", "url": f"https://www.imdb.com/title/{item['imdbId']}/"})
    if kind == "series":
        if item.get("tvdbId"):
            links.append({"label": "TVDB", "url": f"https://thetvdb.com/dereferrer/series/{item['tvdbId']}"})
    elif item.get("tmdbId"):
        links.append({"label": "TMDB", "url": f"https://www.themoviedb.org/movie/{item['tmdbId']}"})
    return links


def arr_link(kind, item):
    """Lien vers la fiche du titre dans l'UI Sonarr/Radarr. Sonarr adresse une
    série par son titleSlug, Radarr un film par son tmdbId (son titleSlug EST
    le tmdbId)."""
    if not DOMAIN:
        return None
    # `arr: True` distingue ce lien des liens vers les bases publiques : il
    # pointe vers NOTRE infra, et les vues le rendent en badge plein (voir
    # .meta-link-arr) plutôt qu'en pastille bordée.
    if kind == "series":
        slug = item.get("titleSlug")
        return {"label": "Sonarr", "arr": True,
                "url": f"https://sonarr.{DOMAIN}/series/{slug}"} if slug else None
    tmdb = item.get("tmdbId")
    return {"label": "Radarr", "arr": True,
            "url": f"https://radarr.{DOMAIN}/movie/{tmdb}"} if tmdb else None


def item_meta(kind, item):
    """Bloc de métadonnées consommé identiquement par les 3 vues (voir
    templates/_meta.html) : URL interne de la jaquette (None si pas de cache,
    la vue n'affiche alors rien au survol) + liens externes et arr."""
    links = external_links(kind, item)
    arr = arr_link(kind, item)
    if arr:
        links.append(arr)
    return {
        "kind": kind,
        "id": item["id"],
        "title": item["title"],
        "poster": f"/poster/{kind}/{item['id']}" if poster_file(kind, item["id"]) else None,
        "links": links,
    }


def build_arr_meta_index():
    """Index chemin bibliothèque -> métadonnées, pour rattacher un torrent au
    titre Sonarr/Radarr dont il porte les fichiers (la vue Torrents ne connaît
    qu'un torrent, contrairement aux vues Séries/Films qui partent déjà de
    l'objet arr). Films indexés par chemin exact du fichier, séries par préfixe
    de dossier — mêmes critères que plan_radarr_deletion/plan_sonarr_unmonitor,
    et pas de traduction de chemin à faire (arr et clearr partagent /data_root).
    Best-effort : un arr injoignable donne juste un index partiel, la vue reste
    fonctionnelle sans jaquette ni lien."""
    series = [(s["path"] + "/", item_meta("series", s)) for s in fetch_series_list() if s.get("path")]
    movies = {}
    for m in fetch_movies_list():
        mf = m.get("movieFile") or {}
        if mf.get("path"):
            movies[mf["path"]] = item_meta("film", m)
    logger.debug("index métadonnées arr : %d série(s), %d film(s) avec fichier", len(series), len(movies))
    return {"series": series, "movies": movies}


def torrent_meta(torrent, library_index, meta_index):
    """Métadonnées du titre auquel appartient ce torrent, ou None (jamais
    importé, ou fichier supprimé de library/). Repart des inodes déjà calculés
    par analyze_torrent_files() — donc aucun stat supplémentaire, et les
    symlinks cross-seed sont déjà résolus."""
    for inode in sorted(torrent.get("_inodes") or ()):
        path = library_index.get(inode)
        if not path:
            continue
        meta = meta_index["movies"].get(path)
        if meta:
            return meta
        for prefix, series_meta in meta_index["series"]:
            if path.startswith(prefix):
                return series_meta
    return None


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
    failed_entries = []
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
            # Les entrées en échec sont rendues à l'appelant : leurs fichiers
            # sont TOUJOURS couverts par un torrent vivant, donc cleanup_orphan_
            # files() ne doit pas les balayer (voir son paramètre `covered`).
            failed_entries.append((torrent, host_files, lib_matches))
            continue
        freed += f
        deleted += 1
    return all_torrents, freed, deleted, failed, failed_entries


def is_seeding(torrent):
    return torrent.get("status") in SEEDING_STATUSES


def orphan_files_under(target_path, covered):
    """[(chemin, taille), ...] des fichiers présents sous `target_path` qu'aucun
    torrent ne couvre — exactement ce que cleanup_orphan_files() supprimera en
    plus des torrents. `covered` est l'ensemble des chemins déjà pris en charge
    par un torrent : ses fichiers Transmission pour un titre hors arr (leurs
    données SONT sous target_path), ses correspondances library/ pour une série
    Sonarr (les données du torrent vivent ailleurs, sous .transmission/data, et
    library/ n'en tient que des hardlinks).

    Sert les écrans de confirmation : sans ça la modale annonçait les seuls
    torrents, donc moins que ce qui allait réellement partir (cas rencontré le
    2026-08-06 sur une série dont 2 épisodes n'avaient plus de torrent, Sonarr
    les ayant retirés du client une fois leur ratio atteint)."""
    if os.path.isdir(target_path):
        on_disk = [os.path.join(walk_root, name)
                   for walk_root, _dirs, files in os.walk(target_path) for name in files]
    else:
        on_disk = [target_path] if os.path.exists(target_path) else []
    orphans = []
    for path in sorted(on_disk):
        if path in covered:
            continue
        try:
            orphans.append((path, os.path.getsize(path)))
        except OSError:
            orphans.append((path, 0))
    return orphans


def series_orphan_files(matched, series_path):
    """Fichiers du dossier d'une série qu'aucun de ses torrents ne couvre. Les
    chemins couverts sont les correspondances library/ (lib_matches), pas les
    fichiers Transmission : c'est le dossier de la série qu'on inspecte."""
    covered = {p for _t, _hf, lib_matches in matched for p, _s in lib_matches}
    return orphan_files_under(series_path, covered)


# --- Suppression d'une série SAISON PAR SAISON --------------------------------
#
# Deux modes, exposés par execute_delete_series(purge=) :
#
#   purge=True  : comportement historique — tout part, la série est retirée de
#                 Sonarr avec exclusion de liste. La sélection de saisons est
#                 alors IGNORÉE (purger une partie d'une série laisserait les
#                 saisons gardées dans library/ sans plus aucun arr pour les
#                 revendiquer : invisibles des trois vues, hors « Orphelins
#                 library/ »).
#   purge=False : les saisons choisies partent, la série RESTE dans Sonarr en
#                 monitorNewItems="all" — c'est ce qui permet à une saison
#                 future d'être téléchargée alors qu'on vient d'effacer les
#                 précédentes. Pas d'exclusion de liste : une saison redemandée
#                 depuis Seerr sera re-téléchargée, et c'est voulu.
#
# LE PLAN PART DES episodefile, PAS DES TORRENTS NI DES DOSSIERS « Season XX ».
#   - Pas des dossiers : leur format est configurable dans Sonarr, vaut
#     « Specials » pour la saison 0, et une série peut n'en avoir aucun. Une
#     heuristique de nom est exactement le genre de devinette qui a produit les
#     bugs d'identification déjà corrigés ailleurs.
#   - Pas des torrents : mesuré le 2026-08-30, 164 des 269 episodefile de cette
#     bibliothèque (61 %) n'ont PLUS AUCUN torrent — Sonarr les retire du client
#     une fois le seedRatio 1.5 atteint sur les indexeurs publics. Des saisons
#     entières n'existent que sous forme de fichiers library/. Partir des
#     torrents ne verrait donc pas la majorité de ce qu'il faut supprimer.
#
# Corollaire : sans la liste des episodefile on ne sait rien. series_episode_
# files() LÈVE au lieu de dégrader, comme _arr_covered_paths() et pour la même
# raison — une suppression de saison qui ne supprimerait presque rien tout en
# s'annonçant réussie est pire qu'une erreur affichée.

def series_episode_files(series_id):
    """episodefile d'une série. Contrairement à fetch_episode_files() (fiche
    détail, best-effort), lève RuntimeError si Sonarr ne répond pas : c'est la
    seule source qui rattache un fichier à une saison, tout le plan en dépend."""
    files = arr_api(SONARR_URL, SONARR_API_KEY, "GET", "/api/v3/episodefile",
                    params={"seriesId": series_id})
    if files is None:
        raise RuntimeError(
            "Sonarr n'a pas répondu : impossible de savoir quel fichier appartient à quelle "
            "saison, donc impossible de supprimer une saison sans risquer d'en emporter une "
            "autre. Vérifiez que Sonarr répond, puis réessayez.")
    return files


def season_numbers(series, episode_files):
    """Saisons proposables : celles de l'objet série (source de vérité, y
    compris une saison connue sans aucun fichier) réunies à celles réellement
    portées par un episodefile — une saison dont Sonarr aurait perdu l'entrée
    resterait sinon insupprimable depuis clearr."""
    return sorted({s["seasonNumber"] for s in series.get("seasons", [])}
                  | {f["seasonNumber"] for f in episode_files})


def season_directories(episode_files, seasons, series_path):
    """Dossiers à balayer pour les fichiers annexes d'une saison, DÉDUITS des
    episodefile — jamais construits depuis un nom de saison.

    Garde indispensable : une série dont les épisodes vivent à plat dans son
    dossier (seasonFolder désactivé) donne dirname == series_path, et balayer
    ce dossier emporterait TOUTES les autres saisons. Dans ce cas on ne balaie
    rien — seuls les fichiers connus de Sonarr partent. Aucune série de cette
    bibliothèque n'est dans ce cas (vérifié le 2026-08-30, les 29 ont des
    dossiers de saison), mais une série ajoutée à la main peut l'être."""
    wanted = set(seasons)
    dirs = set()
    for f in episode_files:
        if f["seasonNumber"] not in wanted:
            continue
        directory = os.path.dirname(f["path"])
        if os.path.normpath(directory) == os.path.normpath(series_path):
            logger.info("série %s : épisodes à plat dans le dossier de la série — "
                        "aucun balayage de fichiers annexes pour la saison %s",
                        series_path, f["seasonNumber"])
            continue
        dirs.add(directory)
    return sorted(dirs)


def plan_season_deletion(state, series, seasons):
    """Plan complet d'une suppression partielle. Lève RuntimeError si Sonarr est
    muet, ValueError si une saison demandée lui est inconnue (client
    désynchronisé — deviner serait supprimer au hasard).

    Trois familles de torrents, à ne surtout pas confondre :
      - `matched`    : tous leurs fichiers library/ appartiennent aux saisons
                       choisies -> supprimés, données comprises ;
      - `straddling` : à cheval sur une saison gardée (pack multi-saisons) ->
                       CONSERVÉS, on ne retire que les hardlinks library/ des
                       saisons choisies. Aucun espace n'est libéré pour ces
                       fichiers-là, les données restent sous .transmission/data
                       et continuent d'être seedées. Rare (0 sur 87 torrents le
                       2026-08-30) mais un pack S01-S03 peut arriver au prochain
                       grab ;
      - le reste     : intouchés, et leurs fichiers library/ sont `covered`,
                       donc protégés du balayage des fichiers annexes.
    """
    wanted = set(seasons)
    if not wanted:
        raise ValueError("aucune saison sélectionnée")
    episode_files = series_episode_files(series["id"])
    known = set(season_numbers(series, episode_files))
    unknown = wanted - known
    if unknown:
        raise ValueError(f"saison(s) inconnue(s) de Sonarr : {sorted(unknown)}")

    path_season = {f["path"]: f["seasonNumber"] for f in episode_files}
    targets = [f for f in episode_files if f["seasonNumber"] in wanted]

    matched, straddling, survivors = [], [], []
    for entry in find_series_torrents(state["all_torrents"], state["library_index"],
                                       state["cross_seed_child_ids"], series["path"]):
        _t, _hf, lib_matches = entry
        touched = {path_season[p] for p, _s in lib_matches if p in path_season}
        if touched and touched <= wanted:
            matched.append(entry)
        else:
            # Torrent à cheval, OU sans aucun fichier rattachable à une saison
            # connue (fichier que Sonarr ne revendique plus) : dans les deux cas
            # on ne le supprime pas. Conservateur par construction — un torrent
            # qu'on n'arrive pas à rattacher n'est jamais supprimé au jugé.
            survivors.append(entry)
            if touched & wanted:
                straddling.append(entry)

    # Chemins library/ qu'un torrent SURVIVANT couvre encore : ni à balayer, ni
    # à compter comme espace libéré (leur inode reste référencé par les données
    # Transmission). Même espace de chemins que series_orphan_files() — les
    # lib_matches, pas les host_files : c'est le dossier de la série qu'on
    # inspecte, et il ne contient que des hardlinks.
    covered = {p for _t, _hf, lm in survivors for p, _s in lm}

    season_dirs = season_directories(episode_files, wanted, series["path"])
    orphans = []
    for directory in season_dirs:
        orphans += orphan_files_under(directory, covered | {f["path"] for f in targets})
    # Les sidecars sont mis à part pour l'AFFICHAGE seulement — ils partent
    # exactement comme le reste. Depuis le metadata writer, une saison a un .nfo
    # par épisode (21 pour One Piece S23) : les lister à côté des vidéos ferait
    # passer une suppression saine pour une hécatombe, et noierait le seul cas
    # qui mérite qu'on le lise — une VIDÉO que Sonarr ne revendique pas.
    sidecars = [(p, s) for p, s in orphans
                if os.path.splitext(p)[1].lower() in SIDECAR_EXTENSIONS]
    extra_files = [(p, s) for p, s in orphans if (p, s) not in set(sidecars)]

    # Taille annoncée = tout ce qui disparaît de la bibliothèque ; espace libéré
    # = seulement ce dont on retire le DERNIER lien. Les deux diffèrent dès
    # qu'un torrent à cheval survit, et afficher l'un pour l'autre serait le
    # même mensonge que les « 52.0Go » d'un film de 26.0Go du 2026-08-01.
    size_bytes = sum(f.get("size", 0) for f in targets) + sum(s for _p, s in orphans)
    # Déjà compté via les host_files de leur torrent : additionner les deux
    # compterait deux fois les mêmes octets physiques (un fichier library/ est
    # un hardlink d'un fichier du torrent, cf. apply_deletion).
    in_matched = {p for _t, _hf, lm in matched for p, _s in lm}
    freed_bytes = (sum(s for _t, hf, _lm in matched for _p, s in hf)
                   + sum(f.get("size", 0) for f in targets
                         if f["path"] not in covered and f["path"] not in in_matched)
                   + sum(s for _p, s in orphans))

    return {
        "series": series,
        "seasons": sorted(wanted),
        "season_dirs": season_dirs,
        "episode_files": targets,
        "episode_file_ids": [f["id"] for f in targets],
        "matched": matched,
        "straddling": straddling,
        "straddling_paths": sorted(p for _t, _hf, lm in straddling for p, _s in lm
                                    if path_season.get(p) in wanted),
        "covered": covered,
        "orphans": orphans,
        "sidecars": sidecars,
        "extra_files": extra_files,
        "size_bytes": size_bytes,
        "freed_bytes": freed_bytes,
    }


def season_stats(series, episode_files):
    """Une ligne par saison pour l'écran de choix : ce que l'utilisateur doit
    voir AVANT de cocher. Ne calcule rien de destructif et ne fait aucun appel
    supplémentaire — les compteurs viennent des episodefile déjà chargés."""
    rows = []
    by_season = {}
    for f in episode_files:
        by_season.setdefault(f["seasonNumber"], []).append(f)
    monitored = {s["seasonNumber"]: bool(s.get("monitored")) for s in series.get("seasons", [])}
    for number in season_numbers(series, episode_files):
        files = by_season.get(number, [])
        rows.append({
            "number": number,
            "episodes": len(files),
            "size": sum(f.get("size", 0) for f in files),
            "monitored": monitored.get(number, False),
        })
    return rows


def season_breakdown(state, series):
    """Une ligne par saison POUR L'ÉCRAN DE CHOIX, calculée en une seule passe :
    un appel episodefile et un find_series_torrents pour toute la série, pas un
    plan_season_deletion() par saison (qui rappellerait Sonarr N fois et
    rescannerait tous les torrents autant de fois).

    Chaque ligne porte SON propre bilan, donc reste exacte quelle que soit la
    sélection — c'est ce qui permet de tenir la promesse « la modale annonce ce
    qui va partir » sans recalcul côté navigateur, là où un total global
    deviendrait faux dès qu'on décoche une case.

    Renvoie (lignes, à_cheval) — les torrents à cheval sont un fait global, pas
    un attribut de saison : ils ne partent dans AUCUN cas partiel."""
    episode_files = series_episode_files(series["id"])
    path_season = {f["path"]: f["seasonNumber"] for f in episode_files}
    monitored = {s["seasonNumber"]: bool(s.get("monitored")) for s in series.get("seasons", [])}

    own, straddling = {}, []
    for entry in find_series_torrents(state["all_torrents"], state["library_index"],
                                       state["cross_seed_child_ids"], series["path"]):
        torrent, _hf, lib_matches = entry
        touched = {path_season[p] for p, _s in lib_matches if p in path_season}
        if len(touched) == 1:
            own.setdefault(touched.pop(), []).append(torrent)
        elif len(touched) > 1:
            straddling.append((torrent, sorted(touched)))

    by_season = {}
    for f in episode_files:
        by_season.setdefault(f["seasonNumber"], []).append(f)

    rows = []
    for number in season_numbers(series, episode_files):
        files = by_season.get(number, [])
        torrents = own.get(number, [])
        rows.append({
            "number": number,
            "episodes": len(files),
            "size": sum(f.get("size", 0) for f in files),
            "monitored": monitored.get(number, False),
            "torrents": len(torrents),
            "seeding": sum(1 for t in torrents if is_seeding(t)),
            "straddling": sum(1 for _t, touched in straddling if number in touched),
        })
    return rows, straddling


def unmonitor_seasons(series, seasons):
    """Désactive les saisons choisies et REMET monitorNewItems="all" dans la
    même écriture.

    Pourquoi forcer monitorNewItems ici : tout l'intérêt du mode sans purge est
    qu'une saison future soit quand même téléchargée. Une série à "none" rendrait
    la promesse fausse en silence. C'est le défaut de Seerr comme de Sonarr
    (vérifié le 2026-08-30 : les 29 séries et le réglage Seerr sont à "all"), donc
    ce n'est qu'un filet — mais il est gratuit, l'écriture a lieu de toute façon,
    et il couvre aussi les séries ajoutées à la main hors Seerr.

    `monitored` est forcé à True pour la même raison : une série globalement non
    suivie ne prendrait aucune nouvelle saison, quel que soit monitorNewItems.

    Contrairement à plan_sonarr_unmonitor() (vue Torrents), AUCUNE condition de
    « saison terminée » : ici l'utilisateur a désigné la saison explicitement,
    y compris une saison en cours de diffusion dont il ne veut plus."""
    fresh = arr_api(SONARR_URL, SONARR_API_KEY, "GET", f"/api/v3/series/{series['id']}")
    if not fresh:
        logger.warning("unmonitor_seasons: série id=%s introuvable au moment d'écrire", series["id"])
        return False
    wanted = set(seasons)
    for season in fresh.get("seasons", []):
        if season["seasonNumber"] in wanted:
            season["monitored"] = False
    fresh["monitored"] = True
    fresh["monitorNewItems"] = "all"
    return arr_write(SONARR_URL, SONARR_API_KEY, "PUT", f"/api/v3/series/{series['id']}",
                     json_body=fresh,
                     what=f"désactivation des saisons {sorted(wanted)} de {series['title']!r}")


def delete_episode_files(file_ids):
    """Retire les episodefile de la base Sonarr — et c'est Sonarr qui supprime
    le hardlink library/ correspondant.

    Appelé AVANT toute suppression de torrent, délibérément :
      - Sonarr est ainsi seul à toucher aux fichiers qu'il revendique, donc sa
        base ne peut pas rester désynchronisée d'un disque qu'on aurait vidé
        derrière son dos ;
      - le déclencheur onEpisodeFileDelete (activé sur la connexion Jellyfin,
        cf. JELLYFIN_TRIGGERS) signale le bon dossier à Jellyfin. En mode sans
        purge il n'y a pas de onSeriesDelete : sans cet appel, la propagation
        vers Jellyfin puis Kodi ne dépendrait plus que du watcher inotify ;
      - un episodefile dont le fichier a déjà disparu est un cas qu'on s'évite.

    Doit être précédé de unmonitor_seasons() : supprimer un episodefile d'une
    saison encore suivie déclenche quasi instantanément la recherche automatique
    interne de Sonarr, qui re-téléchargerait ce qu'on vient d'effacer (piège
    documenté dans CLAUDE.md, constaté sur un grab manuel en 2026-08).

    L'endpoint bulk n'est appelé qu'une fois : une série entière peut porter
    plusieurs dizaines de fichiers."""
    if not file_ids:
        return True
    return arr_write(SONARR_URL, SONARR_API_KEY, "DELETE", "/api/v3/episodefile/bulk",
                     json_body={"episodeFileIds": list(file_ids)},
                     what=f"suppression de {len(file_ids)} episodefile")


def remove_library_paths(paths):
    """Supprime des hardlinks library/ que Sonarr ne revendique plus (fichiers
    annexes, ou épisodes d'un torrent à cheval que delete_episode_files n'a pas
    couverts). Renvoie (nombre, octets)."""
    removed, freed = 0, 0
    for path in paths:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        try:
            os.remove(path)
            removed += 1
            freed += size
            logger.info("fichier supprimé : %s", path)
            prune_empty_dirs(path)
        except OSError as e:
            logger.warning("échec de suppression de %s : %s", path, e)
    return removed, freed


def execute_delete_seasons(client, plan, all_torrents, cross_seed_groups, linked_ids, missing_ids):
    """Exécute un plan de plan_season_deletion(). Ordre imposé, chaque étape
    dépendant de la précédente :

      1. unmonitor des saisons (+ monitorNewItems="all")  -> ferme la fenêtre de
         recherche automatique AVANT de retirer quoi que ce soit ;
      2. DELETE episodefile/bulk                          -> Sonarr retire ses
         entrées ET les hardlinks library/, et notifie Jellyfin ;
      3. suppression des torrents entièrement couverts    -> libère les données ;
      4. hardlinks résiduels des torrents à cheval        -> le torrent survit ;
      5. balayage des fichiers annexes du dossier de saison.

    Renvoie (all_torrents, freed, deleted, failed, arr_ok)."""
    series = plan["series"]
    arr_ok = unmonitor_seasons(series, plan["seasons"])
    arr_ok = delete_episode_files(plan["episode_file_ids"]) and arr_ok

    all_torrents, freed, deleted, failed, failed_entries = bulk_delete_torrents(
        client, plan["matched"], all_torrents, linked_ids, missing_ids, cross_seed_groups)

    # Hardlinks des saisons choisies portés par un torrent CONSERVÉ : Sonarr les
    # a normalement déjà retirés à l'étape 2 (ce sont ses episodefile), d'où le
    # filtre sur l'existence — ne restent ici que ceux qu'il n'a pas pu ou pas
    # su supprimer. Aucun espace libéré, le torrent seede toujours les données.
    straddling_removed, _straddling_freed = remove_library_paths(
        [p for p in plan["straddling_paths"] if os.path.lexists(p)])

    # Les torrents en échec couvrent toujours leurs fichiers : les balayer
    # supprimerait des données encore seedées, jamais annoncées à l'écran.
    still_covered = plan["covered"] | {p for _t, _hf, lm in failed_entries for p, _s in lm}
    orphan_removed = orphan_freed = 0
    # Dossiers pris DANS LE PLAN : les recalculer ici les chercherait dans des
    # episodefile que l'étape 2 vient justement de supprimer, donc dans une
    # liste vide — plus aucun fichier annexe ne serait balayé.
    for directory in plan["season_dirs"]:
        r, f = cleanup_orphan_files(directory, covered=still_covered)
        orphan_removed += r
        orphan_freed += f
    freed += orphan_freed

    # Le dossier de saison lui-même. Les deux fonctions qui élaguent
    # (remove_library_paths, cleanup_orphan_files) ne le font qu'après avoir
    # supprimé un fichier — or ici c'est SONARR qui a supprimé les siens, donc
    # aucune des deux ne tourne dans le cas courant et les dossiers restaient
    # vides sur le disque (constaté le 2026-08-30 sur One-Punch Man S2/S3).
    # prune_empty_dirs_from remonte tant que c'est vide : le dossier de la série
    # survit tant qu'il lui reste ne serait-ce qu'un tvshow.nfo, ce qui est le
    # comportement voulu — la série, elle, est conservée.
    for directory in plan["season_dirs"]:
        prune_empty_dirs_from(directory, LIBRARY_ROOT)

    logger.info("Sonarr: série %r (id=%s) saisons %s — arr %s, %d episodefile retiré(s) par Sonarr, "
                "%d torrent(s) supprimé(s), %d échec(s), %d hardlink(s) résiduel(s), "
                "%d fichier(s) annexe(s), série CONSERVÉE",
                series["title"], series["id"], plan["seasons"], "OK" if arr_ok else "ÉCHOUÉ",
                len(plan["episode_file_ids"]), deleted, failed, straddling_removed, orphan_removed)
    return all_torrents, freed, deleted, failed, arr_ok


def _arr_covered_paths():
    """(fichiers, dossiers) que Sonarr/Radarr revendiquent dans library/ :
    chemins exacts des episodefile/movieFile importés, et dossiers des titres
    qu'ils connaissent encore (pour leurs sidecars, voir SIDECAR_EXTENSIONS).
    Pas de traduction de chemin à faire, arr et clearr partagent /data_root.

    Contrairement au reste du module, PAS best-effort : lève RuntimeError si un
    appel échoue. Sans la liste des fichiers d'un arr, tout ce qu'il gère
    passerait pour orphelin — proposer de supprimer la moitié de library/ sur un
    timeout serait le pire échec possible de ce balayage. Le handler global de
    webapp.py rend déjà un RuntimeError en bandeau lisible."""
    series = arr_api(SONARR_URL, SONARR_API_KEY, "GET", "/api/v3/series")
    movies = arr_api(RADARR_URL, RADARR_API_KEY, "GET", "/api/v3/movie")
    # On teste la FORME attendue, pas seulement None : arr_api ne renvoie None
    # que sur un échec de transport, et rend {} sur un corps de réponse vide
    # (200 sans contenu, 204, réponse tronquée par un arr qui redémarre). {}
    # n'étant pas None, l'ancien test laissait passer — et `for m in movies:`
    # itère alors zéro fois SANS lever, donc couverture vide : les 241 .nfo et
    # tout fichier dont l'arr a déjà retiré le torrent ressortaient orphelins,
    # proposés à la suppression. Exactement la catastrophe que ce garde-fou
    # existe pour empêcher, par le seul chemin qu'il ne couvrait pas.
    if not isinstance(series, list) or not isinstance(movies, list):
        bad = "Sonarr" if not isinstance(series, list) else "Radarr"
        raise RuntimeError(f"{bad} n'a pas renvoyé de liste exploitable : impossible de distinguer "
                           "un fichier orphelin d'un fichier géré par un arr, balayage abandonné.")
    files, dirs = set(), []
    for m in movies:
        movie_file = m.get("movieFile") or {}
        if movie_file.get("path"):
            files.add(movie_file["path"])
        if m.get("path"):
            dirs.append(m["path"].rstrip("/") + "/")
    for s in series:
        if not s.get("path"):
            continue
        dirs.append(s["path"].rstrip("/") + "/")
        episode_files = arr_api(SONARR_URL, SONARR_API_KEY, "GET", "/api/v3/episodefile",
                                params={"seriesId": s["id"]})
        if not isinstance(episode_files, list):  # même raison qu'au-dessus : {} n'est pas None
            raise RuntimeError(f"Sonarr n'a pas renvoyé les fichiers de {s.get('title')!r} : "
                               "balayage abandonné plutôt que de compter ses épisodes orphelins.")
        for episode_file in episode_files:
            if episode_file.get("path"):
                files.add(episode_file["path"])
    logger.debug("couverture arr de library/ : %d fichier(s), %d dossier(s) de titre",
                 len(files), len(dirs))
    return files, dirs


def library_orphan_files(state):
    """[(chemin, taille), ...] des fichiers de library/ qu'aucun torrent ne
    couvre ET qu'aucun arr ne connaît. Comble un trou constaté le 2026-08-06 :
    les 3 vues de clearr partent des torrents Transmission ou des objets arr,
    donc un résidu qui n'est ni l'un ni l'autre n'apparaît nulle part — et n'est
    même pas supprimable depuis Kodi, dont le repli par chemin s'interdit
    library/ (voir is_arr_managed_path).

    Un torrent couvre un fichier s'il partage son inode (hardlink, la seule
    relation qui existe entre .transmission/data et library/) ; un arr le couvre
    si c'est un de ses episodefile/movieFile, ou un sidecar sous le dossier d'un
    titre qu'il connaît encore. Un fichier vidéo posé dans le dossier d'une
    série suivie mais jamais importé par Sonarr est donc bien un orphelin.

    Calculé à la demande (2 + N appels arr, N = nombre de séries), jamais au
    rendu d'un onglet — c'est ce qui justifie un bouton plutôt qu'une 4e vue.
    Repart de l'index de load_full_state() plutôt que de re-walker library/ :
    c'est le même parcours, et son inode est nécessaire au test de couverture
    torrent (un fichier que l'indexation n'a pas pu stat est donc hors
    périmètre, il ressort dans les warnings de build_library_index)."""
    library_index = state["library_index"]
    covered = {library_index[inode]
               for t in state["all_torrents"]
               for inode in (t.get("_inodes") or ())
               if inode in library_index}
    arr_files, arr_dirs = _arr_covered_paths()
    covered |= arr_files

    orphans = []
    for path in sorted(library_index.values()):
        if path in covered:
            continue
        if path.lower().endswith(SIDECAR_EXTENSIONS) and any(path.startswith(d) for d in arr_dirs):
            continue
        try:
            orphans.append((path, os.path.getsize(path)))
        except OSError:
            orphans.append((path, 0))
    logger.info("balayage library/ : %d fichier(s) indexé(s), %d couvert(s), %d orphelin(s)",
                len(library_index), len(covered), len(orphans))
    return orphans


def delete_library_orphans(orphans):
    """Supprime les fichiers rendus par library_orphan_files() et élague les
    dossiers devenus vides jusqu'à library/. Aucun appel Transmission ni arr :
    par construction ces fichiers ne sont connus ni de l'un ni de l'autre."""
    removed, freed, failed = 0, 0, 0
    for path, size in orphans:
        try:
            os.remove(path)
            removed += 1
            freed += size
            logger.info("orphelin de library/ supprimé : %s", path)
            prune_empty_dirs(path, LIBRARY_ROOT)
        except OSError as e:
            logger.warning("échec de suppression de l'orphelin %s : %s", path, e)
            failed += 1
    return removed, freed, failed


def cleanup_orphan_files(target_path, root=LIBRARY_ROOT, covered=()):
    """Supprime tout fichier restant sous un dossier après le passage de
    bulk_delete_torrents — un fichier sans torrent Transmission correspondant
    (retiré par un autre moyen, ou jamais suivi) resterait sinon orphelin
    malgré la promesse "tout le titre est supprimé". `root` borne l'élagage des
    dossiers devenus vides : library/ pour une série Sonarr, completed/ pour un
    titre hors arr (voir execute_delete_media_path).

    `covered` = chemins qu'un torrent VIVANT couvre encore, à ne pas toucher.
    Le raisonnement implicite d'avant — « après bulk_delete_torrents, ce qui
    reste est par définition orphelin » — est faux dès qu'un torrent du lot a
    échoué, puisque la boucle capture l'échec et poursuit : on supprimait alors
    les données de torrents toujours présents dans Transmission, jamais
    annoncées dans l'écran de confirmation (qui, lui, les exclut déjà via
    orphan_files_under). Même espace de chemins que `covered` là-bas : fichiers
    Transmission pour un titre hors arr, correspondances library/ pour une
    série Sonarr."""
    removed = 0
    freed = 0
    covered = set(covered)
    if not os.path.isdir(target_path):
        return removed, freed
    for walk_root, _dirs, files in os.walk(target_path):
        for name in files:
            path = os.path.join(walk_root, name)
            if path in covered:
                logger.info("conservé (torrent toujours présent après un échec de suppression) : %s", path)
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            try:
                os.remove(path)
                removed += 1
                freed += size
                logger.info("fichier orphelin supprimé : %s", path)
                prune_empty_dirs(path, root)
            except OSError as e:
                logger.warning("échec de suppression du fichier orphelin %s : %s", path, e)
    return removed, freed


def execute_delete_series(client, series, matched, all_torrents, cross_seed_groups, linked_ids, missing_ids):
    """Supprime tous les torrents connus de la série (matched, déjà calculé par
    l'appelant pour l'écran de confirmation — pas recalculé ici, ça évite de
    rescanner tous les torrents une deuxième fois pour le même résultat) + les
    fichiers résiduels de son dossier, puis retire la série de Sonarr
    (deleteFiles=false : les fichiers ont déjà été supprimés ici même, comme
    pour un film via plan_radarr_deletion) avec exclusion de liste pour ne
    jamais la voir revenir via un import list sync.

    C'est le chemin de PURGE, et il reste inchangé — la suppression partielle
    passe par plan_season_deletion() + execute_delete_seasons(), volontairement
    séparées plutôt qu'ajoutées ici en paramètres : les deux ne partagent ni
    l'ordre des écritures Sonarr, ni ce qu'elles balaient, ni ce qu'elles
    promettent. La TUI et le bouton « Purger » de l'UI web appellent celle-ci."""
    host_path = series["path"]  # même mount /data_root que Sonarr, aucune traduction nécessaire
    all_torrents, freed, deleted, failed, failed_entries = bulk_delete_torrents(
        client, matched, all_torrents, linked_ids, missing_ids, cross_seed_groups)
    # Espace de chemins : le dossier de la série ne contient que des hardlinks
    # library/, donc ce sont les lib_matches des torrents en échec qu'il faut
    # préserver (mêmes chemins que ceux qu'orphan_files_under exclut côté
    # écran de confirmation), pas leurs fichiers Transmission.
    still_covered = {p for _t, _hf, lib_matches in failed_entries for p, _s in lib_matches}
    orphan_removed, _orphan_freed = cleanup_orphan_files(host_path, covered=still_covered)
    arr_ok = arr_write(SONARR_URL, SONARR_API_KEY, "DELETE", f"/api/v3/series/{series['id']}",
                       params={"deleteFiles": "false", "addImportListExclusion": "true"},
                       what=f"retrait de la série {series['title']!r}")
    logger.info("Sonarr: série %r (id=%s) — retrait arr %s, %d torrent(s) supprimé(s), "
                "%d échec(s), %d fichier(s) orphelin(s)",
                series["title"], series["id"], "OK" if arr_ok else "ÉCHOUÉ",
                deleted, failed, orphan_removed)
    return all_torrents, freed, deleted, failed, arr_ok


def execute_delete_movie_no_torrent(movie):
    """Chemin de repli quand find_movie_torrent() ne trouve aucun torrent
    (jamais téléchargé, ou fichier orphelin hors suivi Transmission) : Radarr
    supprime lui-même son fichier (deleteFiles=true) puisqu'aucun torrent ne
    s'en charge ici, contrairement au chemin normal où plan_radarr_deletion
    laisse toujours deleteFiles=false (le fichier est déjà retiré par ce
    script via lib_matches)."""
    arr_ok = arr_write(RADARR_URL, RADARR_API_KEY, "DELETE", f"/api/v3/movie/{movie['id']}",
                       params={"deleteFiles": "true" if movie.get("hasFile") else "false",
                               "addImportExclusion": "true"},
                       what=f"retrait du film {movie['title']!r}")
    logger.info("Radarr: film %r (id=%s) — retrait arr %s (sans torrent correspondant)",
                movie["title"], movie["id"], "OK" if arr_ok else "ÉCHOUÉ")
    # Ici l'échec est total : c'est Radarr qui devait supprimer le fichier
    # (deleteFiles=true), aucun torrent ne s'en charge sur ce chemin.
    return arr_ok


# --- Titres hors Sonarr/Radarr, résolus par leur chemin -----------------------
#
# Un film/une série récupéré à la main reste sous .transmission/data/completed/
# (monté tel quel comme bibliothèque Jellyfin, cf. jellyfin/docker-compose.
# override.yml) : il n'existe dans aucune des deux instances arr, donc ni
# find_*_by_external_ids ni les vues Séries/Films ne peuvent le retrouver. Le
# seul identifiant que Kodi et clearr ont en commun pour ces titres est alors le
# CHEMIN — et il est même plus fiable que les ids externes, deux dossiers
# distincts pouvant très bien porter le même tvdbId (constaté : les deux saisons
# de Hell's Paradise, téléchargées séparément, sont deux séries pour Jellyfin).
#
# Kodi connaît ce chemin (jellyfin-kodi en chemins directs, useDirectPaths=1),
# mais tel que le voit SA machine — pas tel que le voit ce conteneur
# (/data_root/...). D'où resolve_media_path() : aucune hypothèse sur le préfixe
# du client, on cherche le plus long suffixe de composants qui existe réellement
# sous une racine connue. Fonctionne donc aussi bien pour un Kodi local que pour
# un client qui monterait completed/ par un partage réseau.

MEDIA_ROOTS = (COMPLETED_ROOT, LIBRARY_ROOT)

# Deux composants minimum (catégorie + titre, ex. "anime/Noragami") : avec un
# seul, un chemin se terminant par "film" résoudrait sur completed/film, donc
# sur toute la catégorie.
MIN_MEDIA_PATH_COMPONENTS = 2


def resolve_media_path(client_path):
    """Chemin réel sous /data_root correspondant au chemin vu par un client
    (Kodi), ou None. Les composants vides sont ignorés — Kodi produit des
    doubles slashes (".../completed/anime//Noragami") — et `..` est écarté
    plutôt que résolu : c'est ce qui garantit qu'aucun chemin fourni de
    l'extérieur ne puisse désigner quoi que ce soit hors des racines."""
    parts = [c for c in client_path.replace("\\", "/").split("/") if c not in ("", ".", "..")]
    for size in range(len(parts), MIN_MEDIA_PATH_COMPONENTS - 1, -1):
        suffix = os.path.join(*parts[-size:])
        hits = [os.path.join(root, suffix) for root in MEDIA_ROOTS
                if os.path.exists(os.path.join(root, suffix))]
        if len(hits) == 1:
            logger.info("chemin client %r résolu en %s", client_path, hits[0])
            return hits[0]
        if len(hits) > 1:
            logger.warning("chemin client %r ambigu : %s — abandon", client_path, ", ".join(hits))
            return None
    logger.warning("chemin client %r introuvable sous %s", client_path, " / ".join(MEDIA_ROOTS))
    return None


def is_arr_managed_path(path):
    """True pour un chemin de library/, l'arborescence que Sonarr/Radarr
    organisent. La suppression par chemin s'y interdit : le titre y est presque
    toujours suivi par un arr, le supprimer sans retirer son entrée le ferait
    simplement re-télécharger à la prochaine recherche."""
    return os.path.commonpath([path, LIBRARY_ROOT]) == LIBRARY_ROOT


def find_torrents_under_path(all_torrents, library_index, cross_seed_child_ids, target):
    """Torrents top-level (hors enfants cross-seed, cascadés avec leur parent)
    ayant au moins un fichier dans/sous `target`. Pendant de
    find_series_torrents pour un titre hors arr : on compare aux chemins réels
    des fichiers du torrent, pas à des correspondances library/ — un titre hors
    arr n'a par définition aucun fichier dans library/."""
    prefix = target.rstrip("/") + "/"
    result = []
    for t in all_torrents:
        if t["id"] in cross_seed_child_ids:
            continue
        host_files = torrent_host_files(t)
        if not any(p == target or p.startswith(prefix) for p, _s in host_files):
            continue
        result.append((t, host_files, find_library_matches(host_files, library_index)))
    return result


def plan_media_path_deletion(state, target):
    """Ce qui serait supprimé pour un titre hors arr : ses torrents, et les
    fichiers restants du dossier qu'aucun torrent ne couvre (cas réel : un
    dossier dont les torrents ont été retirés de Transmission depuis longtemps,
    les fichiers étant restés). Sert l'écran de confirmation de l'addon Kodi
    avant d'exécuter quoi que ce soit."""
    matched = find_torrents_under_path(state["all_torrents"], state["library_index"],
                                        state["cross_seed_child_ids"], target)
    covered = {p for _t, host_files, _lm in matched for p, _s in host_files}
    torrent_files = len(covered)
    torrent_bytes = sum(s for _t, host_files, _lm in matched for _p, s in host_files)

    orphans = orphan_files_under(target, covered)
    orphan_bytes = sum(s for _p, s in orphans)

    return {
        "target": target,
        "title": os.path.basename(target.rstrip("/")),
        "matched": matched,
        "torrents": len(matched),
        "seeding": sum(1 for t, _hf, _lm in matched if is_seeding(t)),
        "files": torrent_files + len(orphans),
        "orphan_files": len(orphans),
        # Liste détaillée en plus du compte : la modale Kodi comme celle du web
        # nomment les fichiers sans torrent, sinon leur taille apparaît dans le
        # total sans qu'on puisse voir à quoi elle correspond.
        "orphans": [{"name": os.path.basename(p), "size": human_size(s)} for p, s in orphans],
        "size_bytes": torrent_bytes + orphan_bytes,
    }


def execute_delete_media_path(client, plan, all_torrents, cross_seed_groups, linked_ids, missing_ids):
    """Supprime un titre hors arr : ses torrents (avec leurs données, comme
    partout ailleurs), puis les fichiers du dossier qu'aucun torrent ne couvrait
    — sans quoi un dossier comme celui rencontré le 2026-08-06 (2,6 Go, plus
    aucun torrent) resterait insupprimable depuis Kodi. Aucun appel Sonarr/
    Radarr : par construction, ce titre n'y existe pas."""
    target = plan["target"]
    root = COMPLETED_ROOT if os.path.commonpath([target, COMPLETED_ROOT]) == COMPLETED_ROOT \
        else os.path.dirname(target)
    all_torrents, freed, deleted, failed, failed_entries = bulk_delete_torrents(
        client, plan["matched"], all_torrents, linked_ids, missing_ids, cross_seed_groups)
    # Espace de chemins inverse de celui d'execute_delete_series : les DONNÉES
    # d'un titre hors arr sont sous target, donc ce sont les host_files des
    # torrents en échec qu'il faut préserver (même critère qu'au calcul de
    # `covered` ligne ~1065, qui alimente l'écran de confirmation).
    still_covered = {p for _t, host_files, _lm in failed_entries for p, _s in host_files}
    orphan_removed, orphan_freed = cleanup_orphan_files(target, root, covered=still_covered)
    if os.path.isdir(target):
        prune_empty_dirs_from(target, root)
    elif os.path.isfile(target):
        # Fichier isolé (film posé directement dans completed/film/) qu'aucun
        # torrent ne couvrait : cleanup_orphan_files ne descend que dans un
        # dossier, il reste à le retirer lui-même.
        try:
            orphan_freed += os.path.getsize(target)
            os.remove(target)
            orphan_removed += 1
            logger.info("fichier orphelin supprimé : %s", target)
            prune_empty_dirs(target, root)
        except OSError as e:
            logger.warning("échec de suppression du fichier orphelin %s : %s", target, e)
    logger.info("titre hors arr %r supprimé : %d torrent(s), %d échec(s), %d fichier(s) sans torrent",
                plan["title"], deleted, failed, orphan_removed)
    return all_torrents, freed + orphan_freed, deleted, failed


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


def prune_empty_dirs(path, root=LIBRARY_ROOT):
    prune_empty_dirs_from(os.path.dirname(path), root)


def prune_empty_dirs_from(directory, root=LIBRARY_ROOT):
    d = directory
    while d and os.path.commonpath([d, root]) == root and d != root:
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
        except FileNotFoundError:
            # Déjà parti, et c'est le cas NORMAL sur le chemin de suppression par
            # saison : Sonarr a retiré ses episodefile (et leurs hardlinks) avant
            # qu'on touche aux torrents. Un warning par fichier ferait passer une
            # dizaine de lignes d'alerte pour une suppression parfaitement saine.
            logger.debug("fichier library déjà absent : %s", path)
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
