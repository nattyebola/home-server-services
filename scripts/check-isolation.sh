#!/usr/bin/env bash
# Preuve d'isolement réseau (make check-isolation). Lecture seule.
#
# Ce qui rend sûr `authenticationRequired: disabledForLocalAddresses` sur les
# arr (pas de login depuis le LAN, 2026-11-16) et l'absence d'authentification
# de clearr, c'est qu'aucun chemin ne les atteint sans passer par l'ipAllowList
# de Traefik, sauf depuis le LAN. Ce script le vérifie de bout en bout :
#
#   1. Internet -> IP publique de la maison : seuls 22/80/443 répondent.
#   2. Internet -> vhosts LAN-only : 403, même avec un X-Forwarded-For forgé.
#   3. Internet -> IP de sortie AirVPN : seul le port pair BitTorrent répond.
#   4. Conteneurs exposés (client BitTorrent, webproxy, services WAN) ->
#      arr et clearr : aucune connexion possible, sur aucune de leurs IP.
#      Seule exception : seerr -> sonarr/radarr par leur IP sur seerr-arr.
#
# Le sondeur WAN est vpn-transmission-vpn-1 : il sort par AirVPN, donc c'est un
# vrai client Internet. Ne JAMAIS sonder l'IP publique depuis l'hôte :
# /etc/hosts y fait pointer le domaine sur l'IP LAN, tout paraîtrait ouvert.
# L'IP publique est lue sur 1.1.1.1 pour la même raison.
#
# Sortie : uniquement les échecs, puis un bilan (-v : toutes les lignes).
# Code de retour 1 au moindre échec, 2 si la sonde elle-même est inutilisable.
set -u
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
# shellcheck disable=SC1091
source .env.shared
VERBOSE=0; [ "${1:-}" = "-v" ] && VERBOSE=1
VPN=vpn-transmission-vpn-1
# Une adresse DANS le LAN, pour forger des en-têtes crédibles.
LAN_IP=$(python3 -c "import ipaddress,sys; n=ipaddress.ip_network(sys.argv[1], strict=False); print(n[min(50, n.num_addresses-2)])" "$LAN_CIDR")
ok=0; ko=0; skipped=0

# Une stack non déployée ou arrêtée sur cette instance (nextcloud, komga…) n'a
# aucun conteneur pour servir de point d'attaque : ses sondes sont sautées, pas
# comptées en échec. Toujours affiché (même sans -v), pour qu'une stack tombée
# sur l'instance principale ne fasse pas disparaître ses sondes sans bruit.
running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ]; }
skip() { skipped=$((skipped+1)); printf '  IGNORÉ %-55s conteneur absent ou arrêté\n' "$1"; }

res() { # attendu obtenu libellé
  if [ "$1" = "$2" ]; then
    ok=$((ok+1)); [ $VERBOSE = 1 ] && printf '  OK    %-56s %s\n' "$3" "$2"
  else
    ko=$((ko+1)); printf '  ÉCHEC %-56s attendu=%s obtenu=%s\n' "$3" "$1" "${2:-rien}"
  fi
  return 0
}

# probe <conteneur> <ip> <port> -> ouvert|fermé. Prend l'outil que l'image
# embarque (nc, sinon curl, sinon wget). Un conteneur arrêté ne renvoie rien,
# ce qui compte comme un échec : on ne conclut pas sans avoir sondé.
probe() {
  docker exec "$1" sh -c '
    h=$1; p=$2
    if command -v nc >/dev/null; then nc -z -w 3 "$h" "$p" 2>/dev/null && echo ouvert || echo fermé; exit; fi
    if command -v curl >/dev/null; then curl --noproxy "*" -s -o /dev/null --connect-timeout 3 -m 4 "http://$h:$p/"
      case $? in 7|28) echo fermé;; *) echo ouvert;; esac; exit; fi
    wget -q -T 3 -t 1 -O /dev/null "http://$h:$p/" 2>/dev/null; [ $? -eq 4 ] && echo fermé || echo ouvert' _ "$2" "$3" 2>/dev/null
}

# Code HTTP vu par un client WAN : curl depuis le conteneur VPN, sans Privoxy.
wan_http() { docker exec "$VPN" curl --noproxy '*' -s -o /dev/null -w '%{http_code}' -m 8 "$@"; }

PUB=$(dig +short A "$DOMAIN" @1.1.1.1 | tail -1)
EXIT=$(docker exec "$VPN" curl --noproxy '*' -s -m 8 https://api.ipify.org)
if [ -z "$PUB" ] || [ -z "$EXIT" ]; then echo "IP publique ou IP de sortie VPN introuvable" >&2; exit 2; fi
if [ "$PUB" = "$EXIT" ]; then echo "$VPN ne sort pas par le tunnel : sonde WAN invalide" >&2; exit 2; fi

echo "== 1. Internet -> IP publique de la maison (ports TCP)"
for p in 22 80 443; do res ouvert "$(probe "$VPN" "$PUB" $p)" "port $p (redirigé par la box)"; done
for p in 21 23 25 53 445 631 2468 3389 5055 5432 6379 7878 8000 8080 8082 8096 8118 8443 8989 9091 9696 25600 27036 40125; do
  res fermé "$(probe "$VPN" "$PUB" $p)" "port $p"
done

echo "== 2. Internet -> vhosts LAN-only (HTTPS, vrai SNI)"
for v in sonarr radarr prowlarr clearr transmission; do
  for spoof in "" "X-Forwarded-For: $LAN_IP" "X-Real-IP: $LAN_IP"; do
    res 403 "$(wan_http --resolve "$v.$DOMAIN:443:$PUB" ${spoof:+-H "$spoof"} "https://$v.$DOMAIN/")" \
      "$v${spoof:+ (${spoof%%:*} forgé)}"
  done
  res 403 "$(wan_http --resolve "$v.$DOMAIN:443:$PUB" "https://$v.$DOMAIN/initialize.json")" "$v /initialize.json"
done
# Vhost public en SNI, Host LAN-only derrière : ne doit jamais router.
code=$(wan_http -k --resolve "jellyfin.$DOMAIN:443:$PUB" -H "Host: sonarr.$DOMAIN" "https://jellyfin.$DOMAIN/")
case $code in 403|404|421) res bloqué bloqué "SNI jellyfin + Host sonarr";; *) res bloqué "$code" "SNI jellyfin + Host sonarr";; esac
code=$(wan_http -k --resolve "x:443:$PUB" -H "Host: sonarr" "https://x/")
case $code in 403|404|421) res bloqué bloqué "Host nu « sonarr »";; *) res bloqué "$code" "Host nu « sonarr »";; esac

