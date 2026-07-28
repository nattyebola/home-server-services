#!/usr/bin/env python3
# Interroge l'API RPC Transmission (même mécanisme docker-exec-curl que
# torrent-cleanup.py — RPC_URL n'est joignable que depuis l'intérieur du
# conteneur, réseau vpn-internal isolé) pour produire un JSON de stats
# consommé par generate-dashboard.py : ratio de session (depuis le dernier
# démarrage du daemon) et ratio total (cumulatif) via session-stats, débits
# instantanés (mêmes champs) accompagnés d'une échelle ("speed_scale", max
# observé sur l'historique récent — voir historical_max_speed()), nombre de
# torrents actifs/surveillés, et ratio par tracker en sommant
# uploadedEver/downloadedEver de chaque torrent groupé par host d'annonce
# (noms résolus via Prowlarr — même logique que torrent-cleanup.py).
#
# Sortie best-effort : {"error": "..."} sur stdout si Transmission ou le
# docker exec est injoignable, jamais de sortie vide ni de code de retour
# non-zéro — generate-dashboard.py doit pouvoir afficher un état
# "indisponible" plutôt que planter toute la régénération du dashboard.
import json
import os
import subprocess
import sys
import time
import urllib.parse

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRANSMISSION_CONTAINER = "vpn-transmission-vpn-1"
RPC_URL = "http://localhost:9091/transmission/rpc"

PROWLARR_CONTAINER = "arr-prowlarr-1"
PROWLARR_URL = "http://localhost:9696/api/v1/indexer"

# Fenêtre de rétention de l'historique de débit (voir record_speed_sample) —
# assez large pour couvrir un cycle jour/nuit d'usage typique sans faire
# dériver l'échelle des mètres sur un pic isolé trop ancien.
HISTORY_RETENTION = 25 * 3600


def load_env_file(path):
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


_ARR_ENV = load_env_file(os.path.join(REPO_ROOT, "arr", ".env"))
PROWLARR_API_KEY = _ARR_ENV.get("PROWLARR_API_KEY")
DATA_ROOT = load_env_file(os.path.join(REPO_ROOT, ".env.shared")).get("DATA_ROOT")
HISTORY_PATH = os.path.join(DATA_ROOT, ".transmission-stats-history.jsonl") if DATA_ROOT else None


def parse_tracker_aliases(raw):
    """TRACKER_ALIASES=domaine1=Nom1,domaine2=Nom1,... (arr/.env, voir
    .env.example) — même mécanisme que torrent-cleanup.py, pour le
    rationale complet voir son parse_tracker_aliases()."""
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


class RPCError(RuntimeError):
    pass


