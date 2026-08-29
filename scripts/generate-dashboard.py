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
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = REPO_ROOT / "dashboard"
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
ASSETS_SRC = DASHBOARD_DIR / "assets"
OUT_DIR = DASHBOARD_DIR / "html"

STACKS = ["jellyfin", "nextcloud", "vpn", "arr", "seerr"]

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
PROBE_PATH = {
    "vpn/transmission-proxy": "/transmission/web/images/favicon.ico",
    "arr/clearr": "/static/favicon.png",
}

RULE_KEY_RE = re.compile(r"^traefik\.http\.routers\.([^.]+)\.rule$")
HOST_RE = re.compile(r"Host\(`([^`]+)`\)")

# Configuration dynamique de Traefik où vivent les middlewares LAN-only, et de
# quoi y relever leur nom — voir lan_middleware_names().
LAN_DYNAMIC_FILE = REPO_ROOT / "traefik" / "dynamic" / "lan-only.yml"
LAN_MIDDLEWARE_RE = re.compile(r"^ {4}([A-Za-z0-9_-]+):[ \t]*\n {6}ipAllowList:", re.MULTILINE)

PROWLARR_CONTAINER = "arr-prowlarr-1"
PROWLARR_URL = "http://localhost:9696/api/v1"


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


# Les conteneurs one-off (`docker compose run`, ex. `make clearr` qui lance la
# TUI) portent les mêmes labels project/service que le service lui-même mais
# héritent aussi de son healthcheck, qu'ils ne peuvent pas satisfaire (la TUI
# ne fait tourner aucun serveur HTTP sur :8000). Sans ce filtre, une session
# `make clearr` faisait passer la carte du service web en "healthcheck en
# échec" — et un one-off d'un service arrêté l'aurait fait passer pour démarré.
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
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {}
    if res.returncode != 0:
        return {}
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return {}


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
    failing_ids = {f.get("indexerId") for f in failing}
    return sorted(
        ({"name": i.get("name", "?"), "ok": i.get("id") not in failing_ids} for i in indexers),
        key=lambda e: e["name"].lower(),
    )


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


def render_card(key, host, unhealthy, probe_path):
    name = DISPLAY_NAME.get(key, key)
    logo = LOGO_FILE.get(key)
    logo_class = "logo logo-unhealthy" if unhealthy else "logo"
    img = f'<img src="/assets/logos/{logo}" alt="" class="{logo_class}">' if logo else ""
    warning = '<span class="warning">⚠ healthcheck en échec</span>' if unhealthy else ""
    probe_attr = f' data-probe="https://{host}{probe_path}"' if probe_path else ""
    return render("card-clickable.html", host=host, probe_attr=probe_attr, img=img, warning=warning, name=name)


def render_down_card(key):
    name = DISPLAY_NAME.get(key, key)
    logo = LOGO_FILE.get(key)
    img = f'<img src="/assets/logos/{logo}" alt="" class="logo">' if logo else ""
    return render("card-down.html", img=img, name=name)


def build_cards(running, unhealthy):
    public_cards, local_cards, down_cards = [], [], []
    lan_names = lan_middleware_names()
    for stack in STACKS:
        config = docker_compose_config(stack)
        for service, host, lan in extract_traefik_services(config, lan_names):
            key = f"{stack}/{service}"
            is_unhealthy = key in unhealthy
            if key in running:
                probe_path = PROBE_PATH.get(key, "/favicon.ico") if lan else None
                card = render_card(key, host, is_unhealthy, probe_path)
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


def gauge_svg(pct):
    """Jauge de débit (voir gauge.html) : arc de fond fixe, arc coloré +
    aiguille positionnés au même angle proportionnel à pct (0-100, voir
    speed_pct). Le sweep total ne dépasse jamais 180°, donc le
    large-arc-flag SVG reste toujours 0 (codé en dur dans le template)."""
    pct = max(0, min(100, pct))
    fill_x, fill_y = gauge_point(pct, GAUGE_R)
    needle_x, needle_y = gauge_point(pct, GAUGE_NEEDLE_R)
    return render(
        "gauge.html",
        fill_x=f"{fill_x:.1f}", fill_y=f"{fill_y:.1f}",
        needle_x=f"{needle_x:.1f}", needle_y=f"{needle_y:.1f}",
    )


