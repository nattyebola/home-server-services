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

PROWLARR_CONTAINER = "arr-prowlarr-1"
PROWLARR_URL = "http://localhost:9696/api/v1/indexer"
PROWLARR_API_KEY = load_env_file(os.path.join(REPO_ROOT, "arr", ".env")).get("PROWLARR_API_KEY")

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


def log_deletion(torrent, host_files, lib_matches):
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


def build_prowlarr_tracker_map():
    """domaine (ex. torr9.net) -> nom d'indexeur Prowlarr (ex. Torr9).

    Le domaine du site (indexerUrls/legacyUrls, ce que Prowlarr connaît)
    n'est pas forcément celui du tracker BitTorrent (announce URL vu par
    Transmission) — ex. tracker.torr9.net vs torr9.net. Matché par suffixe
    de domaine, donc ça couvre les sous-domaines type tracker./tk./announce.
    Si le tracker BT utilise un domaine sans rapport (mutualisé entre
    plusieurs sites, vu en pratique), aucun match : on retombe sur le
    hostname brut plutôt que d'inventer un nom."""
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
                domain_map[host.lower()] = name
    logger.info("noms de tracker Prowlarr chargés : %d indexeur(s), %d domaine(s)",
                len(indexers), len(domain_map))
    return domain_map


def resolve_tracker_name(hostname, tracker_map):
    for domain, name in tracker_map.items():
        if hostname == domain or hostname.endswith("." + domain):
            return name
    return hostname


def tracker_display(torrent, tracker_map):
    hosts = tracker_host(torrent)
    if hosts == "?":
        return "?"
    return ",".join(resolve_tracker_name(h, tracker_map) for h in hosts.split(","))


