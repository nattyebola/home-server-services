#!/usr/bin/env python3
# Automatise la configuration d'installation qui se faisait auparavant à la main
# dans les UI (étapes 9c/12a/12d/12e/12f/12g/12j/14c/14d du README). Deux
# sous-commandes, appelées par deux targets Makefile :
#
#   keys      (make api-keys)  collecte les secrets générés au 1er démarrage et
#                              les écrit dans arr/.env : clés API Prowlarr/
#                              Sonarr/Radarr (dans leur config.xml), clé
#                              cross-seed, clé API Jellyfin (créée au besoin).
#   services  (make provision) crée les objets : bibliothèques Jellyfin, apps
#                              Prowlarr, client de téléchargement, root folders
#                              et Connection cross-seed côté Sonarr/Radarr,
#                              puis la configuration complète de Seerr.
#
# Pourquoi deux commandes et pas une : `keys` doit tourner AVANT
# `recyclarr-sync`/`arr-overrides` (qui ont besoin des clés), alors que la
# configuration Seerr de `services` doit tourner APRÈS (elle référence par nom
# les profils qualité que ces deux-là créent). Une seule commande aurait dû être
# lancée deux fois de part et d'autre ; deux commandes rendent l'ordre explicite
# dans le README.
#
# CRÉE SI ABSENT, ne réécrit jamais ce qui existe — contrairement à
# apply-arr-overrides.py, qui est déclaratif et fait autorité sur ses objets.
# La distinction est volontaire : ici on provisionne des objets d'infrastructure
# que l'utilisateur peut légitimement ajuster ensuite dans les UI (catégorie du
# client de téléchargement, bibliothèque Jellyfin supplémentaire...) et qu'un
# script lancé par cron n'a pas à ramener de force à un état théorique. C'est
# aussi pourquoi ce script n'est PAS dans scripts/crontab : il s'exécute à
# l'installation, et reste relançable sans rien casser.
#
# Transport : `docker exec <container> curl`, comme apply-arr-overrides.py et
# transmission-stats.py — pas d'appel depuis l'hôte vers les URLs publiques, ce
# qui éviterait de dépendre du DNS, des certificats et de l'ipallowlist. Chaque
# arr est interrogé depuis son propre conteneur ; Jellyfin et Seerr le sont
# depuis arr-sonarr-1, seul conteneur à joindre les deux (traefik-public pour
# jellyfin, réseau arr pour seerr) et dont l'image embarque curl — celle de
# seerr ne l'a pas.
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARR_ENV = os.path.join(REPO_ROOT, "arr", ".env")
JELLYFIN_ENV = os.path.join(REPO_ROOT, "jellyfin", ".env")
PROWLARR_INDEXERS = os.path.join(REPO_ROOT, "arr", "profiles", "prowlarr-indexers.json")

# Conteneur servant de client HTTP pour Jellyfin et Seerr (voir en-tête).
PROXY_CONTAINER = "arr-sonarr-1"

# URL par laquelle Sonarr/Radarr joignent Prowlarr en retour : Prowlarr valide
# cette accessibilité bidirectionnelle au moment du POST de l'application
# (repéré en testant contre des conteneurs jetables), les deux services doivent
# donc tourner.
PROWLARR_INTERNAL_URL = "http://prowlarr:9696"

JELLYFIN_URL = "http://jellyfin:8096"
SEERR_URL = "http://seerr:5055"

ARRS = {
    "prowlarr": {"container": "arr-prowlarr-1", "url": "http://localhost:9696", "api": "v1",
                 "key_var": "PROWLARR_API_KEY"},
    "sonarr": {"container": "arr-sonarr-1", "url": "http://localhost:8989", "api": "v3",
               "key_var": "SONARR_API_KEY", "service_url": "http://sonarr:8989"},
    "radarr": {"container": "arr-radarr-1", "url": "http://localhost:7878", "api": "v3",
               "key_var": "RADARR_API_KEY", "service_url": "http://radarr:7878"},
}

# Nom de l'application déclarée côté Jellyfin pour la clé API créée par `keys`.
# Une seule clé sert à tout ce qui parle à Jellyfin (Sonarr/Radarr via leur
# connexion, Seerr, clearr) — voir ARCHITECTURE.md, pas de clé par service.
JELLYFIN_KEY_APP = "server (infra as code)"

# 9c — bibliothèques Jellyfin, une par Root Folder arr (voir ARR_ROOT_FOLDERS).
# Chemins vus par le CONTENEUR Jellyfin (/library), pas par Sonarr/Radarr
# (/data_root/library) : deux montages différents des mêmes fichiers.
JELLYFIN_LIBRARIES = [
    {"name": "Films", "collection_type": "movies", "path": "/library/film"},
    {"name": "Séries", "collection_type": "tvshows", "path": "/library/series"},
    {"name": "Animés", "collection_type": "tvshows", "path": "/library/anime"},
]

