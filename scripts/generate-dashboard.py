#!/usr/bin/env python3
# Régénère dashboard/html/index.html à partir des labels Traefik réels des
# stacks (docker compose config --format json) et de l'état d'exécution
# courant (docker ps) — voir CLAUDE.md. Rien n'est codé en dur côté domaine :
# si Host(), le middleware ipallowlist ou l'état running/stopped changent,
# `make dashboard-refresh` reflète l'état réel sans retoucher ce script.
#
# Réécrit en Python le 2026-07-28 (venait d'un script bash+jq qui construisait
# le HTML par concaténation de chaînes — devenu illisible avec l'ajout des
# mètres) : les vues vivent dans dashboard/templates/*.html (string.Template,
# stdlib, substitution seule — les boucles/conditions restent en Python, les
# templates n'ont aucune logique) et dashboard/assets/dashboard.{css,js}, ce
# script ne fait plus que rassembler des données et les passer aux templates.
# Zéro nouvelle dépendance (python3 déjà utilisé par transmission-stats.py et
# apply-arr-overrides.py) — et ça fait sortir jq du chemin de génération.
#
# Seule metadata non dérivable des compose files : le nom affiché et le logo
# (dashboard/assets/logos/) associés à chaque "stack/service" — ajoutés à la
# main ci-dessous quand un nouveau service exposé via Traefik apparaît.
import html
import json
import math
import os
import re
import shlex
import shutil
import string
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = REPO_ROOT / "dashboard"
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
ASSETS_SRC = DASHBOARD_DIR / "assets"
OUT_DIR = DASHBOARD_DIR / "html"

STACKS = ["jellyfin", "nextcloud", "vpn", "arr", "seerr", "komga"]

# nom affiché + logo (dashboard/assets/logos/*.svg) par "stack/service"
DISPLAY_NAME = {
    "jellyfin/jellyfin": "Jellyfin",
    "nextcloud/web": "Nextcloud",
    "vpn/transmission-proxy": "Transmission",
    "arr/prowlarr": "Prowlarr",
    "arr/sonarr": "Sonarr",
    "arr/radarr": "Radarr",
    "arr/clearr": "clearr",
    "seerr/seerr": "Seerr",
    "komga/komga": "Komga",
}
LOGO_FILE = {
    "jellyfin/jellyfin": "jellyfin.svg",
    "nextcloud/web": "nextcloud.svg",
    "vpn/transmission-proxy": "transmission.svg",
    "arr/prowlarr": "prowlarr.svg",
    "arr/sonarr": "sonarr.svg",
    "arr/radarr": "radarr.svg",
    "arr/clearr": "clearr.png",
    "seerr/seerr": "seerr.svg",
    "komga/komga": "komga.svg",
}
# chemin d'une image réelle (200, Content-Type image/*) servie par chaque
# service LAN-only, utilisée par dashboard.js pour détecter un blocage
# ipallowlist (403) côté WAN — voir render_card(). /favicon.ico par défaut ;
# transmission-proxy redirige /favicon.ico vers /transmission/web/ (du HTML,
# pas une image), d'où l'override ci-dessous (vérifié le 2026-07-24). Même
# piège sur clearr (repéré le 2026-08-01) : son appli FastAPI n'a jamais eu
# de route /favicon.ico du tout (404) — la sonde échouait donc aussi bien en
# LAN qu'en WAN, grisant la carte pour tout le monde. Pointé vers l'asset
# statique servi par clearr lui-même plutôt que d'ajouter une route dédiée.
# Les trois Servarr ont grisé de la même façon le 2026-09-17, sans que leur
# /favicon.ico soit en cause : ils s'étaient mis à rediriger toute requête vers
# /login parce que `disabledForLocalAddresses` ne reconnaît plus une IP LAN
# relayée par Traefik. La sonde n'était que le symptôme visible — corrigé côté
# arr (`trustedNetworks`, voir scripts/apply-arr-overrides.py), pas ici. Le
# réflexe à garder : une carte grisée alors que le conteneur est `healthy` peut
# venir de l'authentification du service, pas du chemin sondé ; vérifier d'abord
# `curl -o /dev/null -w '%{http_code} %{content_type}'` sur l'URL data-probe.
# Depuis le 2026-09-29 les arr exigent un login MÊME depuis le LAN
# (`authenticationRequired: enabled`, voir scripts/apply-arr-overrides.py) :
# leur /favicon.ico redirige donc vers /login pour un navigateur sans session.
# /Content/ est servi sans authentification (c'est ce que charge la page de
# login elle-même) — 200 image/png vérifié sur les trois.
SERVARR_PROBE = "/Content/Images/Icons/favicon-32x32.png"
PROBE_PATH = {
    "vpn/transmission-proxy": "/transmission/web/images/favicon.ico",
    "arr/clearr": "/static/favicon.png",
    "arr/prowlarr": SERVARR_PROBE,
    "arr/sonarr": SERVARR_PROBE,
    "arr/radarr": SERVARR_PROBE,
}

RULE_KEY_RE = re.compile(r"^traefik\.http\.routers\.([^.]+)\.rule$")
HOST_RE = re.compile(r"Host\(`([^`]+)`\)")

# Configuration dynamique de Traefik où vivent les middlewares LAN-only, et de
# quoi y relever leur nom — voir lan_middleware_names().
LAN_DYNAMIC_FILE = REPO_ROOT / "traefik" / "dynamic" / "lan-only.yml"
LAN_MIDDLEWARE_RE = re.compile(r"^ {4}([A-Za-z0-9_-]+):[ \t]*\n {6}ipAllowList:", re.MULTILINE)

PROWLARR_CONTAINER = "arr-prowlarr-1"
PROWLARR_URL = "http://localhost:9696/api/v1"

# Files Sonarr/Radarr interrogées pour les compteurs d'imports coincés de la
# carte Torrents (voir arr_stuck_imports()) : "stack/service" (pour le test
# d'exécution), conteneur, URL interne, nom de la clé dans arr/.env, et le
# paramètre « inclure les entrées orphelines » propre à chaque arr.
#
# Ce dernier n'est PAS optionnel et son nom diffère d'un arr à l'autre : sans
# lui, `GET /queue` masque toute entrée dont le titre n'est (plus) au catalogue
# — typiquement un download que l'arr voit chez le client alors que la série ou
# le film a été retiré. Ce sont justement des imports qui ne se débloqueront
# jamais seuls, donc exactement ce que cette carte existe pour montrer. Vécu le
# 2026-09-09 : 5 entrées `importBlocked` invisibles ici, la carte affichant un
# 0 vert rassurant. Le garder aligné avec stuck_queue_records() de
# scripts/manual-import.py, qui a le même besoin.
ARR_QUEUE_APPS = (
    ("arr/sonarr", "arr-sonarr-1", "http://localhost:8989/api/v3", "SONARR_API_KEY",
     "includeUnknownSeriesItems=true"),
    ("arr/radarr", "arr-radarr-1", "http://localhost:7878/api/v3", "RADARR_API_KEY",
     "includeUnknownMovieItems=true"),
)


def human_size(nbytes):
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if abs(nbytes) < 1024 or unit == "To":
            return f"{nbytes:.1f}{unit}" if unit != "o" else f"{int(nbytes)}{unit}"
        nbytes /= 1024


def human_size_rounded(nbytes):
    """Même échelle que human_size() mais sans décimale — valeur plus courte
    pour la carte disque condensée (voir render_disk_row), où le texte sous
    la jauge précise déjà "restant" pour lever l'ambiguïté (libre, pas
    total/utilisé)."""
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if abs(nbytes) < 1024 or unit == "To":
            return f"{round(nbytes)}{unit}"
        nbytes /= 1024


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


def render(template_name, **kwargs):
    path = TEMPLATES_DIR / template_name
    return string.Template(path.read_text()).substitute(**kwargs)


