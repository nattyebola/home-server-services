# traefik-public is declared `external: true` in every stack's compose file so that
# no single `docker compose down` can delete a network the other stacks depend on.
# It must therefore be created once, out-of-band, before any stack is started.
NETWORK := traefik-public
STACKS := traefik jellyfin nextcloud vpn arr seerr

UPDATE_STACKS := nextcloud vpn jellyfin arr seerr

.PHONY: help network up down config logs update update-all backup restore cron-install dashboard-refresh clearr arr-overrides recyclarr-sync kodi-install api-keys provision switch-lan-only-middleware

# `make` sans argument affiche l'aide plutôt que de lancer la première cible
# (c'était `network`, qui ne dit rien de ce que le reste sait faire).
.DEFAULT_GOAL := help

# Aide générée depuis les annotations `##` des cibles elles-mêmes, pas depuis une
# liste séparée : une liste à part diverge dès qu'on ajoute une cible sans y
# penser. Format d'une annotation : `cible: ## <ARGS> — description`, ARGS entre
# chevrons quand la cible en attend.
help: ## — liste les cibles disponibles et leurs arguments
	@echo "usage: make <cible> [ARG=valeur]"
	@echo ""
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-28s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  STACK  : $(STACKS)"
	@echo "  autres : SNAPSHOT=<id|latest> (restore), KODI_HOME=<chemin> (kodi-install)"

# Kodi profile of the user running make (a media client, not a stack) — see the
# kodi-install target and kodi/README.md. Overridable for a Kodi running under
# another user or a non-default profile path: make kodi-install KODI_HOME=...
KODI_HOME ?= $(HOME)/.kodi

network: ## — crée le réseau traefik-public s'il manque (prérequis de tout `up`)
	@docker network inspect $(NETWORK) >/dev/null 2>&1 || docker network create $(NETWORK)

# .env.shared is the single source of truth for PUID/PGID/RENDER_GID/DOMAIN/DATA_ROOT,
# used by every stack. docker compose never reads it unless told to, so up/down/config/logs
# always go through this Makefile instead of calling `docker compose` directly in a stack dir.
# docker-compose.override.yml (gitignored, host-specific bind mounts — see
# */docker-compose.override.yml.example) is loaded when present so the base
# compose files stay free of any one deployment's folder layout.
compose = docker compose --env-file .env.shared $(if $(wildcard $(STACK)/.env),--env-file $(STACK)/.env,) -f $(STACK)/docker-compose.yml $(if $(wildcard $(STACK)/docker-compose.override.yml),-f $(STACK)/docker-compose.override.yml,)

up: network ## STACK=<nom> — démarre (ou met à jour) les conteneurs de la stack
	@test -n "$(STACK)" || (echo "usage: make up STACK=<$(STACKS)>" >&2 && exit 1)
	@# seerr tourne nativement en UID 1000 sans étape root-puis-drop et ne chown
	@# pas son volume : si ${DATA_ROOT}/.seerr/config n'existe pas, c'est Docker
	@# qui le crée, en root:root, et le container crashe en boucle sur EACCES.
	@# Le créer ici (donc en tant que l'utilisateur qui lance make) suffit à
	@# éviter le cas, et remplace le mkdir+chown manuel de l'installation.
	@if [ "$(STACK)" = "seerr" ]; then \
		root=$$(grep '^DATA_ROOT=' .env.shared | cut -d= -f2); \
		test -n "$$root" || (echo "DATA_ROOT not set in .env.shared" >&2 && exit 1); \
		mkdir -p "$$root/.seerr/config"; \
	fi
	@# Même raison que ci-dessus : traefik tourne en PUID:PGID et n'écrirait pas
	@# dans un dossier que Docker aurait créé en root. Porte l'access log
	@# (accessLog dans traefik.yml), sans lequel aucune requête WAN ne laisse de
	@# trace — y compris les 403 des middlewares LAN-only.
	@if [ "$(STACK)" = "traefik" ]; then \
		root=$$(grep '^DATA_ROOT=' .env.shared | cut -d= -f2); \
		test -n "$$root" || (echo "DATA_ROOT not set in .env.shared" >&2 && exit 1); \
		mkdir -p "$$root/.traefik/log"; \
	fi
	@# Les routeurs arr/transmission référencent `<nom>@file` : sans
	@# traefik/dynamic/lan-only.yml, Traefik ne sait pas résoudre leur middleware
	@# et répond 404. Écrit ici (en mode fermé) plutôt que laissé au premier
	@# `make switch-lan-only-middleware`, pour qu'aucun démarrage ne puisse partir
	@# sans lui — et pour le monter côté traefik dès le premier up.
	@scripts/lan-only-middleware.sh ensure
	$(compose) up -d

