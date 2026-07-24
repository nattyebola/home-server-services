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
	local key="$1" host="$2" clickable="$3" probe="${4:-}"
	local name="${DISPLAY_NAME[$key]:-$key}"
	local logo="${LOGO_FILE[$key]:-}"
	local img=""
	[ -n "$logo" ] && img="<img src=\"/assets/logos/$logo\" alt=\"\" class=\"logo\">"
	if [ "$clickable" = "1" ]; then
		local probe_attr=""
		[ -n "$probe" ] && probe_attr=" data-probe=\"https://$host$probe\""
		printf '<a class="card" href="https://%s"%s>%s<span class="name">%s</span><span class="host">%s</span></a>\n' \
			"$host" "$probe_attr" "$img" "$name" "$host"
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
		if [ -n "${RUNNING[$key]:-}" ]; then
			if [ "$lan" = "true" ]; then
				local_cards+="$(card_html "$key" "$host" 1 "${PROBE_PATH[$key]:-/favicon.ico}")"
			else
				public_cards+="$(card_html "$key" "$host" 1)"
			fi
		else
			down_cards+="$(card_html "$key" "$host" 0)"
		fi
	done < <(compose_for "$stack" config --format json 2>/dev/null | jq -c "$JQ_PROGRAM")
done

mkdir -p "$OUT_DIR/assets/logos"
cp "$LOGOS_SRC"/*.svg "$OUT_DIR/assets/logos/"
cp "$REPO_ROOT/dashboard/assets/robots.txt" "$OUT_DIR/robots.txt"

cat >"$OUT_FILE" <<HTML
<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow, noarchive">
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
  .name { font-weight: 600; font-size: .95rem; }
  .host { font-size: .75rem; opacity: .6; word-break: break-all; text-align: center; }
  .empty { opacity: .5; font-size: .9rem; }
</style>
</head>
<body>
  <h1>Services</h1>
  <p class="updated">Généré le $(date '+%Y-%m-%d %H:%M') — make dashboard-refresh</p>

  <section>
    <h2>Public</h2>
    <div class="grid">${public_cards:-<span class=\"empty\">aucun</span>}</div>
  </section>

  <section>
    <h2>Local (LAN)</h2>
    <div class="grid">${local_cards:-<span class=\"empty\">aucun</span>}</div>
  </section>

  <section>
    <h2>Stack non lancée</h2>
    <div class="grid">${down_cards:-<span class=\"empty\">aucune</span>}</div>
  </section>

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