def render_lan_only_banner(data_root):
    """Bandeau affiché tant que `make switch-lan-only-middleware` a ouvert les
    services LAN-only au WAN. C'est la seule trace visible de cet état : les
    cartes de service, elles, sont grisées côté client par une sonde `<img>`
    (voir PROBE_PATH), qui verrait justement ces services répondre — donc un
    visiteur WAN ne pourrait pas distinguer « ouvert exprès » de « toujours
    restreint ». L'échéance est affichée parce que l'oubli est le vrai risque
    de cette bascule.

    Lit le même fichier d'état que scripts/lan-only-middleware.sh, sans jamais
    l'écrire : absent (ou DATA_ROOT inconnu) = fermé, le cas normal."""
    if not data_root:
        return ""
    try:
        deadline = int(Path(data_root, ".lan-only-open-until").read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return ""
    return render("lan-only-banner.html",
                  until=datetime.fromtimestamp(deadline).strftime("%H:%M"))


# Les conteneurs one-off (`docker compose run`, comme l'était l'ancienne TUI
# clearr, retirée le 2026-10-08) portent les mêmes labels project/service que
# le service lui-même mais héritent aussi de son healthcheck, qu'ils ne
# peuvent pas satisfaire (aucun serveur HTTP sur :8000). Sans ce filtre, un
# one-off faisait passer la carte du service web en "healthcheck en échec" —
# et un one-off d'un service arrêté l'aurait fait passer pour démarré.
def docker_ps_set(extra_filters=()):
    args = ["docker", "ps", "--filter", "status=running",
            "--filter", "label=com.docker.compose.oneoff=False"]
    for f in extra_filters:
        args += ["--filter", f]
    args += ["--format", '{{.Label "com.docker.compose.project"}}/{{.Label "com.docker.compose.service"}}']
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("`docker ps` ne répond pas — dashboard non régénéré") from e
    # Échouer bruyamment plutôt que rendre un ensemble vide : sans ce test, un
    # daemon Docker injoignable faisait basculer TOUS les services en « Stack non
    # lancée » et toutes les cartes en placeholder « Arrêté ». La page produite
    # aurait été un rapport de panne générale parfaitement crédible, alors que
    # seule la commande d'inspection avait échoué.
    if res.returncode != 0:
        raise RuntimeError(f"`docker ps` a échoué ({res.returncode}) : "
                           f"{res.stderr.strip()[:200]} — dashboard non régénéré")
    return {line for line in res.stdout.splitlines() if line}


def docker_compose_config(stack):
    args = ["docker", "compose", "--env-file", str(REPO_ROOT / ".env.shared")]
    stack_env = REPO_ROOT / stack / ".env"
    if stack_env.exists():
        args += ["--env-file", str(stack_env)]
    args += ["-f", str(REPO_ROOT / stack / "docker-compose.yml")]
    override = REPO_ROOT / stack / "docker-compose.override.yml"
    if override.exists():
        args += ["-f", str(override)]
    args += ["config", "--format", "json"]
    if not stack_env.exists() and (REPO_ROOT / stack / ".env.example").exists():
        # Stack jamais configurée sur cet hôte : son `env_file: .env` manquant
        # fait échouer `config` (nextcloud, 2026-10-08), donc le dashboard
        # entier. `--no-interpolate` ne lit pas les env_file et suffit à lister
        # ses services pour « Stack non lancée » ; les `${VAR}` restent
        # littérales, sans effet sur une carte d'arrêt (seul le nom compte).
        args.append("--no-interpolate")
    # Échec bruyant, même raisonnement que docker_ps_set() : renvoyer `{}` ici
    # faisait disparaître TOUTE la stack de la page, y compris de « Stack non
    # lancée » (on ne sait plus quels services elle déclare), avec exit 0 et
    # marqueur cron vert — une page crédible et fausse. Planter laisse en place
    # le dernier index.html valide, que le surlignage « page périmée » de
    # dashboard.js signale, et fait passer la tâche au rouge. Une stack non
    # configurée (sans .env) passe par `--no-interpolate`, voir plus haut.
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"`docker compose config` ({stack}) ne répond pas "
                           "— dashboard non régénéré") from e
    if res.returncode != 0:
        raise RuntimeError(f"`docker compose config` ({stack}) a échoué ({res.returncode}) : "
                           f"{res.stderr.strip()[:200]} — dashboard non régénéré")
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"`docker compose config` ({stack}) : JSON illisible "
                           "— dashboard non régénéré") from e


def prowlarr_indexer_health():
    """Liste [{"name":.., "ok": bool}, ...] via l'API Prowlarr — docker exec
    vers son réseau interne, même mécanisme que build_prowlarr_tracker_map()
    dans transmission-stats.py (dupliqué plutôt que partagé, ce dernier est
    un script autonome, pas une lib — même rationale que
    resolve_tracker_name, voir CLAUDE.md). /api/v1/indexerstatus liste les
    indexeurs actuellement désactivés après échecs répétés (vide si tout va
    bien) ; un indexeur présent dans cette liste (par indexerId) compte
    comme en échec. Best-effort : None si clé API absente ou API
    injoignable, pour omettre la carte plutôt que planter la génération du
    dashboard. Triée par nom pour un ordre stable d'une régénération à
    l'autre (pas par état — la couleur suffit à repérer celui en échec)."""
    api_key = load_env_file(REPO_ROOT / "arr" / ".env").get("PROWLARR_API_KEY")
    if not api_key:
        return None
    try:
        # Clé passée par stdin, jamais en argument : l'argv d'un `docker exec`
        # est lisible dans `ps` par tout processus local, et ce script tourne
        # toutes les 5 min par cron.
        def prowlarr_get(path):
            script = ('IFS= read -r k; exec curl -s -H "X-Api-Key: $k" '
                      + shlex.quote(f"{PROWLARR_URL}{path}"))
            return subprocess.run(
                ["docker", "exec", "-i", PROWLARR_CONTAINER, "sh", "-c", script],
                input=api_key, capture_output=True, text=True, timeout=15,
            )

        indexers_res = prowlarr_get("/indexer")
        status_res = prowlarr_get("/indexerstatus")
        indexers = json.loads(indexers_res.stdout)
        failing = json.loads(status_res.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    # Une erreur Servarr (401, 500…) arrive en objet JSON `{"message": …}`, qui
    # passe json.loads : sans ce test, `f.get` sur les clés d'un dict (des str)
    # levait AttributeError et plantait toute la régénération, pas seulement
    # cette carte. Même traitement que les autres échecs : carte omise.
    if not isinstance(indexers, list) or not isinstance(failing, list):
        return None
    failing_ids = {f.get("indexerId") for f in failing if isinstance(f, dict)}
    return sorted(
        ({"name": i.get("name", "?"), "ok": i.get("id") not in failing_ids} for i in indexers),
        key=lambda e: e["name"].lower(),
    )


def arr_stuck_imports(running):
    """Téléchargements Sonarr+Radarr finis mais non importés :
    {"importBlocked": n, "importPending": n}, tous arr confondus. None si le
    compte ne peut pas être établi COMPLÈTEMENT.

    Compté par downloadId distinct, pas par entrée de file : Sonarr crée une
    entrée PAR ÉPISODE d'un même téléchargement, un pack de 12 épisodes
    affichait donc « 12 bloqués » pour une seule chose à débloquer — et
    `manual-import.py`, qui regroupe par downloadId, n'en listait qu'une.

    Tout ou rien, contrairement au best-effort par carte du reste du fichier :
    avec un arr injoignable on afficherait le compte de l'autre seul, donc un
    « 0 » là où des imports peuvent être coincés. C'est exactement ce que
    cette colonne existe pour rendre visible — rien d'autre ne le signale, ni
    le temps, ni un redémarrage, ni la recherche périodique (voir
    .claude/skills/manual-import) — et un faux négatif silencieux y serait
    pire qu'un tiret.

    Mêmes états que stuck_queue_records() dans scripts/manual-import.py, à
    garder alignés : ce qui est compté ici doit être exactement ce que
    `manual-import.py list` sait ensuite traiter."""
    env = load_env_file(REPO_ROOT / "arr" / ".env")
    downloads = {"importBlocked": set(), "importPending": set()}
    for service, container, base_url, key_name, unknown_param in ARR_QUEUE_APPS:
        api_key = env.get(key_name)
        if service not in running or not api_key:
            return None
        try:
            # Clé passée par stdin, jamais en argument — même raison que
            # prowlarr_indexer_health() : l'argv d'un `docker exec` est lisible
            # dans `ps`, et ce script tourne toutes les 5 min par cron.
            script = ('IFS= read -r k; exec curl -s -H "X-Api-Key: $k" '
                      + shlex.quote(f"{base_url}/queue?page=1&pageSize=1000"
                                    f"&{unknown_param}"))
            res = subprocess.run(
                ["docker", "exec", "-i", container, "sh", "-c", script],
                input=api_key, capture_output=True, text=True, timeout=15,
            )
            records = json.loads(res.stdout)["records"]
        except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, TypeError):
            return None
        if not isinstance(records, list):
            return None
        for record in records:
            state = record.get("trackedDownloadState")
            if state in downloads:
                # Préfixé par l'arr : un même infoHash suivi par Sonarr ET Radarr
                # reste deux imports distincts. Sans downloadId (jamais vu sur un
                # téléchargement fini), l'entrée compte seule.
                downloads[state].add((service, record.get("downloadId") or f"queue:{record.get('id')}"))
    return {state: len(ids) for state, ids in downloads.items()}


