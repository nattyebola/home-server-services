#!/bin/sh
# SUDO_ASKPASS : mot de passe sudo demandé dans une fenêtre GNOME (zenity) quand la
# commande tourne sans terminal (prompt `!` ou commande lancée par Claude Code).
# Utilisé par la variable SUDO du Makefile. sudo passe son invite en $1.
exec zenity --password --title="${1:-Mot de passe sudo}"
