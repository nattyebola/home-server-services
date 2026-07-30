#!/usr/bin/env bash
# Usage: require-running.sh <project>/<service> [<project>/<service> ...]
# Exits 0 only if every given docker-compose service currently has a
# running container, 1 otherwise — same "<project>/<service>" labels as
# docker_ps_set() in scripts/generate-dashboard.py. Used to guard
# scripts/crontab entries tied to a stack that might be stopped (skip the
# whole chain rather than let it fail against a missing container every
# tick) and scripts/backup.sh's best-effort Nextcloud DB dump.
set -euo pipefail

for target in "$@"; do
	project="${target%%/*}"
	service="${target#*/}"
	id=$(docker ps --filter "status=running" \
		--filter "label=com.docker.compose.project=$project" \
		--filter "label=com.docker.compose.service=$service" -q)
	[ -n "$id" ] || exit 1
done
