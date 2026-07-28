#!/usr/bin/env bash
# Régénère dashboard/html/index.html à partir des labels Traefik réels des
# stacks (docker compose config --format json) et de l'état d'exécution
# courant (docker ps) — voir CLAUDE.md. Rien n'est codé en dur côté domaine :
# si `Host()`, le middleware ipallowlist ou l'état running/stopped changent,
# `make dashboard-refresh` reflète l'état réel sans retoucher ce script.
#
# Seule metadata non dérivable des compose files : le nom affiché et le logo
# (dashboard/assets/logos/) associés à chaque "stack/service" — ajoutés à la
# main ci-dessous quand un nouveau service exposé via Traefik apparaît.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a
source "$REPO_ROOT/.env.shared"
set +a

STACKS="jellyfin nextcloud vpn arr seerr"
OUT_DIR="$REPO_ROOT/dashboard/html"
OUT_FILE="$OUT_DIR/index.html"
LOGOS_SRC="$REPO_ROOT/dashboard/assets/logos"

compose_for() {
	local stack="$1"; shift
	local args=(--env-file "$REPO_ROOT/.env.shared")
	[ -f "$REPO_ROOT/$stack/.env" ] && args+=(--env-file "$REPO_ROOT/$stack/.env")
	args+=(-f "$REPO_ROOT/$stack/docker-compose.yml")
	[ -f "$REPO_ROOT/$stack/docker-compose.override.yml" ] && args+=(-f "$REPO_ROOT/$stack/docker-compose.override.yml")
	docker compose "${args[@]}" "$@"
}

# nom affiché + logo (dashboard/assets/logos/*.svg) par "stack/service"
declare -A DISPLAY_NAME=(
	[jellyfin/jellyfin]="Jellyfin"
	[nextcloud/web]="Nextcloud"
	[vpn/transmission-proxy]="Transmission"
	[arr/prowlarr]="Prowlarr"
	[arr/sonarr]="Sonarr"
	[arr/radarr]="Radarr"
	[seerr/seerr]="Seerr"
)
declare -A LOGO_FILE=(
	[jellyfin/jellyfin]="jellyfin.svg"
	[nextcloud/web]="nextcloud.svg"
	[vpn/transmission-proxy]="transmission.svg"
	[arr/prowlarr]="prowlarr.svg"
	[arr/sonarr]="sonarr.svg"
	[arr/radarr]="radarr.svg"
	[seerr/seerr]="seerr.svg"
)

# chemin d'une image réelle (200, Content-Type image/*) servie par chaque
# service LAN-only, utilisée par le probing JS pour détecter un blocage
# ipallowlist (403) côté WAN — voir card_html(). /favicon.ico par défaut ;
# transmission-proxy redirige /favicon.ico vers /transmission/web/ (du HTML,
# pas une image), d'où l'override ci-dessous (vérifié le 2026-07-24).
declare -A PROBE_PATH=(
	[vpn/transmission-proxy]="/transmission/web/images/favicon.ico"
)

# services actuellement démarrés, sous forme "projet/service"
declare -A RUNNING
while IFS= read -r line; do
	[ -n "$line" ] && RUNNING["$line"]=1
done < <(docker ps --filter status=running --format '{{.Label "com.docker.compose.project"}}/{{.Label "com.docker.compose.service"}}')

# services avec un healthcheck en échec ("Up (unhealthy)") — voir CLAUDE.md.
# Les services sans healthcheck défini (health=none) n'apparaissent jamais
# ici, ce n'est pas une régression : on n'affiche que ce que Docker sait dire.
declare -A UNHEALTHY
while IFS= read -r line; do
	[ -n "$line" ] && UNHEALTHY["$line"]=1
done < <(docker ps --filter status=running --filter health=unhealthy --format '{{.Label "com.docker.compose.project"}}/{{.Label "com.docker.compose.service"}}')

