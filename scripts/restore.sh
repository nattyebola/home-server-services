#!/usr/bin/env bash
# Restores a restic snapshot to a scratch directory and prints the manual
# steps to bring a stack back — deliberately does NOT overwrite live data or
# import into a running DB by itself; a restore is rare and high-stakes
# enough to want a human looking at each step.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# .env.shared est FACULTATIF ici : sur une machine neuve (le cas même d'une
# restauration après perte du disque), il n'existe pas encore — il est DANS la
# sauvegarde. L'exiger bloquait `make restore` au premier pas ; le recréer à la
# main depuis le .example, comme le message l'invitait à faire, faisait
# construire des commandes fausses dès que DATA_ROOT ou le chemin du checkout
# différaient de l'original. Exercé le 2026-09-29 sur une VM vierge.
if [ -f "$REPO_ROOT/.env.shared" ]; then
	set -a
	source "$REPO_ROOT/.env.shared"
	set +a
fi
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
# `rsync -a --delete "$TARGET$SRC_DATA/..." "$DATA_ROOT/..."`. Un TARGET vide
# les transformerait en rsync d'un dossier VERS LUI-MÊME avec --delete — une
# commande qu'un opérateur en pleine restauration copierait sans la relire.
: "${TARGET:?TARGET vide — refus de continuer}"

echo "==> available snapshots"
restic snapshots

echo "==> restoring snapshot '$SNAPSHOT' to $TARGET"
mkdir -p "$TARGET"
restic restore "$SNAPSHOT" --target "$TARGET"

staging="$(find "$TARGET" -type d -name .staging | head -n1)"

# Chemins D'ORIGINE, lus dans le snapshot : restic restaure sous
# "$TARGET/<chemin absolu d'origine>", qui peut différer de la machine actuelle
# (autre DATA_ROOT, checkout ailleurs). Le .env.shared restauré dit les deux.
restored_shared="$(find "$TARGET" -maxdepth 8 -name .env.shared -not -path '*/.staging/*' | head -n1)"
if [ -z "$restored_shared" ]; then
	echo "Aucun .env.shared dans ce snapshot : impossible de savoir où vivaient les données." >&2
	exit 1
