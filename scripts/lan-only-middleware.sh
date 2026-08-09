#!/usr/bin/env bash
#
# Ouvre ou referme les services LAN-only (prowlarr/sonarr/radarr/clearr et
# transmission) au WAN, sans toucher à la configuration d'un seul service.
#
# Pourquoi ce détour : le middleware `ipAllowList` était déclaré par labels
# Docker, répété sur les 4 services arr et sur transmission-proxy. Un label ne
# peut changer qu'en recréant le conteneur qui le porte — donc impossible de
# basculer sans redémarrer 5 conteneurs, et impossible de le faire depuis un
# simple SSH sans interrompre les téléchargements en cours. Les deux middlewares
# vivent désormais dans le provider `file` de Traefik
# (traefik/dynamic/lan-only.yml, voir traefik/traefik.yml) : réécrire ce fichier
# suffit, Traefik le recharge à chaud (`watch: true`), aucun conteneur n'est
# touché. Les routeurs le référencent en `<nom>@file`.
#
# « Rendre le middleware inopérant » plutôt que le retirer des routeurs : le
# middleware reste en place et référencé, seule sa plage d'adresses change
# (LAN_CIDR -> 0.0.0.0/0 + ::/0, qui laisse tout passer). Le retirer des routeurs
# demanderait d'éditer 5 déclarations et de les recréer, soit exactement ce qu'on
# veut éviter.
#
# Refermeture automatique au bout d'une heure : ouvrir donne accès au WAN à
# Transmission et aux trois arr, tous sans authentification forte — un oubli est
# le vrai risque de cette commande, pas l'ouverture elle-même. L'échéance est
# écrite dans un fichier d'état et un garde cron (scripts/crontab, toutes les
# 5 min) referme dès qu'elle est passée. Le cron plutôt qu'un `sleep` détaché :
# il survit à une déconnexion SSH et à un redémarrage de la machine, ce qu'un
# processus en arrière-plan ne fait pas. Contrepartie assumée : la refermeture
# tombe entre 60 et 65 min après l'ouverture, pas à la seconde.
#
# Sous-commandes :
#   toggle  (défaut) bascule selon l'état courant, puis régénère le dashboard
#   open              ouvre au WAN pour OPEN_WINDOW_SECONDS
#   close             referme (LAN uniquement)
#   rearm             referme SI l'échéance est passée — appelé par cron, muet sinon
#   ensure            écrit le fichier s'il manque, en mode fermé (défaut sûr)
#   status            affiche l'état, code de sortie 0 = fermé, 1 = ouvert
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DYNAMIC_FILE="$REPO_ROOT/traefik/dynamic/lan-only.yml"
OPEN_WINDOW_SECONDS=3600

# Les deux middlewares LAN-only du repo. Même plage pour les deux : ils sont
# séparés parce qu'ils viennent de deux stacks (arr/ et vpn/), pas parce qu'ils
# autorisent des choses différentes.
MIDDLEWARES=(arr-lan-only transmission-lan-only)

# shellcheck source=/dev/null
test -f "$REPO_ROOT/.env.shared" || {
	echo "$(basename "$0"): .env.shared introuvable — voir .env.shared.example" >&2
	exit 1
}
source "$REPO_ROOT/.env.shared"
: "${LAN_CIDR:?LAN_CIDR absent de .env.shared}"
: "${DATA_ROOT:?DATA_ROOT absent de .env.shared}"

# Même convention de dotfile sous DATA_ROOT que .cron-status/ et
# .transmission-stats-history.jsonl : de l'état d'exécution, pas de l'infra
# as code, donc hors du checkout.
STATE_FILE="$DATA_ROOT/.lan-only-open-until"

