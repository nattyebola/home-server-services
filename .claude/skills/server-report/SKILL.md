---
name: server-report
description: Passe en revue l'état complet du serveur (containers, cron, disque, sauvegarde, logs de tous les services) et rapporte en français uniquement ce qui ne va pas, en écartant le bruit déjà résolu. À utiliser quand l'utilisateur demande un rapport d'état, un check de santé du serveur, ou de repasser sur les logs/l'état du projet.
---

# server-report — audit d'état + logs du serveur

## Objectif

**Auditer largement, ne rapporter que les anomalies.** L'utilisateur a
demandé explicitement le 2026-08-03 que ce qui est au vert ne figure pas
dans le rapport : pas de tableau de containers sains, pas de lignes
« disque OK », pas de détail sur une sauvegarde à jour.

Ça ne change **rien** aux vérifications à faire : toutes les sections
ci-dessous s'exécutent quand même, intégralement. C'est la restitution qui
filtre — on ne peut pas affirmer que rien ne va mal sans avoir regardé.

Chaque anomalie retenue doit être **corrélée dans le temps** (avec un
événement connu : redémarrage d'une stack, réinstallation du crontab, etc.)
et **classée** — one-off résolu, récurrent à surveiller, ou action requise.

## 1. Containers

```bash
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.RunningFor}}'
docker ps --filter health=unhealthy --format '{{.Names}}: {{.Status}}'
docker ps -a --filter status=exited --filter status=restarting --filter status=paused --filter status=dead --format '{{.Names}}: {{.Status}}'
docker ps -a --format '{{.Names}}: créé {{.CreatedAt}}' | sort -t: -k2
```

Le dernier tri par date de création est le signal le plus utile : une
stack recréée récemment (alors que les autres tournent depuis des jours)
indique une intervention manuelle (update, restart) — pas une panne, mais
ça explique souvent une carte rouge ou des erreurs de démarrage isolées
ailleurs dans les logs (ex. `cross-seed` qui n'arrive pas à contacter
`sonarr` dans les secondes qui suivent son propre redémarrage).

## 2. Disque, sauvegarde

Depuis la racine du repo (`server/`) :

```bash
source .env.shared
df -h "$DATA_ROOT"
df -h sauvegarde
export RESTIC_REPOSITORY="$PWD/sauvegarde/restic-repo"
export RESTIC_PASSWORD_FILE="$PWD/sauvegarde/restic-password"
restic snapshots --latest 3
```

## 3. Cron / tâches planifiées

C'est la partie qui piège le plus facilement — deux vérifications
distinctes, pas une seule :

1. **Les marqueurs** (`$DATA_ROOT/.cron-status/*`) : âge de chaque fichier
   vs l'intervalle attendu de sa tâche (voir `SCHEDULED_TASKS` dans
   `scripts/generate-dashboard.py`, marge `CRON_MARKER_SLACK` incluse).
   Un marqueur absent ou périmé n'est PAS automatiquement une panne — voir
   point 2.