def lan_middleware_names():
    """Noms des middlewares qui portent un `ipAllowList` dans la configuration
    dynamique de Traefik (traefik/dynamic/lan-only.yml, généré par
    scripts/lan-only-middleware.sh).

    Nécessaire depuis le 2026-08-07 : ces middlewares ne sont plus déclarés par
    labels Docker mais dans ce fichier, pour pouvoir être ouverts au WAN sans
    recréer de conteneur. Les chercher dans les labels du service ne donnait donc
    plus rien et TOUS les services LAN-only ressortaient dans « Public ».

    Lu au parseur maison plutôt qu'avec PyYAML : ce script n'a aucune dépendance
    hors stdlib (c'est ce qui a motivé sa réécriture depuis bash+jq, voir
    CLAUDE.md), et le fichier est produit par nous, à forme fixe. Exiger
    `ipAllowList` sur la ligne suivante plutôt que de ramasser tout nom de
    middleware : un futur middleware d'un autre type dans ce fichier ne doit pas
    faire passer un service pour LAN-only.

    Fichier absent = aucun nom. C'est l'état où Traefik ne résout plus ces
    middlewares et où les routeurs concernés répondent 404 de toute façon ;
    `make up` (via `lan-only-middleware.sh ensure`) le recrée."""
    try:
        text = LAN_DYNAMIC_FILE.read_text()
    except OSError:
        return set()
    return set(LAN_MIDDLEWARE_RE.findall(text))


def extract_traefik_services(config, lan_names):
    """Reproduit la logique du JQ_PROGRAM d'origine : premier router (par
    ordre alphabétique de clé de label, comme jq `keys`) exposant une règle
    Host(`...`), LAN si un de ses middlewares porte un ipallowlist.

    Un service reste classé LAN dès qu'un `ipAllowList` est branché sur son
    routeur, **quelle que soit la plage du moment** : après un
    `make switch-lan-only-middleware` la plage laisse tout passer, mais faire
    basculer les 5 cartes dans « Public » à chaque aller-retour ferait perdre
    l'information utile (« normalement restreint »). C'est le bandeau rouge en
    haut de page qui dit que la restriction est levée, et jusqu'à quand."""
    for name, svc in (config.get("services") or {}).items():
        labels = svc.get("labels") or {}
        # `config --no-interpolate` (stack sans .env) rend les labels tels
        # qu'écrits, en liste `clé=valeur` plutôt qu'en dictionnaire.
        if isinstance(labels, list):
            labels = dict(item.split("=", 1) for item in labels if "=" in item)
        if labels.get("traefik.enable") != "true":
            continue
        rule_key = router = None
        for key in sorted(labels):
            m = RULE_KEY_RE.match(key)
            if m:
                rule_key, router = key, m.group(1)
                break
        if not rule_key:
            continue
        host_match = HOST_RE.search(labels[rule_key])
        if not host_match:
            continue
        middlewares = labels.get(f"traefik.http.routers.{router}.middlewares", "")
        mw_list = [m.split("@")[0] for m in middlewares.split(",") if m]
        # Les deux sources possibles : le fichier dynamique (cas des middlewares
        # LAN-only depuis 2026-08-07) et un label frère sur le même service
        # (forme d'origine, conservée pour tout futur ipallowlist déclaré ainsi).
        lan = any(
            mw in lan_names
            or labels.get(f"traefik.http.middlewares.{mw}.ipallowlist.sourcerange") is not None
            for mw in mw_list
        )
        yield name, host_match.group(1), lan


def render_status_light(state_class, state):
    """Voyant d'état global d'une carte (généralisé depuis la carte Tunnel VPN
    le 2026-10-20) : good/warning/critical, mêmes couleurs que les valeurs de
    la carte. La couleur n'est jamais seule (WCAG 1.4.1) : `state` dit en
    clair pourquoi, en infobulle et pour les lecteurs d'écran."""
    return render("status-light.html", state_class=state_class, state=html.escape(state))


def render_stat_header(title, state=None):
    """En-tête de carte du Monitoring : titre + voyant facultatif. `state` :
    (classe, libellé) ou None quand la carte n'a pas d'état global (Débits)."""
    return render("stat-header.html", title=title,
                  light=render_status_light(*state) if state else "")


# Voyant d'une carte de service selon l'état du healthcheck Docker (voir
# build_cards). « starting » à part : juste après un `make up`, rien n'est
# encore vérifié, l'afficher vert serait affirmer un état inconnu.
HEALTH_LIGHT = {
    "healthy": ("good", "Healthcheck OK"),
    "starting": ("warning", "Healthcheck en cours"),
    "unhealthy": ("critical", "Healthcheck en échec"),
}


def render_card(key, host, health, probe_path):
    """`health` : clé de HEALTH_LIGHT."""
    name = DISPLAY_NAME.get(key, key)
    logo = LOGO_FILE.get(key)
    # Plus de contour rouge autour du logo (retiré le 2026-10-20, jugé de
    # trop) : le voyant rouge + l'avertissement en clair suffisent.
    img = f'<img src="/assets/logos/{logo}" alt="" class="logo">' if logo else ""
    warning = '<span class="warning">⚠ healthcheck en échec</span>' if health == "unhealthy" else ""
    probe_attr = f' data-probe="https://{host}{probe_path}"' if probe_path else ""
    # Voyant = healthcheck Docker. Pas de voyant sur une carte arrêtée
    # (render_down_card) : déjà grisée, et un arrêt peut être volontaire.
    light = render_status_light(*HEALTH_LIGHT[health])
    return render("card-clickable.html", host=host, probe_attr=probe_attr, img=img, warning=warning,
                  name=name, light=light)


def render_down_card(key):
    name = DISPLAY_NAME.get(key, key)
    logo = LOGO_FILE.get(key)
    img = f'<img src="/assets/logos/{logo}" alt="" class="logo">' if logo else ""
    return render("card-down.html", img=img, name=name)


def build_cards(running, unhealthy, starting):
    public_cards, local_cards, down_cards = [], [], []
    lan_names = lan_middleware_names()
    for stack in STACKS:
        config = docker_compose_config(stack)
        for service, host, lan in extract_traefik_services(config, lan_names):
            key = f"{stack}/{service}"
            health = "unhealthy" if key in unhealthy else "starting" if key in starting else "healthy"
            if key in running:
                probe_path = PROBE_PATH.get(key, "/favicon.ico") if lan else None
                card = render_card(key, host, health, probe_path)
                (local_cards if lan else public_cards).append(card)
            else:
                down_cards.append(render_down_card(key))
    return public_cards, local_cards, down_cards


# Repères des mètres : rouge < 1 (en dessous de l'équilibre), jaune 1-2, vert
# >= 2 — seuil torrenting générique, pas le seedRatio=2 propre à Nyaa.si côté
# Sonarr (voir CLAUDE.md, qui ne s'applique qu'à cet indexeur).
def ratio_zone(r):
    if r is None or r >= 2:
        return "good"
    if r < 1:
        return "critical"
    return "warning"


def ratio_pct(r):
    """Échelle visuelle 0-4 (au-delà, barre pleine — le chiffre affiché reste exact)."""
    if r is None:
        return 100
    if r <= 0:
        return 0
    return min(int(r / 4 * 100), 100)


def speed_pct(value, max_value):
    if max_value <= 0:
        return 0
    return min(int(value / max_value * 100), 100)


GAUGE_CX, GAUGE_CY, GAUGE_R = 60, 64, 50
GAUGE_NEEDLE_R = GAUGE_R - 8
# Bornes de zone de la jauge de ratio, en % sur l'échelle 0-4 de ratio_pct
# (25% = ratio 1, 50% = ratio 2) — mêmes seuils que ratio_zone/la mini-barre
# du tableau par tracker.
RATIO_ZONE_PCT = (25, 50)
RATIO_ZONE_CLASSES = ("gauge-zone-critical", "gauge-zone-warning", "gauge-zone-good")
# Espace disque : zones de sévérité à 80%/90% d'occupation (voir
# disk_fill_zone) — plus proche de 90%+ = mauvais.
DISK_ZONE_PCT = (80, 90)


def gauge_point(pct, radius):
    """Point sur l'arc de jauge (voir GAUGE_CX/CY/R) à pct % (0-100) du
    sweep 180°-0°, à la distance `radius` du centre — réutilisé pour les
    arcs (radius=GAUGE_R) et l'aiguille (radius=GAUGE_NEEDLE_R)."""
    theta = math.radians(180 - (pct / 100) * 180)
    return GAUGE_CX + radius * math.cos(theta), GAUGE_CY - radius * math.sin(theta)


def gauge_needles_svg(needles):
    """Aiguilles d'une jauge (voir gauge-needle.html). `needles` : liste de
    (pct 0-100, classe CSS en plus de gauge-needle), dessinées dans l'ordre
    (la dernière passe au-dessus). Le sweep total ne dépasse jamais 180°,
    donc le large-arc-flag SVG des arcs reste toujours 0 (codé en dur dans
    les templates)."""
    lines = []
    for pct, extra_class in needles:
        needle_x, needle_y = gauge_point(max(0, min(100, pct)), GAUGE_NEEDLE_R)
        lines.append(render(
            "gauge-needle.html",
            classes=" ".join(c for c in ("gauge-needle", extra_class) if c),
            needle_x=f"{needle_x:.1f}", needle_y=f"{needle_y:.1f}",
        ))
    return "\n".join(lines)