render() { # $1 = lan|wan
	local mode="$1" ranges label
	if [ "$mode" = "wan" ]; then
		# ::/0 en plus de 0.0.0.0/0 : sans lui un client IPv6 resterait bloqué,
		# et l'ouverture serait silencieusement partielle.
		ranges=$'          - "0.0.0.0/0"\n          - "::/0"'
		label="OUVERT AU WAN"
	else
		ranges="          - \"$LAN_CIDR\""
		label="LAN uniquement ($LAN_CIDR)"
	fi

	local tmp
	tmp=$(mktemp "$(dirname "$DYNAMIC_FILE")/.lan-only.XXXXXX")
	{
		echo "# Généré par scripts/lan-only-middleware.sh — ne pas éditer à la main,"
		echo "# toute bascule réécrit ce fichier. Chargé à chaud par le provider \`file\`"
		echo "# de Traefik (traefik/traefik.yml), aucun conteneur n'est recréé."
		echo "# État : $label"
		echo "http:"
		echo "  middlewares:"
		for name in "${MIDDLEWARES[@]}"; do
			echo "    $name:"
			echo "      ipAllowList:"
			echo "        sourceRange:"
			echo "$ranges"
		done
	} >"$tmp"
	chmod 644 "$tmp"
	# Remplacement atomique : Traefik surveille le dossier, il ne peut donc pas
	# lire un fichier à moitié écrit — ce qu'une écriture en place permettrait.
	mv "$tmp" "$DYNAMIC_FILE"
}

refresh_dashboard() {
	# Le dashboard rend un bandeau tant que l'ouverture est active (voir
	# scripts/generate-dashboard.py) : sans cette régénération il continuerait
	# d'afficher l'état précédent jusqu'au prochain tick cron, 5 min plus tard.
	python3 "$REPO_ROOT/scripts/generate-dashboard.py"
}

open_until() {
	local deadline tmp
	deadline=$(($(date +%s) + OPEN_WINDOW_SECONDS))
	# L'échéance est écrite AVANT l'ouverture, et atomiquement (mktemp + mv,
	# comme le fichier dynamique juste au-dessus). L'ordre inverse laissait une
	# fenêtre où le middleware était ouvert sans qu'aucune échéance n'existe
	# (disque plein, DATA_ROOT non monté) : `rearm` n'avait alors plus rien à
	# refermer et les 5 services restaient joignables depuis Internet
	# indéfiniment. Mieux vaut refuser d'ouvrir que d'ouvrir sans minuterie.
	tmp=$(mktemp "$(dirname "$STATE_FILE")/.lan-only-open-until.XXXXXX") || {
		echo "impossible d'écrire dans $(dirname "$STATE_FILE") — ouverture annulée" >&2
		exit 1
	}
	printf '%s\n' "$deadline" >"$tmp"
	chmod 644 "$tmp"
	mv "$tmp" "$STATE_FILE"
	render wan
	echo "LAN-only DÉSACTIVÉ — services joignables depuis le WAN"
	echo "  refermeture automatique vers $(date -d "@$deadline" '+%H:%M') (garde cron, ±5 min)"
	echo "  concernés : ${MIDDLEWARES[*]}"
}

close_now() {
	render lan
	rm -f "$STATE_FILE"
	echo "LAN-only ACTIF — seul $LAN_CIDR peut atteindre ces services"
}

# Présence du fichier d'état = une fenêtre a été ouverte, même si son contenu
# est devenu illisible. C'est ce test (et pas `deadline`) qui doit décider d'un
# toggle ou d'une refermeture : sinon un fichier corrompu se lit comme « fermé »
# et un toggle RE-ouvrirait au lieu de refermer.
window_open() { [ -f "$STATE_FILE" ]; }

deadline() { # échéance VALIDE, vide si fermé ou si le contenu est inexploitable
	local raw
	window_open || return 0
	raw=$(cat "$STATE_FILE" 2>/dev/null || true)
	case "$raw" in
	'' | *[!0-9]*)
		# Fichier vide ou tronqué (coupure pendant l'écriture, ou écriture par
		# un tiers — il vit sous DATA_ROOT, monté en écriture dans clearr,
		# sonarr et radarr). L'ancienne version injectait ce contenu tel quel
		# dans une comparaison entière : `test` sortait « nombre entier
		# attendu » et retournait faux, donc `rearm` ne refermait jamais,
		# pendant que `status` et le bandeau du dashboard annonçaient tous deux
		# « LAN uniquement ». Les trois canaux de visibilité d'accord, et tous
		# les trois faux. On rend donc vide, et les appelants refermeront —
		# l'échec doit être FERMÉ, jamais ouvert.
		echo "$(basename "$0"): échéance illisible dans $STATE_FILE — traitée comme périmée" >&2
		return 0
		;;
	esac
	printf '%s\n' "$raw"
}

