#!/usr/bin/env python3
"""Compare latence/débit AirVPN entre le serveur actuel et d'autres pays.

Usage:
    python3 scripts/vpn-bench.py nl de ch
    python3 scripts/vpn-bench.py nl de --tracker tracker.yggreborn.org

Ne touche qu'à la ligne `remote` de vpn/custom/default.ovpn (le certificat
client AirVPN est lié au compte, pas au serveur — même fichier réutilisable
pour n'importe quel `[pays].vpn.airdns.org`). Restaure systématiquement la
config d'origine à la fin, même en cas d'erreur ou de Ctrl+C.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OVPN_PATH = REPO_ROOT / "vpn/custom/default.ovpn"
# Backup sur disque (pas seulement en mémoire) : si le script est tué durement
# (SIGKILL, crash), ce fichier permet de restaurer la config à la main plutôt
# que de perdre le certificat client AirVPN.
BACKUP_PATH = REPO_ROOT / "vpn/custom/.default.ovpn.bench-backup"

# Nom de container fixe pour la stack vpn/ (comme TRANSMISSION_CONTAINER dans
# transmission-stats.py) — dépend du nom du dossier compose, pas du déploiement.
CONTAINER = "vpn-transmission-vpn-1"
RPC_URL = "http://localhost:9091/transmission/rpc"

# Cloudflare répond 403 sans User-Agent de navigateur (bot detection).
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


class BenchError(RuntimeError):
    pass


def compose_cmd(*args):
    return [
        "docker", "compose",
        "--env-file", str(REPO_ROOT / ".env.shared"),
        "--env-file", str(REPO_ROOT / "vpn/.env"),
        "-f", str(REPO_ROOT / "vpn/docker-compose.yml"),
        *args,
    ]


def restart_vpn():
    subprocess.run(compose_cmd("restart", "transmission-vpn"), check=True, cwd=REPO_ROOT)


def wait_healthy(timeout=180, interval=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = subprocess.run(
            ["docker", "inspect", "--format={{.State.Health.Status}}", CONTAINER],
            capture_output=True, text=True,
        )
        if res.stdout.strip() == "healthy":
            return True
        time.sleep(interval)
    return False


def swap_remote(country_code):
    text = OVPN_PATH.read_text()
    new_text, n = re.subn(
        r"^remote \S+ (\d+)",
        lambda m: f"remote {country_code}.vpn.airdns.org {m.group(1)}",
        text, count=1, flags=re.MULTILINE,
    )
    if n == 0:
        raise BenchError("Ligne 'remote' introuvable dans default.ovpn — format inattendu, abandon.")
    OVPN_PATH.write_text(new_text)


def current_country_code():
    m = re.search(r"^remote (\S+)\.vpn\.airdns\.org", OVPN_PATH.read_text(), re.MULTILINE)
    if not m:
        raise BenchError("Config actuelle non reconnue comme un remote AirVPN [pays].vpn.airdns.org.")
    return m.group(1)


def _rpc_once(method, arguments, session_id):
    cmd = ["docker", "exec", "-i", CONTAINER, "curl", "-s", "-i",
           "-X", "POST", "-d", "@-", RPC_URL]
    if session_id:
        cmd[6:6] = ["-H", f"X-Transmission-Session-Id: {session_id}"]
    payload = json.dumps({"method": method, "arguments": arguments or {}}).encode()
    res = subprocess.run(cmd, input=payload, capture_output=True, timeout=30)
    text = res.stdout.decode(errors="replace")
    head, _, body = text.partition("\r\n\r\n")
    return head.split("\r\n", 1)[0], head, body


def rpc(method, arguments=None):
    status_line, head, body = _rpc_once(method, arguments, None)
    if "409" in status_line:
        session_id = next(
            (l.split(":", 1)[1].strip() for l in head.split("\r\n")
             if l.lower().startswith("x-transmission-session-id")), None,
        )
        status_line, head, body = _rpc_once(method, arguments, session_id)
    return json.loads(body)


def pick_random_tracker():
    import random
    from urllib.parse import urlparse

    data = rpc("torrent-get", {"fields": ["trackerStats"]})
    hosts = set()
    for t in data["arguments"]["torrents"]:
        for ts in t.get("trackerStats", []):
            h = urlparse(ts["announce"]).hostname
            if h:
                hosts.add(h)
    if not hosts:
        raise BenchError("Aucun tracker actif trouvé via le RPC Transmission — passe --tracker explicitement.")
    return random.choice(sorted(hosts))


def ping_test(host, count=5):
    res = subprocess.run(
        ["docker", "exec", CONTAINER, "ping", "-c", str(count), host],
        capture_output=True, text=True, timeout=count * 2 + 20,
    )
    out = res.stdout
    loss = None
    avg = None
    m = re.search(r"(\d+)% packet loss", out)
    if m:
        loss = int(m.group(1))
    m2 = re.search(r"= [\d.]+/([\d.]+)/[\d.]+/[\d.]+ ms", out)
    if m2:
        avg = float(m2.group(1))
    return {"avg_ms": avg, "loss_pct": loss}


def _parse_curl_stats(text):
    m = re.search(r"HTTP:(\d+) SPEED:([\d.]+)", text)
    if not m:
        return {"http_code": None, "speed_bps": None}
    return {"http_code": int(m.group(1)), "speed_bps": float(m.group(2))}


def download_test(num_bytes):
    res = subprocess.run(
        ["docker", "exec", CONTAINER, "curl", "-A", UA, "-o", "/dev/null", "-s",
         "-w", "HTTP:%{http_code} SPEED:%{speed_download}",
         f"https://speed.cloudflare.com/__down?bytes={num_bytes}"],
        capture_output=True, text=True, timeout=300,
    )
    return _parse_curl_stats(res.stdout)


def upload_test(num_bytes):
    res = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "curl", "-A", UA, "-s", "-o", "/dev/null",
         "-w", "HTTP:%{http_code} SPEED:%{speed_upload}",
         "-X", "POST", "--data-binary", "@-", "https://speed.cloudflare.com/__up"],
        input=os.urandom(num_bytes), capture_output=True, timeout=400,
    )
    return _parse_curl_stats(res.stdout.decode(errors="replace"))


def print_table(results, tracker):
    print(f"\n=== Bench VPN — tracker cible: {tracker} ===")
    print(f"{'Pays':10} {'Latence':>10} {'Perte':>6} {'Down':>14} {'Up':>14}")
    for r in results:
        country = r["country"]
        if r.get("error"):
            print(f"{country:10} {'—':>10} {'—':>6} {'—':>14} {'—':>14}  ({r['error']})")
            continue
        lat = r["ping"]["avg_ms"]
        loss = r["ping"]["loss_pct"]
        down_mbps = (r["down"]["speed_bps"] or 0) * 8 / 1e6
        up_mbps = (r["up"]["speed_bps"] or 0) * 8 / 1e6
        lat_s = f"{lat:.1f} ms" if lat is not None else "—"
        loss_s = f"{loss}%" if loss is not None else "—"
        print(f"{country:10} {lat_s:>10} {loss_s:>6} {down_mbps:>11.1f} Mbit/s {up_mbps:>11.2f} Mbit/s")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("countries", nargs="+", help="codes pays AirVPN à tester en plus du serveur actuel (ex: nl de ch)")
    parser.add_argument("--tracker", help="host du tracker pour le test de latence (défaut: tiré au hasard parmi les torrents actifs)")
    parser.add_argument("--down-bytes", type=int, default=50_000_000)
    parser.add_argument("--up-bytes", type=int, default=25_000_000)
    parser.add_argument("--ping-count", type=int, default=5)
    args = parser.parse_args()

    if BACKUP_PATH.exists():
        print(f"ERREUR: {BACKUP_PATH} existe déjà — un run précédent a probablement été interrompu "
              f"sans pouvoir restaurer la config. Inspecte les deux fichiers et restaure "
              f"manuellement avant de relancer ce script.", file=sys.stderr)
        sys.exit(1)

    original = OVPN_PATH.read_text()
    baseline_cc = current_country_code()
    BACKUP_PATH.write_text(original)

    tracker = args.tracker or pick_random_tracker()
    print(f"Tracker cible: {tracker}")
    print(f"Serveur actuel (baseline): {baseline_cc}")

    results = []
    try:
        plan = [(baseline_cc, False)] + [(cc, True) for cc in args.countries]
        for cc, needs_swap in plan:
            label = cc.upper()
            print(f"\n--- {label} ---")
            if needs_swap:
                swap_remote(cc)
            restart_vpn()
            if not wait_healthy():
                print(f"[{label}] jamais devenu healthy après redémarrage, run ignoré")
                results.append({"country": label, "error": "unhealthy"})
                continue
            ping = ping_test(tracker, args.ping_count)
            down = download_test(args.down_bytes)
            up = upload_test(args.up_bytes)
            results.append({"country": label, "ping": ping, "down": down, "up": up})
            print(f"[{label}] latence {ping['avg_ms']} ms (perte {ping['loss_pct']}%), "
                  f"down {(down['speed_bps'] or 0) * 8 / 1e6:.1f} Mbit/s, "
                  f"up {(up['speed_bps'] or 0) * 8 / 1e6:.1f} Mbit/s")
    finally:
        print(f"\n--- restauration config {baseline_cc.upper()} ---")
        OVPN_PATH.write_text(original)
        restart_vpn()
        if not wait_healthy():
            print("ATTENTION: le container n'est pas revenu healthy après restauration — vérifier manuellement "
                  "(`docker logs vpn-transmission-vpn-1`).", file=sys.stderr)
        BACKUP_PATH.unlink(missing_ok=True)

    print_table(results, tracker)


if __name__ == "__main__":
    main()
