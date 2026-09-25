#!/bin/sh
# Enregistre la clé API Govee dans le trousseau GNOME. Saisie masquée, passée
# par stdin : elle n'apparaît ni dans l'historique du shell ni dans `ps`.
set -eu
cd "$(dirname "$0")"

if [ -t 0 ]; then
    printf 'Clé API Govee : '
    stty -echo
    trap 'stty echo' EXIT INT TERM
    read -r key
    stty echo
    echo
else
    # Pas de terminal (ex. `!` de Claude Code) : fenêtre de saisie masquée.
    key=$(zenity --password --title='Clé API Govee') || { echo 'Annulé.' >&2; exit 1; }
fi

printf '%s\n' "$key" | gjs -m tools/store-key.js