echo "== 3. Internet -> IP de sortie AirVPN (depuis l'hôte, qui sort par la box)"
for p in 22 80 443 8080 8118 9091; do
  timeout 5 bash -c "</dev/tcp/$EXIT/$p" 2>/dev/null && s=ouvert || s=fermé; res fermé "$s" "port $p"
done
peer=$(docker exec "$VPN" sh -c 'echo $TRANSMISSION_PEER_PORT')
timeout 5 bash -c "</dev/tcp/$EXIT/$peer" 2>/dev/null && s=ouvert || s=fermé
res ouvert "$s" "port $peer (pair BitTorrent)"

echo "== 4. Conteneurs exposés -> arr et clearr (toutes leurs IP)"
targets=""
for c in arr-sonarr-1:8989 arr-radarr-1:7878 arr-prowlarr-1:9696 arr-clearr-1:8000; do
  for ip in $(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "${c%:*}"); do
    targets="$targets ${c%:*}@$ip:${c#*:}"
  done
done
# Chaque source a un témoin : une cible qu'elle joint légitimement. Sans lui,
# un « fermé » pourrait venir d'un outil de sonde cassé dans l'image.
ip_on() { docker inspect -f "{{(index .NetworkSettings.Networks \"$2\").IPAddress}}" "$1"; }
for spec in "$VPN:vpn-transmission-proxy-1:vpn_vpn-internal:8080" \
            "vpn-webproxy-1:$VPN:vpn_vpn-internal:9091" \
            "jellyfin-jellyfin-1:traefik-traefik-1:traefik-public:8443" \
            "nextcloud-web-1:traefik-traefik-1:traefik-public:8443" \
            "nextcloud-app-1:nextcloud-bdd-1:nextcloud_default:5432" \
            "komga-komga-1:traefik-traefik-1:traefik-public:8443" \
            "traefik-dashboard-1:traefik-traefik-1:traefik-public:8443" \
            "seerr-seerr-1:arr-sonarr-1:arr_seerr-arr:8989"; do
  IFS=: read -r src wit net port <<<"$spec"
  # Le VPN est la sonde elle-même : déjà exigé plus haut (sortie 2), jamais sauté.
  if ! running "$src"; then skip "$src (source)"; continue; fi
  res ouvert "$(probe "$src" "$(ip_on "$wit" "$net")" "$port")" "$src -> témoin ${wit%-1}:$port"
  # Seerr pilote sonarr/radarr (il a leurs clés) : leur IP sur seerr-arr est
  # légitime. Toutes leurs autres IP, Prowlarr et clearr restent à « fermé ».
  allow=" "
  if [ "$src" = seerr-seerr-1 ]; then
    allow=" $(ip_on arr-sonarr-1 arr_seerr-arr) $(ip_on arr-radarr-1 arr_seerr-arr) "
  fi
  for t in $targets; do
    a=${t#*@}
    case "$allow" in *" ${a%:*} "*) continue;; esac
    res fermé "$(probe "$src" "${a%:*}" "${a#*:}")" "$src -> ${t%@*} $a"
  done
done
# Le conteneur VPN joint l'hôte par sa passerelle Docker : Traefik doit l'y
# traiter en inconnu (IP hors LAN_CIDR).
GW=$(docker exec "$VPN" ip route | awk '/^default/{print $3}')
for v in sonarr radarr prowlarr clearr transmission; do
  res 403 "$(docker exec "$VPN" curl --noproxy '*' -s -o /dev/null -w '%{http_code}' -m 6 \
    --resolve "$v.$DOMAIN:443:$GW" "https://$v.$DOMAIN/")" "$VPN -> hôte:443 Host $v"
done
# Voisin restant du client BitTorrent : cross-seed, protégé par sa clé.
if running arr-cross-seed-1; then
  XS=$(docker inspect -f '{{(index .NetworkSettings.Networks "vpn_vpn-internal").IPAddress}}' arr-cross-seed-1)
  res 401 "$(docker exec "$VPN" curl --noproxy '*' -s -o /dev/null -w '%{http_code}' -m 4 -X POST "http://$XS:2468/api/webhook")" \
    "$VPN -> cross-seed sans clé"
else skip "$VPN -> cross-seed sans clé"; fi
# Alias Jellyfin de Traefik : les arr joignent Jellyfin, jamais l'inverse.
if running jellyfin-jellyfin-1; then
  res 200 "$(docker exec arr-sonarr-1 curl -s -o /dev/null -w '%{http_code}' -m 5 "https://jellyfin.$DOMAIN:8443/health")" \
    "sonarr -> jellyfin (alias Traefik)"
else skip "sonarr -> jellyfin (alias Traefik)"; fi

echo
echo "Bilan : $ok OK, $ko ÉCHEC, $skipped IGNORÉ"
[ $ko -eq 0 ]
