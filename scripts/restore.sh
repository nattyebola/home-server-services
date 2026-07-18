#!/usr/bin/env bash
# Restores a restic snapshot to a scratch directory and prints the manual
# steps to bring a stack back — deliberately does NOT overwrite live data or
# import into a running DB by itself; a restore is rare and high-stakes
# enough to want a human looking at each step.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$REPO_ROOT/sauvegarde"
export RESTIC_REPOSITORY="$BACKUP_DIR/restic-repo"
export RESTIC_PASSWORD_FILE="$BACKUP_DIR/restic-password"

SNAPSHOT="${1:-latest}"
TARGET="${2:-$BACKUP_DIR/restore-$SNAPSHOT}"

echo "==> available snapshots"
restic snapshots

echo "==> restoring snapshot '$SNAPSHOT' to $TARGET"
mkdir -p "$TARGET"
restic restore "$SNAPSHOT" --target "$TARGET"

staging="$(find "$TARGET" -type d -name .staging | head -n1)"

echo
echo "======================================================================"
echo "Restored under: $TARGET"
if [ -n "$staging" ]; then
	echo
	echo "-- infra commit at backup time --"
	cat "$staging/infra-commit.txt" 2>/dev/null || echo "(missing)"
	echo
	echo "-- image digests running at backup time --"
	cat "$staging/image-manifest.txt" 2>/dev/null || echo "(missing)"
fi
echo
echo "-- manual steps to bring nextcloud back --"
echo "1. Match the infra recipe to this backup:"
echo "     git -C $REPO_ROOT checkout \$(cat $staging/infra-commit.txt)"
echo "2. Pin the images above (docker-compose.override.yml with the digests"
echo "   from image-manifest.txt) so the restored DB dump isn't opened by a"
echo "   newer/older Nextcloud than the one that made it."
echo "3. Stop nextcloud, then restore the webroot (data+config+apps):"
echo "     rsync -a --delete ${TARGET}${DATA_ROOT:-/data}/.nextcloud/nexcloud/ \${DATA_ROOT:-/data}/.nextcloud/nexcloud/"
echo "4. Start only db-next, then import the dump:"
echo "     make up STACK=nextcloud   # after commenting out app/web/news-updater, or scale them to 0"
echo "     docker compose ... exec -T db-next sh -c 'psql -U \"\$POSTGRES_USER\" \"\${POSTGRES_DB:-\$POSTGRES_USER}\"' < $staging/nextcloud-db.sql"
echo "5. make up STACK=nextcloud, verify, then remove the digest pin once happy."
echo "======================================================================"