def zone_gauge_svg(pct, zone_pct, zone_classes):
    """Jauge à 3 zones de sévérité fixes (voir gauge-zones.html) : pas d'arc
    de remplissage, le fond est lui-même divisé en zones (rouge/jaune/vert
    ou l'inverse selon `zone_classes`), l'aiguille pointe juste sa position
    dessus. Remplace une balance à bascule (log2(ratio)) jugée peu lisible à
    l'usage pour le ratio — réutilisée aussi pour l'espace disque (mêmes
    zones, ordre inverse) pour n'avoir qu'un seul type d'icône sur tout le
    dashboard plutôt qu'un anneau de progression en plus de la jauge."""
    p1x, p1y = gauge_point(zone_pct[0], GAUGE_R)
    p2x, p2y = gauge_point(zone_pct[1], GAUGE_R)
    needle_x, needle_y = gauge_point(pct, GAUGE_NEEDLE_R)
    return render(
        "gauge-zones.html",
        p1x=f"{p1x:.1f}", p1y=f"{p1y:.1f}", p2x=f"{p2x:.1f}", p2y=f"{p2y:.1f}",
        zone1_class=zone_classes[0], zone2_class=zone_classes[1], zone3_class=zone_classes[2],
        needle_x=f"{needle_x:.1f}", needle_y=f"{needle_y:.1f}",
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


def render_stat_item(value, label, value_class=""):
    return render("stat-multi-item.html", value=value, label=label, value_class=value_class)


def render_multi_stat_column(items):
    return render("multi-stat-column.html", items="\n".join(items))


def render_torrents_files_card(stats):
    """Torrents (Actifs/En pause/En erreur) et Fichiers (Absents/En
    bibliothèque/En cross-seed) fusionnés en une seule carte à 2 colonnes,
    qui prend la largeur de 2 cartes normales (voir .stat-span-2) — demandé
    par l'utilisateur le 2026-07-29 : 2 cartes séparées côte à côte prenaient
    trop de place pour des infos étroitement liées (l'une compte les
    torrents, l'autre leurs fichiers sur disque). Un seul titre "Torrents"
    pour toute la carte (pas un par colonne, retiré le 2026-07-30 — 2 titres
    empilés sur la même carte prêtaient à confusion)."""
    torrents_col = render_multi_stat_column([
        render_stat_item(str(stats["torrents_active"]), "Actifs"),
        render_stat_item(str(stats["torrents_paused"]), "En pause"),
        render_stat_item(
            str(stats["torrents_errored"]), "En erreur",
            value_class="stat-value-critical" if stats["torrents_errored"] else "stat-value-good",
        ),
    ])
    files_col = render_multi_stat_column([
        render_stat_item(
            str(stats["torrents_missing"]), "Absents",
            value_class="stat-value-critical" if stats["torrents_missing"] else "stat-value-good",
        ),
        render_stat_item(str(stats["torrents_linked"]), "En bibliothèque"),
        render_stat_item(str(stats["torrents_cross_seed"]), "En cross-seed"),
    ])
    return render("multi-stat-columns-card.html", title="Torrents", columns=torrents_col + "\n" + files_col)


def render_gauge_column(icon, value, label, value_class=""):
    return render("gauge-column.html", icon=icon, value=value, label=label, value_class=value_class)


def render_dual_gauge_card(title, columns):
    """Débits (descendant/montant) et Ratios (total/session) fusionnés
    chacun en une seule carte à 2 colonnes de jauges (voir .stat-span-2,
    même principe que render_torrents_files_card) — 4 cartes séparées sur
    une même ligne prenaient trop de place pour des paires d'infos
    étroitement liées, demandé par l'utilisateur le 2026-07-30. Un titre de
    carte ("Débits"/"Ratios", pas seulement le libellé de chaque jauge) et
    les 2 colonnes étalées sur toute la largeur de la carte (pas resserrées
    au centre) — même traitement que render_torrents_files_card."""
    return render("dual-gauge-card.html", title=title, columns="\n".join(columns))


def render_ratio_gauge_column(label, value_human, r):
    icon = zone_gauge_svg(ratio_pct(r), RATIO_ZONE_PCT, RATIO_ZONE_CLASSES)
    return render_gauge_column(icon, value_human, label)


def render_speed_gauge_column(label, value, value_human, max_value):
    return render_gauge_column(gauge_svg(speed_pct(value, max_value)), value_human, label)


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
    s'affiche quand même, voir render_disk_card)."""
    if not path:
        return None
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    used_pct = usage.used / usage.total * 100
    return render(
        "disk-row.html", label=label, fill_class=f"meter-fill-{disk_fill_zone(used_pct)}",
        pct=round(used_pct, 1), value=f"{human_size_rounded(usage.free)} restant",
    )


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
    return render("disk-card.html", title="Disques", rows="\n".join(rows))


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
    ("Nextcloud (cron.php)", "nextcloud-cron", 5 * 60, ["nextcloud/app"]),
    ("Rafraîchissement dashboard", "dashboard-refresh", 5 * 60, []),
    ("Recyclarr + overrides arr", "arr-overrides", 24 * 3600, ["arr/sonarr", "arr/radarr"]),
    ("Recherche des manquants", "search-missing", 7 * 24 * 3600, ["arr/sonarr", "arr/radarr"]),
]


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
    ))

    for label, marker_name, interval_seconds, required in SCHEDULED_TASKS:
        if any(svc not in running for svc in required):
            continue
        age_seconds = cron_marker_age_seconds(data_root, marker_name)
        items.append((label, age_seconds is not None and age_seconds <= interval_seconds * CRON_MARKER_SLACK))

    lis = "\n".join(
        # Glyphe + texte caché en plus de la pastille : l'état ne reposait QUE
        # sur la couleur (WCAG 1.4.1), donc invisible pour un daltonien deutan
        # ou protan — ~8 % des hommes — alors que c'est la seule information de
        # la carte, et muet pour un lecteur d'écran (1.1.1), qui n'entendait
        # qu'une liste de noms. Le glyphe hérite de la couleur du point.
        '<li><span class="status-dot status-dot-{cls}"></span>'
        '<span class="status-mark status-mark-{cls}" aria-hidden="true">{mark}</span>'
        '{label}<span class="visually-hidden"> — {state}</span></li>'.format(
            cls="good" if ok else "critical",
            mark="✓" if ok else "✕",
            label=html.escape(label),
            state="OK" if ok else "en échec",
        )
        for label, ok in items
    )
    return render("scheduled-tasks-card.html", items=lis)