JQ_PROGRAM='
  (.services // {}) | to_entries[]
  | select((.value.labels // {})["traefik.enable"] == "true")
  | . as $e
  | ($e.value.labels) as $l
  | (first($l | keys[] | select(test("^traefik\\.http\\.routers\\.[^.]+\\.rule$")))) as $rulekey
  | ($rulekey | capture("^traefik\\.http\\.routers\\.(?<r>[^.]+)\\.rule$").r) as $router
  | ($l[$rulekey] | capture("Host\\(`(?<h>[^`]+)`\\)").h) as $host
  | ($l["traefik.http.routers.\($router).middlewares"] // "") as $mws
  | ([$mws | split(",")[] | select(length>0) | gsub("@.*$";"")]) as $mwlist
  | (([$mwlist[] | select($l["traefik.http.middlewares.\(.).ipallowlist.sourcerange"] != null)] | length) > 0) as $lan
  | {service: $e.key, host: $host, lan: $lan}
'

public_cards=""
local_cards=""
down_cards=""

card_html() {
	local key="$1" host="$2" clickable="$3" probe="${4:-}" unhealthy="${5:-0}"
	local name="${DISPLAY_NAME[$key]:-$key}"
	local logo="${LOGO_FILE[$key]:-}"
	local logo_class="logo"
	local warning=""
	[ "$unhealthy" = "1" ] && logo_class="logo logo-unhealthy" && warning='<span class="warning">⚠ healthcheck en échec</span>'
	local img=""
	[ -n "$logo" ] && img="<img src=\"/assets/logos/$logo\" alt=\"\" class=\"$logo_class\">"
	if [ "$clickable" = "1" ]; then
		local probe_attr=""
		[ -n "$probe" ] && probe_attr=" data-probe=\"https://$host$probe\""
		printf '<a class="card" href="https://%s"%s>%s%s<span class="name">%s</span><span class="host">%s</span></a>\n' \
			"$host" "$probe_attr" "$img" "$warning" "$name" "$host"
	else
		printf '<div class="card card-down">%s<span class="name">%s</span><span class="host">arrêté</span></div>\n' \
			"$img" "$name"
	fi
}

for stack in $STACKS; do
	while IFS= read -r row; do
		[ -z "$row" ] && continue
		service="$(jq -r '.service' <<<"$row")"
		host="$(jq -r '.host' <<<"$row")"
		lan="$(jq -r '.lan' <<<"$row")"
		key="$stack/$service"
		unhealthy=0
		[ -n "${UNHEALTHY[$key]:-}" ] && unhealthy=1
		if [ -n "${RUNNING[$key]:-}" ]; then
			if [ "$lan" = "true" ]; then
				local_cards+="$(card_html "$key" "$host" 1 "${PROBE_PATH[$key]:-/favicon.ico}" "$unhealthy")"
			else
				public_cards+="$(card_html "$key" "$host" 1 "" "$unhealthy")"
			fi
		else
			down_cards+="$(card_html "$key" "$host" 0)"
		fi
	done < <(compose_for "$stack" config --format json 2>/dev/null | jq -c "$JQ_PROGRAM")
done

# Statistiques Transmission (ratios/débits) — voir scripts/transmission-stats.py.
# Contrairement aux cartes de service ci-dessus, cette section est visible
# WAN et LAN (pas de gating card-lan-blocked) : ce sont des chiffres agrégés,
# pas un accès de contrôle au client — décision explicite de l'utilisateur.
# Snapshot pris au moment de la génération (cron toutes les 5 min, comme le
# reste du dashboard), pas de rafraîchissement live. N'est calculée que si la
# stack vpn (transmission-vpn) tourne — sinon la section entière est omise
# plutôt que d'afficher un message "indisponible" : voir CLAUDE.md.
transmission_stats_html=""
tracker_rows=""
if [ -n "${RUNNING[vpn/transmission-vpn]:-}" ] \
	&& stats_json="$(python3 "$REPO_ROOT/scripts/transmission-stats.py" 2>/dev/null)" \
	&& [ -n "$stats_json" ] && [ "$(jq -r 'has("error")' <<<"$stats_json")" = "false" ]; then
	tracker_rows="$(jq -r '.trackers[]
		| "<tr><td>\(.name)</td><td>\(.ratio_display)</td><td>\(.uploaded_human)</td><td>\(.downloaded_human)</td></tr>"
	' <<<"$stats_json")"
	transmission_stats_html="$(jq -r '
		"<div class=\"stats-grid\">"
		+ "<div class=\"stat\"><span class=\"stat-value\">" + .total.ratio_display + "</span><span class=\"stat-label\">Ratio total</span></div>"
		+ "<div class=\"stat\"><span class=\"stat-value\">" + .session.ratio_display + "</span><span class=\"stat-label\">Ratio session (" + .session.uptime_human + ")</span></div>"
		+ (if .day then "<div class=\"stat\"><span class=\"stat-value\">" + .day.ratio_display + "</span><span class=\"stat-label\">" + .day.label + "</span></div>" else "" end)
		+ "<div class=\"stat\"><span class=\"stat-value\">↓ " + .download_speed_human + "</span><span class=\"stat-label\">Débit descendant</span></div>"
		+ "<div class=\"stat\"><span class=\"stat-value\">↑ " + .upload_speed_human + "</span><span class=\"stat-label\">Débit montant</span></div>"
		+ "</div>"
	' <<<"$stats_json")"
fi

# Chaque section n'est incluse que si elle a du contenu — pas de placeholder
# "aucun"/"indisponible" pour une section vide (demandé le 2026-07-28).
public_section=""
if [ -n "$public_cards" ]; then
	public_section="$(cat <<SECTION
  <section>
    <h2>Public</h2>
    <div class="grid">${public_cards}</div>
  </section>
SECTION
)"
fi

local_section=""
if [ -n "$local_cards" ]; then
	local_section="$(cat <<SECTION
  <section>
    <h2>Local (LAN)</h2>
    <div class="grid">${local_cards}</div>
  </section>
SECTION
)"
fi

down_section=""
if [ -n "$down_cards" ]; then
	down_section="$(cat <<SECTION
  <section>
    <h2>Stack non lancée</h2>
    <div class="grid">${down_cards}</div>
  </section>
SECTION
)"
fi

transmission_section=""
if [ -n "$transmission_stats_html" ]; then
	transmission_section="$(cat <<SECTION
  <section>
    <h2>Transmission — ratios &amp; débits</h2>
    ${transmission_stats_html}
    ${tracker_rows:+<table class=\"tracker-table\"><thead><tr><th>Tracker</th><th>Ratio</th><th>Envoyé</th><th>Reçu</th></tr></thead><tbody>$tracker_rows</tbody></table>}
    <p class="note">Ratio calculé côté client Transmission — peut différer du ratio réel compté par chaque tracker.</p>
  </section>
SECTION
)"
fi

mkdir -p "$OUT_DIR/assets/logos"
cp "$LOGOS_SRC"/*.svg "$OUT_DIR/assets/logos/"
cp "$REPO_ROOT/dashboard/assets/robots.txt" "$OUT_DIR/robots.txt"
cp "$REPO_ROOT/dashboard/assets/favicon.png" "$OUT_DIR/assets/favicon.png"
cp "$REPO_ROOT/dashboard/assets/favicon.ico" "$OUT_DIR/favicon.ico"

cat >"$OUT_FILE" <<HTML
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow, noarchive">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/png" href="/assets/favicon.png">
<title>Services — ${DOMAIN}</title>
<style>
  :root { color-scheme: light dark; }
  body {
    margin: 0; padding: 2.5rem 1.5rem; font-family: system-ui, sans-serif;
    background: #f4f4f5; color: #18181b;
  }
  @media (prefers-color-scheme: dark) {
    body { background: #18181b; color: #f4f4f5; }
  }
  h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
  .updated { font-size: .85rem; opacity: .6; margin: 0 0 2rem; }
  section { max-width: 960px; margin: 0 auto 2.5rem; }
  section h2 {
    font-size: .8rem; text-transform: uppercase; letter-spacing: .08em;
    opacity: .6; margin: 0 0 .75rem;
  }
  .grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 1rem;
  }
  .card {
    display: flex; flex-direction: column; align-items: center; gap: .4rem;
    padding: 1.25rem 1rem; border-radius: 12px; text-decoration: none;
    color: inherit; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.1);
    transition: transform .1s ease;
  }
  @media (prefers-color-scheme: dark) {
    .card { background: #27272a; box-shadow: none; }
  }
  a.card:hover { transform: translateY(-2px); }
  .card-down { opacity: .45; }
  .card-lan-blocked { opacity: .45; pointer-events: none; }
  .logo { width: 40px; height: 40px; object-fit: contain; }
  .logo-unhealthy {
    outline: 3px solid #ef4444; outline-offset: 3px; border-radius: 8px;
  }
  .warning {
    font-size: .7rem; font-weight: 600; color: #ef4444; text-align: center;
  }
  .name { font-weight: 600; font-size: .95rem; }
  .host { font-size: .75rem; opacity: .6; word-break: break-all; text-align: center; }
  .stats-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 1rem; margin-bottom: 1rem;
  }
  .stat {
    display: flex; flex-direction: column; align-items: center; gap: .3rem;
    padding: 1rem; border-radius: 12px; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.1);
  }
  @media (prefers-color-scheme: dark) {
    .stat { background: #27272a; box-shadow: none; }
  }
  .stat-value { font-weight: 700; font-size: 1.3rem; }
  .stat-label { font-size: .75rem; opacity: .6; text-align: center; }
  .tracker-table {
    width: 100%; border-collapse: collapse; background: #fff; border-radius: 12px;
    overflow: hidden; font-size: .85rem;
  }
  @media (prefers-color-scheme: dark) {
    .tracker-table { background: #27272a; }
  }
  .tracker-table th, .tracker-table td { padding: .5rem .75rem; text-align: left; }
  .tracker-table th { font-size: .7rem; text-transform: uppercase; opacity: .6; }
  .tracker-table tr:nth-child(even) { background: rgba(127,127,127,.08); }
  .note { font-size: .75rem; opacity: .5; margin: .75rem 0 0; }
</style>
</head>
<body>
  <h1>Services</h1>
  <p class="updated">Généré le $(date '+%Y-%m-%d %H:%M') — make dashboard-refresh</p>

${public_section}
${local_section}
${down_section}
${transmission_section}

  <script>
    // Grise les cartes LAN-only quand l'appelant est sur le WAN : leur
    // ipallowlist Traefik répond 403 à la sonde ci-dessous, ce que <img>
    // distingue d'un chargement réussi sans dépendre de CORS.
    document.querySelectorAll('.card[data-probe]').forEach(function (card) {
      var probe = new Image();
      probe.onerror = function () {
        card.classList.add('card-lan-blocked');
        card.removeAttribute('href');
        var host = card.querySelector('.host');
        if (host) { host.textContent = 'accessible en LAN uniquement'; }
      };
      probe.src = card.dataset.probe + '?t=' + Date.now();
    });
  </script>
</body>
</html>
HTML

echo "dashboard régénéré : $OUT_FILE"