down: ## STACK=<nom> — arrête et supprime les conteneurs de la stack
	@test -n "$(STACK)" || (echo "usage: make down STACK=<$(STACKS)>" >&2 && exit 1)
	$(compose) down

config: ## STACK=<nom> — affiche le compose résolu (labels, env, montages)
	@test -n "$(STACK)" || (echo "usage: make config STACK=<$(STACKS)>" >&2 && exit 1)
	$(compose) config

logs: ## STACK=<nom> — suit les logs de la stack (Ctrl-C pour sortir)
	@test -n "$(STACK)" || (echo "usage: make logs STACK=<$(STACKS)>" >&2 && exit 1)
	$(compose) logs -f

# pull the latest image for each service, rebuild the ones with a local
# Dockerfile (nextcloud app/web ; arr/clearr), then recreate. nextcloud
# additionally needs its post-upgrade occ maintenance run every time app:
# gets a new image.
update: network ## STACK=<nom> — pull/rebuild puis recrée la stack
	@test -n "$(STACK)" || (echo "usage: make update STACK=<$(STACKS)>" >&2 && exit 1)
	$(compose) pull
	@if [ "$(STACK)" = "nextcloud" ] || [ "$(STACK)" = "arr" ]; then $(compose) build -q; fi
	$(compose) up -d --remove-orphans
	@if [ "$(STACK)" = "nextcloud" ]; then \
		$(compose) exec app ./occ app:update --all -n && \
		$(compose) exec app ./occ db:add-missing-columns && \
		$(compose) exec app ./occ db:add-missing-indices && \
		$(compose) exec app ./occ db:add-missing-primary-keys && \
		$(compose) exec app ./occ maintenance:mimetype:update-js && \
		$(compose) exec app ./occ maintenance:mimetype:update-db; \
	fi

# runs `update` for every stack that had update logic in the old ~/docker
# script (nextcloud, vpn, jellyfin — traefik was never part of it, but can
# still be updated on its own with `make update STACK=traefik`). Keeps going
# on a failed stack instead of aborting the rest (a stuck vpn pull shouldn't
# block jellyfin/arr/seerr from updating), reports a pass/fail summary at
# the end, then prunes now-dangling :latest images (every update leaves the
# previous digest orphaned, see CLAUDE.md image-tag decision) and refreshes
# the dashboard so it reflects the new containers without waiting for the
# next 5-min cron tick.
update-all: ## — `update` sur toutes les stacks, prune les images orphelines, régénère le dashboard
	@failed=""; \
	for s in $(UPDATE_STACKS); do \
		echo "\n======================== update $$s ========================\n"; \
		$(MAKE) update STACK=$$s || failed="$$failed $$s"; \
	done; \
	docker image prune -f; \
	$(MAKE) dashboard-refresh; \
	echo ""; \
	if [ -n "$$failed" ]; then \
		echo "échec(s) :$$failed" >&2; \
		exit 1; \
	fi; \
	echo "tous les stacks mis à jour avec succès"

# régénère dashboard/html/index.html à partir des labels Traefik réels
# (docker compose config) et de l'état d'exécution courant (docker ps) —
# voir scripts/generate-dashboard.py (vues dans dashboard/templates/).
# Servi par le service dashboard de traefik/docker-compose.yml (make up
# STACK=traefik), pas besoin qu'il tourne pour régénérer le contenu.
dashboard-refresh: ## — régénère dashboard/html/ (aussi fait par cron toutes les 5 min)
	@python3 scripts/generate-dashboard.py

# TUI de nettoyage manuel (même nom que le sous-domaine LAN-only clearr.${DOMAIN},
# arr/docker-compose.yml) : liste les torrents Transmission, supprime à la
# demande le torrent (+ fichiers) et les fichiers hardlinkés correspondants
# dans library/ — voir arr/clearr/app/. Web (service `clearr`, démarré en
# continu par `make up STACK=arr`) et TUI (ce target, ponctuel) partagent la
# même image/le même core.py ; ancien script hôte scripts/torrent-cleanup.py
# retiré (tournait via docker exec, incompatible avec la conteneurisation).
# `tui` seul, pas `python -m app tui` : l'image a un ENTRYPOINT
# ["python", "-m", "app"] (arr/clearr/Dockerfile), les arguments donnés à `run`
# s'y ajoutent — les répéter faisait voir `python` à argparse comme
# sous-commande (`invalid choice: 'python'`).
clearr: STACK := arr
clearr: network ## — TUI de nettoyage torrents/bibliothèque (équivalent console de clearr.<domaine>)
	@$(compose) run --rm -it clearr tui

