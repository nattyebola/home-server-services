#!/usr/bin/env python3
# TUI de nettoyage manuel : liste les torrents Transmission (un par ligne,
# même pour un pack saison), et à la sélection supprime le torrent (+ ses
# fichiers) et les fichiers hardlinkés correspondants dans library/ — les
# deux copies visées par CLAUDE.md "Un film/série téléchargé... 3 emplacements"
# (bibliothèque + téléchargement ; cross-seed n'a pas besoin d'être géré à
# part, ses torrents injectés vivent dans cette même instance Transmission).
#
# Ne touche pas à Sonarr/Radarr : si le titre est encore monitored, il peut
# le re-demander plus tard (RSS/recherche manuelle) — désactiver le
# monitoring ou blacklister la release reste un geste séparé, volontaire.
#
# Marqueur 'M' (torrent dont le fichier a disparu du disque, ex. supprimé
# manuellement hors de cet outil) + Maj+P pour les purger tous en un coup
# côté Transmission — ajouté le 2026-07-28 après un rattrapage cross-seed
# qui en a fait remonter 6 d'un coup, tous antérieurs à la stack arr.
#
# Arbre cross-seed (→/l déplier, ←/h replier) : les torrents qui partagent
# au moins un fichier réel (même (dev, inode), voir build_cross_seed_groups)
# sont une seule et même release injectée sur plusieurs indexeurs par
# arr/cross-seed — regroupés sous un parent (le téléchargement d'origine,
# pas une entrée .cross-seed-links/) avec un compteur repliable plutôt que
# listés comme des torrents indépendants sans rapport apparent entre eux.
# Supprimer le parent supprime aussi ses enfants cross-seed (voir
# do_delete/apply_deletion, ajouté le 2026-07-29) : une fois le
# téléchargement d'origine parti, ses cross-seeds n'ont plus rien à seeder.
# Supprimer un enfant seul ne touche ni au parent ni aux autres cross-seeds
# — comme pour n'importe quel torrent, delete-local-data retire son propre
# fichier (un symlink .cross-seed-links/<tracker>/... pour un enfant), sans
# suivre le lien vers sa cible.
import curses
import json
import logging
import os
import subprocess
import sys
import time
import traceback
import urllib.parse

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRANSMISSION_CONTAINER = "vpn-transmission-vpn-1"
RPC_URL = "http://localhost:9091/transmission/rpc"

COLOR_LINKED = 1   # vert : fichier(s) présents dans library/, ratio confortable
COLOR_DANGER = 2   # rouge : action destructive, ratio bas, échec
COLOR_WARN = 3     # jaune : à vérifier (pas de correspondance library/, ratio moyen)
COLOR_HEADER = 4   # cyan : en-têtes/titres
COLORS_ON = False  # positionné dans main() selon curses.has_colors()


def cp(n):
    return curses.color_pair(n) if COLORS_ON else 0


def ratio_color(ratio):
    if ratio < 1.0:
        return COLOR_DANGER
    if ratio < 3.0:
        return COLOR_WARN
    return COLOR_LINKED


def load_env_file(path):
    """Vide si le fichier n'existe pas (arr/.env est optionnel — voir
    PROWLARR_API_KEY plus bas) plutôt que de lever, .env.shared reste
    obligatoire mais c'est load_data_root() qui le fait échouer clairement."""
    values = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key] = value
    except FileNotFoundError:
        pass
    return values


def load_data_root():
    env = load_env_file(os.path.join(REPO_ROOT, ".env.shared"))
    if "DATA_ROOT" not in env:
        raise RuntimeError(f"{REPO_ROOT}/.env.shared introuvable ou sans DATA_ROOT")
    return env["DATA_ROOT"]


try:
    DATA_ROOT = load_data_root()
except RuntimeError as e:
    # Avant curses : une sortie directe et lisible plutôt qu'une trace Python
    # brute (le bloc try/except de __main__ ne couvre que curses.wrapper(main),
    # pas cette initialisation faite à l'import du module).
    print(f"Erreur : {e}", file=sys.stderr)
    sys.exit(1)
LIBRARY_ROOT = os.path.join(DATA_ROOT, "library")
TRANSMISSION_DATA_ROOT = os.path.join(DATA_ROOT, ".transmission", "data")
LOG_PATH = os.path.join(DATA_ROOT, ".torrent-cleanup.log")

_ARR_ENV = load_env_file(os.path.join(REPO_ROOT, "arr", ".env"))


def parse_tracker_aliases(raw):
    """TRACKER_ALIASES=domaine1=Nom1,domaine2=Nom1,... (arr/.env, voir
    .env.example) — trackers publics génériques qu'aucune API ne permet de
    rattacher à un indexeur (Prowlarr n'expose que les domaines du site,
    voir build_prowlarr_tracker_map, pas ceux du tracker BitTorrent
    lui-même embarqué dans les .torrent). Propre à ce déploiement (quels
    trackers publics tel ou tel indexeur utilise en pratique) : en config
    plutôt qu'en dur dans un script versionné sur un repo public — demandé
    par l'utilisateur le 2026-07-28 après un premier essai en dur pour
    Nyaa.si."""
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


TRACKER_ALIASES = parse_tracker_aliases(_ARR_ENV.get("TRACKER_ALIASES", ""))

PROWLARR_CONTAINER = "arr-prowlarr-1"
PROWLARR_URL = "http://localhost:9696/api/v1/indexer"
PROWLARR_API_KEY = _ARR_ENV.get("PROWLARR_API_KEY")

RADARR_CONTAINER = "arr-radarr-1"
RADARR_URL = "http://localhost:7878"
RADARR_API_KEY = _ARR_ENV.get("RADARR_API_KEY")

SONARR_CONTAINER = "arr-sonarr-1"
SONARR_URL = "http://localhost:8989"
SONARR_API_KEY = _ARR_ENV.get("SONARR_API_KEY")

# Chemin vu par sonarr/radarr pour un fichier de library/ : les deux montent
# ${DATA_ROOT}:/data_root en un seul mount (voir CLAUDE.md, fix hardlink) —
# un chemin host sous DATA_ROOT devient /data_root/... côté conteneur.
def host_to_arr_path(host_path):
    return host_path.replace(DATA_ROOT, "/data_root", 1)