fi
SRC_REPO="$(dirname "${restored_shared#$TARGET}")"
SRC_DATA="$(grep '^DATA_ROOT=' "$restored_shared" | cut -d= -f2-)"
: "${SRC_DATA:?DATA_ROOT absent du .env.shared restauré}"
# Destination : le DATA_ROOT de la machine actuelle s'il est déjà configuré,
# sinon le même qu'à l'origine (cas normal : on refait la même machine).
DATA_ROOT="${DATA_ROOT:-$SRC_DATA}"

# Commit de l'infra au moment de la sauvegarde : pour information seulement.
# On restaure avec la version COURANTE du dépôt (plus de tags `backup-*`
# depuis le 2026-09-29) — revenir à l'infra de la sauvegarde réintroduisait des
# bugs corrigés depuis, et la version courante relit très bien les données des
# précédentes (restauration complète exercée sur une VM vierge le même jour).
recorded=""
if [ -n "$staging" ] && [ -s "$staging/infra-commit.txt" ]; then
	recorded="$(cat "$staging/infra-commit.txt")"
fi

echo
echo "======================================================================"
echo "Restauré sous : $TARGET"
if [ -n "$staging" ]; then
	echo
	echo "-- état de l'infra au moment de la sauvegarde --"
	echo "   ${recorded:-(infra-commit.txt manquant)}  (pour information)"
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
if [ "$SRC_DATA" != "$DATA_ROOT" ] || [ "$SRC_REPO" != "$REPO_ROOT" ]; then
	echo "⚠ Chemins différents de l'origine — les commandes ci-dessous en tiennent compte :"
	echo "     données : $SRC_DATA  ->  $DATA_ROOT"
	echo "     dépôt   : $SRC_REPO  ->  $REPO_ROOT"
	echo "   Après l'étape 1, corriger DATA_ROOT dans $REPO_ROOT/.env.shared, et les"
	echo "   chemins des docker-compose.override.yml copiés."
	echo
fi
echo "0. Rester sur la version ACTUELLE du dépôt : elle relit les données des"
echo "   versions précédentes. Seulement si un service refuse ses données"
echo "   restaurées (migration de schéma à l'envers) : épingler son image sur le"
echo "   digest du manifeste ci-dessus, dans son docker-compose.override.yml."
echo
echo "1. Secrets et configuration hors dépôt (gitignorés, donc absents de git) :"
# Énumérés depuis ce qui a RÉELLEMENT été restauré, pas depuis une liste figée :
# la liste des fichiers sauvegardés a changé dans le temps (jellyfin/.env ajouté
# le 2026-08-05, prowlarr-indexers.json le 2026-08-09), donc un snapshot ancien
# n'en contient pas autant qu'un récent. Afficher une liste théorique ferait
# copier des chemins inexistants — constaté en exerçant la procédure sur le
# snapshot du 2026-08-09, antérieur au dernier de ces ajouts.
# Même liste que les ajouts de scripts/backup.sh : overrides compose et
# vpn/custom/ (dossier, d'où le `cp -r`).
restored_conf="$(find "${TARGET}${SRC_REPO}" -maxdepth 3 \
	\( -name ".env" -o -name ".env.shared" -o -name "prowlarr-indexers.json" \
	-o -name "docker-compose.override.yml" \) \
	2>/dev/null | sort)"
restored_vpn="${TARGET}${SRC_REPO}/vpn/custom"
if [ -n "$restored_conf" ]; then
	while IFS= read -r f; do
		echo "     cp $f  ${REPO_ROOT}${f#$TARGET$SRC_REPO}"
	done <<<"$restored_conf"
	[ -d "$restored_vpn" ] && echo "     cp -r $restored_vpn/.  $REPO_ROOT/vpn/custom/"
else
	echo "     (aucun fichier de configuration dans ce snapshot — vérifier son contenu)"
fi
echo "   Sans eux, ni make api-keys ni make provision ne peuvent tourner."
echo "   Un fichier attendu et absent = il a été ajouté à scripts/backup.sh APRÈS"
echo "   cette sauvegarde ; le recréer depuis son .example et le renseigner."
echo
echo "2. Arborescences de service, stack par stack, CONTENEURS ARRÊTÉS"
echo "   (sur une machine neuve : mkdir -p $DATA_ROOT, et rien n'est encore lancé) :"
echo "     make down STACK=arr && mkdir -p ${DATA_ROOT}/.arr/ && rsync -a --delete ${TARGET}${SRC_DATA}/.arr/ ${DATA_ROOT}/.arr/"
echo "     make down STACK=jellyfin && mkdir -p ${DATA_ROOT}/.jellyfin/config/ && rsync -a --delete ${TARGET}${SRC_DATA}/.jellyfin/config/ ${DATA_ROOT}/.jellyfin/config/"
echo "     make down STACK=seerr && mkdir -p ${DATA_ROOT}/.seerr/config/ && rsync -a --delete ${TARGET}${SRC_DATA}/.seerr/config/ ${DATA_ROOT}/.seerr/config/"
echo "     make down STACK=vpn && mkdir -p ${DATA_ROOT}/.transmission/config/ && rsync -a --delete ${TARGET}${SRC_DATA}/.transmission/config/ ${DATA_ROOT}/.transmission/config/"
echo "     make down STACK=komga && mkdir -p ${DATA_ROOT}/.komga/config/ && rsync -a --delete ${TARGET}${SRC_DATA}/.komga/config/ ${DATA_ROOT}/.komga/config/"
echo "   (mkdir -p : sur une machine neuve les dossiers parents n'existent pas, et"
echo "   rsync ne crée que le dernier niveau.)"
echo "   Restaurer à chaud écraserait des fichiers qu'un service a ouverts —"
echo "   les bases SQLite de Sonarr/Radarr/Prowlarr en particulier."
echo "   Ces arborescences portent ce que le dépôt ne sait PAS recréer :"
echo "   indexeurs Prowlarr, plugins et transcodage VAAPI de Jellyfin,"
echo "   settings.json de Transmission (dont le peer-port ouvert côté VPN)."
echo
echo "3. Nextcloud — webroot puis base, dans cet ordre :"
echo "     make down STACK=nextcloud"
echo "     mkdir -p ${DATA_ROOT}/.nextcloud/nexcloud/ && rsync -a --delete ${TARGET}${SRC_DATA}/.nextcloud/nexcloud/ ${DATA_ROOT}/.nextcloud/nexcloud/"
echo "   Démarrer db-next SEUL (app ne doit pas voir une base vide) :"
echo "     make rebuild STACK=nextcloud SERVICE=db-next"
# `make rebuild` rend la main dès le démarrage du conteneur, pas quand Postgres
# accepte les connexions : sur un cluster neuf, l'initialisation prend quelques
# secondes et un psql lancé aussitôt échouait (« No such file or directory » sur
# la socket) — exercé le 2026-09-29 sur une VM vierge. Attente en TCP et pas sur
# la socket : pendant l'initialisation, l'image lance un serveur TEMPORAIRE qui
# n'écoute que sur la socket, puis l'arrête ; un pg_isready par la socket
# pouvait réussir sur lui et laisser l'import tomber sur son arrêt.
echo "   Attendre que Postgres accepte les connexions (initialisation d'un cluster neuf) :"
echo "     until docker exec nextcloud-db-next-1 sh -c 'pg_isready -h 127.0.0.1 -U \"\$POSTGRES_USER\"' >/dev/null 2>&1; do sleep 2; done"
if [ -n "$staging" ] && [ -f "$staging/nextcloud-db.sql" ]; then
	psql_cmd="docker exec -i nextcloud-db-next-1 sh -c 'psql -U \"\$POSTGRES_USER\" -d postgres'"
	# Rôles d'abord : la base appartient au rôle oc_<admin> de Nextcloud, que
	# pg_dump ne sauvegarde pas. Absent des snapshots d'avant le 2026-09-29.
	if [ -f "$staging/nextcloud-roles.sql" ]; then
		echo "   Rôles (l'erreur « role \"postgres\" already exists » est attendue) :"
		echo "     $psql_cmd < $staging/nextcloud-roles.sql"
	else
		echo "   ⚠ pas de nextcloud-roles.sql (snapshot antérieur au 2026-09-29) : recréer"
		echo "     le rôle à la main, nom et mot de passe = dbuser/dbpassword de"
		echo "     ${DATA_ROOT}/.nextcloud/nexcloud/config/config.php :"
		echo "     CREATE ROLE <dbuser> LOGIN CREATEDB PASSWORD '<dbpassword>';"
	fi
	# Toujours supprimer la base d'abord : depuis que db-next porte
	# POSTGRES_DB=nextcloud (2026-09-29, sans quoi une installation neuve
	# échouait), un cluster neuf naît AVEC une base `nextcloud` vide, possédée par
	# POSTGRES_USER. Le dump en --create échouerait sur « already exists », un
	# ancien dump s'importerait dans une base au mauvais propriétaire.
	echo "   Supprimer la base (vide sur un cluster neuf : créée par POSTGRES_DB) :"
	echo "     docker exec nextcloud-db-next-1 sh -c 'dropdb -U \"\$POSTGRES_USER\" --if-exists nextcloud'"
	# Dump en --create depuis le 2026-09-29 : il crée lui-même la base. Les
	# plus anciens supposent qu'elle existe déjà.
	if grep -q '^CREATE DATABASE' "$staging/nextcloud-db.sql"; then
		echo "   Base (le dump la recrée lui-même, avec son propriétaire) :"
	else
		echo "   Base (dump antérieur au 2026-09-29, sans CREATE DATABASE) :"
		echo "     docker exec nextcloud-db-next-1 sh -c 'createdb -U \"\$POSTGRES_USER\" -O <dbuser> nextcloud'"
		psql_cmd="docker exec -i nextcloud-db-next-1 sh -c 'psql -U \"\$POSTGRES_USER\" -d nextcloud'"
	fi
	echo "     $psql_cmd < $staging/nextcloud-db.sql"
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
