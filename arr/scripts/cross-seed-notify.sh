#!/bin/sh
# Custom Script Connect pour Sonarr/Radarr — déclenche cross-seed dès qu'un
# import/upgrade se termine, au lieu d'attendre son cycle "inject" périodique.
# Sonarr/Radarr exposent les mêmes infos sous des noms de variable préfixés
# différemment (sonarr_/radarr_), d'où le fallback ci-dessous.
set -eu

eventtype="${sonarr_eventtype:-${radarr_eventtype:-}}"
download_id="${sonarr_download_id:-${radarr_download_id:-}}"

# Sonarr/Radarr appellent aussi ce script en mode "Test" depuis leur UI avec
# des variables factices — ne pas planter dessus, juste sortir proprement.
if [ "$eventtype" = "Test" ]; then
  echo "cross-seed-notify: événement Test, rien à faire"
  exit 0
fi

if [ -z "$download_id" ]; then
  echo "cross-seed-notify: pas de download_id (téléchargement pas via un client torrent ?), ignoré"
  exit 0
fi

curl -fsS -X POST "http://cross-seed:2468/api/webhook?apikey=${CROSSSEED_API_KEY}" \
  --data-urlencode "infoHash=${download_id}" \
  -o /dev/null
