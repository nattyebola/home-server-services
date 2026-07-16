# traefik-public is declared `external: true` in every stack's compose file so that
# no single `docker compose down` can delete a network the other stacks depend on.
# It must therefore be created once, out-of-band, before any stack is started.
NETWORK := traefik-public

.PHONY: network
network:
	@docker network inspect $(NETWORK) >/dev/null 2>&1 || docker network create $(NETWORK)
