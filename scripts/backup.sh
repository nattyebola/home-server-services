#!/usr/bin/env bash
# Weekly restic backup: Nextcloud DB + data, .env secrets, and a manifest of
# the exact image digests running at backup time (compose files stay on
# :latest — see CLAUDE.md — so the manifest is what makes a restore
# reproducible instead of pulling whatever :latest resolves to that day).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$REPO_ROOT/sauvegarde"
STAGING_DIR="$BACKUP_DIR/.staging"
export RESTIC_REPOSITORY="$BACKUP_DIR/restic-repo"
export RESTIC_PASSWORD_FILE="$BACKUP_DIR/restic-password"

STACKS="traefik jellyfin nextcloud vpn"

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
compose_for nextcloud exec -T db-next \
	sh -c 'pg_dump -U "$POSTGRES_USER" "${POSTGRES_DB:-$POSTGRES_USER}"' \
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
restic backup \
	"$REPO_ROOT/.env.shared" \
	"$REPO_ROOT/nextcloud/.env" \
	"$REPO_ROOT/vpn/.env" \
	"${DATA_ROOT:-/data}/.nextcloud/nexcloud" \
	"$STAGING_DIR" \
	--tag weekly --tag "commit-$(git -C "$REPO_ROOT" rev-parse --short HEAD)"

echo "==> pruning old snapshots (keep last 8 weekly, ~2 months)"
restic forget --keep-weekly 8 --prune

echo "==> tagging infra repo state, if it changed since the last backup tag"
last_tag="$(git -C "$REPO_ROOT" tag --list 'backup-*' --sort=-creatordate | head -n1)"
last_tag_commit=""
[ -n "$last_tag" ] && last_tag_commit="$(git -C "$REPO_ROOT" rev-list -n1 "$last_tag")"
current_commit="$(git -C "$REPO_ROOT" rev-parse HEAD)"

if [ "$last_tag_commit" != "$current_commit" ]; then
	new_tag="backup-$(date +%F)"
	git -C "$REPO_ROOT" tag -a "$new_tag" -m "Infra state at backup $(date +%F)"
	echo "created git tag $new_tag"
	if git -C "$REPO_ROOT" remote get-url origin >/dev/null 2>&1; then
		git -C "$REPO_ROOT" push origin "$new_tag"
	else
		echo "no 'origin' remote configured yet — tag stays local for now" >&2
	fi
else
	echo "infra unchanged since $last_tag — no new tag"
fi

echo "==> done: $(restic snapshots --latest 1 --compact)"
