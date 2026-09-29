#!/usr/bin/env python3
# Crée, en tant que l'utilisateur qui lance `make up`, les dossiers source des
# bind-mounts d'une stack qui vivent sous DATA_ROOT et n'existent pas encore.
#
# Pourquoi : un bind-mount dont la source manque, c'est Docker qui la crée, en
# root:root. Presque tous nos services tournent en PUID:PGID (`user:` dans les
# compose, ou images non-root) et ne chownent pas leur volume : ils plantent
# alors en boucle sur EACCES. Vérifié le 2026-09-29 en suivant
# docs/installation.md sur une VM neuve : Jellyfin crashait sur
# `Access to the path '/config/log' is denied`, et `library/` naissait en
# root — ce qui aurait ensuite bloqué tous les imports Sonarr/Radarr. En prod
# rien ne se voyait, les dossiers existant depuis longtemps.
#
# Lit la sortie de `docker compose config --format json` sur stdin plutôt
# qu'une liste figée : un montage ajouté à un compose est couvert sans y
# penser. Seules les sources sous les racines passées en argument sont créées
# (DATA_ROOT et le checkout, pour les dossiers générés comme dashboard/html/,
# que Docker créait lui aussi en root au premier `make up STACK=traefik`) — un
# chemin propre à la machine (override) qui manque est une erreur de
# configuration à voir, pas à masquer (cf. le piège des placeholders
# `/path/to/…` des .example).
#
# Un montage de FICHIER manquant (ex. .clearr.log) serait créé ici comme un
# dossier, exactement l'erreur de Docker : le Makefile crée ces fichiers
# AVANT d'appeler ce script, qui ne voit donc plus que des dossiers manquants.
import json
import os
import sys


def main():
    roots = [os.path.realpath(r) for r in sys.argv[1:]]
    raw = sys.stdin.read()
    if not raw.strip():
        # `docker compose config` a échoué (son erreur est déjà affichée) :
        # refuser plutôt que de continuer sans rien avoir créé.
        sys.exit("ensure-bind-dirs : `docker compose config` n'a rien rendu — make up interrompu")
    config = json.loads(raw)
    for service in (config.get("services") or {}).values():
        for volume in service.get("volumes") or []:
            source = volume.get("source") or ""
            if volume.get("type") != "bind" or not source:
                continue
            real = os.path.realpath(source)
            if not any(real == root or real.startswith(root + os.sep) for root in roots):
                continue
            if not os.path.lexists(source):
                os.makedirs(source, exist_ok=True)
                print(f"créé : {source}")


if __name__ == "__main__":
    main()