# Un seul fichier de log, pensé pour du debug/maintenance a posteriori : DEBUG
# = détail RPC/lookups (utile pour comprendre un comportement inattendu),
# INFO = actions normales (lancement, suppressions), WARNING = anomalie
# récupérée sans bloquer (fichier déjà absent, échec de suppression isolé),
# ERROR = échec qui empêche l'action demandée. Niveau DEBUG volontairement
# actif par défaut : c'est un outil manuel, pas un service tournant en
# continu, le volume de log reste négligeable.
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("torrent-cleanup")


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
        cmd = ["docker", "exec", "-i", TRANSMISSION_CONTAINER, "curl", "-s", "-i",
               "-X", "POST", "-d", "@-", RPC_URL]
        if self.session_id:
            cmd[6:6] = ["-H", f"X-Transmission-Session-Id: {self.session_id}"]
        payload = json.dumps({"method": method, "arguments": arguments or {}}).encode()
        try:
            res = subprocess.run(cmd, input=payload, capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            logger.error("RPC %s: timeout (docker exec %s injoignable ?)", method, TRANSMISSION_CONTAINER)
            raise RuntimeError(f"Transmission RPC timeout sur {method}")
        if res.returncode != 0:
            logger.error("RPC %s: docker exec a échoué (code %s): %s", method, res.returncode,
                         res.stderr.decode(errors="replace").strip())
            raise RuntimeError(f"docker exec {TRANSMISSION_CONTAINER} a échoué — container arrêté ?")
        text = res.stdout.decode(errors="replace")
        head, _, body = text.partition("\r\n\r\n")
        status_line = head.split("\r\n", 1)[0]
        if " 409 " in status_line:
            for line in head.split("\r\n"):
                if line.lower().startswith("x-transmission-session-id:"):
                    self.session_id = line.split(":", 1)[1].strip()
            logger.debug("nouveau X-Transmission-Session-Id obtenu, on rejoue %s", method)
            return self.call(method, arguments)
        if not body.strip():
            logger.error("RPC %s: réponse vide (statut: %s)", method, status_line)
            raise RuntimeError(f"réponse Transmission vide (statut: {status_line})")
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
    échoue alors qu'ils dérivent bien du même indexeur (repéré le 2026-07-28,
    YggReborn ne se résolvait jamais)."""
    parts = hostname.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else hostname


def build_prowlarr_tracker_map():
    """domaine de base (ex. tr4ker.net) -> nom d'indexeur Prowlarr (ex.
    TR4KER). Si le tracker BT utilise un domaine de base sans rapport avec
    le(s) site(s) Prowlarr (mutualisé entre plusieurs indexeurs — ex. les
    trackers publics génériques que Nyaa.si ajoute en plus du sien,
    open.stealth.si/opentrackr.org/exodus.desync.com/tracker.torrent.eu.org,
    aucun n'appartenant à Nyaa en propre), aucun match possible : on retombe
    sur le hostname brut plutôt que d'inventer un nom — cf. base_domain()."""
    if not PROWLARR_API_KEY:
        logger.warning("PROWLARR_API_KEY introuvable dans arr/.env — noms de tracker non résolus")
        return {}
    cmd = ["docker", "exec", PROWLARR_CONTAINER, "curl", "-s",
           "-H", f"X-Api-Key: {PROWLARR_API_KEY}", PROWLARR_URL]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        logger.warning("Prowlarr injoignable (timeout) — noms de tracker non résolus")
        return {}
    if res.returncode != 0:
        logger.warning("docker exec %s a échoué — noms de tracker non résolus : %s",
                        PROWLARR_CONTAINER, res.stderr.decode(errors="replace").strip())
        return {}
    try:
        indexers = json.loads(res.stdout)
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
    # TRACKER_ALIASES (arr/.env) d'abord : couvre les trackers publics
    # génériques qu'aucune API ne permet de rattacher à un indexeur (voir
    # parse_tracker_aliases). Matché en exact, jamais via base_domain — un
    # domaine public à 2 labels type eu.org serait un bien pire faux positif
    # que l'inverse.
    if hostname in TRACKER_ALIASES:
        return TRACKER_ALIASES[hostname]
    return tracker_map.get(base_domain(hostname), hostname)


def tracker_display(torrent, tracker_map):
    hosts = tracker_host(torrent)
    if hosts == "?":
        return "?"
    # dict.fromkeys plutôt qu'un set : dédup en préservant l'ordre. Via
    # TRACKER_ALIASES, plusieurs hosts d'un même torrent peuvent résoudre
    # vers le même nom (ex. les 5 trackers publics de Nyaa) — sans ça, la
    # colonne affichait "Nyaa.si,Nyaa.si,Nyaa.si,Nyaa.si,Nyaa.si".
    names = (resolve_tracker_name(h, tracker_map) for h in hosts.split(","))
    return ",".join(dict.fromkeys(names))


def arr_api(container, base_url, api_key, method, path, params=None, json_body=None):
    """GET/PUT/DELETE générique vers Sonarr/Radarr, même schéma docker-exec-curl
    que Transmission/Prowlarr. Renvoie None sur tout échec (clé absente,
    container injoignable, timeout, JSON illisible) — chaque appelant doit
    dégrader proprement plutôt que planter (best-effort, comme
    build_prowlarr_tracker_map)."""
    if not api_key:
        return None
    url = base_url + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    cmd = ["docker", "exec", "-i", container, "curl", "-s", "-X", method, "-H", f"X-Api-Key: {api_key}"]
    payload = None
    if json_body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", "@-"]
        payload = json.dumps(json_body).encode()
    cmd.append(url)
    try:
        res = subprocess.run(cmd, input=payload, capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        logger.warning("arr_api %s %s: timeout", method, path)
        return None
    if res.returncode != 0:
        logger.warning("arr_api %s %s: docker exec a échoué: %s", method, path,
                        res.stderr.decode(errors="replace").strip())
        return None
    if not res.stdout.strip():
        return {}
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        logger.warning("arr_api %s %s: réponse illisible: %r", method, path, res.stdout[:300])
        return None


def plan_radarr_deletion(container_paths):
    """container_paths: set de chemins (vus par les conteneurs arr) déjà
    identifiés comme présents dans library/. Renvoie (plan, chemins_matchés)
    — les chemins matchés sont retirés du set par l'appelant avant de tenter
    le matching Sonarr (un fichier ne peut être qu'un film OU un épisode)."""
    if not RADARR_API_KEY or not container_paths:
        return [], set()
    movies = arr_api(RADARR_CONTAINER, RADARR_URL, RADARR_API_KEY, "GET", "/api/v3/movie")
    if not movies:
        return [], set()
    plan, matched = [], set()
    for m in movies:
        mf = m.get("movieFile")
        if mf and mf.get("path") in container_paths:
            matched.add(mf["path"])
            plan.append({
                "description": f'Radarr : "{m["title"]}" retiré complètement (+ exclusion de liste)',
                "kind": "radarr_delete",
                "movie_id": m["id"],
                "title": m["title"],
            })
    return plan, matched


def plan_sonarr_unmonitor(container_paths):
    """Regroupe par série (préfixe de series.path), puis par saison. Une
    saison n'est désactivée en bloc que si TOUS ses fichiers connus de
    Sonarr sont dans ce qu'on supprime ET qu'elle est terminée (aucun
    épisode restant à venir) — sinon on désactive juste les épisodes
    concernés, pour ne pas couper une saison en cours de diffusion (cf.
    discussion utilisateur : les prochains épisodes doivent continuer à
    être surveillés)."""
    if not SONARR_API_KEY or not container_paths:
        return []
    series_list = arr_api(SONARR_CONTAINER, SONARR_URL, SONARR_API_KEY, "GET", "/api/v3/series")
    if not series_list:
        return []
    plan = []
    for series in series_list:
        prefix = series["path"] + "/"
        matched_paths = {p for p in container_paths if p.startswith(prefix)}
        if not matched_paths:
            continue
        episodefiles = arr_api(SONARR_CONTAINER, SONARR_URL, SONARR_API_KEY, "GET", "/api/v3/episodefile",
                                params={"seriesId": series["id"]}) or []
        matched_ef_ids = {ef["id"] for ef in episodefiles if ef["path"] in matched_paths}
        if not matched_ef_ids:
            continue
        episodes = arr_api(SONARR_CONTAINER, SONARR_URL, SONARR_API_KEY, "GET", "/api/v3/episode",
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
    """Point d'entrée : chemins host déjà trouvés dans library/ (find_library_matches)
    -> plan d'actions Sonarr/Radarr. Best-effort : une clé API absente ou une
    instance injoignable réduit juste le plan, ne bloque jamais la suppression
    des fichiers elle-même."""
    container_paths = {host_to_arr_path(p) for p, _size in lib_matches}
    radarr_plan, matched = plan_radarr_deletion(container_paths)
    remaining = container_paths - matched
    sonarr_plan = plan_sonarr_unmonitor(remaining)
    return radarr_plan + sonarr_plan


def execute_arr_plan(plan):
    for action in plan:
        try:
            if action["kind"] == "radarr_delete":
                arr_api(RADARR_CONTAINER, RADARR_URL, RADARR_API_KEY, "DELETE",
                        f"/api/v3/movie/{action['movie_id']}",
                        params={"deleteFiles": "false", "addImportExclusion": "true"})
                logger.info("Radarr: film retiré id=%s (%s)", action["movie_id"], action["title"])
            elif action["kind"] == "sonarr_season":
                series = arr_api(SONARR_CONTAINER, SONARR_URL, SONARR_API_KEY, "GET",
                                  f"/api/v3/series/{action['series_id']}")
                if not series:
                    logger.warning("sonarr_season: série id=%s introuvable au moment d'exécuter", action["series_id"])
                    continue
                for season in series["seasons"]:
                    if season["seasonNumber"] == action["season_number"]:
                        season["monitored"] = False
                arr_api(SONARR_CONTAINER, SONARR_URL, SONARR_API_KEY, "PUT",
                        f"/api/v3/series/{action['series_id']}", json_body=series)
                logger.info("Sonarr: série id=%s saison %s désactivée", action["series_id"], action["season_number"])
            elif action["kind"] == "sonarr_episodes":
                arr_api(SONARR_CONTAINER, SONARR_URL, SONARR_API_KEY, "PUT", "/api/v3/episode/monitor",
                        json_body={"episodeIds": action["episode_ids"], "monitored": False})
                logger.info("Sonarr: épisodes désactivés ids=%s", action["episode_ids"])
        except Exception as e:
            logger.warning("échec d'exécution de l'action arr %r : %s", action.get("description"), e)


# Champs de tri disponibles (touche 's' pour passer au suivant, 'S' pour
# inverser le sens du champ courant) — même ordre que les colonnes affichées.
# BIB/ABS (bibliothèque/absent, ex-marqueurs 'L'/'M') lisent des attributs
# précalculés (_linked/_missing, posés une seule fois dans main() en même
# temps que linked_ids/missing_ids) plutôt que de refaire un lookup dans ces
# sets ici : un lambda de SORT_FIELDS ne reçoit que le torrent, pas les sets
# externes.
SORT_FIELDS = [
    ("BIB", lambda t: t["_linked"]),
    ("ABS", lambda t: t["_missing"]),
    ("AGE", lambda t: t["addedDate"]),
    ("TAILLE", lambda t: t["totalSize"]),
    ("RATIO", lambda t: t["uploadRatio"]),
    ("TRACKER", lambda t: t.get("_tracker_name", "")),
    ("NOM", lambda t: t["name"].lower()),
]


def sort_torrents(torrents, sort_idx, reverse):
    _label, key_func = SORT_FIELDS[sort_idx]
    torrents.sort(key=key_func, reverse=reverse)


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
            # l'outil pour un seul torrent (déjà arrivé en dev : une
            # ValueError non rattrapée ici tuait la session au chargement,
            # avant même l'affichage de la liste).
            logger.warning("torrent %r (id=%s) : %s — fichier ignoré",
                            torrent.get("name"), torrent.get("id"), e)
    return paths


def build_library_index():
    """(device, inode) -> chemin, pour tout fichier sous library/ — construit
    une seule fois par session plutôt qu'à chaque torrent (évite de re-walker
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
    tel que VU PAR LE CONTENEUR (/data/completed/...), pas le chemin hôte où
    tourne ce script. Un os.stat() nu suit le lien avec la racine de l'hôte,
    qui n'a pas de /data : le fichier semble absent alors qu'il existe très
    bien — constaté le 2026-07-28, 91 faux positifs sur ~230 torrents (tous
    les cross-seeds injectés dans le rattrapage du jour) avant ce fix.
    Renvoie None si le fichier (ou sa cible réelle) n'existe pas."""
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
    import Sonarr/Radarr, voir delete_by_inode_cli() — et cherche le torrent
    Transmission dont un des fichiers y résout (même resolved_stat() que le
    reste du script, donc même gestion des symlinks cross-seed). Renvoie le
    torrent (dict) ou None."""
    for torrent in client.list_torrents():
        for path, _size in torrent_host_files(torrent):
            st = resolved_stat(path)
            if st and (st.st_dev, st.st_ino) == (dev, ino):
                return torrent
    return None


def delete_by_inode_cli(dev, ino, dry_run):
    """Mode non-interactif utilisé par le skill anime-vf
    (.claude/skills/anime-vf/SKILL.md) : après qu'une nouvelle release ait
    remplacé un fichier library/ existant, retrouve et supprime le torrent
    de l'ANCIENNE version par inode. L'appelant doit avoir capturé ce
    (dev, inode) AVANT l'import — une fois la nouvelle release importée,
    Sonarr/Radarr a pu déjà supprimer ce chemin côté library/, un stat a
    posteriori échouerait. Ne touche PAS au monitoring Sonarr/Radarr
    (contrairement à une suppression dans la TUI, voir plan_sonarr_unmonitor) :
    l'épisode reste surveillé, on vient de le remplacer par une meilleure
    release, pas de le retirer. Affiche un JSON sur stdout pour que
    l'appelant (Claude) parse le résultat sans dépendre du log."""
    client = TransmissionClient()
    torrent = find_torrent_by_inode(client, dev, ino)
    if not torrent:
        print(json.dumps({"found": False, "deleted": False, "torrent": None}))
        return
    if dry_run:
        print(json.dumps({"found": True, "deleted": False, "torrent": torrent["name"]}))
        return
    client.remove_torrent(torrent["id"])
    logger.info("delete-by-inode: torrent %r (id=%s) supprimé (remplacé par une nouvelle release)",
                torrent["name"], torrent["id"])
    print(json.dumps({"found": True, "deleted": True, "torrent": torrent["name"]}))


def analyze_torrent_files(host_files, library_index):
    """Une seule passe resolved_stat() par torrent, qui alimente à la fois les
    marqueurs affichés (évite de stat chaque fichier deux fois) et le
    regroupement cross-seed (build_cross_seed_groups, via les inodes
    renvoyés) : linked=True si au moins un fichier a une correspondance
    library/ (hardlink, colonne BIB), missing=True si AUCUN fichier du
    torrent n'existe plus sur disque — le cas Transmission "No data found!"
    (torrent orphelin, colonne ABS), vu en pratique sur des torrents
    antérieurs à la mise en place de la stack arr dont le fichier a été
    supprimé par un autre moyen que cet outil. inodes = (dev, inode) de
    chaque fichier réellement présent — un torrent cross-seedé et l'original
    dont il dérive partagent ces mêmes inodes malgré deux entrées
    Transmission distinctes."""
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
    dédié .cross-seed-links/<tracker>/... (cf. CLAUDE.md, fix hardlink
    cross-seed) plutôt que directement sous /data/completed. Sert à choisir
    le "parent" d'un groupe cross-seed (build_cross_seed_groups) : le
    téléchargement réel, pas l'un des symlinks pointant dessus."""
    return "/.cross-seed-links/" in torrent.get("downloadDir", "")


def build_cross_seed_groups(all_torrents):
    """Regroupe les torrents qui partagent au moins un fichier réel (même
    (dev, inode), voir analyze_torrent_files/t["_inodes"]) — un torrent
    cross-seedé (arr/cross-seed, linkType symlink par défaut, cf. CLAUDE.md)
    pointe vers les mêmes données qu'un torrent déjà présent sur un autre
    indexeur : même contenu, deux entrées Transmission distinctes qui
    n'apparaissent autrement reliées par rien dans la liste. Union-find sur
    les inodes partagés plutôt qu'une comparaison torrent x torrent (O(n²),
    ~300 torrents sur ce déploiement). N'a besoin d'aucun nouveau stat — les
    inodes sont déjà mis en cache sur chaque torrent (t["_inodes"]) au
    chargement, donc peut être rappelée à bas coût après chaque suppression
    pour refléter les groupes restants (un cross-seed dont l'original a été
    supprimé redevient un torrent seul, potentiellement marqué ABS — son
    symlink pointe alors dans le vide). Renvoie (groups, child_ids) :
    groups = {parent_id: [torrent_enfant, ...]} (uniquement les groupes de
    taille >= 2), child_ids = ids à retirer du niveau racine de l'arbre
    (déjà représentés sous leur parent, voir build_tree)."""
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


def col_label(name, width, active, reverse):
    text = name + (" ▼" if reverse else " ▲") if active else name
    return f"{text:<{width}}"


# Lignes réservées hors liste : en-tête + séparateur (2) + ligne session +
# pied de page (2) — factorisé car draw_list() et la boucle de main() (pour
# le défilement) doivent rester d'accord sur ce nombre. max(1, ...) pour
# éviter une slice à borne négative sur un terminal minuscule.
def visible_rows(h):
    return max(1, h - 4)


def draw_list(stdscr, tree_rows, selected, offset, filter_str, linked_ids, missing_ids, sort_idx, sort_reverse,
              session_freed_bytes, session_deletions, expanded_ids, total_torrents, group_count):
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    header = (
        f"{col_label('BIB', 3, sort_idx == 0, sort_reverse)} "
        f"{col_label('ABS', 3, sort_idx == 1, sort_reverse)} "
        f"{col_label('AGE', 7, sort_idx == 2, sort_reverse)} "
        f"{col_label('TAILLE', 9, sort_idx == 3, sort_reverse)} "
        f"{col_label('RATIO', 6, sort_idx == 4, sort_reverse)} "
        f"{col_label('TRACKER', 20, sort_idx == 5, sort_reverse)} "
        f"{col_label('NOM', 3, sort_idx == 6, sort_reverse)}"
    )
    stdscr.addstr(0, 0, header[:w - 1], curses.A_BOLD | cp(COLOR_HEADER))
    stdscr.addstr(1, 0, "-" * min(w - 1, len(header)), cp(COLOR_HEADER))
    visible = visible_rows(h)
    for i, tree_row in enumerate(tree_rows[offset:offset + visible]):
        t = tree_row["torrent"]
        row = 2 + i
        is_selected = offset + i == selected
        linked = t["id"] in linked_ids
        missing = t["id"] in missing_ids
        base_attr = curses.A_REVERSE if is_selected else curses.A_NORMAL
        l_attr = base_attr if (is_selected or not linked) else base_attr | cp(COLOR_LINKED)
        m_attr = base_attr if (is_selected or not missing) else base_attr | cp(COLOR_DANGER)

        col = 0
        l_str = f"{'✓' if linked else '':<3} "
        stdscr.addstr(row, col, l_str[:max(0, w - 1 - col)], l_attr)
        col += len(l_str)
        m_str = f"{'✓' if missing else '':<3} "
        stdscr.addstr(row, col, m_str[:max(0, w - 1 - col)], m_attr)
        col += len(m_str)
        age_str = f"{human_age(t['addedDate']):<7} "
        stdscr.addstr(row, col, age_str[:max(0, w - 1 - col)], base_attr)
        col += len(age_str)
        size_str = f"{human_size(t['totalSize']):<9} "
        stdscr.addstr(row, col, size_str[:max(0, w - 1 - col)], base_attr)
        col += len(size_str)
        ratio_str = f"{t['uploadRatio']:<6.2f} "
        ratio_attr = base_attr if is_selected else base_attr | cp(ratio_color(t["uploadRatio"]))
        stdscr.addstr(row, col, ratio_str[:max(0, w - 1 - col)], ratio_attr)
        col += len(ratio_str)
        tracker_str = f"{t.get('_tracker_name', '?')[:19]:<20} "
        stdscr.addstr(row, col, tracker_str[:max(0, w - 1 - col)], base_attr)
        col += len(tracker_str)
        if tree_row["depth"] == 0 and tree_row["child_count"]:
            glyph = "▾" if t["id"] in expanded_ids else "▸"
            n = tree_row["child_count"]
            name = f"{glyph} {t['name']} ({n} cross-seed{'s' if n > 1 else ''})"
        elif tree_row["depth"] == 1:
            name = f"  └ {t['name']}"
        else:
            name = t["name"]
        stdscr.addstr(row, col, name[:max(0, w - 1 - col)], base_attr)
    session_line = f"Session : {session_deletions} suppression(s), {human_size(session_freed_bytes)} libéré(s)"
    stdscr.addstr(h - 2, 0, session_line[:w - 1], curses.A_BOLD | cp(COLOR_LINKED))

    sort_label, _ = SORT_FIELDS[sort_idx]
    footer = f"{total_torrents} torrents ({len(linked_ids)} avec fichier(s) bibliothèque BIB, {len(missing_ids)} fichier manquant ABS"
    if group_count:
        footer += f", {group_count} groupe(s) cross-seed"
    footer += ")"
    footer += f" | tri: {sort_label} {'▼' if sort_reverse else '▲'}"
    if filter_str:
        footer += f" | filtre: {filter_str}"
    footer += " | ? aide"
    stdscr.addstr(h - 1, 0, footer[:w - 1], curses.A_DIM)
    stdscr.refresh()


# Un raccourci par ligne (touche, description) — source unique pour show_help(),
# plutôt que la liste condensée qu'affichait le footer avant (devenue illisible
# une fois 'P' ajouté, cf. discussion utilisateur du 2026-07-28).
HELP_KEYS = [
    ("↑/↓, j/k", "naviguer"),
    ("PgUp/PgDown", "naviguer par page"),
    ("/", "filtrer par nom"),
    ("s", "champ de tri suivant"),
    ("S", "inverser le sens du tri"),
    ("→ / l", "déplier les cross-seeds du torrent sélectionné"),
    ("← / h", "replier"),
    ("Entrée", "supprimer (avec confirmation)"),
    ("D", "supprimer sans confirmation"),
    ("P", "purger tous les torrents marqués ABS (avec confirmation)"),
    ("?", "cette aide"),
    ("q / Échap", "quitter"),
]


def show_help(stdscr):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    lines = [("Raccourcis", curses.A_BOLD | cp(COLOR_HEADER)), ("", curses.A_NORMAL)]
    key_width = max(len(k) for k, _ in HELP_KEYS)
    for key, desc in HELP_KEYS:
        lines.append((f"  {key:<{key_width}}  {desc}", curses.A_NORMAL))
    lines.append(("", curses.A_NORMAL))
    lines.append(("Colonnes : BIB = coche verte si fichier(s) présents dans library/, "
                   "ABS = coche rouge si fichier manquant sur disque",
                   curses.A_NORMAL))
    lines.append(("▸/▾ devant un nom : torrent cross-seedé sur plusieurs indexeurs (même fichier",
                   curses.A_NORMAL))
    lines.append(("réel qu'un ou plusieurs autres torrents) — replié par défaut, voir →/l.",
                   curses.A_NORMAL))
    lines.append(("", curses.A_NORMAL))
    lines.append(("Appuyez sur une touche pour revenir", curses.A_DIM))
    for i, (line, attr) in enumerate(lines[:h - 1]):
        stdscr.addstr(i, 0, line[:w - 1], attr)
    stdscr.refresh()
    stdscr.getch()


def confirm_delete(stdscr, torrent, library_index, cross_seed_groups):
    host_files = torrent_host_files(torrent)
    lib_matches = find_library_matches(host_files, library_index)
    arr_plan = plan_arr_actions(lib_matches)
    dependents = cross_seed_groups.get(torrent["id"], [])
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    none_attr = curses.A_NORMAL
    lines = [
        (f"Supprimer : {torrent['name']}", curses.A_BOLD | cp(COLOR_HEADER)),
        ("", none_attr),
    ]
    if dependents:
        # Ce torrent est le parent (téléchargement d'origine) d'un groupe
        # cross-seed — apply_deletion supprime ses enfants avec lui (même
        # contenu, plus rien à seeder une fois ce torrent parti).
        lines.append((f"⚠ {len(dependents)} torrent(s) cross-seedé(s) seront supprimés avec lui "
                       f"(même contenu, ne libère pas d'espace supplémentaire) :",
                       curses.A_BOLD | cp(COLOR_DANGER)))
        for c in dependents[:10]:
            lines.append((f"  - {c['name']} ({c.get('_tracker_name', '?')})", cp(COLOR_DANGER)))
        if len(dependents) > 10:
            lines.append((f"  ... et {len(dependents) - 10} de plus", cp(COLOR_DANGER)))
        lines.append(("", none_attr))
    lines.append((f"Fichiers Transmission ({len(host_files)}) — {human_size(sum(s for _, s in host_files))} :", none_attr))
    for path, size in host_files[:10]:
        lines.append((f"  - {os.path.basename(path)} ({human_size(size)})", none_attr))
    if len(host_files) > 10:
        lines.append((f"  ... et {len(host_files) - 10} de plus", none_attr))
    lines.append(("", none_attr))
    if lib_matches:
        lines.append((f"Fichiers bibliothèque correspondants ({len(lib_matches)}) — {human_size(sum(s for _, s in lib_matches))} :",
                       curses.A_BOLD | cp(COLOR_LINKED)))
        for path, size in lib_matches[:10]:
            lines.append((f"  - {path.replace(LIBRARY_ROOT, 'library')} ({human_size(size)})", cp(COLOR_LINKED)))
        if len(lib_matches) > 10:
            lines.append((f"  ... et {len(lib_matches) - 10} de plus", cp(COLOR_LINKED)))
    else:
        lines.append(("Aucun fichier bibliothèque correspondant trouvé (jamais importé, ou déjà supprimé).",
                       curses.A_BOLD | cp(COLOR_WARN)))
    lines.append(("", none_attr))
    if arr_plan:
        lines.append((f"Actions Sonarr/Radarr ({len(arr_plan)}) :", curses.A_BOLD | cp(COLOR_LINKED)))
        for action in arr_plan:
            lines.append((f"  - {action['description']}", cp(COLOR_LINKED)))
        lines.append(("", none_attr))
    total = sum(s for _, s in host_files) + sum(s for _, s in lib_matches)
    lines.append((f"Espace total libéré : {human_size(total)}", curses.A_BOLD))
    lines.append(("", none_attr))
    lines.append(("Confirmer la suppression ? [o/N]", curses.A_BOLD | cp(COLOR_DANGER)))
    for i, (line, attr) in enumerate(lines[:h - 1]):
        stdscr.addstr(i, 0, line[:w - 1], attr)
    stdscr.refresh()
    curses.echo()
    key = stdscr.getch()
    curses.noecho()
    if key in (ord("o"), ord("O"), ord("y"), ord("Y")):
        return host_files, lib_matches, arr_plan
    return None


def confirm_bulk_delete(stdscr, torrents):
    """Écran de confirmation pour Maj+P (purge groupée des torrents marqués
    ABS) — mêmes conventions que confirm_delete, mais pas d'espace "libéré"
    à annoncer : par définition ces fichiers ont déjà disparu du disque."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    none_attr = curses.A_NORMAL
    lines = [
        (f"Purger {len(torrents)} torrent(s) au fichier disparu (marqués ABS) :",
         curses.A_BOLD | cp(COLOR_HEADER)),
        ("", none_attr),
    ]
    for t in torrents[:15]:
        lines.append((f"  - {t['name']}", cp(COLOR_DANGER)))
    if len(torrents) > 15:
        lines.append((f"  ... et {len(torrents) - 15} de plus", cp(COLOR_DANGER)))
    lines.append(("", none_attr))
    lines.append(("Retirés de Transmission uniquement (rien à supprimer localement, les fichiers", none_attr))
    lines.append(("sont déjà absents du disque) — vérifiez qu'aucun n'a simplement changé", none_attr))
    lines.append(("d'emplacement avant de confirmer.", none_attr))
    lines.append(("", none_attr))
    lines.append((f"Confirmer la suppression de {len(torrents)} torrent(s) ? [o/N]",
                   curses.A_BOLD | cp(COLOR_DANGER)))
    for i, (line, attr) in enumerate(lines[:h - 1]):
        stdscr.addstr(i, 0, line[:w - 1], attr)
    stdscr.refresh()
    curses.echo()
    key = stdscr.getch()
    curses.noecho()
    return key in (ord("o"), ord("O"), ord("y"), ord("Y"))


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
    # lui plutôt que de laisser des entrées mortes dans Transmission (demandé
    # par l'utilisateur). remove_torrent(delete-local-data=True) suffit pour
    # le "lien logique" : le fichier d'un enfant est un symlink
    # (.cross-seed-links/<tracker>/..., cf. is_cross_seed_entry) — Transmission
    # appelle unlink() dessus comme sur n'importe quel fichier, ce qui retire
    # le symlink lui-même sans toucher sa cible (déjà supprimée juste
    # au-dessus). Pas de find_library_matches/plan_arr_actions rejoués pour
    # ces enfants : même inode que le parent, donc mêmes correspondances
    # library/ et même plan Sonarr/Radarr déjà traités ci-dessus — les
    # rejouer serait redondant (fichier déjà supprimé, action arr déjà faite).
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
    libérés) — factorise ce que les chemins Entrée (confirmée), D (directe)
    et P (purge groupée) ont en commun une fois host_files/lib_matches/
    arr_plan connus. Si torrent est le parent d'un groupe cross-seed, ses
    enfants sont supprimés avec lui (voir do_delete) ; freed ne compte que
    les octets du parent — les enfants ne libèrent aucun espace disque
    supplémentaire, ce sont des symlinks vers les mêmes fichiers."""
    dependents = cross_seed_groups.get(torrent["id"], [])
    do_delete(client, torrent, host_files, lib_matches, arr_plan, dependents)
    removed_ids = {torrent["id"]} | {c["id"] for c in dependents}
    remaining = [t for t in all_torrents if t["id"] not in removed_ids]
    linked_ids.difference_update(removed_ids)
    missing_ids.difference_update(removed_ids)
    freed = sum(s for _, s in host_files) + sum(s for _, s in lib_matches)
    return remaining, freed


def main(stdscr):
    global COLORS_ON
    logger.info("=== démarrage torrent-cleanup (DATA_ROOT=%s) ===", DATA_ROOT)
    curses.curs_set(0)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(COLOR_LINKED, curses.COLOR_GREEN, -1)
        curses.init_pair(COLOR_DANGER, curses.COLOR_RED, -1)
        curses.init_pair(COLOR_WARN, curses.COLOR_YELLOW, -1)
        curses.init_pair(COLOR_HEADER, curses.COLOR_CYAN, -1)
        COLORS_ON = True
    client = TransmissionClient()
    stdscr.addstr(0, 0, "Chargement des torrents...")
    stdscr.refresh()
    all_torrents = client.list_torrents()

    stdscr.addstr(0, 0, "Résolution des noms de tracker (Prowlarr)...")
    stdscr.clrtoeol()
    stdscr.refresh()
    tracker_map = build_prowlarr_tracker_map()
    for t in all_torrents:
        t["_tracker_name"] = tracker_display(t, tracker_map)

    stdscr.addstr(0, 0, "Indexation de library/...")
    stdscr.clrtoeol()
    stdscr.refresh()
    library_index = build_library_index()
    linked_ids, missing_ids = set(), set()
    for t in all_torrents:
        linked, missing, inodes = analyze_torrent_files(torrent_host_files(t), library_index)
        # Posés sur le torrent lui-même (pas seulement dans linked_ids/missing_ids)
        # pour que les lambdas L/M de SORT_FIELDS puissent trier dessus — un
        # lambda n'a accès qu'au torrent, pas à ces sets — et pour que
        # build_cross_seed_groups puisse être rappelée sans re-stat après
        # chaque suppression.
        t["_linked"] = linked
        t["_missing"] = missing
        t["_inodes"] = inodes
        if linked:
            linked_ids.add(t["id"])
        if missing:
            missing_ids.add(t["id"])

    cross_seed_groups, cross_seed_child_ids = build_cross_seed_groups(all_torrents)
    expanded_ids = set()

    sort_idx, sort_reverse = 2, False  # AGE ascendant par défaut (le plus ancien en premier)
    sort_torrents(all_torrents, sort_idx, sort_reverse)
    logger.info("torrents avec correspondance library/ : %d/%d", len(linked_ids), len(all_torrents))
    logger.info("torrents avec fichier manquant sur disque : %d/%d", len(missing_ids), len(all_torrents))
    logger.info("groupes cross-seed détectés : %d", len(cross_seed_groups))

    filter_str = ""
    selected = 0
    offset = 0
    message = ""
    message_color = COLOR_LINKED
    session_freed_bytes = 0
    session_deletions = 0

    while True:
        top_level = [t for t in all_torrents if t["id"] not in cross_seed_child_ids]
        tree_rows = build_tree(top_level, cross_seed_groups, expanded_ids, filter_str)
        selected = max(0, min(selected, len(tree_rows) - 1)) if tree_rows else 0
        h, _w = stdscr.getmaxyx()
        visible = visible_rows(h)
        if selected < offset:
            offset = selected
        if selected >= offset + visible:
            offset = selected - visible + 1

        draw_list(stdscr, tree_rows, selected, offset, filter_str, linked_ids, missing_ids, sort_idx, sort_reverse,
                  session_freed_bytes, session_deletions, expanded_ids, len(all_torrents), len(cross_seed_groups))
        if message:
            stdscr.addstr(0, 0, message[: stdscr.getmaxyx()[1] - 1], curses.A_BOLD | cp(message_color))
            stdscr.refresh()
            message = ""

        key = stdscr.getch()
        if key in (ord("q"), 27):
            break
        elif key == ord("?"):
            show_help(stdscr)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = min(selected + 1, len(tree_rows) - 1) if tree_rows else 0
        elif key in (curses.KEY_UP, ord("k")):
            selected = max(selected - 1, 0)
        elif key == curses.KEY_NPAGE:
            selected = min(selected + visible, len(tree_rows) - 1) if tree_rows else 0
        elif key == curses.KEY_PPAGE:
            selected = max(selected - visible, 0)
        elif key in (curses.KEY_RIGHT, ord("l")) and tree_rows:
            # Déplier : uniquement pertinent sur une ligne racine avec des
            # cross-seeds (child_count > 0) — no-op sinon.
            row = tree_rows[selected]
            if row["child_count"] > 0:
                expanded_ids.add(row["torrent"]["id"])
        elif key in (curses.KEY_LEFT, ord("h")) and tree_rows:
            # Replier : sur une ligne enfant, replie le groupe parent et
            # ramène le curseur dessus plutôt que de laisser la sélection
            # retomber arbitrairement sur la ligne suivante après le
            # rétrécissement de la liste.
            row = tree_rows[selected]
            collapse_id = row["torrent"]["id"] if row["depth"] == 0 else row["parent_id"]
            if collapse_id is not None:
                expanded_ids.discard(collapse_id)
                if row["depth"] == 1:
                    tree_rows = build_tree(top_level, cross_seed_groups, expanded_ids, filter_str)
                    ids = [r["torrent"]["id"] for r in tree_rows]
                    if collapse_id in ids:
                        selected = ids.index(collapse_id)
        elif key == ord("/"):
            curses.echo()
            stdscr.addstr(stdscr.getmaxyx()[0] - 1, 0, "filtre> ")
            stdscr.clrtoeol()
            stdscr.refresh()
            filter_str = stdscr.getstr(stdscr.getmaxyx()[0] - 1, 8, 60).decode(errors="replace")
            curses.noecho()
            offset = 0
        elif key in (ord("s"), ord("S")):
            selected_id = tree_rows[selected]["torrent"]["id"] if tree_rows else None
            if key == ord("s"):
                sort_idx = (sort_idx + 1) % len(SORT_FIELDS)
                sort_reverse = False
            else:
                sort_reverse = not sort_reverse
            sort_torrents(all_torrents, sort_idx, sort_reverse)
            top_level = [t for t in all_torrents if t["id"] not in cross_seed_child_ids]
            tree_rows = build_tree(top_level, cross_seed_groups, expanded_ids, filter_str)
            if selected_id is not None:
                ids = [r["torrent"]["id"] for r in tree_rows]
                selected = ids.index(selected_id) if selected_id in ids else 0
            offset = 0
        elif key in (curses.KEY_ENTER, 10, 13) and tree_rows:
            torrent = tree_rows[selected]["torrent"]
            try:
                result = confirm_delete(stdscr, torrent, library_index, cross_seed_groups)
                if result:
                    host_files, lib_matches, arr_plan = result
                    all_torrents, freed = apply_deletion(client, torrent, host_files, lib_matches, arr_plan,
                                                          all_torrents, linked_ids, missing_ids, cross_seed_groups)
                    cross_seed_groups, cross_seed_child_ids = build_cross_seed_groups(all_torrents)
                    session_freed_bytes += freed
                    session_deletions += 1
                    message = f"Supprimé : {torrent['name']}"
                    message_color = COLOR_LINKED
            except Exception as e:
                logger.error("échec de la suppression de %r : %s", torrent["name"], e)
                message = f"ÉCHEC (voir {LOG_PATH}) : {e}"
                message_color = COLOR_DANGER
        elif key == ord("D") and tree_rows:
            # Suppression directe, sans écran de confirmation (demandé
            # explicitement) — contrairement à Entrée. À utiliser en
            # connaissance de cause.
            torrent = tree_rows[selected]["torrent"]
            try:
                host_files = torrent_host_files(torrent)
                lib_matches = find_library_matches(host_files, library_index)
                arr_plan = plan_arr_actions(lib_matches)
                all_torrents, freed = apply_deletion(client, torrent, host_files, lib_matches, arr_plan,
                                                      all_torrents, linked_ids, missing_ids, cross_seed_groups)
                cross_seed_groups, cross_seed_child_ids = build_cross_seed_groups(all_torrents)
                session_freed_bytes += freed
                session_deletions += 1
                message = f"Supprimé (sans confirmation) : {torrent['name']}"
                message_color = COLOR_LINKED
            except Exception as e:
                logger.error("échec de la suppression rapide de %r : %s", torrent["name"], e)
                message = f"ÉCHEC (voir {LOG_PATH}) : {e}"
                message_color = COLOR_DANGER
        elif key == ord("P"):
            # Purge groupée de tous les torrents marqués ABS (fichier disparu
            # du disque), pas seulement ceux du filtre courant — le but est
            # un rattrapage global, comme demandé après le cas cross-seed du
            # 2026-07-28. Chaque suppression est indépendante (échec isolé
            # n'interrompt pas les suivantes), même logique que execute_arr_plan.
            missing_torrents = [t for t in all_torrents if t["id"] in missing_ids]
            if not missing_torrents:
                message = "Aucun torrent avec fichier manquant (marqué ABS)"
                message_color = COLOR_WARN
            elif confirm_bulk_delete(stdscr, missing_torrents):
                deleted, failed, skipped = 0, 0, 0
                for torrent in missing_torrents:
                    # Un torrent de cette liste peut avoir déjà été supprimé
                    # en cascade par un parent traité plus tôt dans cette même
                    # purge (cross-seed enfant lui-même marqué ABS, donc
                    # présent dans missing_torrents en plus d'être visé par
                    # apply_deletion via cross_seed_groups) — pas une erreur.
                    current_ids = {t["id"] for t in all_torrents}
                    if torrent["id"] not in current_ids:
                        skipped += 1
                        continue
                    try:
                        host_files = torrent_host_files(torrent)
                        lib_matches = find_library_matches(host_files, library_index)
                        arr_plan = plan_arr_actions(lib_matches)
                        all_torrents, freed = apply_deletion(client, torrent, host_files, lib_matches, arr_plan,
                                                              all_torrents, linked_ids, missing_ids, cross_seed_groups)
                        session_freed_bytes += freed
                        session_deletions += 1
                        deleted += 1
                    except Exception as e:
                        logger.error("échec de la purge de %r (id=%s) : %s", torrent["name"], torrent["id"], e)
                        failed += 1
                cross_seed_groups, cross_seed_child_ids = build_cross_seed_groups(all_torrents)
                message = f"Purge : {deleted} supprimé(s)"
                if skipped:
                    message += f", {skipped} déjà supprimé(s) en cascade"
                if failed:
                    message += f", {failed} échec(s) (voir {LOG_PATH})"
                message_color = COLOR_DANGER if failed else COLOR_LINKED


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "delete-by-inode":
        # Mode non-interactif, voir delete_by_inode_cli() — pas de curses ici.
        if len(sys.argv) < 4:
            print("usage: torrent-cleanup.py delete-by-inode <dev> <ino> [--dry-run]", file=sys.stderr)
            sys.exit(1)
        try:
            delete_by_inode_cli(int(sys.argv[2]), int(sys.argv[3]), "--dry-run" in sys.argv[4:])
        except RuntimeError as e:
            logger.error("delete-by-inode: %s", e)
            print(f"Erreur : {e} (voir {LOG_PATH})", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    try:
        curses.wrapper(main)
        logger.info("=== fin normale (touche q) ===")
    except RuntimeError as e:
        logger.error("arrêt sur erreur : %s", e)
        print(f"Erreur : {e} (voir {LOG_PATH})", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        # curses.wrapper restaure déjà le terminal avant de relaisser
        # filer l'exception (non catchée par `except Exception` ci-dessous,
        # BaseException pas Exception) — sans ce handler, Ctrl+C affichait
        # une traceback complète au lieu de quitter comme la touche 'q'.
        logger.info("=== interrompu (Ctrl+C) ===")
        sys.exit(130)
    except Exception:
        logger.error("crash inattendu :\n%s", traceback.format_exc())
        print(f"Crash inattendu, trace complète dans {LOG_PATH}", file=sys.stderr)
        raise