def _rpc_once(method, arguments, session_id):
    cmd = ["docker", "exec", "-i", TRANSMISSION_CONTAINER, "curl", "-s", "-i",
           "-X", "POST", "-d", "@-", RPC_URL]
    if session_id:
        cmd[6:6] = ["-H", f"X-Transmission-Session-Id: {session_id}"]
    payload = json.dumps({"method": method, "arguments": arguments or {}}).encode()
    try:
        res = subprocess.run(cmd, input=payload, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise RPCError(f"Transmission RPC timeout sur {method}")
    if res.returncode != 0:
        raise RPCError(f"docker exec {TRANSMISSION_CONTAINER} a échoué — container arrêté ?")
    text = res.stdout.decode(errors="replace")
    head, _, body = text.partition("\r\n\r\n")
    return head.split("\r\n", 1)[0], head, body


def rpc(method, arguments=None):
    """CSRF Transmission : premier appel sans session-id -> 409 avec le vrai
    id dans l'en-tête, on rejoue une fois avec (même mécanisme que
    TransmissionClient.call dans torrent-cleanup.py)."""
    status_line, head, body = _rpc_once(method, arguments, None)
    if " 409 " in status_line:
        session_id = None
        for line in head.split("\r\n"):
            if line.lower().startswith("x-transmission-session-id:"):
                session_id = line.split(":", 1)[1].strip()
        status_line, head, body = _rpc_once(method, arguments, session_id)
    if not body.strip():
        raise RPCError(f"réponse Transmission vide (statut: {status_line})")
    data = json.loads(body)
    if data.get("result") != "success":
        raise RPCError(f"Transmission RPC error: {data}")
    return data["arguments"]


def base_domain(hostname):
    """Domaine de base (2 derniers labels) — voir la même fonction dans
    torrent-cleanup.py pour le rationale complet (sous-domaines frères type
    tracker.yggreborn.org vs www.yggreborn.org, un suffix match nu échoue)."""
    parts = hostname.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else hostname


def build_prowlarr_tracker_map():
    """domaine de base -> nom d'indexeur Prowlarr. Best-effort : une erreur
    ici ne doit jamais faire échouer tout le script, juste faire retomber
    l'affichage sur le hostname brut (voir resolve_tracker_name)."""
    if not PROWLARR_API_KEY:
        return {}
    cmd = ["docker", "exec", PROWLARR_CONTAINER, "curl", "-s",
           "-H", f"X-Api-Key: {PROWLARR_API_KEY}", PROWLARR_URL]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        return {}
    if res.returncode != 0:
        return {}
    try:
        indexers = json.loads(res.stdout)
    except json.JSONDecodeError:
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
    return domain_map


def resolve_tracker_name(hostname, tracker_map):
    if hostname in TRACKER_ALIASES:
        return TRACKER_ALIASES[hostname]
    return tracker_map.get(base_domain(hostname), hostname)


def tracker_hosts(torrent):
    hosts = []
    for t in torrent.get("trackerStats") or []:
        host = urllib.parse.urlparse(t.get("announce", "")).hostname
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def ratio(uploaded, downloaded):
    return (uploaded / downloaded) if downloaded else None


def ratio_display(r):
    return f"{r:.2f}" if r is not None else "∞"


def human_size(nbytes):
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if abs(nbytes) < 1024 or unit == "To":
            return f"{nbytes:.1f}{unit}" if unit != "o" else f"{int(nbytes)}{unit}"
        nbytes /= 1024


def human_rate(bytes_per_sec):
    return f"{human_size(bytes_per_sec)}/s"


def human_duration(seconds):
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = [p for p in (f"{days}j" if days else "", f"{hours}h" if hours else "",
                          f"{minutes}min" if not days and minutes else "") if p]
    return " ".join(parts) if parts else "< 1min"


def load_history(path):
    entries = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return entries


def save_history(path, entries):
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


SPEED_SCALE_FLOOR = 1024 * 1024  # 1 Mo/s — plancher pour éviter un mètre dégénéré (division par ~0) tant que l'historique est vide/plat


def historical_max_speed(history, download_speed, upload_speed):
    """Échelle des mètres de débit (voir generate-dashboard.py) : pas de
    valeur "ligne max" figée en config (le débit VPN réel dépend du serveur
    distant, de la charge du tracker, etc., pas juste de la capacité FAI) —
    le maximum observé sur l'historique récent (HISTORY_RETENTION) sert
    d'échelle, avec un plancher pour rester lisible tant que rien n'a encore
    été échantillonné à pleine vitesse."""
    down_max = max([e.get("download_speed", 0) for e in history] + [download_speed, SPEED_SCALE_FLOOR])
    up_max = max([e.get("upload_speed", 0) for e in history] + [upload_speed, SPEED_SCALE_FLOOR])
    return {"download_max": down_max, "upload_max": up_max}


def record_speed_sample(now, download_speed, upload_speed):
    """Persiste un échantillon de débit (horodatage) à chaque appel (un par
    régénération du dashboard, cron 5 min) sous HISTORY_PATH, purgé au-delà
    de HISTORY_RETENTION, et renvoie l'échelle des mètres de débit (voir
    historical_max_speed). Best-effort : DATA_ROOT absent ou fichier
    illisible/non-inscriptible -> None (l'appelant retombe alors sur un
    fallback sans historique) plutôt qu'un crash de tout le script.
    Portait aussi un ratio glissant sur 24h jusqu'au 2026-07-28 (retiré à la
    demande de l'utilisateur, jugé pas utile) — l'historique persisté ne sert
    plus qu'à cette échelle de débit."""
    if not HISTORY_PATH:
        return None
    history = load_history(HISTORY_PATH)
    history.append({"ts": now, "download_speed": download_speed, "upload_speed": upload_speed})
    history = [e for e in history if now - e["ts"] <= HISTORY_RETENTION]
    save_history(HISTORY_PATH, history)
    return historical_max_speed(history, download_speed, upload_speed)


def main():
    session = rpc("session-stats")
    torrents = rpc("torrent-get", {"fields": ["trackerStats", "uploadedEver", "downloadedEver",
                                               "status", "error"]})["torrents"]

    # status 0 = stopped/paused côté Transmission (spec RPC) — "actifs" =
    # tout le reste (en train de télécharger, de vérifier, ou en seed) ;
    # "surveillés" = tous les torrents présents dans le client, actifs ou non.
    torrents_active = sum(1 for t in torrents if t.get("status", 0) != 0)
    torrents_total = len(torrents)
    # status 4 = downloading (spec RPC). error != 0 = torrent en erreur
    # (tracker injoignable, fichier introuvable sur disque, etc. —
    # errorString donne le détail, pas exposé ici, juste le compte).
    torrents_downloading = sum(1 for t in torrents if t.get("status", 0) == 4)
    torrents_errored = sum(1 for t in torrents if t.get("error", 0) != 0)

    tracker_map = build_prowlarr_tracker_map()
    per_tracker = {}
    for t in torrents:
        # Un torrent multi-tracker (fréquent : releases postées à la fois sur
        # trackers publics et privés) compte pour chacun de ses hosts
        # d'annonce — impossible de départager quel octet a été uploadé "pour"
        # quel tracker côté RPC Transmission, donc le total par tracker
        # surestime légèrement le volume réel global mais reste correct par
        # tracker pris individuellement. Dédupliqué par NOM résolu, pas par
        # host brut : via TRACKER_ALIASES, plusieurs hosts d'un même torrent
        # peuvent résoudre vers le même nom — sans cette dédup, un tel
        # torrent comptait une fois par host (5x pour Nyaa) au lieu d'une
        # fois par tracker logique, gonflant les volumes affichés d'un
        # facteur 5 (constaté le 2026-07-28, ratio non affecté par
        # coïncidence — numérateur et dénominateur gonflés pareil).
        seen_names = set()
        for host in tracker_hosts(t) or ["?"]:
            name = resolve_tracker_name(host, tracker_map) if host != "?" else "?"
            if name in seen_names:
                continue
            seen_names.add(name)
            entry = per_tracker.setdefault(name, {"uploaded": 0, "downloaded": 0})
            entry["uploaded"] += t.get("uploadedEver", 0)
            entry["downloaded"] += t.get("downloadedEver", 0)

    trackers_out = sorted(
        (
            {"name": name, "uploaded": v["uploaded"], "downloaded": v["downloaded"],
             "uploaded_human": human_size(v["uploaded"]), "downloaded_human": human_size(v["downloaded"]),
             "ratio": ratio(v["uploaded"], v["downloaded"]),
             "ratio_display": ratio_display(ratio(v["uploaded"], v["downloaded"]))}
            for name, v in per_tracker.items()
        ),
        key=lambda e: e["uploaded"], reverse=True,
    )

    cur = session["current-stats"]
    cum = session["cumulative-stats"]
    download_speed, upload_speed = session["downloadSpeed"], session["uploadSpeed"]
    session_ratio = ratio(cur["uploadedBytes"], cur["downloadedBytes"])
    total_ratio = ratio(cum["uploadedBytes"], cum["downloadedBytes"])
    try:
        speed_scale = record_speed_sample(time.time(), download_speed, upload_speed)
    except OSError:
        speed_scale = None
    # Fallback sans historique (DATA_ROOT absent/illisible, ou erreur
    # d'écriture) : le mètre de débit retombe sur le seul échantillon
    # courant, pas d'échelle basée sur le passé récent.
    speed_scale = speed_scale or historical_max_speed([], download_speed, upload_speed)
    return {
        "download_speed": download_speed,
        "download_speed_human": human_rate(download_speed),
        "upload_speed": upload_speed,
        "upload_speed_human": human_rate(upload_speed),
        "speed_scale": speed_scale,
        "torrents_active": torrents_active,
        "torrents_total": torrents_total,
        "torrents_downloading": torrents_downloading,
        "torrents_errored": torrents_errored,
        "session": {
            "uploaded": cur["uploadedBytes"],
            "uploaded_human": human_size(cur["uploadedBytes"]),
            "downloaded": cur["downloadedBytes"],
            "downloaded_human": human_size(cur["downloadedBytes"]),
            "ratio": session_ratio,
            "ratio_display": ratio_display(session_ratio),
            "seconds_active": cur["secondsActive"],
            "uptime_human": human_duration(cur["secondsActive"]),
        },
        "total": {
            "uploaded": cum["uploadedBytes"],
            "uploaded_human": human_size(cum["uploadedBytes"]),
            "downloaded": cum["downloadedBytes"],
            "downloaded_human": human_size(cum["downloadedBytes"]),
            "ratio": total_ratio,
            "ratio_display": ratio_display(total_ratio),
        },
        "trackers": trackers_out,
    }


if __name__ == "__main__":
    try:
        json.dump(main(), sys.stdout)
    except Exception as e:
        json.dump({"error": str(e)}, sys.stdout)
