#!/usr/bin/env python3
# Force un ratio-limite de 2 sur tous les torrents Nyaa.si (seedRatioMode=1,
# seedRatioLimit=2) — demandé par l'utilisateur le 2026-07-28 pour les
# torrents actuels ET futurs. Idempotent (ne touche que ce qui n'est pas déjà
# à jour) : conçu pour tourner en cron (voir scripts/crontab) plutôt qu'une
# seule fois à la main, pour rattraper aussi les torrents Nyaa ajoutés après
# ce passage. Même mécanisme docker-exec-curl et même logique de résolution
# de nom de tracker que transmission-stats.py/torrent-cleanup.py (dupliquée,
# pas factorisée — voir CLAUDE.md).
import json
import os
import subprocess
import sys
import urllib.parse

TRANSMISSION_CONTAINER = "vpn-transmission-vpn-1"
RPC_URL = "http://localhost:9091/transmission/rpc"

PROWLARR_CONTAINER = "arr-prowlarr-1"
PROWLARR_URL = "http://localhost:9696/api/v1/indexer"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TARGET_TRACKER_NAME = "Nyaa.si"
RATIO_LIMIT = 2.0


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


PROWLARR_API_KEY = load_env_file(os.path.join(REPO_ROOT, "arr", ".env")).get("PROWLARR_API_KEY")


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
    parts = hostname.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else hostname


def build_prowlarr_tracker_map():
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


# Alias manuels pour des trackers publics génériques — voir la même
# constante dans torrent-cleanup.py/transmission-stats.py (confirmé par
# l'utilisateur le 2026-07-28, ce bundle exact n'appartient qu'à Nyaa.si
# dans cette bibliothèque).
MANUAL_TRACKER_ALIASES = {
    "nyaa.tracker.wf": "Nyaa.si",
    "open.stealth.si": "Nyaa.si",
    "tracker.opentrackr.org": "Nyaa.si",
    "exodus.desync.com": "Nyaa.si",
    "tracker.torrent.eu.org": "Nyaa.si",
}


def resolve_tracker_name(hostname, tracker_map):
    if hostname in MANUAL_TRACKER_ALIASES:
        return MANUAL_TRACKER_ALIASES[hostname]
    return tracker_map.get(base_domain(hostname), hostname)


def tracker_names(torrent, tracker_map):
    names = set()
    for t in torrent.get("trackerStats") or []:
        host = urllib.parse.urlparse(t.get("announce", "")).hostname
        if host:
            names.add(resolve_tracker_name(host, tracker_map))
    return names


def main():
    tracker_map = build_prowlarr_tracker_map()
    torrents = rpc("torrent-get", {
        "fields": ["id", "name", "trackerStats", "seedRatioMode", "seedRatioLimit"],
    })["torrents"]

    nyaa_torrents = [t for t in torrents if TARGET_TRACKER_NAME in tracker_names(t, tracker_map)]
    to_fix = [t for t in nyaa_torrents if t["seedRatioMode"] != 1 or t["seedRatioLimit"] != RATIO_LIMIT]
    if to_fix:
        rpc("torrent-set", {
            "ids": [t["id"] for t in to_fix],
            "seedRatioMode": 1,
            "seedRatioLimit": RATIO_LIMIT,
        })
    print(f"{TARGET_TRACKER_NAME}: {len(nyaa_torrents)} torrent(s), {len(to_fix)} mis à jour "
          f"(ratio-limite {RATIO_LIMIT})")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Erreur : {e}", file=sys.stderr)
        sys.exit(1)
