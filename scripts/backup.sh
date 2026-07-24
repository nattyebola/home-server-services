#!/usr/bin/env bash
# Weekly restic backup: Nextcloud DB + data, .env secrets, and a manifest of
# the exact image digests running at backup time (compose files stay on
# :latest — see CLAUDE.md — so the manifest is what makes a restore
# reproducible instead of pulling whatever :latest resolves to that day).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a
source "$REPO_ROOT/.env.shared"
set +a
BACKUP_DIR="$REPO_ROOT/sauvegarde"
STAGING_DIR="$BACKUP_DIR/.staging"
export RESTIC_REPOSITORY="$BACKUP_DIR/restic-repo"
export RESTIC_PASSWORD_FILE="$BACKUP_DIR/restic-password"

STACKS="traefik jellyfin nextcloud vpn arr seerr"

compose_for() {
	local stack="$1"; shift
	local args=(--env-file "$REPO_ROOT/.env.shared")
	[ -f "$REPO_ROOT/$stack/.env" ] && args+=(--env-file "$REPO_ROOT/$stack/.env")
	args+=(-f "$REPO_ROOT/$stack/docker-compose.yml")
	docker compose "${args[@]}" "$@"
}

mkdir -p "$STAGING_DIR"

if [ ! -f "$RESTIC_PASSWORD_FILE" ]; then
	echo "No restic password file at $RESTIC_PASSWORD_FILE — generating one." >&2
	echo "Keep a copy of it somewhere else (password manager); without it the backup is unreadable." >&2
	( umask 077; openssl rand -base64 32 > "$RESTIC_PASSWORD_FILE" )
fi

if ! restic snapshots >/dev/null 2>&1; then
	restic init
fi

echo "==> dumping nextcloud DB (consistent, not a raw copy of db-next's data dir)"
# La base réelle de Nextcloud est "nextcloud" (POSTGRES_DB=nextcloud codé en
# dur sur le service app, docker-compose.yml) — pas $POSTGRES_USER. nextcloud/.env
# ne définit que POSTGRES_USER/PASSWORD (le superuser d'amorçage de l'image
# postgres, "postgres"), donc l'ancien fallback "${POSTGRES_DB:-$POSTGRES_USER}"
# dumpait silencieusement la base "postgres" (quasi vide) au lieu de "nextcloud"
# — repéré le 2026-07-24 en testant `make restore` pour de vrai : toutes les
# sauvegardes précédentes avaient un dump DB vide (26 lignes, 0 `COPY`).
compose_for nextcloud exec -T db-next \
	sh -c 'pg_dump -U "$POSTGRES_USER" "${POSTGRES_DB:-nextcloud}"' \
	>"$STAGING_DIR/nextcloud-db.sql"

echo "==> recording exact image digests currently running"
: >"$STAGING_DIR/image-manifest.txt"
for stack in $STACKS; do
	for cid in $(compose_for "$stack" ps -q); do
		docker inspect "$cid" --format '{{.Name}} {{.Config.Image}} {{.Image}}' \
			>>"$STAGING_DIR/image-manifest.txt"
	done
done

git -C "$REPO_ROOT" rev-parse HEAD >"$STAGING_DIR/infra-commit.txt"

echo "==> restic backup"
# Deliberately NOT backed up: $DATA_ROOT/library (media, redownloadable via
# arr), $DATA_ROOT/.transmission/data (in-flight/completed downloads, same
# reasoning), $DATA_ROOT/.jellyfin/cache (transcodes/image cache, purely
# regenerated) — huge and disposable, would blow up the restic repo for no
# recovery value.
restic backup \
	"$REPO_ROOT/.env.shared" \
	"$REPO_ROOT/traefik/.env" \
	"$REPO_ROOT/nextcloud/.env" \
	"$REPO_ROOT/vpn/.env" \
	"$REPO_ROOT/arr/.env" \
	"$DATA_ROOT/.nextcloud/nexcloud" \
	"$DATA_ROOT/.jellyfin/config" \
	"$DATA_ROOT/.arr" \
	"$DATA_ROOT/.seerr/config" \
	"$DATA_ROOT/.transmission/config" \
	"$STAGING_DIR" \
	--tag weekly --tag "commit-$(git -C "$REPO_ROOT" rev-parse --short HEAD)"

echo "==> checking repository integrity (structure + 5% of data packs read back)"
# 5%/week averages a full --read-data pass roughly every ~5 months without
# adding much I/O to each run — catches silent corruption long before a
# restore would, instead of only discovering it then.
restic check --read-data-subset=5%

echo "==> pruning old snapshots (keep last 8 weekly, ~2 months)"
restic forget --keep-weekly 8 --prune

echo "==> tagging infra repo state, if it changed since the last backup tag"
last_tag="$(git -C "$REPO_ROOT" tag --list 'backup-*' --sort=-creatordate | head -n1)"
last_tag_commit=""
[ -n "$last_tag" ] && last_tag_commit="$(git -C "$REPO_ROOT" rev-list -n1 "$last_tag")"
current_commit="$(git -C "$REPO_ROOT" rev-parse HEAD)"

if [ "$last_tag_commit" != "$current_commit" ]; then
	new_tag="backup-$(date +%F)"
	if git -C "$REPO_ROOT" tag --list "$new_tag" | grep -q .; then
		echo "tag $new_tag already exists for an earlier commit today — moving it to HEAD"
		git -C "$REPO_ROOT" tag -d "$new_tag" >/dev/null
	fi
	git -C "$REPO_ROOT" tag -a "$new_tag" -m "Infra state at backup $(date +%F)"
	echo "created git tag $new_tag"
	if git -C "$REPO_ROOT" remote get-url origin >/dev/null 2>&1; then
		git -C "$REPO_ROOT" push --force origin "$new_tag"
	else
		echo "no 'origin' remote configured yet — tag stays local for now" >&2
	fi
else
	echo "infra unchanged since $last_tag — no new tag"
fi

echo "==> done: $(restic snapshots --latest 1 --compact)"