def render_indexers_card(running):
    """Liste chaque indexeur Prowlarr avec un point coloré par état (voir
    prowlarr_indexer_health()), plutôt qu'un simple compte agrégé — permet
    de voir directement LEQUEL est en échec sans changer d'écran. Placeholder
    si arr/prowlarr est arrêté (pas de docker exec tenté pour rien) ; None
    (carte omise, comportement best-effort inchangé) si Prowlarr tourne mais
    reste injoignable/clé API absente pour une autre raison."""
    if "arr/prowlarr" not in running:
        return render_stat_placeholder("Indexeurs")
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
    return render("indexers-card.html", items=items)


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

    cards = []
    tracker_card = ""

    if not vpn_running:
        cards += [
            render_stat_placeholder("Débits", "stat-span-2"),
            render_stat_placeholder("Ratios", "stat-span-2"),
            render_stat_placeholder("Torrents", "stat-span-2"),
        ]
        tracker_card = render_stat_placeholder("Ratio par tracker", "stat-span-3")
    elif stats:
        cards += [
            render_dual_gauge_card("Débits", [
                render_speed_gauge_column("Descendant", stats["download_speed"],
                                          stats["download_speed_human"], stats["speed_scale"]["download_max"]),
                render_speed_gauge_column("Montant", stats["upload_speed"],
                                          stats["upload_speed_human"], stats["speed_scale"]["upload_max"]),
            ]),
            render_dual_gauge_card("Ratios", [
                render_ratio_gauge_column("Total", stats["total"]["ratio_display"], stats["total"]["ratio"]),
                render_ratio_gauge_column(f"Session ({stats['session']['uptime_human']})",
                                          stats["session"]["ratio_display"], stats["session"]["ratio"]),
            ]),
        ]
        if stats.get("trackers"):
            rows = "\n".join(render_tracker_row(t) for t in stats["trackers"])
            tracker_card = render("tracker-card.html", rows=rows)

        # Torrents (actifs/en pause/en erreur) et Fichiers (absents/en
        # bibliothèque/en cross-seed — mêmes marqueurs BIB/ABS que le service
        # clearr, arr/clearr/app/core.py, voir CLAUDE.md) fusionnés en une seule carte à
        # 2 colonnes (voir render_torrents_files_card) — 2 cartes séparées
        # avaient été essayées d'abord, jugées trop encombrantes côte à côte
        # par l'utilisateur.
        cards.append(render_torrents_files_card(stats))

    indexers_card = render_indexers_card(running)
    if indexers_card:
        cards.append(indexers_card)

    disk_card = render_disk_card(data_root, backup_dir)
    if disk_card:
        cards.append(disk_card)

    # Carte tracker (span 3) placée juste avant "Tâches planifiées" (span 1) :
    # les cartes précédentes remplissent exactement les rangées précédentes
    # (4 slots chacune), donc ces deux-là tombent naturellement sur la même
    # rangée, tracker en 1re position et tâches planifiées en 2e — demandé
    # par l'utilisateur le 2026-07-30, voir CLAUDE.md.
    if tracker_card:
        cards.append(tracker_card)

    scheduled_tasks_card = render_scheduled_tasks_card(data_root, running)
    if scheduled_tasks_card:
        cards.append(scheduled_tasks_card)

    if not cards:
        return ""

    return render(
        "section-transmission.html",
        stats_flow=render("stats-flow.html", cards="\n".join(cards)),
    )


