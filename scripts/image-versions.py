#!/usr/bin/env python3
# Journal des montées de version, alimenté par `make update` et lu par le
# skill `changelogs` (.claude/skills/changelogs/SKILL.md).
#
#   <compose config json> | image-versions.py snapshot <stack>   avant le pull
#   <compose config json> | image-versions.py record <stack>     après le up
#   image-versions.py recap --since <epoch>    récap de fin d'update-all
#   image-versions.py pending                  entrées non traitées (JSON)
#   image-versions.py mark <id>... | --all     marque des entrées traitées
#
# La version est lue AVANT le pull : une fois l'ancienne image purgée par
# `docker image prune` (fin d'update-all), elle serait introuvable. La photo
# « avant » vit dans un fichier jusqu'au `record` ; si un update échoue
# entre les deux, elle est gardée telle quelle et sert de point de départ
# au suivant (sinon la montée de version ratée disparaîtrait du journal).
#
# Toujours best-effort : une erreur ici s'affiche mais ne fait jamais
# échouer `make update` (code de retour 0).
import datetime
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# D'où lire la version de chaque service, et où sont ses notes de version.
# "<projet>/<service>", comme scripts/require-running.sh.
#   version : label:<clé> | env:<VAR> | jar:<chemin dans l'image>
#             (défaut : label org.opencontainers.image.version)
#   repo    : dépôt GitHub des notes de version (None : voir `notes`)
#   tier    : "app" détaillée par le skill, "infra" résumée en une ligne
#             sauf changement cassant ou faille de sécurité
# Les labels ne sont pas fiables partout : komga porte celui de sa base
# Ubuntu (« 26.04 »), d'où la lecture du manifeste de son jar.
OCI_VERSION = "label:org.opencontainers.image.version"
SOURCES = {
    "traefik/traefik": {"repo": "traefik/traefik", "tier": "app"},
    "traefik/socket-proxy": {"repo": "Tecnativa/docker-socket-proxy", "tier": "infra"},
    "traefik/dashboard": {"version": "env:NGINX_VERSION", "repo": "nginx/nginx", "tier": "infra"},
    "jellyfin/jellyfin": {"repo": "jellyfin/jellyfin", "tier": "app"},
    "nextcloud/app": {"version": "env:NEXTCLOUD_VERSION", "repo": "nextcloud/server", "tier": "app"},
    "nextcloud/web": {"version": "env:NGINX_VERSION", "repo": "nginx/nginx", "tier": "infra"},
    "nextcloud/db-next": {"version": "env:PG_VERSION", "repo": None, "tier": "infra",
                          "notes": "https://www.postgresql.org/docs/release/"},
    "vpn/transmission-vpn": {"repo": "haugene/docker-transmission-openvpn", "tier": "app"},
    "vpn/transmission-proxy": {"version": "env:NGINX_VERSION", "repo": "nginx/nginx", "tier": "infra"},
    "vpn/webproxy": {"version": "env:NGINX_VERSION", "repo": "nginx/nginx", "tier": "infra"},
    "arr/sonarr": {"repo": "Sonarr/Sonarr", "tier": "app"},
    "arr/radarr": {"repo": "Radarr/Radarr", "tier": "app"},
    "arr/prowlarr": {"repo": "Prowlarr/Prowlarr", "tier": "app"},
    "arr/cross-seed": {"repo": "cross-seed/cross-seed", "tier": "app"},
    "arr/clearr": {"version": "env:PYTHON_VERSION", "repo": "python/cpython", "tier": "infra"},
    "seerr/seerr": {"repo": "seerr-team/seerr", "tier": "app"},
    "komga/komga": {"version": "jar:/app/application.jar", "repo": "gotson/komga", "tier": "app"},
}


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


DATA_ROOT = load_env_file(os.path.join(REPO_ROOT, ".env.shared")).get("DATA_ROOT")
JOURNAL_DIR = os.path.join(DATA_ROOT, ".update-journal") if DATA_ROOT else None
JOURNAL = os.path.join(JOURNAL_DIR, "journal.jsonl") if JOURNAL_DIR else None


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def normalize(raw):
    """« v3.7.13 », « version-6.13.7 », « 4.0.20.3014-ls326 » → numéro amont.
    Le suffixe -lsNNN (rebuild linuxserver de la même version) est retiré
    exprès : sans lui, chaque mise à jour de leur base OS ferait une entrée."""
    if not raw:
        return None
    v = re.sub(r"^(version-|v)", "", raw.strip())
    return re.sub(r"-ls\d+$", "", v) or None


def jar_version(image_id, path):
    # Image jamais démarrée : `docker create` suffit pour en extraire un fichier.
    created = docker("create", image_id)
    if created.returncode != 0:
        return None
    cid = created.stdout.strip()
    try:
        tar = subprocess.run(["docker", "cp", f"{cid}:{path}", "-"], capture_output=True, check=False)
        if tar.returncode != 0:
            return None
        with tarfile.open(fileobj=io.BytesIO(tar.stdout)) as t:
            member = t.next()
            jar = zipfile.ZipFile(io.BytesIO(t.extractfile(member).read()))
        manifest = jar.read("META-INF/MANIFEST.MF").decode()
        m = re.search(r"^Implementation-Version:\s*(\S+)", manifest, re.M)
        return m.group(1) if m else None
    finally:
        docker("rm", cid)


def read_version(image_id, how):
    kind, _, key = how.partition(":")
    if kind == "jar":
        return jar_version(image_id, key)
    out = docker("image", "inspect", image_id, "--format", "{{json .Config}}")
    if out.returncode != 0:
        return None
    config = json.loads(out.stdout)
    if kind == "label":
        return (config.get("Labels") or {}).get(key)
    if kind == "env":
        for entry in config.get("Env") or []:
            name, _, value = entry.partition("=")
            if name == key:
                return value
    return None


