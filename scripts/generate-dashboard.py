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
# torrent-cleanup.py) — et ça fait sortir jq du chemin de génération.
#
# Seule metadata non dérivable des compose files : le nom affiché et le logo
# (dashboard/assets/logos/) associés à chaque "stack/service" — ajoutés à la
# main ci-dessous quand un nouveau service exposé via Traefik apparaît.
import html
import json
import math
import os
import re
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
    "seerr/seerr": "Seerr",
}
LOGO_FILE = {
    "jellyfin/jellyfin": "jellyfin.svg",
    "nextcloud/web": "nextcloud.svg",
    "vpn/transmission-proxy": "transmission.svg",
    "arr/prowlarr": "prowlarr.svg",
    "arr/sonarr": "sonarr.svg",
    "arr/radarr": "radarr.svg",
    "seerr/seerr": "seerr.svg",
}
# chemin d'une image réelle (200, Content-Type image/*) servie par chaque
# service LAN-only, utilisée par dashboard.js pour détecter un blocage
# ipallowlist (403) côté WAN — voir render_card(). /favicon.ico par défaut ;
# transmission-proxy redirige /favicon.ico vers /transmission/web/ (du HTML,
# pas une image), d'où l'override ci-dessous (vérifié le 2026-07-24).
PROBE_PATH = {
    "vpn/transmission-proxy": "/transmission/web/images/favicon.ico",
}

RULE_KEY_RE = re.compile(r"^traefik\.http\.routers\.([^.]+)\.rule$")
HOST_RE = re.compile(r"Host\(`([^`]+)`\)")

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