def copy_assets():
    logos_out = OUT_DIR / "assets" / "logos"
    logos_out.mkdir(parents=True, exist_ok=True)
    # *.svg et *.png (clearr.png, ajouté le 2026-08-01 — avant ça, seul le
    # glob *.svg existait, donc un logo PNG n'était jamais copié dans le
    # dossier servi : carré d'image cassée sur le dashboard malgré un
    # <img src> correct dans le HTML généré). On vide d'abord logos_out : un
    # ancien fichier resté d'un format précédent (ex. clearr.svg après son
    # remplacement par clearr.png) ne doit pas traîner indéfiniment.
    for stale in logos_out.iterdir():
        stale.unlink()
    for logo in list((ASSETS_SRC / "logos").glob("*.svg")) + list((ASSETS_SRC / "logos").glob("*.png")):
        shutil.copy(logo, logos_out / logo.name)
    shutil.copy(ASSETS_SRC / "dashboard.css", OUT_DIR / "assets" / "dashboard.css")
    shutil.copy(ASSETS_SRC / "dashboard.js", OUT_DIR / "assets" / "dashboard.js")
    shutil.copy(ASSETS_SRC / "robots.txt", OUT_DIR / "robots.txt")
    shutil.copy(ASSETS_SRC / "favicon.png", OUT_DIR / "assets" / "favicon.png")
    shutil.copy(ASSETS_SRC / "favicon.ico", OUT_DIR / "favicon.ico")


def main():
    env_shared = load_env_file(REPO_ROOT / ".env.shared")
    domain = env_shared.get("DOMAIN", "")
    running = docker_ps_set()
    unhealthy = docker_ps_set(["health=unhealthy"])

    public_cards, local_cards, down_cards = build_cards(running, unhealthy)

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
    # le bandeau « ouvert au WAN » doit être fiable.
    tmp_file = out_file.with_suffix(".html.tmp")
    tmp_file.write_text(page)
    os.replace(tmp_file, out_file)
    print(f"dashboard régénéré : {out_file}")


if __name__ == "__main__":
    main()
