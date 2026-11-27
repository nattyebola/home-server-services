#!/usr/bin/env python3
# Cherche ce qui a RÉUSSI depuis le WAN, pas ce qui a échoué.
#
# Pourquoi ce script existe : compter les 404 et les 401 des scanners revient
# à ne regarder que les tentatives qui ont échoué (biais du survivant, relevé
# le 2026-11-27). Une sonde qui passe ou un identifiant volé ne laisse aucune
# erreur. Ce script regarde donc l'inverse, en lecture seule :
#
#   1. Traefik (access log JSON, seule trace des requêtes WAN) :
#      a. une sonde de scanner (.env, .git, wp-login…) qui reçoit un 2xx.
#         Le code seul ment : une appli à page unique (Komga, /next/) renvoie
#         200 et sa page d'index sur N'IMPORTE QUEL chemin. Chaque 2xx est
#         donc rejoué contre un chemin témoin inexistant du même dossier, en
#         local : même statut et même taille = page de repli, écartée.
#      b. un routeur LAN-only (middleware *-lan-only@file) qui répond < 400 à
#         une IP du WAN : le filtre ne tient plus (ou l'ouverture temporaire
#         de `make switch-lan-only-middleware` est en cours).
#      c. une requête AUTHENTIFIÉE réussie depuis un préfixe /16 jamais vu.
#   2. SSH : une connexion acceptée depuis un préfixe /16 WAN jamais vu.
#   3. Nextcloud : un jeton d'authentification (appareil, mot de passe
#      d'application) jamais vu. Nextcloud n'en garde pas l'IP.
#   4. Jellyfin : un appareil jamais vu, ou une session ouverte depuis un
#      préfixe WAN jamais vu (journal d'activité, IP réelle grâce à
#      KnownProxies, voir docs/medias.md).
#
# Limite assumée : un attaquant qui a un identifiant valide ET passe par un
# préfixe déjà connu (même opérateur mobile) reste invisible. Rien dans les
# journaux ne le distingue d'un usage légitime.
#
# « Jamais vu » = absent de la référence DATA_ROOT/.wan-audit/connus.json.
# `--accept` y ajoute tout ce qui est visible (14 jours d'access log pour
# Traefik) : à lancer seulement après avoir relu la sortie, c'est valider que
# tout ce qui est listé est à soi. Sans --accept, rien n'est écrit.
#
# Sortie : une ligne par constat, les IP réduites à leur /16 (jamais l'IP
# complète ni le domaine : la sortie finit dans des rapports). Les erreurs
# d'une source vont sur stderr (« ERREUR <source> : … ») sans bloquer les
# autres ; code de retour 1 dans ce cas.
import argparse
import datetime
import glob
import gzip
import http.client
import ipaddress
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.request
import uuid

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASELINE_REL = ".wan-audit/connus.json"
ACCESS_LOG_REL = ".traefik/log"
NEXTCLOUD_CONTAINER = "nextcloud-app-1"

# Chemins qu'aucun client légitime de ce serveur ne demande. Volontairement
# étroit : pas de « admin » ni de « .php » génériques (Nextcloud en sert).
PROBE_RE = re.compile(
    r"/\.env|/\.git/|/\.aws/|/\.ssh/|wp-(login|admin|content|includes)|xmlrpc\.php"
    r"|phpmyadmin|/vendor/phpunit|/cgi-bin/|/actuator|/server-status"
    r"|/config\.(json|ya?ml|php)$",
    re.I,
)

# Requêtes qui n'aboutissent (2xx) qu'avec une session ou un identifiant
# valide. Mesuré le 2026-11-27 sur 14 jours : seuls Nextcloud (DAV des
# téléphones) et Seerr en ont depuis le WAN. Les chemins publics de chaque
# appli sont exclus (calendriers publics, capabilities, /Users/Public).
AUTH_PATHS = {
    "nextcloud": re.compile(r"^/remote\.php/(dav|webdav)/(?!public)|^/ocs/(?!v[12]\.php/cloud/capabilities)"),
    "jellyfin": re.compile(r"^/(Users/(?!Public)|Items|Sessions|Shows|Videos|Audio)"),
    "seerr": re.compile(r"^/api/v1/auth/(me|local|jellyfin)"),
    "komga": re.compile(r"^/api/v[12]/users/me"),
}