# `Nfo` en tête des lecteurs de métadonnées locaux : c'est la moitié aval du
# correctif d'identification du 2026-08-06. apply-arr-overrides.py provisionne
# l'ÉCRITURE des .nfo côté Sonarr/Radarr (XBMC_METADATA_*), mais sans cette
# option Jellyfin peut ne pas les LIRE en priorité et réidentifie alors chaque
# titre par une recherche TMDB sur le nom de dossier — ce qui avait donné
# « Dead Man » identifié comme « Dead Man Walking », et un One Piece de 2023 au
# lieu de 1999. Un titre mal identifié est aussi un titre insupprimable depuis
# Kodi, dont les ids externes ne matchent alors plus rien côté arr.
#
# C'est déjà la valeur observée sur les 6 bibliothèques de ce déploiement, donc
# vraisemblablement le défaut de Jellyfin — mais un défaut amont n'est pas une
# garantie avec des images en :latest, et cette chaîne-là coûte trop cher à
# rediagnostiquer pour dépendre d'un implicite.
JELLYFIN_METADATA_READER_ORDER = ["Nfo"]

# 12f — Root Folders. Le préfixe /data_root est essentiel : Sonarr/Radarr
# montent tout ${DATA_ROOT} en un seul volume, condition du hardlink à l'import
# (voir ARCHITECTURE.md).
ARR_ROOT_FOLDERS = {
    "sonarr": ["/data_root/library/series", "/data_root/library/anime"],
    "radarr": ["/data_root/library/film"],
}

# 12e — Transmission comme client de téléchargement. Joint par son nom de
# service sur vpn-internal ; RPC non authentifié (réseau Docker isolé), d'où
# l'absence d'identifiants. Les catégories donnent les sous-dossiers
# completed/<catégorie> côté Transmission, sur lesquels s'appuie le remote path
# mapping.
ARR_DOWNLOAD_CLIENT = {
    "sonarr": {"tvCategory": "sonarr"},
    "radarr": {"movieCategory": "radarr"},
}

# 12e (suite) — Remote path mapping, la pièce qui fait tenir le fix hardlink du
# 2026-07-23. Transmission annonce ses téléchargements sous /data/completed/
# (son propre montage), Sonarr/Radarr voient les mêmes fichiers sous
# /data_root/.transmission/data/completed/ (le montage unique ${DATA_ROOT},
# condition du hardlink à l'import). Sans ce mapping, les arr reçoivent un
# chemin qui n'existe pas chez eux.
#
# Ajouté ici le 2026-08-09 : il n'était recréé par RIEN, alors que son absence
# est le seul trou de provisioning qui casse le chemin nominal de bout en bout —
# une installation neuve téléchargerait correctement et n'importerait jamais
# rien, stacks vertes et healthchecks au vert, bibliothèque vide. Les trois
# valeurs découlent des montages du dépôt, rien n'y identifie ce déploiement.
# `host` doit correspondre au champ `host` du client de téléchargement
# ci-dessus : c'est par lui que l'arr rattache le mapping au client.
ARR_REMOTE_PATH_MAPPING = {
    "host": "transmission-vpn",
    "remotePath": "/data/completed/",
    "localPath": "/data_root/.transmission/data/completed/",
}

# 12f — tags créés sur les deux arr. Libellé en tirets et pas en underscores :
# Radarr valide `^[a-z0-9-]+$` et refuse un underscore, que Sonarr accepte
# pourtant — le même libellé des deux côtés est nécessaire pour n'avoir qu'un
# seul filtre à écrire en aval.
# `pour-les-enfants` (2026-08-06, demandé) : posé depuis Seerr au moment de la
# requête (override "tags" par requête, colonne `tags` de sa table
# media_request), il ressort dans le `<tag>` du .nfo écrit par l'arr, puis dans
# les Tags de l'item Jellyfin, puis dans la table `tag` de Kodi — voir
# l'entrée metadata writer de CLAUDE.md pour cette chaîne. Un tag arr sert
# aussi de cible à des règles côté arr (release profiles, restrictions), d'où
# sa création ici plutôt que dans les seuls .nfo.
ARR_TAGS = ["pour-les-enfants"]

# 12g — Connection "Custom Script" pour cross-seed. Custom Script et pas
# Webhook : le type Webhook envoie un payload de test factice que cross-seed
# rejette, ce qui empêche d'enregistrer la connexion (voir ISSUES.md). Le chemin
# est celui du montage dans arr/docker-compose.yml.
CROSS_SEED_SCRIPT = "/config/custom-cross-seed-notify.sh"

# 14c — profils qualité et dossiers que Seerr doit utiliser, désignés par NOM :
# les ids sont propres à l'instance. Ces profils sont créés par
# `make recyclarr-sync` + `make arr-overrides`, qui doivent donc tourner avant.
SEERR_RADARR_PROFILE = "[SQP] SQP-1 WEB (2160p)"
SEERR_SONARR_PROFILE = "WEB-2160p (Combined)"
SEERR_SONARR_ANIME_PROFILE = "Anime (Fansub) VOSTFR"