# collecte les secrets générés au premier démarrage et les écrit dans arr/.env
# (clés API Prowlarr/Sonarr/Radarr lues dans leur config.xml, clé cross-seed,
# clé API Jellyfin créée au besoin) — voir scripts/provision.py. À lancer AVANT
# recyclarr-sync/arr-overrides, qui ont besoin de ces clés.
api-keys: ## — collecte les clés API générées au 1er démarrage dans arr/.env (avant arr-overrides)
	@python3 scripts/provision.py keys

# crée les objets de configuration qui se faisaient à la main dans les UI :
# bibliothèques Jellyfin, applications Prowlarr, client de téléchargement, root
# folders et Connection cross-seed côté Sonarr/Radarr, configuration Seerr —
# voir scripts/provision.py. À lancer APRÈS arr-overrides : la config Seerr
# référence par nom les profils qualité que celui-ci provisionne. Idempotent et
# strictement additif (ne réécrit jamais un objet existant), donc relançable.
provision: ## — crée les objets de config des UI (biblios Jellyfin, objets arr, Seerr) — après arr-overrides
	@python3 scripts/provision.py services

# réapplique les tailles de quality definition + le champ language Radarr
# que recyclarr ne gère pas et resynchronise à leurs défauts à chaque
# `recyclarr sync` — voir arr/recyclarr/recyclarr.yml et
# scripts/apply-arr-overrides.py. Aussi enchaîné par cron juste après
# `recyclarr-sync`, voir scripts/crontab.
arr-overrides: ## — réapplique les réglages arr que recyclarr écrase (aussi enchaîné par cron)
	@python3 scripts/apply-arr-overrides.py

# lance `recyclarr sync` en one-shot — le service recyclarr (arr/docker-compose.yml)
# n'a plus de scheduler interne et est sous `profiles: [manual]`, donc
# absent de `make up STACK=arr` ; seul ce target le démarre. Enchaîné par
# cron avec `arr-overrides` juste après, voir scripts/crontab.
recyclarr-sync: STACK := arr
recyclarr-sync: network ## — lance `recyclarr sync` en one-shot (aussi enchaîné par cron)
	@$(compose) run --rm recyclarr sync

# ouvre/referme au WAN les services normalement restreints au LAN
# (transmission + prowlarr/sonarr/radarr/clearr) en réécrivant la plage
# d'adresses de leur middleware ipAllowList, chargée à chaud par Traefik depuis
# traefik/dynamic/lan-only.yml — aucun conteneur n'est recréé, ce qui est tout
# l'intérêt par rapport à commenter un label par service. Refermeture
# automatique une heure après l'ouverture, assurée par le garde `rearm` de
# scripts/crontab (donc résistante à une déconnexion SSH et à un redémarrage).
# Régénère le dashboard à la fin dans les deux sens : c'est lui qui porte le
# bandeau d'avertissement tant que l'ouverture est active.
switch-lan-only-middleware: ## — ouvre/referme les services LAN-only au WAN (referme seul au bout d'1 h)
	@scripts/lan-only-middleware.sh toggle

# weekly restic backup (nextcloud DB dump + data + .env secrets + image
# digest manifest) — see scripts/backup.sh. Also run by cron, see CLAUDE.md.
backup: ## — sauvegarde restic (aussi faite par cron le dimanche à 3 h)
	@scripts/backup.sh

# restore a restic snapshot to sauvegarde/restore-<snapshot>/ and print the
# manual steps to bring it back — see scripts/restore.sh.
restore: ## SNAPSHOT=<id|latest> — restaure un snapshot dans sauvegarde/ sans toucher au live
	@scripts/restore.sh $(if $(SNAPSHOT),$(SNAPSHOT),latest)