def docker_ps_set(extra_filters=()):
    args = ["docker", "ps", "--filter", "status=running"]
    for f in extra_filters:
        args += ["--filter", f]
    args += ["--format", '{{.Label "com.docker.compose.project"}}/{{.Label "com.docker.compose.service"}}']
    res = subprocess.run(args, capture_output=True, text=True)
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
    res = subprocess.run(args, capture_output=True, text=True)
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
        indexers_res = subprocess.run(
            ["docker", "exec", PROWLARR_CONTAINER, "curl", "-s",
             "-H", f"X-Api-Key: {api_key}", f"{PROWLARR_URL}/indexer"],
            capture_output=True, text=True, timeout=15,
        )
        status_res = subprocess.run(
            ["docker", "exec", PROWLARR_CONTAINER, "curl", "-s",
             "-H", f"X-Api-Key: {api_key}", f"{PROWLARR_URL}/indexerstatus"],
            capture_output=True, text=True, timeout=15,
        )
        indexers = json.loads(indexers_res.stdout)
        failing = json.loads(status_res.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    failing_ids = {f.get("indexerId") for f in failing}
    return sorted(
        ({"name": i.get("name", "?"), "ok": i.get("id") not in failing_ids} for i in indexers),
        key=lambda e: e["name"].lower(),
    )


def extract_traefik_services(config):
    """Reproduit la logique du JQ_PROGRAM d'origine : premier router (par
    ordre alphabétique de clé de label, comme jq `keys`) exposant une règle
    Host(`...`), LAN si un de ses middlewares porte un ipallowlist."""
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
        lan = any(
            labels.get(f"traefik.http.middlewares.{mw}.ipallowlist.sourcerange") is not None
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
    for stack in STACKS:
        config = docker_compose_config(stack)
        for service, host, lan in extract_traefik_services(config):
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


def render_stat_card(icon, value, label, value_class=""):
    return render("stat-card.html", icon=icon, value=value, label=label, value_class=value_class)


def render_stat_item(value, label, value_class=""):
    return render("stat-multi-item.html", value=value, label=label, value_class=value_class)


def render_multi_stat_column(title, items):
    return render("multi-stat-column.html", title=title, items="\n".join(items))


def render_torrents_files_card(stats):
    """Torrents (Actifs/En pause/En erreur) et Fichiers (Absents/En
    bibliothèque/En cross-seed) fusionnés en une seule carte à 2 colonnes,
    qui prend la largeur de 2 cartes normales (voir .stat-span-2) — demandé
    par l'utilisateur le 2026-07-29 : 2 cartes séparées côte à côte prenaient
    trop de place pour des infos étroitement liées (l'une compte les
    torrents, l'autre leurs fichiers sur disque)."""
    torrents_col = render_multi_stat_column("Torrents", [
        render_stat_item(str(stats["torrents_active"]), "Actifs"),
        render_stat_item(str(stats["torrents_paused"]), "En pause"),
        render_stat_item(
            str(stats["torrents_errored"]), "En erreur",
            value_class="stat-value-critical" if stats["torrents_errored"] else "stat-value-good",
        ),
    ])
    files_col = render_multi_stat_column("Fichiers", [
        render_stat_item(
            str(stats["torrents_missing"]), "Absents",
            value_class="stat-value-critical" if stats["torrents_missing"] else "stat-value-good",
        ),
        render_stat_item(str(stats["torrents_linked"]), "En bibliothèque"),
        render_stat_item(str(stats["torrents_cross_seed"]), "En cross-seed"),
    ])
    return render("multi-stat-columns-card.html", columns=torrents_col + "\n" + files_col)


def render_ratio_card(label, value_human, r):
    icon = zone_gauge_svg(ratio_pct(r), RATIO_ZONE_PCT, RATIO_ZONE_CLASSES)
    return render_stat_card(icon, value_human, label)


def render_speed_card(label, value, value_human, max_value):
    return render_stat_card(gauge_svg(speed_pct(value, max_value)), value_human, label)


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
RESTIC_REPO_DIR = REPO_ROOT / "sauvegarde" / "restic-repo"
RESTIC_PASSWORD_FILE = REPO_ROOT / "sauvegarde" / "restic-password"


def last_backup_age_days():
    """Âge (jours, flottant) du snapshot restic le plus récent — appelle le
    binaire restic en direct sur l'hôte (comme scripts/backup.sh, pas de
    docker exec ici : restic ne tourne dans aucun conteneur). Best-effort :
    None si restic/le mot de passe/le dépôt est absent ou injoignable, pour
    omettre la carte plutôt que planter la génération du dashboard."""
    if not RESTIC_PASSWORD_FILE.exists():
        return None
    env = {**os.environ, "RESTIC_REPOSITORY": str(RESTIC_REPO_DIR),
           "RESTIC_PASSWORD_FILE": str(RESTIC_PASSWORD_FILE)}
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


def render_backup_age_card():
    age_days = last_backup_age_days()
    if age_days is None:
        return None
    critical = age_days > BACKUP_MAX_AGE_DAYS
    value = "auj." if age_days < 1 else f"{int(age_days)}j"
    return render_stat_card("", value, "Dernière sauvegarde",
                             value_class="stat-value-critical" if critical else "stat-value-good")


def render_indexers_card():
    """Liste chaque indexeur Prowlarr avec un point coloré par état (voir
    prowlarr_indexer_health()), plutôt qu'un simple compte agrégé — permet
    de voir directement LEQUEL est en échec sans changer d'écran. None si
    Prowlarr est injoignable/clé API absente."""
    indexers = prowlarr_indexer_health()
    if indexers is None:
        return None
    items = "\n".join(
        '<li><span class="indexer-dot indexer-dot-{}"></span>{}</li>'.format(
            "good" if i["ok"] else "critical", html.escape(i["name"]),
        )
        for i in indexers
    )
    return render("indexers-card.html", items=items)


def render_tracker_row(tracker):
    meter = render(
        "mini-meter.html", value=tracker["ratio_display"],
        fill_class=f"meter-fill-{ratio_zone(tracker['ratio'])}", pct=ratio_pct(tracker["ratio"]),
    )
    return render(
        "tracker-row.html", name=tracker["name"], meter=meter,
        uploaded=tracker["uploaded_human"], downloaded=tracker["downloaded_human"],
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
    espace disque, dernière sauvegarde et indexeurs Prowlarr ont chacun leur
    propre disponibilité (best-effort indépendant, voir
    render_disk_card()/render_backup_age_card()/render_indexers_card()).
    Toute la section est omise seulement si aucune carte n'est disponible."""
    stats = fetch_transmission_stats(running)

    cards = []
    tracker_details = ""

    if stats:
        cards += [
            render_speed_card("Débit descendant", stats["download_speed"], stats["download_speed_human"],
                              stats["speed_scale"]["download_max"]),
            render_speed_card("Débit montant", stats["upload_speed"], stats["upload_speed_human"],
                              stats["speed_scale"]["upload_max"]),
            render_ratio_card("Ratio total", stats["total"]["ratio_display"], stats["total"]["ratio"]),
            render_ratio_card(f"Ratio session ({stats['session']['uptime_human']})",
                              stats["session"]["ratio_display"], stats["session"]["ratio"]),
        ]
        if stats.get("trackers"):
            rows = "\n".join(render_tracker_row(t) for t in stats["trackers"])
            tracker_details = render("tracker-details.html", rows=rows)

        # Torrents (actifs/en pause/en erreur) et Fichiers (absents/en
        # bibliothèque/en cross-seed — mêmes marqueurs BIB/ABS que
        # torrent-cleanup.py, voir CLAUDE.md) fusionnés en une seule carte à
        # 2 colonnes (voir render_torrents_files_card) — 2 cartes séparées
        # avaient été essayées d'abord, jugées trop encombrantes côte à côte
        # par l'utilisateur.
        cards.append(render_torrents_files_card(stats))

    indexers_card = render_indexers_card()
    if indexers_card:
        cards.append(indexers_card)

    disk_card = render_disk_card(data_root, backup_dir)
    if disk_card:
        cards.append(disk_card)

    backup_age_card = render_backup_age_card()
    if backup_age_card:
        cards.append(backup_age_card)

    if not cards:
        return ""

    return render(
        "section-transmission.html",
        stats_flow=render("stats-flow.html", cards="\n".join(cards)),
        tracker_details=tracker_details,
    )


def copy_assets():
    logos_out = OUT_DIR / "assets" / "logos"
    logos_out.mkdir(parents=True, exist_ok=True)
    for svg in (ASSETS_SRC / "logos").glob("*.svg"):
        shutil.copy(svg, logos_out / svg.name)
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
        generated_ms=str(int(now.timestamp() * 1000)),
        sections="\n".join(sections),
    )
    out_file = OUT_DIR / "index.html"
    out_file.write_text(page)
    print(f"dashboard régénéré : {out_file}")


if __name__ == "__main__":
    main()