class Skipped(Exception):
    """Prérequis absent (clé non renseignée, service non démarré...) : on le
    signale et on continue, sans faire échouer tout le script."""


def log(message):
    print(message, flush=True)


# --- fichiers .env -----------------------------------------------------------

def read_env(path):
    values = {}
    if not os.path.exists(path):
        return values
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key] = value
    return values


def fill_env(path, key, value):
    """Renseigne `key` seulement si elle est absente ou vide, en préservant
    commentaires et ordre du fichier (édition ligne par ligne, pas de
    réécriture depuis un dict) — ces fichiers sont écrits à la main et leurs
    commentaires expliquent chaque valeur."""
    lines = open(path).read().splitlines(keepends=True) if os.path.exists(path) else []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"# {key}="):
            current = stripped.split("=", 1)[1] if "=" in stripped else ""
            if current and not stripped.startswith("#"):
                return False
            lines[i] = f"{key}={value}\n"
            open(path, "w").writelines(lines)
            return True
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append(f"{key}={value}\n")
    open(path, "w").writelines(lines)
    return True


# --- HTTP via docker exec curl ----------------------------------------------

def request(container, url, method="GET", headers=(), body=None, allow_status=()):
    cmd = ["docker", "exec"]
    if body is not None:
        cmd.append("-i")
    cmd += [container, "curl", "-s", "-w", "\n%{http_code}", "-X", method]
    for header in headers:
        cmd += ["-H", header]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
    cmd.append(url)
    res = subprocess.run(cmd, input=json.dumps(body) if body is not None else None,
                         capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise Skipped(f"docker exec {container} a échoué (conteneur arrêté ?)")
    payload, _, status = res.stdout.rpartition("\n")
    status = status.strip()
    # curl -s sort 0 sur un 4xx/5xx : c'est le code HTTP qui fait foi (même
    # piège que dans apply-arr-overrides.py).
    if not (status.startswith("2") or status in allow_status):
        raise RuntimeError(f"{method} {url} -> HTTP {status} : {payload[:300]}")
    if not payload.strip():
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return payload


def arr_request(name, path, method="GET", body=None, api_key=None):
    spec = ARRS[name]
    url = f"{spec['url']}/api/{spec['api']}{path}"
    return request(spec["container"], url, method,
                   headers=[f"X-Api-Key: {api_key}"], body=body)


# --- keys : 12a + 12j -------------------------------------------------------

def arr_api_key_from_disk(name, data_root):
    """La clé est générée au 1er démarrage et écrite dans config.xml — la lire
    là plutôt que dans l'UI est tout l'intérêt de l'étape."""
    path = os.path.join(data_root, ".arr", name, "config", "config.xml")
    if not os.path.exists(path):
        raise Skipped(f"{path} absent — {name} n'a jamais démarré ?")
    content = open(path).read()
    start = content.find("<ApiKey>")
    end = content.find("</ApiKey>")
    if start < 0 or end < 0:
        raise Skipped(f"pas de <ApiKey> dans {path}")
    return content[start + len("<ApiKey>"):end].strip()


def cross_seed_api_key():
    res = subprocess.run(
        ["docker", "compose", "--env-file", os.path.join(REPO_ROOT, ".env.shared"),
         "--env-file", ARR_ENV, "-f", os.path.join(REPO_ROOT, "arr", "docker-compose.yml"),
         "exec", "-T", "cross-seed", "cross-seed", "api-key"],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=120)
    if res.returncode != 0:
        raise Skipped("`cross-seed api-key` a échoué (service arrêté ?)")
    # La commande logue sa config avant la clé : ne garder que la dernière
    # ligne non vide, qui est la clé elle-même.
    lines = [l.strip() for l in res.stdout.splitlines() if l.strip()]
    if not lines:
        raise Skipped("`cross-seed api-key` n'a rien renvoyé")
    return lines[-1]


def jellyfin_token(admin_user, admin_password):
    """Jellyfin n'accepte pas la création d'une clé API par clé API : il faut un
    token de session obtenu avec les identifiants admin (d'où jellyfin/.env)."""
    auth = ('MediaBrowser Client="server-infra", Device="make", '
            'DeviceId="server-infra-provision", Version="1.0.0"')
    result = request(PROXY_CONTAINER, f"{JELLYFIN_URL}/Users/AuthenticateByName", "POST",
                     headers=[f"Authorization: {auth}"],
                     body={"Username": admin_user, "Pw": admin_password})
    token = (result or {}).get("AccessToken")
    if not token:
        raise Skipped("authentification Jellyfin refusée — vérifier jellyfin/.env")
    return token


def jellyfin_api_key(token):
    """Réutilise la clé déjà créée pour cette app si elle existe (idempotence),
    sinon en crée une. Jellyfin expose les clés en clair sur /Auth/Keys, ce qui
    permet de les relire après création — l'API ne renvoie rien à la création."""
    existing = request(PROXY_CONTAINER, f"{JELLYFIN_URL}/Auth/Keys",
                       headers=[f"X-Emby-Token: {token}"])
    for item in (existing or {}).get("Items", []):
        if item.get("AppName") == JELLYFIN_KEY_APP:
            return item["AccessToken"], False
    request(PROXY_CONTAINER,
            f"{JELLYFIN_URL}/Auth/Keys?App={JELLYFIN_KEY_APP.replace(' ', '%20')}",
            "POST", headers=[f"X-Emby-Token: {token}"])
    after = request(PROXY_CONTAINER, f"{JELLYFIN_URL}/Auth/Keys",
                    headers=[f"X-Emby-Token: {token}"])
    for item in (after or {}).get("Items", []):
        if item.get("AppName") == JELLYFIN_KEY_APP:
            return item["AccessToken"], True
    raise RuntimeError("clé API Jellyfin créée mais introuvable à la relecture")


def command_keys(shared, done, skipped):
    data_root = shared["DATA_ROOT"]
    for name in ("prowlarr", "sonarr", "radarr"):
        var = ARRS[name]["key_var"]
        try:
            if fill_env(ARR_ENV, var, arr_api_key_from_disk(name, data_root)):
                done.append(f"{var} renseignée dans arr/.env")
        except Skipped as e:
            skipped.append(f"{var} : {e}")
    try:
        if fill_env(ARR_ENV, "CROSSSEED_API_KEY", cross_seed_api_key()):
            done.append("CROSSSEED_API_KEY renseignée dans arr/.env")
    except Skipped as e:
        skipped.append(f"CROSSSEED_API_KEY : {e}")

    jellyfin_env = read_env(JELLYFIN_ENV)
    user = jellyfin_env.get("JELLYFIN_ADMIN_USER")
    password = jellyfin_env.get("JELLYFIN_ADMIN_PASSWORD")
    if not user or not password:
        skipped.append("JELLYFIN_API_KEY : JELLYFIN_ADMIN_USER/PASSWORD absents de "
                       "jellyfin/.env (voir .env.example)")
        return
    try:
        key, created = jellyfin_api_key(jellyfin_token(user, password))
    except Skipped as e:
        skipped.append(f"JELLYFIN_API_KEY : {e}")
        return
    if created:
        done.append(f"clé API Jellyfin créée ({JELLYFIN_KEY_APP})")
    if fill_env(ARR_ENV, "JELLYFIN_API_KEY", key):
        done.append("JELLYFIN_API_KEY renseignée dans arr/.env")


# --- services : 9c ----------------------------------------------------------

def provision_jellyfin_libraries(jellyfin_key, done, skipped):
    """Une clé API suffit ici (vérifié le 2026-08-05 en créant puis supprimant une
    bibliothèque jetable) : Jellyfin la traite comme une session élevée pour
    /Library/VirtualFolders. Pas besoin des identifiants admin, contrairement à
    la création d'une clé API elle-même — donc `make provision` ne dépend que de
    arr/.env."""
    existing = request(PROXY_CONTAINER, f"{JELLYFIN_URL}/Library/VirtualFolders",
                       headers=[f"X-Emby-Token: {jellyfin_key}"]) or []
    by_name = {v["Name"]: v for v in existing}
    for library in JELLYFIN_LIBRARIES:
        current = by_name.get(library["name"])
        if current:
            if library["path"] not in current["Locations"]:
                skipped.append(f"bibliothèque Jellyfin {library['name']!r} existe mais ne "
                               f"contient pas {library['path']} — à vérifier à la main")
            continue
        request(PROXY_CONTAINER,
                f"{JELLYFIN_URL}/Library/VirtualFolders"
                f"?name={library['name'].replace(' ', '%20')}"
                f"&collectionType={library['collection_type']}&refreshLibrary=true",
                "POST", headers=[f"X-Emby-Token: {jellyfin_key}"],
                body={"LibraryOptions": {"PathInfos": [{"Path": library["path"]}],
                                          "EnableRealtimeMonitor": True,
                                          "LocalMetadataReaderOrder": JELLYFIN_METADATA_READER_ORDER}})
        done.append(f"bibliothèque Jellyfin {library['name']!r} créée ({library['path']})")


# --- services : 12d / 12e / 12f / 12g --------------------------------------

def field_value(obj, name):
    return next((f.get("value") for f in obj.get("fields", []) if f["name"] == name), None)


def provision_root_folders(name, api_key, data_root, done, skipped):
    existing = {r["path"] for r in arr_request(name, "/rootfolder", api_key=api_key)}
    for path in ARR_ROOT_FOLDERS[name]:
        if path in existing:
            continue
        # Sonarr/Radarr refusent un root folder inexistant : le dossier hôte
        # correspondant doit être créé d'abord (/data_root == ${DATA_ROOT}).
        host_path = os.path.join(data_root, path[len("/data_root/"):])
        os.makedirs(host_path, exist_ok=True)
        arr_request(name, "/rootfolder", "POST", {"path": path}, api_key)
        done.append(f"{name} : root folder {path} ajouté")


def provision_tags(name, api_key, done, skipped):
    existing = {t["label"] for t in arr_request(name, "/tag", api_key=api_key)}
    for label in ARR_TAGS:
        if label in existing:
            continue
        arr_request(name, "/tag", "POST", {"label": label}, api_key)
        done.append(f"{name} : tag {label} ajouté")


def provision_download_client(name, api_key, done, skipped):
    existing = arr_request(name, "/downloadclient", api_key=api_key)
    if any(c["implementation"] == "Transmission" for c in existing):
        return
    fields = {"host": "transmission-vpn", "port": 9091, "useSsl": False,
              "urlBase": "/transmission/", "addPaused": False}
    fields.update(ARR_DOWNLOAD_CLIENT[name])
    arr_request(name, "/downloadclient", "POST", {
        "name": "Transmission", "implementation": "Transmission",
        "configContract": "TransmissionSettings", "protocol": "torrent",
        "enable": True, "priority": 1,
        "removeCompletedDownloads": True, "removeFailedDownloads": True,
        "tags": [],
        "fields": [{"name": k, "value": v} for k, v in fields.items()],
    }, api_key)
    done.append(f"{name} : client de téléchargement Transmission ajouté")


def provision_remote_path_mapping(name, api_key, done, skipped):
    existing = arr_request(name, "/remotepathmapping", api_key=api_key)
    wanted = ARR_REMOTE_PATH_MAPPING
    # Comparaison sur les trois champs et pas seulement sur le host : deux
    # mappings peuvent coexister pour le même client (dossiers différents), et
    # on ne veut recréer que CELUI-ci s'il manque.
    if any(m.get("host") == wanted["host"]
           and m.get("remotePath") == wanted["remotePath"]
           and m.get("localPath") == wanted["localPath"] for m in existing):
        return
    arr_request(name, "/remotepathmapping", "POST", dict(wanted), api_key)
    done.append(f"{name} : remote path mapping {wanted['remotePath']} -> {wanted['localPath']} ajouté")


def provision_cross_seed_script(name, api_key, done, skipped):
    existing = arr_request(name, "/notification", api_key=api_key)
    if any(n["implementation"] == "CustomScript"
           and field_value(n, "path") == CROSS_SEED_SCRIPT for n in existing):
        return
    arr_request(name, "/notification", "POST", {
        "name": "cross-seed", "implementation": "CustomScript",
        "configContract": "CustomScriptSettings",
        "onGrab": False, "onDownload": True, "onUpgrade": True,
        "includeHealthWarnings": False, "tags": [],
        "fields": [{"name": "path", "value": CROSS_SEED_SCRIPT},
                   {"name": "arguments", "value": ""}],
    }, api_key)
    done.append(f"{name} : Connection cross-seed (Custom Script) ajoutée")


def provision_prowlarr_indexers(arr_env, done, skipped):
    """Crée les indexeurs décrits par arr/profiles/prowlarr-indexers.json.

    Ils ne vivaient que dans la base Prowlarr : sur une installation neuve,
    Prowlarr démarrait vide, les deux applications se synchronisaient sans rien
    pousser, Sonarr/Radarr n'avaient aucun indexeur et cross-seed interrogeait
    des ids inexistants — sans une seule erreur bloquante. Même signature que le
    bug Torznab de 2026-07-28, resté silencieux quatre jours.

    Le fichier est gitignoré (il nomme les trackers, cf. son .example) mais
    sauvegardé par scripts/backup.sh. Créé-si-absent comme le reste de ce
    script : un indexeur est un objet que l'utilisateur ajuste ensuite dans
    l'UI, on ne le ramène pas de force à un état théorique.

    Le corps est bâti sur /api/v1/indexer/schema plutôt que sur un dump : les
    ids sont propres à l'instance, et tout champ non listé garde le défaut de la
    définition Cardigann — donc suit les mises à jour amont au lieu d'être figé.
    """
    prowlarr_key = arr_env.get("PROWLARR_API_KEY")
    if not prowlarr_key:
        raise Skipped("PROWLARR_API_KEY absente de arr/.env — `make api-keys` d'abord")
    if not os.path.exists(PROWLARR_INDEXERS):
        raise Skipped(f"{os.path.relpath(PROWLARR_INDEXERS, REPO_ROOT)} absent "
                      "— copier le .example à côté et l'adapter")
    with open(PROWLARR_INDEXERS, encoding="utf-8") as f:
        wanted = json.load(f).get("indexers", [])
    if not wanted:
        raise Skipped("aucun indexeur déclaré dans prowlarr-indexers.json")

    existing = {i["name"] for i in arr_request("prowlarr", "/indexer", api_key=prowlarr_key)}
    schema = None
    for spec in wanted:
        if spec["name"] in existing:
            continue
        if schema is None:  # 621 définitions, ne le charger que si on crée vraiment
            schema = {s["definitionName"]: s
                      for s in arr_request("prowlarr", "/indexer/schema", api_key=prowlarr_key)}
        base = schema.get(spec["definitionName"])
        if base is None:
            raise RuntimeError(f"définition Cardigann {spec['definitionName']!r} inconnue de "
                               "Prowlarr — vérifier definitionName dans prowlarr-indexers.json")
        body = dict(base)
        body.update({"name": spec["name"], "enable": True,
                     "priority": spec.get("priority", 25), "tags": []})
        body["fields"] = [dict(f) for f in base.get("fields", [])]

        values = dict(spec.get("fields", {}))
        if spec.get("baseUrl"):
            values["baseUrl"] = spec["baseUrl"]
        for field, var in spec.get("secrets", {}).items():
            secret = arr_env.get(var)
            if not secret:
                # Sans le secret l'indexeur serait créé mais ne répondrait à
                # aucune recherche : mieux vaut ne pas le créer du tout et le
                # dire, plutôt que de laisser un objet inerte dans Prowlarr.
                raise Skipped(f"{spec['name']} : {var} absente de arr/.env")
            values[field] = secret
        for field in body["fields"]:
            if field["name"] in values:
                field["value"] = values.pop(field["name"])
        if values:
            raise RuntimeError(f"{spec['name']} : champ(s) {sorted(values)} absent(s) du schéma "
                               f"de {spec['definitionName']!r}")

        arr_request("prowlarr", "/indexer", "POST", body, prowlarr_key)
        done.append(f"prowlarr : indexeur {spec['name']} ajouté")


def provision_prowlarr_apps(arr_env, done, skipped):
    prowlarr_key = arr_env.get("PROWLARR_API_KEY")
    existing = arr_request("prowlarr", "/applications", api_key=prowlarr_key)
    have = {a["implementation"] for a in existing}
    # syncCategories : les catégories Newznab/Torznab que Prowlarr synchronise
    # vers chaque app. Reprises des valeurs par défaut de Prowlarr pour chaque
    # implémentation (films 2xxx, séries 5xxx, anime 5070 à part côté Sonarr).
    apps = {
        "Radarr": {"contract": "RadarrSettings", "target": "radarr",
                   "fields": {"syncCategories": [2000, 2010, 2020, 2030, 2040, 2045,
                                                  2050, 2060, 2070, 2080, 2090]}},
        "Sonarr": {"contract": "SonarrSettings", "target": "sonarr",
                   "fields": {"syncCategories": [5000, 5010, 5020, 5030, 5040, 5045,
                                                  5050, 5090],
                               "animeSyncCategories": [5070],
                               "syncAnimeStandardFormatSearch": True}},
    }
    for implementation, spec in apps.items():
        if implementation in have:
            continue
        target_key = arr_env.get(ARRS[spec["target"]]["key_var"])
        if not target_key:
            skipped.append(f"app Prowlarr {implementation} : "
                           f"{ARRS[spec['target']]['key_var']} absente de arr/.env")
            continue
        fields = {"prowlarrUrl": PROWLARR_INTERNAL_URL,
                   "baseUrl": ARRS[spec["target"]]["service_url"],
                   "apiKey": target_key}
        fields.update(spec["fields"])
        arr_request("prowlarr", "/applications", "POST", {
            "name": implementation, "implementation": implementation,
            "configContract": spec["contract"], "syncLevel": "fullSync",
            "enable": True, "tags": [],
            "fields": [{"name": k, "value": v} for k, v in fields.items()],
        }, prowlarr_key)
        done.append(f"Prowlarr : application {implementation} connectée")


# --- services : 14c / 14d --------------------------------------------------

def seerr_request(path, method="GET", body=None, api_key=None, allow_status=()):
    return request(PROXY_CONTAINER, f"{SEERR_URL}/api/v1{path}", method,
                   headers=[f"X-Api-Key: {api_key}"], body=body,
                   allow_status=allow_status)


def seerr_settings_path(data_root):
    return os.path.join(data_root, ".seerr", "config", "settings.json")


def arr_profile_id(name, api_key, profile_name):
    profiles = arr_request(name, "/qualityprofile", api_key=api_key)
    match = next((p for p in profiles if p["name"] == profile_name), None)
    if match is None:
        raise Skipped(f"profil {profile_name!r} absent de {name} — "
                      "`make recyclarr-sync` puis `make arr-overrides` d'abord")
    return match["id"]


def provision_seerr(shared, arr_env, done, skipped):
    settings_file = seerr_settings_path(shared["DATA_ROOT"])
    if not os.path.exists(settings_file):
        raise Skipped("settings.json de Seerr absent — `make up STACK=seerr` d'abord")
    settings = json.load(open(settings_file))
    seerr_key = settings.get("main", {}).get("apiKey")
    if not seerr_key:
        raise Skipped("pas de main.apiKey dans le settings.json de Seerr")

    jellyfin_env = read_env(JELLYFIN_ENV)
    admin_user = jellyfin_env.get("JELLYFIN_ADMIN_USER")
    admin_password = jellyfin_env.get("JELLYFIN_ADMIN_PASSWORD")
    jellyfin_key = arr_env.get("JELLYFIN_API_KEY")
    initialized = bool(settings.get("public", {}).get("initialized"))
    changed = False

    # Les identifiants admin ne servent qu'à créer le compte propriétaire, donc
    # seulement sur une instance jamais initialisée : ne pas les exiger sinon,
    # sous peine de bloquer les vérifications additives ci-dessous sur une
    # installation en service.
    if not initialized and not (admin_user and admin_password):
        raise Skipped("JELLYFIN_ADMIN_USER/PASSWORD absents de jellyfin/.env — "
                      "nécessaires pour créer le compte propriétaire")
    if not jellyfin_key:
        raise Skipped("JELLYFIN_API_KEY absente de arr/.env — `make api-keys` d'abord")

    if not initialized:
        # Le compte propriétaire de Seerr est créé depuis un compte Jellyfin
        # existant : c'est ce POST qui l'importe, et il n'exige pas d'être
        # authentifié tant que l'instance n'est pas initialisée.
        seerr_request("/auth/jellyfin", "POST", {
            "username": admin_user, "password": admin_password,
            "hostname": "jellyfin", "port": 8096, "useSsl": False, "urlBase": "",
            "email": f"{admin_user}@localhost", "serverType": 2,
        }, seerr_key)
        done.append("Seerr : compte propriétaire créé depuis le compte Jellyfin")
        changed = True

    # Strictement additif : une connexion Jellyfin déjà configurée n'est pas
    # réécrite. Sans cette garde, relancer le script sur une installation en
    # service remplacerait son adresse (souvent l'URL publique, réglée à la main
    # dans l'assistant) par le nom de service interne — un changement non
    # demandé sur une config qui marche.
    if not settings.get("jellyfin", {}).get("apiKey"):
        seerr_request("/settings/jellyfin", "POST", {
            "ip": "jellyfin", "port": 8096, "useSsl": False, "urlBase": "",
            "apiKey": jellyfin_key,
        }, seerr_key)
        done.append("Seerr : connexion Jellyfin configurée")
        changed = True

    if not settings.get("jellyfin", {}).get("libraries"):
        # sync=true force Seerr à réinterroger Jellyfin plutôt que de rendre son
        # cache : sur une installation neuve il n'a encore jamais vu les
        # bibliothèques créées juste avant.
        libraries = seerr_request("/settings/jellyfin/library?sync=true",
                                   api_key=seerr_key) or []
        wanted = {library["name"] for library in JELLYFIN_LIBRARIES}
        enable = [library["id"] for library in libraries if library["name"] in wanted]
        if not enable:
            skipped.append("Seerr : aucune des bibliothèques attendues n'est visible côté "
                           "Jellyfin — bibliothèques créées ?")
        else:
            seerr_request(f"/settings/jellyfin/library?enable={','.join(enable)}",
                          api_key=seerr_key)
            done.append(f"Seerr : {len(enable)} bibliothèque(s) Jellyfin activée(s)")
            changed = True

    for name, payload in (
        ("radarr", {
            "name": "radarr", "hostname": "radarr", "port": 7878,
            "apiKey": arr_env.get("RADARR_API_KEY"), "useSsl": False, "baseUrl": "",
            "activeProfileName": SEERR_RADARR_PROFILE,
            "activeDirectory": ARR_ROOT_FOLDERS["radarr"][0],
            "is4k": False, "minimumAvailability": "released", "tags": [],
            "isDefault": True, "syncEnabled": True, "preventSearch": False,
            "tagRequests": False,
        }),
        ("sonarr", {
            "name": "sonarr", "hostname": "sonarr", "port": 8989,
            "apiKey": arr_env.get("SONARR_API_KEY"), "useSsl": False, "baseUrl": "",
            "activeProfileName": SEERR_SONARR_PROFILE,
            "activeDirectory": ARR_ROOT_FOLDERS["sonarr"][0],
            "activeAnimeProfileName": SEERR_SONARR_ANIME_PROFILE,
            "activeAnimeDirectory": ARR_ROOT_FOLDERS["sonarr"][1],
            "animeSeriesType": "anime", "enableSeasonFolders": True,
            "is4k": False, "isDefault": True, "syncEnabled": True,
            "preventSearch": False, "tagRequests": False, "monitorNewItems": "all",
            "tags": [], "animeTags": [],
        }),
    ):
        existing = seerr_request(f"/settings/{name}", api_key=seerr_key) or []
        if any(server.get("hostname") == payload["hostname"] for server in existing):
            continue
        key_var = ARRS[name]["key_var"]
        if not payload["apiKey"]:
            skipped.append(f"Seerr : {key_var} absente de arr/.env")
            continue
        payload["activeProfileId"] = arr_profile_id(name, payload["apiKey"],
                                                    payload["activeProfileName"])
        if name == "sonarr":
            payload["activeAnimeProfileId"] = arr_profile_id(
                name, payload["apiKey"], payload["activeAnimeProfileName"])
        seerr_request(f"/settings/{name}", "POST", payload, seerr_key)
        done.append(f"Seerr : {name} connecté ({payload['activeProfileName']})")
        changed = True

    if not initialized:
        seerr_request("/settings/initialize", "POST", {}, seerr_key)
        done.append("Seerr : installation marquée comme terminée")

    # 14d — sans ce scan, Seerr ignore ce qui est déjà téléchargé et le propose
    # à tort en requête ; son cron interne ne passerait que bien plus tard. Ne
    # se déclenche que si quelque chose a été configuré ci-dessus : relancer le
    # script sur une installation en service n'a pas à relancer un scan complet.
    if changed:
        seerr_request("/settings/jobs/jellyfin-full-scan/run", "POST", api_key=seerr_key)
        done.append("Seerr : job « Jellyfin Full Library Scan » lancé")


# --- points d'entrée -------------------------------------------------------

def run_step(label, function, done, skipped, errors, *args):
    """Best-effort par objet, comme apply-arr-overrides.py l'est par domaine : un
    échec isolé ne doit pas emporter le reste du provisioning. Le cas concret qui
    l'a imposé (repéré en testant contre un Sonarr jetable) : Sonarr VALIDE le
    client de téléchargement en s'y connectant au moment du POST, donc un
    `vpn/transmission-vpn` arrêté fait échouer cet objet-là — sans cette
    isolation, root folders, Connections, apps Prowlarr et Seerr sautaient avec
    lui."""
    try:
        function(*args, done, skipped)
    except Skipped as e:
        skipped.append(f"{label} : {e}")
    except Exception as e:
        errors.append(f"{label} : {e}")


def command_services(shared, done, skipped, errors):
    arr_env = read_env(ARR_ENV)

    def jellyfin_libraries(done, skipped):
        jellyfin_key = arr_env.get("JELLYFIN_API_KEY")
        if not jellyfin_key:
            raise Skipped("JELLYFIN_API_KEY absente de arr/.env — `make api-keys` d'abord")
        provision_jellyfin_libraries(jellyfin_key, done, skipped)

    run_step("bibliothèques Jellyfin", jellyfin_libraries, done, skipped, errors)

    for name in ("sonarr", "radarr"):
        api_key = arr_env.get(ARRS[name]["key_var"])
        if not api_key:
            skipped.append(f"{name} : {ARRS[name]['key_var']} absente de arr/.env "
                           "— `make api-keys` d'abord")
            continue
        run_step(f"{name} root folders", provision_root_folders, done, skipped, errors,
                 name, api_key, shared["DATA_ROOT"])
        run_step(f"{name} tags", provision_tags, done, skipped, errors, name, api_key)
        run_step(f"{name} client de téléchargement", provision_download_client,
                 done, skipped, errors, name, api_key)
        run_step(f"{name} remote path mapping", provision_remote_path_mapping,
                 done, skipped, errors, name, api_key)
        run_step(f"{name} Connection cross-seed", provision_cross_seed_script,
                 done, skipped, errors, name, api_key)

    def prowlarr_apps(done, skipped):
        if not arr_env.get("PROWLARR_API_KEY"):
            raise Skipped("PROWLARR_API_KEY absente de arr/.env — `make api-keys` d'abord")
        provision_prowlarr_apps(arr_env, done, skipped)

    # Indexeurs AVANT les applications : une application déclenche un sync vers
    # Sonarr/Radarr dès sa création, autant qu'elle ait quelque chose à pousser.
    run_step("indexeurs Prowlarr", provision_prowlarr_indexers, done, skipped, errors, arr_env)
    run_step("applications Prowlarr", prowlarr_apps, done, skipped, errors)
    run_step("Seerr", provision_seerr, done, skipped, errors, shared, arr_env)


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command not in ("keys", "services"):
        print("usage: provision.py {keys|services}", file=sys.stderr)
        return 2
    shared = read_env(os.path.join(REPO_ROOT, ".env.shared"))
    if not shared.get("DATA_ROOT"):
        print("DATA_ROOT absent de .env.shared", file=sys.stderr)
        return 1

    done, skipped, errors = [], [], []
    try:
        if command == "keys":
            command_keys(shared, done, skipped)
        else:
            command_services(shared, done, skipped, errors)
    except Exception as e:
        print(f"erreur: {e}", file=sys.stderr)
        return 1
    for line in done:
        log(f"fait: {line}")
    if not done and not errors:
        log("déjà provisionné, rien à faire")
    for line in skipped:
        log(f"ignoré: {line}")
    for line in errors:
        print(f"erreur: {line}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
