# Sauvegarde et restauration

Chargé à la demande depuis `CLAUDE.md`. À lire avant de toucher à
`scripts/backup.sh`/`scripts/restore.sh` ou de lancer une restauration.

## Sauvegarde — repères rapides

- `make backup` (aussi via cron dimanche 3h) : dump `pg_dump` Nextcloud +
  manifeste des digests d'images en cours d'exécution + `restic backup` +
  `restic check --read-data-subset=5%` + `restic forget --group-by host
  --keep-weekly 8 --prune` + tag git `backup-YYYY-MM-DD` si l'infra a changé
  depuis le dernier tag de ce type, poussé sur `origin`.
  **Le tag est sauté si `user.name`/`user.email` ne sont pas configurés** pour
  le repo : sur un déploiement tiers `git tag -a` échouerait faute d'identité
  et ferait planter le script *après* un backup pourtant réussi, et on ne veut
  pas de tags de sauvegardes qui ne sont pas les nôtres. Le test porte sur
  `git config --get`, pas sur `git var GIT_COMMITTER_IDENT` : ce dernier
  fabrique une identité depuis le compte Unix et le hostname, donc il
  réussirait là où `git tag -a` échoue.
  Le dump est **supprimé du staging après le backup** : il porte les hachages
  de mots de passe et les jetons d'application de Nextcloud, il n'a pas à
  rester en clair sur le disque une fois dans le dépôt chiffré.
  **`--group-by host` n'est pas cosmétique** : `restic forget` groupe par
  défaut sur (host, paths) et applique la politique à chaque groupe
  *séparément*, donc chaque changement de la liste de chemins crée un nouveau
  groupe et l'ancien cesse d'être élagué — il l'a déjà fait, en épinglant
  ~10 GiB. Pire que l'espace perdu : juste après un tel changement, la
  rétention réelle retombe à un seul snapshot alors que le message annonce
  toujours 8 semaines.
- `make restore SNAPSHOT=<id|latest>` : restaure dans un dossier à part et
  affiche les étapes manuelles — ne touche jamais le live automatiquement.
- **Mot de passe restic dans `~/.config/server-restic-password`** (hors du
  repo, généré au premier `make backup`) — pas de copie ailleurs = dépôt
  illisible en cas de perte. `sauvegarde/restic-password` est l'ancien
  emplacement : `backup.sh`/`restore.sh` s'y replient encore
  (`LEGACY_PASSWORD_FILE`), mais toute commande `restic` lancée à la main doit
  viser le nouveau chemin, sinon `Resolving password failed`.
- Les `.env` de chaque stack sont collectés **par boucle sur `$STACKS`**, pas
  listés à la main : un `.env` ajouté plus tard (c'est arrivé avec
  `jellyfin/.env`, qui porte les identifiants dont `make provision` a besoin)
  n'existerait sinon **nulle part ailleurs que sur ce disque** — gitignoré et
  non sauvegardé. La boucle rend le prochain couvert par construction.
- Délibérément **pas** sauvegardés : `library` (media, re-téléchargeable via
  arr), `.transmission/data`, `.jellyfin/cache` — énormes et jetables.
- **Exclusions internes aux arborescences sauvegardées** (tableau `excludes`
  de `backup.sh`, revu chemin par chemin avec l'utilisateur le 2026-09-24) :
  aperçus Nextcloud (`appdata_*/preview`, ~10 Gio avant une purge qui a
  divisé le snapshot du 20/09 par deux), `nextcloud.log*`/`audit.log*` (l'app
  `admin_audit` est d'ailleurs désactivée depuis le 2026-09-24 : elle suit le
  `loglevel` global, à 2, et jetait donc ses événements INFO depuis juin 2024 —
  ne pas la réactiver sans un `log.condition` dédié), logs et
  `MediaCover` des arr, `resources/` de recyclarr, `transmission.log`,
  `metadata/` de Jellyfin (rafraîchissement complet accepté après
  restauration). **Gardés exprès**, ne pas les proposer à nouveau : le code
  Nextcloud (l'entrypoint arbitre install/upgrade sur `version.php`,
  `custom_apps/` porte la beta de News), les `Backups/` des arr (seule copie
  cohérente de leur base, restic lit les `.db` à chaud), corbeille et
  versions Nextcloud. Critère de tri : ce qui change chaque semaine coûte,
  le reste est dédupliqué et ne coûte qu'une fois.
- Résilience visée : perte du disque `DATA_ROOT` → restauration depuis
  `sauvegarde/` (sur un disque différent). Perte de celui-ci → seul
  l'infra-as-code est récupérable depuis GitHub, la sauvegarde restic est
  perdue avec (accepté pour l'instant, pas d'offsite).

## Sauvegarde et restauration

- **`scripts/backup.sh` dumpait silencieusement la mauvaise base Postgres
  depuis le début** (repéré en testant `make restore` pour de vrai — jusque-là
  jamais exercé) : `pg_dump -U "$POSTGRES_USER" "${POSTGRES_DB:-$POSTGRES_USER}"`
  retombe sur `$POSTGRES_USER` (« postgres », le superuser d'amorçage) quand
  `POSTGRES_DB` n'est pas définie — et `nextcloud/.env` ne la définit jamais.
  Toutes les sauvegardes précédentes avaient donc un `nextcloud-db.sql` de 26
  lignes au lieu du vrai dump (279 tables). La base réelle s'appelle
  `nextcloud`, codée en dur sur le service `app`. Fix : fallback
  `${POSTGRES_DB:-nextcloud}`.
  **Deux leçons générales** : un chemin de restauration jamais exercé peut
  cacher ce genre de bug arbitrairement longtemps ; et un `${VAR:-default}`
  est un piège quand `VAR` n'est définie nulle part — le fallback devient le
  cas normal, silencieusement.
- **`jellyfin.db` corrompue (`SQLite Error 11: database disk image is
  malformed`)** : ne pas juste supprimer le fichier pour forcer une
  régénération — `config/config/system.xml` garde
  `IsStartupWizardCompleted=true`, donc Jellyfin se croit en mise à niveau et
  plante en boucle sur d'anciennes migrations qui supposent un schéma
  existant. Avant de reset, essayer une réparation : `sqlite3 <copie>.db
  ".recover" > dump.sql`, réimporter dans un fichier neuf,
  `PRAGMA integrity_check`/`foreign_key_check`, `REINDEX; VACUUM;` — a
  fonctionné sans perte de données malgré la corruption. Si reset complet
  malgré tout : repasser `IsStartupWizardCompleted` à `false`.
- **`data/SQLiteBackups/` de Jellyfin est vide par conception**, ce n'est pas
  une sauvegarde périodique : Jellyfin y copie `jellyfin.db` juste avant une
  migration de schéma et l'efface dès qu'elle réussit (`will attempt to backup
  the file now` … `Attempt to cleanup JellyfinDb backup` dans les logs, vérifié
  le 2026-09-24). Une copie n'y reste qu'après une migration ratée. Ne pas le
  surveiller ni s'inquiéter de le voir vide. La sauvegarde native de Jellyfin
  (`/Backup`) n'est volontairement pas utilisée : l'utilisateur ne veut pas de
  sauvegardes en plus du backup restic, la seule copie de `jellyfin.db` est
  donc celle, à chaud, du snapshot hebdo.

