#!/usr/bin/env bash
# Restores a restic snapshot to a scratch directory and prints the manual
# steps to bring a stack back — deliberately does NOT overwrite live data or
# import into a running DB by itself; a restore is rare and high-stakes
# enough to want a human looking at each step.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a
source "$REPO_ROOT/.env.shared"
set +a
BACKUP_DIR="$REPO_ROOT/sauvegarde"
export RESTIC_REPOSITORY="$BACKUP_DIR/restic-repo"
# Même résolution que scripts/backup.sh : hors de l'arborescence sauvegardée
# par défaut, l'ancien emplacement accepté en repli.
RESTIC_PASSWORD_FILE="${RESTIC_PASSWORD_FILE:-$HOME/.config/server-restic-password}"
[ -f "$RESTIC_PASSWORD_FILE" ] || RESTIC_PASSWORD_FILE="$BACKUP_DIR/restic-password"
export RESTIC_PASSWORD_FILE

SNAPSHOT="${1:-latest}"
TARGET="${2:-$BACKUP_DIR/restore-$SNAPSHOT}"
# Garde-fou : les instructions affichées plus bas construisent des commandes
# `rsync -a --delete "$TARGET$DATA_ROOT/..." "$DATA_ROOT/..."`. Un TARGET vide
# les transformerait en rsync d'un dossier VERS LUI-MÊME avec --delete — une
# commande qu'un opérateur en pleine restauration copierait sans la relire.
: "${TARGET:?TARGET vide — refus de continuer}"
: "${DATA_ROOT:?DATA_ROOT absent de .env.shared}"

echo "==> available snapshots"
restic snapshots

echo "==> restoring snapshot '$SNAPSHOT' to $TARGET"
mkdir -p "$TARGET"
restic restore "$SNAPSHOT" --target "$TARGET"

staging="$(find "$TARGET" -type d -name .staging | head -n1)"

# Le commit enregistré peut ne plus exister : l'historique a été réécrit le
# 2026-08-09 (voir CLAUDE.md), ce qui a changé TOUS les SHA. Exercé pour de
# vrai le même jour — l'étape 1 échouait en l'état. On propose donc le tag
# `backup-*` correspondant, qui lui a suivi la réécriture.
infra_ref=""
infra_note=""
if [ -n "$staging" ] && [ -s "$staging/infra-commit.txt" ]; then
	recorded="$(cat "$staging/infra-commit.txt")"
	if git -C "$REPO_ROOT" cat-file -e "$recorded" 2>/dev/null; then
		infra_ref="$recorded"
	else
		infra_ref="$(git -C "$REPO_ROOT" tag --list 'backup-*' --sort=-creatordate | head -n1)"
		infra_note="le commit enregistré ($recorded) n'existe plus (historique réécrit) — repli sur le tag ci-dessus"
	fi
fi

echo
echo "======================================================================"
echo "Restauré sous : $TARGET"
if [ -n "$staging" ]; then
	echo
	echo "-- état de l'infra au moment de la sauvegarde --"
	if [ -n "$infra_ref" ]; then
		echo "   $infra_ref"
		[ -n "$infra_note" ] && echo "   ⚠ $infra_note"
	else
		echo "   (infra-commit.txt manquant)"
	fi
	echo
	echo "-- digests des images en cours d'exécution alors --"
	cat "$staging/image-manifest.txt" 2>/dev/null || echo "   (manquant)"
fi

echo
echo "-- étapes manuelles --"
echo "Rien n'a été écrit hors de $TARGET : la suite est à faire à la main,"
echo "une restauration étant assez rare et risquée pour mériter un humain à"
echo "chaque étape."
echo
echo "0. Se remettre sur la recette d'infra de cette sauvegarde :"
if [ -n "$infra_ref" ]; then
	echo "     git -C $REPO_ROOT checkout $infra_ref"
else
	echo "     (référence inconnue — repartir du tag backup-* le plus proche)"