def gauge_svg(needles):
    """Jauge de débit (voir gauge.html) : arc de fond fixe + aiguilles. Plus
    d'arc de remplissage depuis le passage à 2 aiguilles sur une même jauge
    (2026-10-20, voir render_speed_card) : deux remplissages superposés ne
    se liraient pas."""
    return render("gauge.html", needles=gauge_needles_svg(needles))


def zone_gauge_svg(needles, zone_pct, zone_classes):
    """Jauge à 3 zones de sévérité fixes (voir gauge-zones.html) : pas d'arc
    de remplissage, le fond est lui-même divisé en zones (rouge/jaune/vert
    ou l'inverse selon `zone_classes`), les aiguilles pointent juste leur
    position dessus. Remplace une balance à bascule (log2(ratio)) jugée peu
    lisible à l'usage pour le ratio. `needles` : voir gauge_needles_svg."""
    p1x, p1y = gauge_point(zone_pct[0], GAUGE_R)
    p2x, p2y = gauge_point(zone_pct[1], GAUGE_R)
    return render(
        "gauge-zones.html",
        p1x=f"{p1x:.1f}", p1y=f"{p1y:.1f}", p2x=f"{p2x:.1f}", p2y=f"{p2y:.1f}",
        zone1_class=zone_classes[0], zone2_class=zone_classes[1], zone3_class=zone_classes[2],
        needles=gauge_needles_svg(needles),
    )


def render_stat_placeholder(title, span_class=""):
    """Remplace une carte dont les données dépendent d'une stack arrêtée
    (voir CLAUDE.md) — la carte reste dans le flux (même gabarit de span
    que la carte réelle, pour ne pas décaler la mise en page des cartes
    suivantes) plutôt que de disparaître silencieusement, comme
    card-down.html pour les cartes de service Public/Local."""
    classes = " ".join(c for c in ("stat", span_class, "stat-placeholder") if c)
    return render("stat-placeholder.html", classes=classes, title=title)


def render_stat_card(icon, value, label, value_class=""):
    return render("stat-card.html", icon=icon, value=value, label=label, value_class=value_class)


def render_stat_item(value, label, value_class="", title=""):
    """`title` rend une infobulle native (comme les libellés de tracker repliés
    de clearr) : toutes les valeurs de la carte Torrents en portent une, la
    place manquant pour dire en libellé ce que chaque compteur mesure
    exactement (voir render_torrents_files_card)."""
    title_attr = f' title="{html.escape(title, quote=True)}"' if title else ""
    return render("stat-multi-item.html", value=value, label=label,
                  value_class=value_class, title_attr=title_attr)


def render_multi_stat_column(items):
    return render("multi-stat-column.html", items="\n".join(items))


def render_torrents_files_card(stats, imports):
    """Torrents (Actifs/En pause/En erreur), Fichiers (Absents/En
    bibliothèque/En cross-seed) et Imports (Bloqués/En attente) fusionnés en
    une seule carte à 3 colonnes, qui prend la largeur de 2 cartes normales
    (voir .stat-span-2) — demandé par l'utilisateur le 2026-07-29 : 2 cartes
    séparées côte à côte prenaient trop de place pour des infos étroitement
    liées (l'une compte les torrents, l'autre leurs fichiers sur disque). Un
    seul titre "Torrents" pour toute la carte (pas un par colonne, retiré le
    2026-07-30 — 2 titres empilés sur la même carte prêtaient à confusion).

    3e colonne ajoutée le 2026-08-31, sur demande, sans toucher au span de la
    carte : le flux Monitoring est calé sur des rangées de 4 slots (voir
    build_stats_section), un span-3 aurait décalé la carte tracker et les
    tâches planifiées. Les colonnes tombent donc à ~137px, d'où des libellés
    courts ("Bloqués"/"En attente") — sans titre de colonne, "Bloqués" seul se
    lirait comme un état de torrent. Les 8 valeurs de la carte portent donc un
    title natif donnant leur définition exacte (généralisé le 2026-08-31 : ces
    libellés tiennent en deux mots, mais aucun ne dit ce qui est réellement
    compté — "Actifs" est un status RPC, "Absents"/"En bibliothèque" les
    marqueurs ABS/BIB de clearr).

    `imports` à None (un arr arrêté ou injoignable, voir arr_stuck_imports())
    affiche "—" et non 0 : ici un zéro faux annoncerait précisément l'absence
    de l'anomalie qu'on cherche à voir."""
    torrents_col = render_multi_stat_column([
        render_stat_item(
            str(stats["torrents_active"]), "Actifs",
            title="Torrents non arrêtés côté Transmission (status != 0) : en "
                  "téléchargement, en vérification ou en seed",
        ),
        render_stat_item(
            str(stats["torrents_paused"]), "En pause",
            title="Torrents arrêtés (status 0), terminés ou non",
        ),
        render_stat_item(
            str(stats["torrents_errored"]), "En erreur",
            value_class="stat-value-critical" if stats["torrents_errored"] else "stat-value-good",
            title="Torrents signalés en erreur par Transmission : tracker "
                  "injoignable, données introuvables sur disque…",
        ),
    ])
    files_col = render_multi_stat_column([
        render_stat_item(
            str(stats["torrents_missing"]), "Absents",
            value_class="stat-value-critical" if stats["torrents_missing"] else "stat-value-good",
            title="Torrents dont plus aucun fichier n'existe sur disque "
                  "(marqueur ABS de clearr)",
        ),
        render_stat_item(
            str(stats["torrents_linked"]), "En bibliothèque",
            title="Torrents dont au moins un fichier est aussi dans library/ "
                  "par hardlink (marqueur BIB de clearr)",
        ),
        render_stat_item(
            str(stats["torrents_cross_seed"]), "En cross-seed",
            title="Entrées injectées par cross-seed, rangées sous "
                  ".cross-seed-links/ et non sous completed/",
        ),
    ])
    # Colorées comme "En erreur"/"Absents" au-dessus (rouge si > 0, vert
    # sinon) : un import coincé est une anomalie, pas une valeur de service.
    # Aucune classe en revanche quand le compte est inconnu — "—" n'est ni bon
    # ni mauvais.
    imports_col = render_multi_stat_column([
        render_stat_item(
            str(imports["importBlocked"]) if imports else "—", "Bloqués",
            value_class=(("stat-value-critical" if imports["importBlocked"] else "stat-value-good")
                         if imports else ""),
            title="Imports bloqués par Sonarr/Radarr (importBlocked) — "
                  "téléchargements terminés que l'arr a refusé d'importer "
                  "(un pack compte pour un, pas un par épisode)",
        ),
        render_stat_item(
            str(imports["importPending"]) if imports else "—", "En attente",
            value_class=(("stat-value-critical" if imports["importPending"] else "stat-value-good")
                         if imports else ""),
            title="Imports en attente (importPending) — l'arr a renoncé à "
                  "rattacher le fichier ; ne se débloque pas tout seul",
        ),
    ])
    return render("multi-stat-columns-card.html", header=render_stat_header("Torrents", torrents_state(stats, imports)),
                  columns="\n".join([torrents_col, files_col, imports_col]))


def torrents_state(stats, imports):
    """Rouge dès qu'une des valeurs colorées en rouge dans la carte est > 0 ;
    jaune si les imports sont inconnus (« — ») sans autre anomalie : rien ne
    permet alors d'annoncer « tout va bien »."""
    counts = [
        ("En erreur", stats["torrents_errored"]),
        ("Absents", stats["torrents_missing"]),
    ]
    if imports:
        counts += [("Imports bloqués", imports["importBlocked"]),
                   ("Imports en attente", imports["importPending"])]
    # « Libellé : n » plutôt que « n libellé » : pas d'accord singulier/pluriel à gérer.
    anomalies = [f"{label} : {n}" for label, n in counts if n]
    if anomalies:
        return "critical", " · ".join(anomalies)
    if not imports:
        return "warning", "Imports inconnus (Sonarr ou Radarr injoignable)"
    return "good", "Aucune anomalie"


def render_needle_gauge_card(title, icon, first, second, state=None):
    """Carte d'une case : une seule jauge à 2 aiguilles (Débits
    descendant/montant, Ratios total/session), au lieu de 2 jauges côte à
    côte en stat-span-2 — demandé le 2026-10-20 pour libérer de la place
    dans le flux (voir build_stats_section). 1re aiguille neutre, 2e bleue ;
    la pastille devant chaque valeur reprend la couleur de son aiguille.
    `first`/`second` : (valeur affichée, libellé, définition en title=) ;
    `state` : voyant d'état global, voir render_stat_header."""
    items = [
        render("needle-legend-item.html", value=value, label=label, tooltip=tooltip,
               swatch_class=swatch_class)
        for (value, label, tooltip), swatch_class in
        ((first, "needle-swatch-first"), (second, "needle-swatch-second"))
    ]
    return render("needle-gauge-card.html", header=render_stat_header(title, state), icon=icon,
                  items="\n".join(items))