2. **Le crontab réellement installé peut être en retard sur le fichier
   versionné** (`scripts/crontab`) si `make cron-install` n'a pas été
   relancé après une modif. Comparer :
   ```bash
   crontab -l
   diff <(crontab -l) <(sed -e 's/__REPO_ROOT__/...réel.../' scripts/crontab)
   ```
   Si ça ne suffit pas à trancher (le `crontab -l` peut déjà être à jour
   alors qu'un job du jour a tourné AVANT la réinstallation), remonter dans
   `journalctl` (ou `/var/log/syslog*`) sur la fenêtre horaire du job en
   question pour voir la commande **réellement exécutée** par cron à ce
   moment-là :
   ```bash
   journalctl --since "today 00:00:00" --until "today 00:20:00" | grep -i cron
   ```
   Une commande loggée sans le guard `require-running.sh`/sans le
   `&& date +%s > .../marqueur` alors que le fichier `scripts/crontab`
   actuel les contient = le crontab live n'avait pas encore été réinstallé
   à ce moment précis. C'est comme ça qu'on a expliqué, le 2026-07-30, une
   carte "Recyclarr + overrides arr" rouge qui n'était pas une panne mais
   simplement un job qui n'avait pas encore eu sa première occasion de
   tourner sous le nouveau format guardé.
   `last reboot` permet d'écarter/confirmer un redémarrage hôte comme
   cause d'une recréation de containers.

## 4. Logs de chaque service

Balayage large d'abord, puis creuser ce qui ressort :

```bash
for c in traefik-traefik-1 arr-cross-seed-1 nextcloud-app-1 vpn-transmission-vpn-1 seerr-seerr-1 jellyfin-jellyfin-1 arr-sonarr-1 arr-radarr-1 arr-prowlarr-1; do
  echo "=== $c ==="
  docker logs --tail 200 "$c" 2>&1 | grep -iE "error|warn|fail|panic" | tail -10
done
```

Pour chaque motif trouvé, avant de le reporter comme un problème :
- **Fréquence** : `docker logs --since 48h <container> 2>&1 | grep -c "<motif>"`,
  puis `... | cut -c1-13 | sort | uniq -c` pour voir si c'est concentré sur
  une seule fenêtre (rafale ponctuelle) ou étalé (récurrent/chronique).
- **Résolution** : `docker logs --since <horodatage de la première
  occurrence> <container> 2>&1 | grep "<motif>"` — si rien après quelques
  minutes/heures, c'est résolu, à mentionner comme tel sans donner
  l'impression que ça continue.
- **Contexte** : `grep -B3` autour de l'erreur si la ligne seule ne dit pas
  ce qui a échoué (stack traces, etc.).
- **Ne jamais reprendre le libellé d'une erreur au pied de la lettre** —
  piège rencontré le 2026-08-03 : les `API Request Limit reached for X
  (Prowlarr)` de Sonarr ont été rapportés comme des quotas d'indexeur
  atteints, alors que la ligne `<error>` juste au-dessus disait
  `due to recent failures` : c'était le backoff d'échec de Prowlarr, sur
  trois causes racines distinctes (401 d'auth, 530 Cloudflare, un seul vrai
  quota). Un composant qui relaie l'erreur d'un autre la requalifie souvent
  à tort. Pour tout ce qui touche aux 429/désactivations d'indexeurs,
  déléguer au skill **`indexer-quota`** plutôt que de conclure ici.

Les fichiers de log internes (`/config/logs/*.txt` dans les conteneurs
Servarr) sont plus fiables que `docker logs` quand il faut une fenêtre
horaire précise : horodatés à la seconde et non tronqués par
`max-size`/`max-file`. Ils tournent, donc dédupliquer les occurrences vues
dans plusieurs fichiers.

## 5. Git

```bash
git status --porcelain=v1
git status -sb   # confirme l'alignement avec origin/<branche>
```

## 6. Format du rapport — anomalies seulement

Structure :

1. **Une seule ligne de couverture**, en tête, listant ce qui a été vérifié
   et trouvé sain — sans détail ni tableau. Ex. : *« Vérifiés et au vert :
   16/16 containers healthy, disque (41 %/48 %), sauvegarde (snapshot du
   02/08), crontab en phase, git propre. »* C'est le seul endroit où le vert
   apparaît, et il tient en une phrase. Ne pas le supprimer : sans lui, on
   ne distingue pas « vérifié et sain » de « pas vérifié ».
2. **Un tableau `Service | Constat | Statut`** des anomalies uniquement
   (Statut = résolu / à surveiller / action requise). Un service sans
   anomalie n'a pas de ligne.
3. **Le résumé final**, une phrase : soit la liste des actions réellement
   nécessaires, soit « rien qui demande une action ».

Règles de restitution :

- **Le bruit résolu ne mérite qu'une ligne de tableau**, pas un paragraphe :
  une rafale terminée, une panne tracker passée, un warning cosmétique
  récurrent. Le mentionner sert à dire « vu, écarté » — pas à documenter.
- **Ne pas gonfler un constat pour remplir le rapport.** Un rapport à deux
  lignes d'anomalies est un bon rapport.
- **Chiffrer toute anomalie retenue** (occurrences, fenêtre horaire,
  première/dernière occurrence) — sans ça, impossible de la classer.
- **Distinguer une cause externe d'une cause locale** : panne tracker,
  scan internet sur un vhost WAN, quota d'un service tiers. Externe et
  terminé = ligne de tableau en « résolu », pas une action.
- Ne jamais reporter une info sensible trouvée dans les logs (domaine réel,
  IP, jeton de partage, clé d'API) telle quelle si ce n'est pas nécessaire
  — décrire génériquement (« le domaine principal », « une URL d'abonnement
  calendrier »), cf. `CLAUDE.md` : repo public, jamais de PII ni de valeur
  propre au déploiement en clair.
