# traefik-public is declared `external: true` in every stack's compose file so that
# no single `docker compose down` can delete a network the other stacks depend on.
# It must therefore be created once, out-of-band, before any stack is started.
NETWORK := traefik-public
STACKS := traefik portainer jellyfin nextcloud

.PHONY: network up down config logs

network:
	@docker network inspect $(NETWORK) >/dev/null 2>&1 || docker network create $(NETWORK)

# .env.shared is the single source of truth for PUID/PGID/RENDER_GID/DOMAIN/DATA_ROOT,
# used by every stack. docker compose never reads it unless told to, so up/down/config/logs
# always go through this Makefile instead of calling `docker compose` directly in a stack dir.
compose = docker compose --env-file .env.shared $(if $(wildcard $(STACK)/.env),--env-file $(STACK)/.env,) -f $(STACK)/docker-compose.yml

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
