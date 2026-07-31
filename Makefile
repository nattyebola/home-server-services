# traefik-public is declared `external: true` in every stack's compose file so that
# no single `docker compose down` can delete a network the other stacks depend on.
# It must therefore be created once, out-of-band, before any stack is started.
NETWORK := traefik-public
STACKS := traefik jellyfin nextcloud vpn arr seerr

UPDATE_STACKS := nextcloud vpn jellyfin arr seerr

.PHONY: network up down config logs update update-all backup restore cron-install dashboard-refresh clearr arr-overrides recyclarr-sync

network:
	@docker network inspect $(NETWORK) >/dev/null 2>&1 || docker network create $(NETWORK)

# .env.shared is the single source of truth for PUID/PGID/RENDER_GID/DOMAIN/DATA_ROOT,
# used by every stack. docker compose never reads it unless told to, so up/down/config/logs
# always go through this Makefile instead of calling `docker compose` directly in a stack dir.
# docker-compose.override.yml (gitignored, host-specific bind mounts — see
# */docker-compose.override.yml.example) is loaded when present so the base
# compose files stay free of any one deployment's folder layout.
compose = docker compose --env-file .env.shared $(if $(wildcard $(STACK)/.env),--env-file $(STACK)/.env,) -f $(STACK)/docker-compose.yml $(if $(wildcard $(STACK)/docker-compose.override.yml),-f $(STACK)/docker-compose.override.yml,)

up: network
	@test -n "$(STACK)" || (echo "usage: make up STACK=<$(STACKS)>" >&2 && exit 1)
	$(compose) up -d

down:
	@test -n "$(STACK)" || (echo "usage: make down STACK=<$(STACKS)>" >&2 && exit 1)
	$(compose) down

config:
	@test -n "$(STACK)" || (echo "usage: make config STACK=<$(STACKS)>" >&2 && exit 1)
	$(compose) config

logs:
	@test -n "$(STACK)" || (echo "usage: make logs STACK=<$(STACKS)>" >&2 && exit 1)
	$(compose) logs -f

# pull the latest image for each service, rebuild the ones with a local
# Dockerfile (nextcloud app/web ; arr/clearr), then recreate. nextcloud
# additionally needs its post-upgrade occ maintenance run every time app:
# gets a new image.
update: network
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
update-all:
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
dashboard-refresh:
	@python3 scripts/generate-dashboard.py

# TUI de nettoyage manuel (même nom que le sous-domaine LAN-only clearr.${DOMAIN},
# arr/docker-compose.yml) : liste les torrents Transmission, supprime à la
# demande le torrent (+ fichiers) et les fichiers hardlinkés correspondants
# dans library/ — voir arr/clearr/app/. Web (service `clearr`, démarré en
# continu par `make up STACK=arr`) et TUI (ce target, ponctuel) partagent la
# même image/le même core.py ; ancien script hôte scripts/torrent-cleanup.py
# retiré (tournait via docker exec, incompatible avec la conteneurisation).
clearr: STACK := arr
clearr: network
	@$(compose) run --rm -it clearr python -m app tui

# réapplique les tailles de quality definition + le champ language Radarr
# que recyclarr ne gère pas et resynchronise à leurs défauts à chaque
# `recyclarr sync` — voir arr/recyclarr/recyclarr.yml et
# scripts/apply-arr-overrides.py. Aussi enchaîné par cron juste après
# `recyclarr-sync`, voir scripts/crontab.
arr-overrides:
	@python3 scripts/apply-arr-overrides.py

# lance `recyclarr sync` en one-shot — le service recyclarr (arr/docker-compose.yml)
# n'a plus de scheduler interne et est sous `profiles: [manual]`, donc
# absent de `make up STACK=arr` ; seul ce target le démarre. Enchaîné par
# cron avec `arr-overrides` juste après, voir scripts/crontab.
recyclarr-sync: STACK := arr
recyclarr-sync: network
	@$(compose) run --rm recyclarr sync

# weekly restic backup (nextcloud DB dump + data + .env secrets + image
# digest manifest) — see scripts/backup.sh. Also run by cron, see CLAUDE.md.
backup:
	@scripts/backup.sh

# restore a restic snapshot to sauvegarde/restore-<snapshot>/ and print the
# manual steps to bring it back — see scripts/restore.sh.
restore:
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
cron-install:
	@test -f .env.shared || (echo ".env.shared missing — see .env.shared.example" >&2 && exit 1)
	$(eval PUID := $(shell grep '^PUID=' .env.shared | cut -d= -f2))
	$(eval DATA_ROOT := $(shell grep '^DATA_ROOT=' .env.shared | cut -d= -f2))
	@test -n "$(PUID)" || (echo "PUID not set in .env.shared" >&2 && exit 1)
	@test -n "$(DATA_ROOT)" || (echo "DATA_ROOT not set in .env.shared" >&2 && exit 1)
	@mkdir -p "$(DATA_ROOT)/.cron-status"
	sed -e "s|__REPO_ROOT__|$(CURDIR)|g" -e "s|__PUID__|$(PUID)|g" -e "s|__DATA_ROOT__|$(DATA_ROOT)|g" scripts/crontab | crontab -
	@echo "installed crontab:"
	@crontab -l