def running_image(project, service):
    out = docker("ps", "-q", "--filter", "status=running",
                 "--filter", f"label=com.docker.compose.project={project}",
                 "--filter", f"label=com.docker.compose.service={service}")
    cid = out.stdout.split()[:1]
    if not cid:
        return None
    return docker("inspect", cid[0], "--format", "{{.Image}}").stdout.strip() or None


def local_image(ref):
    out = docker("image", "inspect", ref, "--format", "{{.Id}}")
    return out.stdout.strip() if out.returncode == 0 else None


def snapshot(compose_config):
    """{"<projet>/<service>": {"image", "id", "version"}} pour chaque service
    de la stack. Image du conteneur qui tourne s'il y en a un (c'est elle qui
    a servi), sinon l'image locale (service lancé à la demande)."""
    project = compose_config["name"]
    result = {}
    for service, spec in compose_config.get("services", {}).items():
        key = f"{project}/{service}"
        # Service à `build:` sans `image:` : compose le nomme <projet>-<service>.
        ref = spec.get("image") or f"{project}-{service}"
        image_id = running_image(project, service) or local_image(ref)
        how = SOURCES.get(key, {}).get("version", OCI_VERSION)
        raw = read_version(image_id, how) if image_id else None
        result[key] = {"image": ref, "id": image_id, "version": normalize(raw)}
    return result


def baseline_path(stack):
    return os.path.join(JOURNAL_DIR, f"baseline-{stack}.json")


def cmd_snapshot(stack):
    path = baseline_path(stack)
    if os.path.exists(path):
        print(f"image-versions: photo d'avant d'un update précédent inachevé gardée ({path})")
        return
    # Calculée avant d'ouvrir le fichier, écrite atomiquement : une photo vide
    # (compose config en échec) serait sinon gardée par tous les updates suivants.
    data = snapshot(json.load(sys.stdin))
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)


def changed(before, after):
    if not after.get("id") or before.get("id") == after.get("id"):
        return False
    # Version connue des deux côtés : seule elle compte (un rebuild de la même
    # version, ou une modif de code de clearr, n'a pas de changelog).
    if before.get("version") and after.get("version"):
        return before["version"] != after["version"]
    return True


def append_journal(entries):
    with open(JOURNAL, "a") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def cmd_record(stack):
    path = baseline_path(stack)
    try:
        with open(path) as f:
            before = json.load(f)
    except FileNotFoundError:
        print(f"image-versions: pas de photo d'avant pour {stack}, rien à comparer", file=sys.stderr)
        return
    except json.JSONDecodeError:
        os.remove(path)
        print(f"image-versions: photo d'avant illisible pour {stack}, jetée", file=sys.stderr)
        return
    after = snapshot(json.load(sys.stdin))
    now = datetime.datetime.now().astimezone()
    entries = []
    for key, new in after.items():
        old = before.get(key, {})
        if not changed(old, new):
            continue
        src = SOURCES.get(key, {})
        entries.append({
            "id": f"{now:%Y%m%dT%H%M%S}-{key.replace('/', '-')}",
            "date": now.isoformat(timespec="seconds"),
            "epoch": int(now.timestamp()),
            "service": key,
            "image": new["image"],
            "from": old.get("version"),
            "to": new.get("version"),
            "from_id": old.get("id"),
            "to_id": new.get("id"),
            "repo": src.get("repo"),
            "notes": src.get("notes"),
            "tier": src.get("tier", "infra"),
            "processed": None,
        })
        print(f"  ↑ {key} : {old.get('version') or '?'} → {new.get('version') or '?'}")
    if entries:
        append_journal(entries)
    os.remove(path)


def read_journal():
    try:
        with open(JOURNAL) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def cmd_recap(since):
    entries = [e for e in read_journal() if e["epoch"] >= since]
    if not entries:
        print("aucune nouvelle version")
        return
    print("nouvelles versions (`/changelogs` dans Claude Code pour le détail) :")
    for e in entries:
        print(f"  {e['service']:<24} {e['from'] or '?'} → {e['to'] or '?'}")


def cmd_pending():
    print(json.dumps([e for e in read_journal() if not e["processed"]], ensure_ascii=False, indent=1))


def cmd_mark(ids, mark_all):
    entries = read_journal()
    today = datetime.date.today().isoformat()
    marked = 0
    for e in entries:
        if not e["processed"] and (mark_all or e["id"] in ids):
            e["processed"] = today
            marked += 1
    # Réécriture atomique : un journal tronqué perdrait tout l'historique.
    tmp = JOURNAL + ".tmp"
    with open(tmp, "w") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, JOURNAL)
    print(f"{marked} entrée(s) marquée(s) traitée(s)")


def main():
    args = sys.argv[1:]
    if not JOURNAL_DIR:
        print("image-versions: DATA_ROOT introuvable dans .env.shared", file=sys.stderr)
        return
    cmd = args[0] if args else ""
    if cmd == "snapshot" and len(args) == 2:
        cmd_snapshot(args[1])
    elif cmd == "record" and len(args) == 2:
        cmd_record(args[1])
    elif cmd == "recap" and len(args) == 3 and args[1] == "--since":
        cmd_recap(int(args[2]))
    elif cmd == "pending":
        cmd_pending()
    elif cmd == "mark" and len(args) > 1:
        cmd_mark(set(args[1:]), "--all" in args)
    else:
        print(__doc__ or "usage: voir l'en-tête de scripts/image-versions.py", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # best-effort : ne jamais faire échouer make update
        print(f"image-versions: {exc!r} (journal des versions non mis à jour)", file=sys.stderr)