# Écart de taille toléré entre une réponse et son témoin pour les dire
# identiques (en-têtes ou jeton CSRF variables d'une réponse à l'autre).
WITNESS_SLACK_BYTES = 64


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


def fmt(moment):
    return moment.astimezone().strftime("%d/%m %H:%M")


def span(entry, moment):
    """entry = [nombre, première, dernière] : les fichiers de log ne sont pas
    lus dans l'ordre chronologique (access.log avant access.log.1)."""
    entry[0] += 1
    entry[1] = min(entry[1], moment)
    entry[2] = max(entry[2], moment)


class Audit:
    def __init__(self, data_root, lan_cidr, domain, since, accept):
        self.data_root = data_root
        self.lan = ipaddress.ip_network(lan_cidr) if lan_cidr else None
        self.domain = domain
        self.since = since
        self.accept = accept
        self.baseline_path = os.path.join(data_root, BASELINE_REL)
        try:
            with open(self.baseline_path) as f:
                self.baseline = json.load(f)
        except FileNotFoundError:
            self.baseline = {}
        self.first_run = not self.baseline
        self.findings = []
        self.added = 0
        self.errors = False

    # --- utilitaires

    def wan_prefix(self, raw):
        """Préfixe /16 d'une IP du WAN, None si elle est locale ou invalide."""
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return None
        if ip.is_private or ip.is_loopback or ip.is_link_local or (self.lan and ip in self.lan):
            return None
        if ip.version == 6:
            return str(ipaddress.ip_network(f"{ip}/32", strict=False))
        return ".".join(str(ip).split(".")[:2]) + ".x.x"

    def known(self, *path):
        node = self.baseline
        for key in path:
            node = node.get(key, {}) if isinstance(node, dict) else {}
        return set(node) if isinstance(node, list) else set()

    def remember(self, values, *path):
        """En mode --accept, ajoute values à la référence sous path."""
        if not self.accept:
            return
        node = self.baseline
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = sorted(set(node.get(path[-1], [])) | set(values))

    def report(self, source, text, novel=False):
        """novel=True : élément absent de la référence. En mode --accept il
        vient d'y être inscrit : le libellé « inconnu » serait faux, la ligne
        est marquée « + » à la place."""
        if novel and self.accept:
            self.added += 1
            self.findings.append(f"+ [{source}] {text.replace(' inconnu', '', 1)}")
        else:
            self.findings.append(f"[{source}] {text}")

    def error(self, source, exc):
        print(f"ERREUR {source} : {exc}", file=sys.stderr)
        self.errors = True

    # --- 1. Traefik

    def access_log_records(self, all_files):
        log_dir = os.path.join(self.data_root, ACCESS_LOG_REL)
        files = sorted(glob.glob(os.path.join(log_dir, "access.log*")))
        if not files:
            raise FileNotFoundError(f"aucun access log sous {ACCESS_LOG_REL}")
        for path in files:
            opener = gzip.open if path.endswith(".gz") else open
            with opener(path, "rt", errors="replace") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        start = datetime.datetime.fromisoformat(rec["StartUTC"].replace("Z", "+00:00"))
                    except (ValueError, KeyError):
                        continue
                    if all_files or start >= self.since:
                        yield start, rec

    def witness_matches(self, host, path, status, size, cache):
        """Rejoue un chemin témoin inexistant du même dossier sur Traefik en
        local. True si la réponse a même statut et même taille : le 2xx de la
        sonde n'était que la page de repli de l'appli."""
        folder = path.split("?")[0].rsplit("/", 1)[0]
        key = (host, folder)
        if key not in cache:
            ctx = ssl.create_default_context()
            # 127.0.0.1 : le certificat ne peut pas correspondre, et la
            # requête ne quitte pas la machine.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection("127.0.0.1", 443, context=ctx, timeout=15)
            try:
                conn.request("GET", f"{folder}/wan-audit-temoin-{uuid.uuid4().hex[:8]}", headers={"Host": host})
                resp = conn.getresponse()
                cache[key] = (resp.status, len(resp.read()))
            finally:
                conn.close()
        w_status, w_size = cache[key]
        return w_status == status and abs(w_size - size) <= WITNESS_SLACK_BYTES

    def lan_only_routers(self):
        routers = set()
        label = re.compile(r"traefik\.http\.routers\.([\w-]+)\.middlewares=[^\"\n]*-lan-only@file")
        for path in glob.glob(os.path.join(REPO_ROOT, "*", "docker-compose.yml")):
            with open(path) as f:
                routers |= set(label.findall(f.read()))
        return routers

    def check_traefik(self):
        lan_only = self.lan_only_routers()
        witness_cache = {}
        probes = {}       # (routeur, chemin) -> [préfixes]
        lan_leaks = {}    # routeur -> [nb, première, dernière]
        auth_seen = {}    # routeur -> {préfixe: [nb, première, dernière]}
        # --accept lit les 14 jours conservés pour amorcer la référence ;
        # sinon seulement la fenêtre demandée.
        for start, rec in self.access_log_records(all_files=self.accept):
            prefix = self.wan_prefix(rec.get("ClientHost", ""))
            if prefix is None:
                continue
            router = (rec.get("RouterName") or "").split("@")[0]
            status = rec.get("DownstreamStatus") or 0
            path = rec.get("RequestPath") or ""
            in_window = start >= self.since
            stamp = fmt(start)

            if in_window and router in lan_only and 0 < status < 400:
                span(lan_leaks.setdefault(router, [0, start, start]), start)

            if not 200 <= status < 300:
                continue

            if in_window and PROBE_RE.search(path):
                host = rec.get("RequestHost") or ""
                size = rec.get("DownstreamContentSize") or 0
                try:
                    if self.witness_matches(host, path, status, size, witness_cache):
                        continue
                except OSError as exc:
                    self.error("traefik", f"témoin injoignable pour {router} : {exc}")
                    continue
                probes.setdefault((router, path.split("?")[0][:80]), []).append(f"{prefix} le {stamp}")

            if router in AUTH_PATHS and AUTH_PATHS[router].search(path):
                span(auth_seen.setdefault(router, {}).setdefault(prefix, [0, start, start]), start)

        for (router, path), hits in probes.items():
            self.report("traefik", f"{router} : sonde servie en 2xx, différente de la page de repli : {path} ({len(hits)}×, {hits[0]})")
        for router, (n, first, last) in lan_leaks.items():
            self.report("traefik", f"{router} (LAN-only) : {n} réponses < 400 au WAN, du {fmt(first)} au {fmt(last)}")
        for router, prefixes in sorted(auth_seen.items()):
            known = self.known("traefik", router)
            for prefix, (n, first, last) in sorted(prefixes.items()):
                if prefix not in known:
                    self.report("traefik", f"{router} : requêtes authentifiées depuis un préfixe inconnu {prefix} ({n}×, du {fmt(first)} au {fmt(last)})", novel=True)
            self.remember(prefixes, "traefik", router)

    # --- 2. SSH

    def check_ssh(self):
        hours = 14 * 24 if self.accept else None
        since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)) if hours else self.since
        out = subprocess.run(
            ["journalctl", "-q", "-u", "ssh", "-u", "sshd", "--since", since.astimezone().strftime("%Y-%m-%d %H:%M:%S"), "-o", "short-iso"],
            capture_output=True, text=True, check=True,
        ).stdout
        seen = {}  # utilisateur -> {préfixe: [méthode, nb, dernière]}
        for line in out.splitlines():
            m = re.search(r"^(\S+) .*Accepted (\S+) for (\S+) from (\S+)", line)
            if not m:
                continue
            prefix = self.wan_prefix(m.group(4))
            if prefix is None:
                continue
            entry = seen.setdefault(m.group(3), {}).setdefault(prefix, [m.group(2), 0, ""])
            entry[1] += 1
            entry[2] = m.group(1)[:16].replace("T", " ")
        for user, prefixes in seen.items():
            known = self.known("ssh", user)
            for prefix, (method, n, last) in prefixes.items():
                if prefix not in known:
                    self.report("ssh", f"{user} : connexion acceptée ({method}) depuis un préfixe inconnu {prefix} ({n}×, dernière {last})", novel=True)
            self.remember(prefixes, "ssh", user)

    # --- 3. Nextcloud

    def occ_json(self, *args):
        out = subprocess.run(
            ["docker", "exec", NEXTCLOUD_CONTAINER, "./occ", *args, "--output=json"],
            capture_output=True, text=True, check=True,
        ).stdout
        return json.loads(out)

    def check_nextcloud(self):
        for user in self.occ_json("user:list"):
            known = self.known("nextcloud_tokens", user)
            tokens = self.occ_json("user:auth-tokens:list", user)
            for tok in tokens:
                if str(tok["id"]) not in known:
                    last = datetime.datetime.fromtimestamp(tok["lastActivity"]).strftime("%d/%m %H:%M")
                    self.report("nextcloud", f"{user} : jeton inconnu « {tok.get('name', '')[:60]} » (dernière activité {last})", novel=True)
            self.remember([str(t["id"]) for t in tokens], "nextcloud_tokens", user)

    # --- 4. Jellyfin

    def jellyfin_get(self, api_key, path):
        host = f"jellyfin.{self.domain}"
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(
            f"https://127.0.0.1{path}",
            headers={"Host": host, "Authorization": f'MediaBrowser Token="{api_key}"'},
        )
        with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
            return json.load(resp)

    def check_jellyfin(self):
        api_key = load_env_file(os.path.join(REPO_ROOT, "arr", ".env")).get("JELLYFIN_API_KEY")
        if not api_key or not self.domain:
            raise RuntimeError("JELLYFIN_API_KEY (arr/.env) ou DOMAIN (.env.shared) absent")
        devices = self.jellyfin_get(api_key, "/Devices")["Items"]
        known = self.known("jellyfin", "devices")
        for dev in devices:
            if dev["Id"] not in known:
                self.report("jellyfin", f"appareil inconnu « {dev.get('Name', '')} » / {dev.get('AppName', '')} ({dev.get('LastUserName', '?')}, {dev.get('DateLastActivity', '')[:10]})", novel=True)
        self.remember([d["Id"] for d in devices], "jellyfin", "devices")

        min_date = self.since if not self.accept else datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=14)
        entries = self.jellyfin_get(
            api_key,
            f"/System/ActivityLog/Entries?limit=1000&minDate={min_date.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        )["Items"]
        known_ip = self.known("jellyfin", "ip")
        seen = {}
        for e in entries:
            if e.get("Type") not in ("AuthenticationSucceeded", "SessionStarted"):
                continue
            m = re.search(r"(\d+\.\d+\.\d+\.\d+|[0-9a-f:]{3,})\s*$", e.get("ShortOverview") or "", re.I)
            prefix = self.wan_prefix(m.group(1)) if m else None
            if prefix:
                seen.setdefault(prefix, []).append(f"{e['Date'][:16].replace('T', ' ')} {e.get('Name', '')[:50]}")
        for prefix, hits in seen.items():
            if prefix not in known_ip:
                self.report("jellyfin", f"session depuis un préfixe inconnu {prefix} ({len(hits)}×, ex. {hits[0]})", novel=True)
        self.remember(seen, "jellyfin", "ip")

    # ---

    def run(self):
        for name, check in (("traefik", self.check_traefik), ("ssh", self.check_ssh),
                            ("nextcloud", self.check_nextcloud), ("jellyfin", self.check_jellyfin)):
            try:
                check()
            except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, RuntimeError, KeyError) as exc:
                self.error(name, exc)
        if self.accept:
            os.makedirs(os.path.dirname(self.baseline_path), exist_ok=True)
            tmp = self.baseline_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.baseline, f, indent=1, sort_keys=True)
            os.replace(tmp, self.baseline_path)