case "${1:-toggle}" in
toggle)
	if window_open; then close_now; else open_until; fi
	refresh_dashboard
	;;
open)
	open_until
	refresh_dashboard
	;;
close)
	close_now
	refresh_dashboard
	;;
rearm)
	# Muet quand il n'y a rien à faire : appelé toutes les 5 min par cron, il ne
	# doit rien écrire dans les logs le reste du temps.
	#
	# Referme dès que le fichier d'état existe ET que l'échéance est atteinte
	# OU illisible : une échéance qu'on ne sait pas lire ne doit jamais valoir
	# « laisse ouvert » (voir deadline()).
	if window_open; then
		until_ts=$(deadline)
		if [ -z "$until_ts" ] || [ "$(date +%s)" -ge "$until_ts" ]; then
			close_now
			refresh_dashboard
		fi
	fi
	;;
ensure)
	# Filet pour une installation neuve : les 5 routeurs référencent
	# `<nom>@file`, donc sans ce fichier Traefik ne sait pas résoudre leur
	# middleware et répond 404 sur prowlarr/sonarr/radarr/clearr/transmission.
	# Appelé par `make up` (voir Makefile) pour qu'on ne puisse pas démarrer la
	# stack sans lui.
	#
	# Rend l'état que décrit le fichier d'état, pas systématiquement « fermé » :
	# un `make up` pendant une fenêtre d'ouverture aurait sinon refermé l'accès
	# en laissant `status` et le bandeau du dashboard annoncer « ouvert jusqu'à
	# HH:MM » — l'utilisateur se croirait encore joignable alors qu'il ne l'est
	# plus. Le défaut reste fermé : pas de fichier d'état, ou échéance déjà
	# passée, donnent LAN uniquement (et l'échéance périmée est nettoyée, pour ne
	# pas laisser `rearm` et le bandeau vivre sur un état mort).
	if [ ! -f "$DYNAMIC_FILE" ]; then
		until_ts=$(deadline)
		if [ -n "$until_ts" ] && [ "$(date +%s)" -lt "$until_ts" ]; then
			render wan
			echo "traefik/dynamic/lan-only.yml recréé (ouvert au WAN, fenêtre en cours)"
		else
			rm -f "$STATE_FILE"
			render lan
			echo "traefik/dynamic/lan-only.yml créé (LAN uniquement)"
		fi
	fi
	;;
status)
	until_ts=$(deadline)
	if window_open && [ -z "$until_ts" ]; then
		# Fenêtre ouverte mais échéance illisible : le dire franchement plutôt
		# que d'afficher « LAN uniquement », qui serait un mensonge tant que le
		# prochain `rearm` (≤ 5 min) n'a pas refermé.
		echo "état INCOHÉRENT : $STATE_FILE existe mais son échéance est illisible."
		echo "  l'accès est probablement encore ouvert — lancez '$(basename "$0") close' maintenant."
		exit 1
	fi
	if [ -n "$until_ts" ]; then
		left=$((until_ts - $(date +%s)))
		printf 'ouvert au WAN — refermeture vers %s (%d min restantes)\n' \
			"$(date -d "@$until_ts" '+%H:%M')" "$(((left > 0 ? left : 0) / 60))"
		exit 1
	fi
	echo "LAN uniquement ($LAN_CIDR)"
	;;
*)
	echo "usage: $(basename "$0") [toggle|open|close|rearm|ensure|status]" >&2
	exit 2
	;;
esac