def render_speed_card(stats):
    """Chaque aiguille reste sur SA propre échelle (maximum observé sur ~25 h
    dans ce sens, voir historical_max_speed) comme les 2 jauges d'avant : à
    échelle commune, l'aiguille montante resterait collée à 0 la plupart du
    temps. La jauge se lit donc « près de son max habituel », pas comme une
    comparaison descendant/montant."""
    scale = stats["speed_scale"]
    icon = gauge_svg([
        (speed_pct(stats["download_speed"], scale["download_max"]), ""),
        (speed_pct(stats["upload_speed"], scale["upload_max"]), "gauge-needle-second"),
    ])
    return render_needle_gauge_card(
        "Débits", icon,
        (stats["download_speed_human"], "Descendant",
         "Débit descendant instantané ; aiguille relative au plus haut débit descendant observé sur ~25 h"),
        (stats["upload_speed_human"], "Montant",
         "Débit montant instantané ; aiguille relative au plus haut débit montant observé sur ~25 h"),
    )


def render_ratio_card(stats):
    """Session dessinée en dernier (au-dessus) : c'est elle qui bouge, le
    total varie à peine d'un jour à l'autre."""
    total, session = stats["total"], stats["session"]
    icon = zone_gauge_svg(
        [(ratio_pct(total["ratio"]), ""), (ratio_pct(session["ratio"]), "gauge-needle-second")],
        RATIO_ZONE_PCT, RATIO_ZONE_CLASSES,
    )
    return render_needle_gauge_card(
        "Ratios", icon,
        (total["ratio_display"], "Total",
         "Ratio cumulé de tous les torrents présents (envoyé / reçu)"),
        (session["ratio_display"], f"Session ({session['uptime_human']})",
         "Ratio depuis le dernier démarrage de Transmission"),
        # Voyant sur le total seul : la session repart de zéro à chaque
        # redémarrage, son ratio n'a de sens qu'après des jours.
        state={"good": ("good", "Ratio total ≥ 2"),
               "warning": ("warning", "Ratio total entre 1 et 2"),
               "critical": ("critical", "Ratio total < 1")}[ratio_zone(total["ratio"])],
    )


def disk_fill_zone(used_pct):
    """Zone de sévérité (mêmes seuils que l'ancienne jauge en arc,
    DISK_ZONE_PCT) pour la mini-barre de la carte disque condensée — rouge
    >=90% occupé, jaune >=80%, vert sinon."""
    if used_pct >= DISK_ZONE_PCT[1]:
        return "critical"
    if used_pct >= DISK_ZONE_PCT[0]:
        return "warning"
    return "good"


def render_disk_row(path, label):
    """Une ligne label + mini-barre pour la carte disque — le texte (espace
    libre, arrondi via human_size_rounded, suffixé "restant" pour lever
    l'ambiguïté sur ce que représente la valeur) est placé sous la barre,
    pas à côté (voir disk-row.html, demandé le 2026-07-29). Best-effort :
    None si le chemin est absent/illisible (pour que l'autre ligne
    s'affiche quand même, voir render_disk_card). Renvoie (html, zone)."""
    if not path:
        return None
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    used_pct = usage.used / usage.total * 100
    zone = disk_fill_zone(used_pct)
    return render(
        "disk-row.html", label=label, fill_class=f"meter-fill-{zone}",
        pct=round(used_pct, 1), value=f"{human_size_rounded(usage.free)} restant",
    ), zone


def render_disk_card(data_root, backup_dir):
    """Carte disque condensée en mini-barres (une ligne par disque) plutôt
    qu'en 2 jauges en arc empilées — demandé le 2026-07-29 pour que la carte
    ne prenne que la place d'une seule carte normale du flux, pas une carte
    de hauteur double. DATA_ROOT (téléchargements/bibliothèque) et le disque
    hébergeant sauvegarde/ (restic) sont 2 disques physiques distincts sur ce
    déploiement (voir résilience dans README/ARCHITECTURE), d'où 2 lignes
    plutôt qu'un seul chiffre qui masquerait lequel des deux se remplit.
    Toujours tentée, indépendante de l'état de vpn/transmission-vpn
    (contrairement au reste de cette section). Best-effort par ligne (voir
    render_disk_row) : la carte n'est omise que si aucune des deux n'est
    disponible."""
    rows = [r for r in (
        render_disk_row(data_root, "Téléchargements"),
        render_disk_row(str(backup_dir), "Sauvegarde"),
    ) if r]
    if not rows:
        return None
    # Voyant = le disque le plus plein des deux (mêmes seuils que disk_fill_zone).
    zones = {zone for _, zone in rows}
    if "critical" in zones:
        state = ("critical", f"Un disque est occupé à {DISK_ZONE_PCT[1]} % ou plus")
    elif "warning" in zones:
        state = ("warning", f"Un disque est occupé à {DISK_ZONE_PCT[0]} % ou plus")
    else:
        state = ("good", "Espace disque suffisant")
    return render("disk-card.html", header=render_stat_header("Disques", state),
                  rows="\n".join(html_row for html_row, _ in rows))


# scripts/crontab lance `make backup` le dimanche 3h (hebdomadaire) — rouge
# si le dernier snapshot restic dépasse cet intervalle, signe qu'un passage
# a été manqué ou a échoué silencieusement plutôt que la fenêtre normale
# entre deux cron.
BACKUP_MAX_AGE_DAYS = 7

# Au-delà, dashboard.js signale que la page servie est périmée — donc que le
# cron de régénération ne tourne plus. 3 ticks de l'intervalle */5 : assez large
# pour absorber le jitter du scheduler et un tick manqué (même raison que
# CRON_MARKER_SLACK), assez court pour que ça se voie dans la demi-heure.
DASHBOARD_STALE_AFTER_SECONDS = 15 * 60

RESTIC_REPO_DIR = REPO_ROOT / "sauvegarde" / "restic-repo"
# Même résolution que scripts/backup.sh : le mot de passe vit hors du repo (il
# n'a pas à être dans l'arborescence qu'on sauvegarde), l'ancien emplacement
# restant accepté en repli tant qu'un déploiement ne l'a pas migré. Pointer le
# seul ancien chemin faisait échouer last_backup_age_days() en silence, donc
# affichait la sauvegarde en rouge alors qu'elle tournait (constaté le
# 2026-08-24, migration du mot de passe faite sans toucher à ce fichier).
RESTIC_PASSWORD_FILE = Path.home() / ".config" / "server-restic-password"
LEGACY_RESTIC_PASSWORD_FILE = REPO_ROOT / "sauvegarde" / "restic-password"

# Tâches de scripts/crontab qui écrivent un marqueur `date +\%s > ...` à la
# fin d'une exécution réussie (le `&&` du cron ne l'atteint pas si une étape
# précédente échoue) — voir cron_marker_age_seconds() et scripts/crontab.
# La sauvegarde restic n'en fait pas partie : son propre check (âge réel du
# dernier snapshot, ci-dessus) est plus fiable qu'un marqueur de fin de
# script, qui ne prouve que "le script est allé jusqu'au bout", pas que le
# snapshot produit est valide.
# 4e élément : service(s) "<project>/<service>" (mêmes clés que le `running`
# de docker_ps_set()) qui doivent tourner pour que la tâche ait un sens —
# scripts/crontab garde chaque ligne correspondante derrière le même check
# via scripts/require-running.sh, donc le cron lui-même ne tourne pas non
# plus dans ce cas (voir CLAUDE.md). Liste vide = pas de stack associée,
# toujours affichée (rafraîchissement dashboard : c'est justement lui qui
# doit tourner pour refléter qu'une stack est arrêtée).
# 5e élément : description affichée au survol (attribut `title` de la ligne).
CRON_STATUS_DIR_NAME = ".cron-status"
# Marge appliquée à l'intervalle attendu de chaque tâche (voir
# render_scheduled_tasks_card()) — sans marge, un marqueur comparé pile à
# l'intervalle du cron (ex. 300s pour un cron */5) passe rouge à tort dès que
# la régénération du dashboard tombe dans les toutes dernières secondes avant
# le tick suivant (jitter du scheduler cron, ou simplement un `make
# dashboard-refresh` manuel qui ne tombe pas pile sur le cycle) alors que la
# tâche tourne normalement — constaté le 2026-07-30 sur "Rafraîchissement
# dashboard" lui-même.
CRON_MARKER_SLACK = 1.2
SCHEDULED_TASKS = [
    ("Nextcloud (cron.php)", "nextcloud-cron", 5 * 60, ["nextcloud/app"],
     "Toutes les 5 min : tâches de fond de Nextcloud (synchronisation, "
     "notifications, nettoyage, corbeille, versions…)."),
    ("Rafraîchissement dashboard", "dashboard-refresh", 5 * 60, [],
     "Toutes les 5 min : régénère cette page (état des conteneurs, "
     "disques, torrents, tâches)."),
    ("Config arr (overrides)", "arr-overrides", 24 * 3600, ["arr/sonarr", "arr/radarr"],
     "Chaque nuit à 0 h : réapplique la config versionnée des arr (profils de "
     "qualité, custom formats, tailles, délai, Jellyfin, .nfo) — toute "
     "modification faite dans l'UI est annulée."),
    ("Recherche des manquants", "search-missing", 7 * 24 * 3600, ["arr/sonarr", "arr/radarr"],
     "Le lundi à 5 h : relance la recherche des épisodes et films déjà sortis "
     "mais absents (ratés par le RSS), avec un plafond pour ménager les trackers."),
    ("Marqueurs de finale", "mark-finales", 24 * 3600, ["arr/sonarr"],
     "Chaque nuit à 2 h : repose les marqueurs de fin de saison/série dans "
     "les .nfo des épisodes (affichés dans Kodi)."),
    ("Imports rattachés par ID", "manual-import", 15 * 60, ["arr/sonarr", "arr/radarr"],
     "Toutes les 15 min : importe les téléchargements finis que Sonarr/Radarr "
     "n'ont rattachés que par ID (titre non reconnu) et laissent bloqués en file."),
]
BACKUP_TASK_DESCRIPTION = (
    "Le dimanche à 3 h : sauvegarde restic (dump de la base Nextcloud, données, "
    "secrets .env, manifeste des images). Vert si le dernier snapshot a moins de 7 jours."
)