def main():
    parser = argparse.ArgumentParser(description="Cherche ce qui a réussi depuis le WAN (lecture seule).")
    parser.add_argument("--hours", type=int, default=48, help="fenêtre examinée (défaut 48 h)")
    parser.add_argument("--accept", action="store_true",
                        help="ajoute tout ce qui est visible à la référence des éléments connus (après relecture)")
    args = parser.parse_args()

    env = load_env_file(os.path.join(REPO_ROOT, ".env.shared"))
    data_root = env.get("DATA_ROOT")
    if not data_root:
        sys.exit("ERREUR : DATA_ROOT absent de .env.shared")
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=args.hours)
    audit = Audit(data_root, env.get("LAN_CIDR"), env.get("DOMAIN"), since, args.accept)
    audit.run()

    if audit.first_run and not args.accept:
        print("Pas encore de référence : tout ce qui suit est « inconnu ». Relire, puis `--accept`.")
    for line in audit.findings:
        print(line)
    if args.accept:
        # Les sondes et fuites LAN-only ne s'acceptent pas : elles restent
        # affichées sans « + » et ne comptent pas.
        print(f"Référence mise à jour ({audit.added} éléments ajoutés, marqués « + »).")
    sys.exit(1 if audit.errors else 0)


if __name__ == "__main__":
    main()
