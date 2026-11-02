#!/bin/sh
# Partie root de `make steam-tv-install` : autologin sur tty3 et touches multimédia (triggerhappy).
# Usage : sudo steam-tv/install-root.sh <utilisateur>   (ré-exécutable sans risque)
set -eu
user=${1:?usage: install-root.sh <utilisateur>}
# Le nom passe dans sed et dans des fichiers root : on n'accepte qu'un login Unix ordinaire.
echo "$user" | grep -Eq '^[a-z_][a-z0-9_-]*$' || { echo "nom d'utilisateur invalide : $user" >&2; exit 1; }
id -u "$user" >/dev/null
here=$(dirname "$(readlink -f "$0")")
render() { sed "s/__USER__/$user/g" "$1"; }

# xprop (x11-utils) : luminosité ; triggerhappy : raccourcis globaux
apt-get install -y triggerhappy x11-utils
for cmd in gamescope wpctl python3; do
  command -v "$cmd" >/dev/null || echo "ATTENTION : $cmd introuvable" >&2
done

install -d /etc/systemd/system/getty@tty3.service.d \
           /etc/systemd/system/triggerhappy.service.d \
           /etc/triggerhappy/triggers.d
render "$here/systemd/getty@tty3-autologin.conf" > /etc/systemd/system/getty@tty3.service.d/autologin.conf
install -m 644 "$here/systemd/triggerhappy-root.conf" /etc/systemd/system/triggerhappy.service.d/root.conf
install -m 644 "$here/triggers.conf" /etc/triggerhappy/triggers.d/steam-tv.conf
# Copie, pas lien : triggerhappy l'exécute en root, il ne doit pas rester modifiable par l'utilisateur.
render "$here/steam-tv-key" > /usr/local/bin/steam-tv-key
chmod 755 /usr/local/bin/steam-tv-key
chmod 644 /etc/systemd/system/getty@tty3.service.d/autologin.conf

systemctl daemon-reload
systemctl enable triggerhappy.service
systemctl restart triggerhappy.service

# logind démarre autovt@tty3, alias de getty@ : vérifier que le drop-in s'y applique.
systemctl cat autovt@tty3 | grep -q -- "--autologin $user" \
  || { echo "autologin non appliqué à autovt@tty3" >&2; exit 1; }
systemctl is-active --quiet triggerhappy.service
echo "OK : autologin tty3 et touches multimédia installés pour $user"