# Champs de tri disponibles (touche 's' pour passer au suivant, 'S' pour
# inverser le sens du champ courant) — même ordre que les colonnes affichées.
SORT_FIELDS = [
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
        try:
            st = os.stat(path)
        except FileNotFoundError:
            logger.warning("fichier attendu absent du disque (déjà déplacé/supprimé ?) : %s", path)
            continue
        hit = library_index.get((st.st_dev, st.st_ino))
        if hit:
            matches.append((hit, st.st_size))
    logger.debug("find_library_matches: %d fichier(s) torrent -> %d correspondance(s) library/",
                 len(host_files), len(matches))
    return matches


def has_library_match(host_files, library_index):
    for path, _size in host_files:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            continue
        if (st.st_dev, st.st_ino) in library_index:
            return True
    return False


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


def draw_list(stdscr, torrents, selected, offset, filter_str, linked_ids, sort_idx, sort_reverse,
              session_freed_bytes, session_deletions):
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    header = (
        f"{'':<2}"
        f"{col_label('AGE', 7, sort_idx == 0, sort_reverse)} "
        f"{col_label('TAILLE', 9, sort_idx == 1, sort_reverse)} "
        f"{col_label('RATIO', 6, sort_idx == 2, sort_reverse)} "
        f"{col_label('TRACKER', 20, sort_idx == 3, sort_reverse)} "
        f"{col_label('NOM', 3, sort_idx == 4, sort_reverse)}"
    )
    stdscr.addstr(0, 0, header[:w - 1], curses.A_BOLD | cp(COLOR_HEADER))
    stdscr.addstr(1, 0, "-" * min(w - 1, len(header)), cp(COLOR_HEADER))
    visible = visible_rows(h)
    for i, t in enumerate(torrents[offset:offset + visible]):
        row = 2 + i
        is_selected = offset + i == selected
        linked = t["id"] in linked_ids
        marker = "L " if linked else "  "
        base_attr = curses.A_REVERSE if is_selected else curses.A_NORMAL

        col = 0
        stdscr.addstr(row, col, marker, base_attr | (0 if is_selected else cp(COLOR_LINKED) if linked else 0))
        col += len(marker)
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
        stdscr.addstr(row, col, t["name"][:max(0, w - 1 - col)], base_attr)
    session_line = f"Session : {session_deletions} suppression(s), {human_size(session_freed_bytes)} libéré(s)"
    stdscr.addstr(h - 2, 0, session_line[:w - 1], curses.A_BOLD | cp(COLOR_LINKED))

    sort_label, _ = SORT_FIELDS[sort_idx]
    footer = f"{len(torrents)} torrents ({len(linked_ids)} avec fichier(s) bibliothèque, marqués 'L')"
    footer += f" | tri: {sort_label} {'▼' if sort_reverse else '▲'}"
    if filter_str:
        footer += f" | filtre: {filter_str}"
    footer += " | ↑/↓ naviguer, / filtrer, s tri, S inverser, Entrée confirmer, D suppr. directe, q quitter"
    stdscr.addstr(h - 1, 0, footer[:w - 1], curses.A_DIM)
    stdscr.refresh()


def confirm_delete(stdscr, torrent, library_index):
    host_files = torrent_host_files(torrent)
    lib_matches = find_library_matches(host_files, library_index)
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    none_attr = curses.A_NORMAL
    lines = [
        (f"Supprimer : {torrent['name']}", curses.A_BOLD | cp(COLOR_HEADER)),
        ("", none_attr),
        (f"Fichiers Transmission ({len(host_files)}) — {human_size(sum(s for _, s in host_files))} :", none_attr),
    ]
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
        return host_files, lib_matches
    return None


def do_delete(client, torrent, host_files, lib_matches):
    log_deletion(torrent, host_files, lib_matches)
    client.remove_torrent(torrent["id"])
    for path, _size in lib_matches:
        try:
            os.remove(path)
            logger.info("fichier library supprimé : %s", path)
            prune_empty_dirs(path)
        except OSError as e:
            logger.warning("échec de suppression du fichier library %s : %s", path, e)


def apply_deletion(client, torrent, host_files, lib_matches, all_torrents, linked_ids):
    """Effectue la suppression et renvoie (nouvelle liste all_torrents, octets
    libérés) — factorise ce que les chemins Entrée (confirmée) et D (directe)
    ont en commun une fois host_files/lib_matches connus."""
    do_delete(client, torrent, host_files, lib_matches)
    deleted_id = torrent["id"]
    remaining = [t for t in all_torrents if t["id"] != deleted_id]
    linked_ids.discard(deleted_id)
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

    sort_idx, sort_reverse = 0, False  # AGE ascendant par défaut (le plus ancien en premier)
    sort_torrents(all_torrents, sort_idx, sort_reverse)

    stdscr.addstr(0, 0, "Indexation de library/...")
    stdscr.clrtoeol()
    stdscr.refresh()
    library_index = build_library_index()
    linked_ids = {t["id"] for t in all_torrents if has_library_match(torrent_host_files(t), library_index)}
    logger.info("torrents avec correspondance library/ : %d/%d", len(linked_ids), len(all_torrents))

    filter_str = ""
    selected = 0
    offset = 0
    message = ""
    message_color = COLOR_LINKED
    session_freed_bytes = 0
    session_deletions = 0

    while True:
        torrents = [t for t in all_torrents if filter_str.lower() in t["name"].lower()]
        selected = max(0, min(selected, len(torrents) - 1)) if torrents else 0
        h, _w = stdscr.getmaxyx()
        visible = visible_rows(h)
        if selected < offset:
            offset = selected
        if selected >= offset + visible:
            offset = selected - visible + 1

        draw_list(stdscr, torrents, selected, offset, filter_str, linked_ids, sort_idx, sort_reverse,
                  session_freed_bytes, session_deletions)
        if message:
            stdscr.addstr(0, 0, message[: stdscr.getmaxyx()[1] - 1], curses.A_BOLD | cp(message_color))
            stdscr.refresh()
            message = ""

        key = stdscr.getch()
        if key in (ord("q"), 27):
            break
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = min(selected + 1, len(torrents) - 1) if torrents else 0
        elif key in (curses.KEY_UP, ord("k")):
            selected = max(selected - 1, 0)
        elif key == curses.KEY_NPAGE:
            selected = min(selected + visible, len(torrents) - 1) if torrents else 0
        elif key == curses.KEY_PPAGE:
            selected = max(selected - visible, 0)
        elif key == ord("/"):
            curses.echo()
            stdscr.addstr(stdscr.getmaxyx()[0] - 1, 0, "filtre> ")
            stdscr.clrtoeol()
            stdscr.refresh()
            filter_str = stdscr.getstr(stdscr.getmaxyx()[0] - 1, 8, 60).decode(errors="replace")
            curses.noecho()
            offset = 0
        elif key in (ord("s"), ord("S")):
            selected_id = torrents[selected]["id"] if torrents else None
            if key == ord("s"):
                sort_idx = (sort_idx + 1) % len(SORT_FIELDS)
                sort_reverse = False
            else:
                sort_reverse = not sort_reverse
            sort_torrents(all_torrents, sort_idx, sort_reverse)
            torrents = [t for t in all_torrents if filter_str.lower() in t["name"].lower()]
            if selected_id is not None:
                ids = [t["id"] for t in torrents]
                selected = ids.index(selected_id) if selected_id in ids else 0
            offset = 0
        elif key in (curses.KEY_ENTER, 10, 13) and torrents:
            torrent = torrents[selected]
            try:
                result = confirm_delete(stdscr, torrent, library_index)
                if result:
                    host_files, lib_matches = result
                    all_torrents, freed = apply_deletion(client, torrent, host_files, lib_matches,
                                                          all_torrents, linked_ids)
                    session_freed_bytes += freed
                    session_deletions += 1
                    message = f"Supprimé : {torrent['name']}"
                    message_color = COLOR_LINKED
            except Exception as e:
                logger.error("échec de la suppression de %r : %s", torrent["name"], e)
                message = f"ÉCHEC (voir {LOG_PATH}) : {e}"
                message_color = COLOR_DANGER
        elif key == ord("D") and torrents:
            # Suppression directe, sans écran de confirmation (demandé
            # explicitement) — contrairement à Entrée. À utiliser en
            # connaissance de cause.
            torrent = torrents[selected]
            try:
                host_files = torrent_host_files(torrent)
                lib_matches = find_library_matches(host_files, library_index)
                all_torrents, freed = apply_deletion(client, torrent, host_files, lib_matches,
                                                      all_torrents, linked_ids)
                session_freed_bytes += freed
                session_deletions += 1
                message = f"Supprimé (sans confirmation) : {torrent['name']}"
                message_color = COLOR_LINKED
            except Exception as e:
                logger.error("échec de la suppression rapide de %r : %s", torrent["name"], e)
                message = f"ÉCHEC (voir {LOG_PATH}) : {e}"
                message_color = COLOR_DANGER


if __name__ == "__main__":
    try:
        curses.wrapper(main)
        logger.info("=== fin normale (touche q) ===")
    except RuntimeError as e:
        logger.error("arrêt sur erreur : %s", e)
        print(f"Erreur : {e} (voir {LOG_PATH})", file=sys.stderr)
        sys.exit(1)
    except Exception:
        logger.error("crash inattendu :\n%s", traceback.format_exc())
        print(f"Crash inattendu, trace complète dans {LOG_PATH}", file=sys.stderr)
        raise