fi
echo "   Puis épingler les images du manifeste ci-dessus dans un"
echo "   docker-compose.override.yml, pour ne pas ouvrir des données restaurées"
echo "   avec une version applicative différente de celle qui les a écrites."
echo
echo "1. Secrets et configuration hors dépôt (gitignorés, donc absents de git) :"
# Énumérés depuis ce qui a RÉELLEMENT été restauré, pas depuis une liste figée :
# la liste des fichiers sauvegardés a changé dans le temps (jellyfin/.env ajouté
# le 2026-08-05, prowlarr-indexers.json le 2026-08-09), donc un snapshot ancien
# n'en contient pas autant qu'un récent. Afficher une liste théorique ferait
# copier des chemins inexistants — constaté en exerçant la procédure sur le
# snapshot du 2026-08-09, antérieur au dernier de ces ajouts.
restored_conf="$(find "${TARGET}${REPO_ROOT}" -maxdepth 3 \
	\( -name ".env" -o -name ".env.shared" -o -name "prowlarr-indexers.json" \) \
	2>/dev/null | sort)"
if [ -n "$restored_conf" ]; then
	while IFS= read -r f; do
		echo "     cp $f  ${f#$TARGET}"
	done <<<"$restored_conf"
else
	echo "     (aucun fichier de configuration dans ce snapshot — vérifier son contenu)"
fi
echo "   Sans eux, ni make api-keys ni make provision ne peuvent tourner."
echo "   Un fichier attendu et absent = il a été ajouté à scripts/backup.sh APRÈS"
echo "   cette sauvegarde ; le recréer depuis son .example et le renseigner."
echo
echo "2. Arborescences de service, stack par stack, CONTENEURS ARRÊTÉS :"
echo "     make down STACK=arr && rsync -a --delete ${TARGET}${DATA_ROOT}/.arr/ ${DATA_ROOT}/.arr/"
echo "     make down STACK=jellyfin && rsync -a --delete ${TARGET}${DATA_ROOT}/.jellyfin/config/ ${DATA_ROOT}/.jellyfin/config/"
echo "     make down STACK=seerr && rsync -a --delete ${TARGET}${DATA_ROOT}/.seerr/config/ ${DATA_ROOT}/.seerr/config/"
echo "     make down STACK=vpn && rsync -a --delete ${TARGET}${DATA_ROOT}/.transmission/config/ ${DATA_ROOT}/.transmission/config/"
echo "   Restaurer à chaud écraserait des fichiers qu'un service a ouverts —"
echo "   les bases SQLite de Sonarr/Radarr/Prowlarr en particulier."
echo "   Ces arborescences portent ce que le dépôt ne sait PAS recréer :"
echo "   indexeurs Prowlarr, plugins et transcodage VAAPI de Jellyfin,"
echo "   settings.json de Transmission (dont le peer-port ouvert côté VPN)."
echo
echo "3. Nextcloud — webroot puis base, dans cet ordre :"
echo "     make down STACK=nextcloud"
echo "     rsync -a --delete ${TARGET}${DATA_ROOT}/.nextcloud/nexcloud/ ${DATA_ROOT}/.nextcloud/nexcloud/"
echo "   Démarrer db-next SEUL (commenter app/web/news-updater), puis importer :"
if [ -n "$staging" ]; then
	echo "     docker compose ... exec -T db-next sh -c 'psql -U \"\$POSTGRES_USER\" \"\${POSTGRES_DB:-nextcloud}\"' < $staging/nextcloud-db.sql"
else
	echo "     (dump introuvable dans ce snapshot — vérifier .staging/)"
fi
echo
echo "4. Tout relancer, vérifier, puis retirer l'épinglage des images :"
echo "     make up STACK=<chaque stack>   # traefik en premier"
echo "     make test                      # chemins destructifs de clearr"
echo "     make dashboard-refresh"
echo
echo "Ce qui n'est PAS dans la sauvegarde, par choix : la bibliothèque"
echo "(${DATA_ROOT}/library) et les téléchargements — retéléchargeables via les"
echo "arr — ainsi que le cache Jellyfin, régénéré tout seul."
echo "======================================================================"