def cron_marker_age_seconds(data_root, marker_name):
    """Âge (secondes) du marqueur écrit par un job de scripts/crontab à la
    fin d'une exécution réussie. None si DATA_ROOT est absent ou si le
    marqueur n'existe pas encore (jamais tourné depuis l'installation de ce
    marqueur, ou tâche jamais terminée avec succès)."""
    if not data_root:
        return None
    marker = Path(data_root) / CRON_STATUS_DIR_NAME / marker_name
    try:
        return datetime.now().timestamp() - int(marker.read_text().strip())
    except (OSError, ValueError):
        return None


def last_backup_age_days():
    """Âge (jours, flottant) du snapshot restic le plus récent — appelle le
    binaire restic en direct sur l'hôte (comme scripts/backup.sh, pas de
    docker exec ici : restic ne tourne dans aucun conteneur). Best-effort :
    None si restic/le mot de passe/le dépôt est absent ou injoignable, pour
    omettre la carte plutôt que planter la génération du dashboard."""
    password_file = RESTIC_PASSWORD_FILE
    if not password_file.exists():
        password_file = LEGACY_RESTIC_PASSWORD_FILE
    if not password_file.exists():
        return None
    env = {**os.environ, "RESTIC_REPOSITORY": str(RESTIC_REPO_DIR),
           "RESTIC_PASSWORD_FILE": str(password_file)}
    try:
        res = subprocess.run(
            ["restic", "snapshots", "--latest", "1", "--json"],
            capture_output=True, text=True, timeout=30, env=env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if res.returncode != 0:
        return None
    try:
        snapshots = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None
    if not snapshots:
        return None
    latest_ts = max(datetime.fromisoformat(s["time"]) for s in snapshots)
    return (datetime.now(latest_ts.tzinfo) - latest_ts).total_seconds() / 86400


def render_scheduled_tasks_card(data_root, running):
    """Remplace l'ancienne carte solo "Dernière sauvegarde" (ajouté le
    2026-07-30) : une carte-liste (même gabarit que render_indexers_card)
    listant chaque tâche planifiée connue avec un point coloré — vert si
    elle a tourné avec succès il y a moins de temps que l'écart normal
    entre deux occurrences de son cron, rouge sinon (jamais tournée avec
    succès, ou plus tournée depuis plus longtemps que prévu). Une tâche
    dont la/les stack(s) associée(s) ne tourne(nt) pas (voir
    SCHEDULED_TASKS) est entièrement absente de la liste plutôt que rouge —
    le cron lui-même ne tourne pas non plus dans ce cas (voir
    scripts/require-running.sh, scripts/crontab), donc rouge serait un faux
    signal d'échec pour un arrêt volontaire. Sauvegarde restic et
    rafraîchissement dashboard n'ont pas de stack associée : toujours
    affichées."""
    items = []

    backup_age_days = last_backup_age_days()
    items.append((
        "Sauvegarde restic",
        backup_age_days is not None and backup_age_days <= BACKUP_MAX_AGE_DAYS * CRON_MARKER_SLACK,
        BACKUP_TASK_DESCRIPTION,
    ))

    for label, marker_name, interval_seconds, required, description in SCHEDULED_TASKS:
        if any(svc not in running for svc in required):
            continue
        age_seconds = cron_marker_age_seconds(data_root, marker_name)
        items.append((label, age_seconds is not None and age_seconds <= interval_seconds * CRON_MARKER_SLACK,
                      description))

    lis = "\n".join(
        # Glyphe + texte caché en plus de la pastille : l'état ne reposait QUE
        # sur la couleur (WCAG 1.4.1), donc invisible pour un daltonien deutan
        # ou protan — ~8 % des hommes — alors que c'est la seule information de
        # la carte, et muet pour un lecteur d'écran (1.1.1), qui n'entendait
        # qu'une liste de noms. Le glyphe hérite de la couleur du point.
        '<li title="{description}"><span class="status-dot status-dot-{cls}"></span>'
        '<span class="status-mark status-mark-{cls}" aria-hidden="true">{mark}</span>'
        '{label}<span class="visually-hidden"> — {state}</span></li>'.format(
            cls="good" if ok else "critical",
            mark="✓" if ok else "✕",
            label=html.escape(label),
            state="OK" if ok else "en échec",
            description=html.escape(description),
        )
        for label, ok, description in items
    )
    failed = [label for label, ok, _ in items if not ok]
    state = (("critical", f"En retard ou en échec : {', '.join(failed)}") if failed
             else ("good", "Toutes les tâches ont tourné à temps"))
    return render("scheduled-tasks-card.html", header=render_stat_header("Tâches planifiées", state), items=lis)


def render_indexers_card(running):
    """Liste chaque indexeur Prowlarr avec un point coloré par état (voir
    prowlarr_indexer_health()), plutôt qu'un simple compte agrégé — permet
    de voir directement LEQUEL est en échec sans changer d'écran. Placeholder
    si arr/prowlarr est arrêté (pas de docker exec tenté pour rien) ; None
    (carte omise, comportement best-effort inchangé) si Prowlarr tourne mais
    reste injoignable/clé API absente pour une autre raison."""
    if "arr/prowlarr" not in running:
        return render_stat_placeholder("Indexeurs", "stat-span-2")
    indexers = prowlarr_indexer_health()
    if indexers is None:
        return None
    items = "\n".join(
        # Même raison que render_scheduled_tasks_card ci-dessus : « quel
        # indexeur est tombé » ne doit pas dépendre de la perception des couleurs.
        '<li><span class="status-dot status-dot-{cls}"></span>'
        '<span class="status-mark status-mark-{cls}" aria-hidden="true">{mark}</span>'
        '{label}<span class="visually-hidden"> — {state}</span></li>'.format(
            cls="good" if i["ok"] else "critical",
            mark="✓" if i["ok"] else "✕",
            label=html.escape(i["name"]),
            state="OK" if i["ok"] else "en échec",
        )
        for i in indexers
    )
    failed = [i["name"] for i in indexers if not i["ok"]]
    state = (("critical", f"En échec : {', '.join(failed)}") if failed
             else ("good", "Tous les indexeurs répondent"))
    return render("indexers-card.html", header=render_stat_header("Indexeurs", state), items=items)


def render_tracker_row(tracker):
    meter = render(
        "mini-meter.html", value=tracker["ratio_display"],
        fill_class=f"meter-fill-{ratio_zone(tracker['ratio'])}", pct=ratio_pct(tracker["ratio"]),
    )
    # Vue par défaut : seulement les trackers privés, c'est-à-dire un indexeur
    # réellement configuré dans Prowlarr ("official") ET annoncé par lui comme
    # non public ("private") — le ratio n'y est une monnaie que là. Sont donc
    # masqués aussi bien les trackers publics bruts embarqués dans un .torrent
    # (ex. tracker.p2p-world.net, ou les ~20 trackers d'une seule release
    # multi-tracker) que les indexeurs publics qu'on interroge nous-mêmes
    # (Nyaa.si). Révélés par le switch (voir tracker-card.html/dashboard.js),
    # pas de filtrage côté Python : la ligne existe toujours dans le HTML.
    is_private = tracker.get("official") and tracker.get("private")
    row_class = "" if is_private else "tracker-row-other"
    # html.escape comme les cartes voisines (render_indexers_card,
    # render_scheduled_tasks_card) : string.Template n'échappe rien, et ce nom
    # n'est pas toujours un libellé Prowlarr — c'est le hostname brut d'une URL
    # d'annonce lue dans un .torrent tiers dès qu'aucun indexeur ne le
    # revendique. Seul des trois emplacements de la section à ne pas le faire,
    # sur la seule page du dépôt exposée au WAN.
    return render(
        "tracker-row.html", name=html.escape(tracker["name"]), meter=meter,
        uploaded=tracker["uploaded_human"], downloaded=tracker["downloaded_human"],
        row_class=row_class,
    )


def fetch_transmission_stats(running):
    """Best-effort : None si vpn/transmission-vpn ne tourne pas ou si
    transmission-stats.py ne sort pas de JSON exploitable — les cartes
    Débits/Ratio/torrents et la table par tracker en dépendent toutes,
    contrairement à l'espace disque et aux indexeurs Prowlarr ci-dessous qui
    ont leur propre disponibilité indépendante."""
    if "vpn/transmission-vpn" not in running:
        return None
    try:
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "transmission-stats.py")],
            capture_output=True, text=True, timeout=40,
        )
        stats = json.loads(res.stdout) if res.stdout.strip() else {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    return stats if stats and "error" not in stats else None


TUNNEL_CONTAINER = "vpn-transmission-vpn-1"
TUNNEL_PING_TARGET = "1.1.1.1"
# Seuils (orange, rouge) de la carte Tunnel VPN. Repères du 2026-10-20 : sain
# vers les Pays-Bas ~30 ms / 0 %, route dégradée vers la Belgique 3,4 s et
# 40-90 % de pertes (voir docs/telechargement.md#choix-du-serveur-airvpn).
TUNNEL_LOSS_PCT = (5, 20)
TUNNEL_RTT_MS = (150, 500)
# Codes renvoyés par l'API AirVPN ; un pays absent s'affiche en code brut.
COUNTRY_NAMES = {
    "at": "Autriche", "be": "Belgique", "ch": "Suisse", "de": "Allemagne",
    "es": "Espagne", "fr": "France", "gb": "Royaume-Uni", "it": "Italie",
    "nl": "Pays-Bas", "se": "Suède",
}


def tunnel_exec(cmd, timeout):
    """stdout d'une commande dans le conteneur VPN, None si elle échoue."""
    try:
        res = subprocess.run(["docker", "exec", TUNNEL_CONTAINER, "sh", "-c", cmd],
                             capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    return res.stdout if res.returncode == 0 else None


def tunnel_health():
    """Mesures prises DANS le conteneur VPN, donc à travers le tunnel. Chaque
    valeur vaut None quand sa mesure échoue, sauf `loss` : un ping qui ne sort
    pas du tout, c'est 100 % de pertes, pas une inconnue."""
    health = {"loss": 100, "rtt": None, "port_open": None,
              "airvpn": None, "country": None, "uptime": None}

    out = tunnel_exec(f"ping -q -c 10 -i 0.2 -W 2 {TUNNEL_PING_TARGET}", 15) or ""
    m = re.search(r"([\d.]+)% packet loss", out)
    if m:
        health["loss"] = round(float(m.group(1)))
    m = re.search(r"= [\d.]+/([\d.]+)/", out)
    if m:
        health["rtt"] = float(m.group(1))

    out = tunnel_exec("transmission-remote -pt", 15)
    if out and "Port is open" in out:
        health["port_open"] = "Yes" in out

    # API d'AirVPN plutôt qu'un service de géolocalisation tiers : elle dit en
    # plus si le trafic sort bien par AirVPN. Elle renvoie aussi le nom du
    # serveur, volontairement jamais affiché : la page est publique, et ce nom
    # donne l'IP de sortie, celle qui seede.
    out = tunnel_exec("curl -s -m 8 https://airvpn.org/api/whatismyip/", 12)
    try:
        info = json.loads(out) if out else {}
    except json.JSONDecodeError:
        info = {}
    if info.get("result") == "ok":
        health["airvpn"] = bool(info.get("airvpn"))
        health["country"] = (info.get("geo") or {}).get("code")

    # Âge du processus OpenVPN = âge du tunnel : l'image arrête le conteneur à
    # chaque chute du tunnel (remap-usr1 SIGTERM, ping-exit), jamais de
    # reconnexion dans le même processus.
    out = tunnel_exec('ps -o etimes= -p "$(pidof openvpn)"', 5)
    if out and out.strip().isdigit():
        health["uptime"] = int(out.strip())
    return health


def tunnel_state(h):
    """(classe, libellé) de l'état global du tunnel, porté par le voyant."""
    if h["loss"] >= 100:
        return "critical", "Coupé"
    if h["airvpn"] is False:
        return "critical", "Hors AirVPN"
    if h["port_open"] is False:
        return "critical", "Port fermé"
    rtt = h["rtt"] or 0
    if h["loss"] >= TUNNEL_LOSS_PCT[1] or rtt >= TUNNEL_RTT_MS[1]:
        return "critical", "Dégradé"
    if h["loss"] >= TUNNEL_LOSS_PCT[0] or rtt >= TUNNEL_RTT_MS[0]:
        return "warning", "Ralenti"
    return "good", "Sain"


def human_uptime(seconds):
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} j"


def render_tunnel_card(running):
    """Toujours rendue quand le VPN tourne, jamais omise : un tunnel qui ne
    répond plus est précisément ce que la carte doit montrer (même logique
    que les compteurs d'imports, voir render_torrents_files_card)."""
    if "vpn/transmission-vpn" not in running:
        return render_stat_placeholder("Tunnel VPN")
    h = tunnel_health()
    cls, label = tunnel_state(h)

    def level(value, thresholds):
        if value is None:
            return ""
        if value >= thresholds[1]:
            return "stat-value-critical"
        return "stat-value-warning" if value >= thresholds[0] else "stat-value-good"

    # « Hors AirVPN » à la place du pays : le voyant n'est qu'une couleur, une
    # ligne doit dire en clair chaque état rouge (les autres le font déjà :
    # pertes à 100 %, port « Fermé »).
    country = h["country"]
    if h["airvpn"] is False:
        country_value, country_class = "Hors AirVPN", "stat-value-critical"
    else:
        country_value = COUNTRY_NAMES.get(country, country.upper()) if country else "—"
        country_class = ""
    rows = [
        render_stat_item(
            country_value, "Pays", value_class=country_class,
            title="Pays de sortie du tunnel, selon l'API d'AirVPN",
        ),
        render_stat_item(
            f"{h['rtt']:.0f} ms" if h["rtt"] is not None else "—", "Latence",
            value_class=level(h["rtt"], TUNNEL_RTT_MS),
            title=f"Ping moyen vers {TUNNEL_PING_TARGET} à travers le tunnel (10 paquets)",
        ),
        render_stat_item(
            f"{h['loss']} %", "Pertes", value_class=level(h["loss"], TUNNEL_LOSS_PCT),
            title="Part des 10 pings restés sans réponse",
        ),
        render_stat_item(
            {True: "Ouvert", False: "Fermé", None: "—"}[h["port_open"]], "Port",
            value_class={True: "stat-value-good", False: "stat-value-critical", None: ""}[h["port_open"]],
            title="Port entrant de Transmission, testé par transmission-remote -pt ; "
                  "fermé, plus aucun pair ne peut se connecter à nous",
        ),
        render_stat_item(
            human_uptime(h["uptime"]) if h["uptime"] is not None else "—", "Connecté",
            title="Depuis le dernier (re)démarrage du tunnel",
        ),
    ]
    return render("tunnel-card.html", header=render_stat_header("Tunnel VPN", (cls, label)), rows="\n".join(rows))


def build_stats_section(running, data_root, backup_dir):
    """Visible WAN et LAN, pas de gating card-lan-blocked (chiffres agrégés
    en snapshot, pas un accès de contrôle au client — voir CLAUDE.md).
    Toutes les cartes sont à plat dans un seul flux (voir stats-flow.html) —
    pas de sous-section/titre de groupe : chaque carte porte son propre
    libellé (le titre de l'ancienne sous-section replié dedans, ex. "Débit
    descendant", voir CLAUDE.md). Débits/Ratio/Torrents viennent tous de
    transmission-stats.py (best-effort, omis ensemble si indisponible) ;
    espace disque, tâches planifiées et indexeurs Prowlarr ont chacun leur
    propre disponibilité (best-effort indépendant, voir
    render_disk_card()/render_scheduled_tasks_card()/render_indexers_card()).
    Toute la section est omise seulement si aucune carte n'est disponible.
    Débits/Ratios/Torrents/Ratio par tracker et Indexeurs affichent un
    placeholder (render_stat_placeholder) plutôt que de disparaître quand
    leur stack (vpn/transmission-vpn, arr/prowlarr) est arrêtée — demandé le
    2026-07-30 : une carte absente ne se distinguait pas d'un problème
    silencieux, contrairement à un arrêt volontaire explicite. Ce placeholder
    ne couvre que le cas "stack arrêtée" : si la stack tourne mais que la
    donnée reste indisponible pour une autre raison (transmission-stats.py en
    échec, clé API Prowlarr absente), comportement best-effort inchangé
    (carte omise, pas de placeholder)."""
    vpn_running = "vpn/transmission-vpn" in running
    stats = fetch_transmission_stats(running)

    speed_card = ratio_card = torrents_card = tracker_card = ""

    if not vpn_running:
        speed_card = render_stat_placeholder("Débits")
        ratio_card = render_stat_placeholder("Ratios")
        torrents_card = render_stat_placeholder("Torrents", "stat-span-2")
        tracker_card = render_stat_placeholder("Ratio par tracker", "stat-span-3")
    elif stats:
        speed_card = render_speed_card(stats)
        ratio_card = render_ratio_card(stats)
        if stats.get("trackers"):
            rows = "\n".join(render_tracker_row(t) for t in stats["trackers"])
            tracker_card = render("tracker-card.html", rows=rows)

        # Torrents (actifs/en pause/en erreur), Fichiers (absents/en
        # bibliothèque/en cross-seed — mêmes marqueurs BIB/ABS que le service
        # clearr, arr/clearr/app/core.py, voir CLAUDE.md) et Imports
        # (bloqués/en attente, seule colonne à ne pas venir de Transmission)
        # fusionnés en une seule carte à 3 colonnes (voir
        # render_torrents_files_card) — 2 cartes séparées avaient été essayées
        # d'abord, jugées trop encombrantes côte à côte par l'utilisateur.
        # Corollaire assumé du rattachement à cette carte : vpn/transmission-vpn
        # arrêté remplace tout le bloc par un placeholder, compteurs d'imports
        # compris, alors qu'ils ne dépendent que des arr.
        torrents_card = render_torrents_files_card(stats, arr_stuck_imports(running))

    # Ordre calé sur des rangées de 4 slots (span entre parenthèses), refait
    # le 2026-10-20 pour mettre le tunnel en tête, puis le disque entre
    # Ratios et Torrents (Débits et Ratios ramenés à une case, Indexeurs
    # passé à 2 colonnes sur 2 cases), à la demande :
    #   Tunnel(1) Débits(1) Indexeurs(2) / Ratios(1) Disque(1) Torrents(2) /
    #   Tâches(1) Tracker(3)
    # 12 slots, 3 rangées pleines (Tâches passé devant Tracker, à la demande). Tracker et tâches planifiées restent sur
    # la même rangée (demandé le 2026-07-30, voir CLAUDE.md). Une carte omise
    # (best-effort) décale la suite.
    cards = [c for c in (
        render_tunnel_card(running),
        speed_card,
        render_indexers_card(running),
        ratio_card,
        render_disk_card(data_root, backup_dir),
        torrents_card,
        render_scheduled_tasks_card(data_root, running),
        tracker_card,
    ) if c]

    if not cards:
        return ""

    return render(
        "section-transmission.html",
        stats_flow=render("stats-flow.html", cards="\n".join(cards)),
    )


def write_atomically(dest, data):
    """Remplace `dest` par `data` (bytes) sans jamais exposer de fichier à
    moitié écrit. Deux régénérations peuvent tourner en même temps (le tick cron
    */5 et le garde `rearm` de lan-only-middleware.sh tombent sur la même
    minute) : un temporaire au nom FIXE était alors tronqué par l'une pendant
    que l'autre le renommait — page coupée publiée, et `os.replace` de l'autre
    en FileNotFoundError. Nom unique (mkstemp) dans le même dossier, pour que le
    rename reste atomique (même système de fichiers).

    Remplacer le fichier ne gêne pas nginx : c'est le DOSSIER dashboard/html/
    qui est monté (cf. traefik/docker-compose.yml), il voit donc le nouvel
    inode ; un montage de fichier resterait, lui, figé sur l'ancien."""
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        # mkstemp crée en 0600 : illisible par nginx, qui tourne sous un autre
        # uid dans son conteneur (403 sur tout le dashboard).
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def is_temp_file(path):
    return path.name.startswith(".") and path.name.endswith(".tmp")


def copy_assets():
    logos_out = OUT_DIR / "assets" / "logos"
    logos_out.mkdir(parents=True, exist_ok=True)
    # *.svg et *.png (clearr.png, ajouté le 2026-08-01 — avant ça, seul le
    # glob *.svg existait, donc un logo PNG n'était jamais copié dans le
    # dossier servi : carré d'image cassée sur le dashboard malgré un
    # <img src> correct dans le HTML généré).
    logos = list((ASSETS_SRC / "logos").glob("*.svg")) + list((ASSETS_SRC / "logos").glob("*.png"))
    for logo in logos:
        write_atomically(logos_out / logo.name, logo.read_bytes())
    # Un ancien fichier resté d'un format précédent (ex. clearr.svg après son
    # remplacement par clearr.png) ne doit pas traîner indéfiniment. Retiré
    # APRÈS la copie et seulement s'il n'a plus de source : l'ancien « vider
    # puis recopier » laissait, pendant une régénération, un dossier sans logos
    # (images cassées servies) et faisait planter l'autre run concurrent sur un
    # fichier disparu sous lui. Les temporaires d'un run concurrent sont
    # épargnés ; `missing_ok` couvre le cas où l'autre run a déjà nettoyé.
    wanted = {logo.name for logo in logos}
    for stale in logos_out.iterdir():
        if stale.name not in wanted and not is_temp_file(stale):
            stale.unlink(missing_ok=True)
    for src, dest in (
        (ASSETS_SRC / "dashboard.css", OUT_DIR / "assets" / "dashboard.css"),
        (ASSETS_SRC / "dashboard.js", OUT_DIR / "assets" / "dashboard.js"),
        (ASSETS_SRC / "robots.txt", OUT_DIR / "robots.txt"),
        (ASSETS_SRC / "favicon.png", OUT_DIR / "assets" / "favicon.png"),
        (ASSETS_SRC / "favicon.ico", OUT_DIR / "favicon.ico"),
    ):
        write_atomically(dest, src.read_bytes())


def main():
    env_shared = load_env_file(REPO_ROOT / ".env.shared")
    domain = env_shared.get("DOMAIN", "")
    running = docker_ps_set()
    unhealthy = docker_ps_set(["health=unhealthy"])
    starting = docker_ps_set(["health=starting"])

    public_cards, local_cards, down_cards = build_cards(running, unhealthy, starting)

    # Chaque section n'est incluse que si elle a du contenu — pas de
    # placeholder "aucun"/"indisponible" pour une section vide.
    sections = []
    if public_cards:
        sections.append(render("section-grid.html", title="Public", cards="\n".join(public_cards)))
    if local_cards:
        sections.append(render("section-grid.html", title="Local (LAN)", cards="\n".join(local_cards)))
    if down_cards:
        sections.append(render("section-grid.html", title="Stack non lancée", cards="\n".join(down_cards)))
    stats_html = build_stats_section(running, env_shared.get("DATA_ROOT"), REPO_ROOT / "sauvegarde")
    if stats_html:
        sections.append(stats_html)

    copy_assets()

    now = datetime.now()
    page = render(
        "page.html",
        domain=domain,
        updated=now.strftime("%Y-%m-%d %H:%M"),
        # Horodatage machine + seuil, consommés par dashboard.js. C'est la SEULE
        # vérification d'état qui ne dépend pas de ce script : la carte « Tâches
        # planifiées » est rendue par lui, donc s'il casse (erreur Python, docker
        # indisponible) le dernier index.html valide continue d'être servi avec
        # toutes ses pastilles au vert — et masque du même coup toutes les pannes
        # qu'il aurait dû rapporter. Un surlignage client existait, retiré le
        # 2026-07-30 comme « redondant avec la carte » ; il était en fait le seul
        # signal indépendant, d'où son retour ici.
        generated_epoch=int(now.timestamp()),
        stale_after=DASHBOARD_STALE_AFTER_SECONDS,
        lan_banner=render_lan_only_banner(env_shared.get("DATA_ROOT")),
        sections="\n".join(sections),
    )
    out_file = OUT_DIR / "index.html"
    # Écriture atomique : deux lignes cron régénèrent le dashboard, toutes les
    # 5 min chacune (le tick régulier et le garde `rearm` de
    # lan-only-middleware.sh), donc à la minute où une fenêtre WAN expire les
    # deux tombent ensemble. Une écriture en place (write_text tronque puis
    # écrit) sert alors une page tronquée au visiteur — précisément au moment où
    # le bandeau « ouvert au WAN » doit être fiable. Temporaire à nom unique,
    # voir write_atomically().
    write_atomically(out_file, page.encode())
    print(f"dashboard régénéré : {out_file}")


if __name__ == "__main__":
    main()