# installs scripts/crontab as this host's crontab (nextcloud cron.php +
# weekly backup) — versioned here instead of only living in the live
# crontab, where it would otherwise vanish silently (migration, reinstall...).
# __REPO_ROOT__, __PUID__ and __DATA_ROOT__ in scripts/crontab are substituted
# with this checkout's absolute path and .env.shared's PUID/DATA_ROOT (cron
# never loads .env.shared itself) so the file stays portable across
# machines/users. .cron-status/ (added 2026-07-30) holds one heartbeat file
# per scheduled task, written by scripts/crontab on success — read by
# scripts/generate-dashboard.py's "Tâches planifiées" card (see
# cron_marker_age_seconds()) ; created here rather than left to the first
# cron tick so the dashboard doesn't have to guess between "never ran" and
# "directory missing".
# The substituted content goes through scripts/install-crontab.sh rather than
# straight into `crontab -`: this repo's jobs are merged into a marked block and
# every other line of the crontab (jobs the user added by hand, unrelated to
# this repo) is preserved — piping into `crontab -` replaced the whole crontab
# and dropped them silently.
cron-install: ## — installe les crons du repo dans le crontab, en préservant les jobs perso
	@test -f .env.shared || (echo ".env.shared missing — see .env.shared.example" >&2 && exit 1)
	$(eval PUID := $(shell grep '^PUID=' .env.shared | cut -d= -f2))
	$(eval DATA_ROOT := $(shell grep '^DATA_ROOT=' .env.shared | cut -d= -f2))
	@test -n "$(PUID)" || (echo "PUID not set in .env.shared" >&2 && exit 1)
	@test -n "$(DATA_ROOT)" || (echo "DATA_ROOT not set in .env.shared" >&2 && exit 1)
	@mkdir -p "$(DATA_ROOT)/.cron-status"
	@# logrotate n'interprète aucune variable : ses chemins doivent être
	@# littéraux. On rend donc le fichier avec les mêmes substitutions que le
	@# crontab, vers DATA_ROOT (état d'exécution, hors du checkout — il
	@# contiendrait sinon des chemins propres à ce déploiement dans un dépôt
	@# public). La ligne cron pointe sur ce rendu, pas sur la source.
	@sed -e "s|__REPO_ROOT__|$(CURDIR)|g" -e "s|__DATA_ROOT__|$(DATA_ROOT)|g" scripts/logrotate.conf > "$(DATA_ROOT)/.logrotate.conf"
	@echo "rendered $(DATA_ROOT)/.logrotate.conf"
	sed -e "s|__REPO_ROOT__|$(CURDIR)|g" -e "s|__PUID__|$(PUID)|g" -e "s|__DATA_ROOT__|$(DATA_ROOT)|g" scripts/crontab | scripts/install-crontab.sh "$(CURDIR)"
	@echo "installed crontab:"
	@crontab -l

# installs the "Supprimer avec clearr" context menu addon (kodi/context.clearr)
# into this user's Kodi profile: an entry on any movie/TV show that asks clearr
# to delete it (torrents + library files + Sonarr/Radarr entry). Copied rather
# than symlinked — Kodi refuses to load an addon whose directory is a symlink
# outside its addons dir. clearr's URL holds ${DOMAIN}, which can't live in a
# versioned file (public repo), so it is a Kodi addon setting pre-filled here
# from .env.shared — same reason the crontab placeholders are substituted above.
# Never overwrites an existing settings.xml: Kodi rewrites that file itself, and
# the URL may have been adjusted by hand since.
kodi-install: ## [KODI_HOME=<chemin>] — installe l'addon de menu contextuel clearr dans Kodi
	@test -f .env.shared || (echo ".env.shared missing — see .env.shared.example" >&2 && exit 1)
	$(eval DOMAIN := $(shell grep '^DOMAIN=' .env.shared | cut -d= -f2))
	@test -n "$(DOMAIN)" || (echo "DOMAIN not set in .env.shared" >&2 && exit 1)
	@test -d "$(KODI_HOME)" || (echo "$(KODI_HOME) not found — run Kodi once first, or pass KODI_HOME=" >&2 && exit 1)
	@mkdir -p "$(KODI_HOME)/addons" "$(KODI_HOME)/userdata/addon_data/context.clearr"
	@rm -rf "$(KODI_HOME)/addons/context.clearr"
	@cp -r kodi/context.clearr "$(KODI_HOME)/addons/context.clearr"
	@find "$(KODI_HOME)/addons/context.clearr" -name __pycache__ -prune -exec rm -rf {} +
	@if [ -f "$(KODI_HOME)/userdata/addon_data/context.clearr/settings.xml" ]; then \
		echo "settings.xml already there — clearr URL left untouched"; \
	else \
		printf '<settings version="2">\n    <setting id="clearr_url">https://clearr.%s</setting>\n</settings>\n' \
			"$(DOMAIN)" > "$(KODI_HOME)/userdata/addon_data/context.clearr/settings.xml"; \
		echo "clearr URL set to https://clearr.$(DOMAIN)"; \
	fi
	@echo "installed to $(KODI_HOME)/addons/context.clearr — restart Kodi to load it"
