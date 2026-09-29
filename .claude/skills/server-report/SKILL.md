---
name: server-report
description: Passe en revue l'état complet du serveur (containers, cron, disque, sauvegarde, imports arr en attente, logs de tous les services) et rapporte en français uniquement ce qui ne va pas, en écartant le bruit déjà résolu. À utiliser quand l'utilisateur demande un rapport d'état, un check de santé du serveur, ou de repasser sur les logs/l'état du projet.
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
# le mot de passe vit hors du repo depuis un durcissement de backup.sh —
# `sauvegarde/restic-password` est l'ancien emplacement, encore accepté en repli
export RESTIC_PASSWORD_FILE="$HOME/.config/server-restic-password"
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

**Bruit connu, à ne pas remonter** (demandé par l'utilisateur le
2026-09-28) — vérifier la condition, puis l'omettre du rapport, même en
ligne « résolu » :

- **`vpn-transmission-vpn` : `AEAD Decrypt error: bad packet ID (may be a
  replay)`**. Ce sont des paquets UDP qui arrivent dans le désordre côté
  AirVPN, en rafales pouvant atteindre des dizaines de milliers de lignes.
  Ils sont inoffensifs **tant que le tunnel tient** : le container est
  healthy et n'a pas redémarré, et `docker logs --since 48h` ne montre ni
  `Inactivity timeout`, ni `SIGUSR1`/`SIGTERM`, ni `Restart pause`. Il n'y a
  pas non plus plusieurs `Initialization Sequence Completed` (la
  renégociation TLS horaire est normale et n'en produit pas). Exclure ce
  motif (`grep -v AEAD`) avant de chercher les vraies erreurs. Ne le
  remonter que si l'une de ces conditions tombe : ce ne sont alors plus les
  AEAD le constat, c'est la coupure du tunnel.
- **`traefik-dashboard-1` : `404` sur `/remote.php/dav/public-calendars/…`**
  (décidé le 2026-09-09). Un abonnement calendrier client pointe sur le
  domaine nu / `www.` au lieu de `nextcloud.` ; l'utilisateur a choisi de ne
  pas le corriger. Ni ligne de tableau, ni mention dans la ligne de
  couverture. Les autres 404 de ce vhost (scans WAN `wp-login.php`, `.env`,
  `.git/config`…) restent traités normalement.

Les fichiers de log internes (`/config/logs/*.txt` dans les conteneurs
Servarr) sont plus fiables que `docker logs` quand il faut une fenêtre
horaire précise : horodatés à la seconde et non tronqués par
`max-size`/`max-file`. Ils tournent, donc dédupliquer les occurrences vues
dans plusieurs fichiers.

## 5. Imports en attente (Sonarr/Radarr)

Un téléchargement à 100 % qui n'est pas importé ne se voit nulle part
ailleurs : container healthy, aucune erreur dans les logs, aucune carte du
dashboard. Seule la file de l'arr le montre. On lance donc le diagnostic du
skill **`manual-import`**, **en lecture seule** : jamais `apply`, `assign`
ni purge depuis ce rapport.

```bash
python3 scripts/manual-import.py list --json | python3 -c '
import json, sys
from collections import Counter
TEMP = ("TBA title",)   # rejets qui se lèvent seuls, cf. plus bas
data = json.load(sys.stdin)
for arr, items in data.items():
    groups = Counter()
    for i in items:
        why = "; ".join(i.get("reasons") or i.get("queue_reasons") or [])
        kind = "temporaire" if any(t in why for t in TEMP) else i["kind"]
        groups[(kind, i.get("target") or i["title"], why)] += 1
    for (kind, target, why), n in groups.items():
        print(f"{arr}: [{kind}] {target} ×{n} — {why[:90]}")
'
```

Un arr injoignable ou sans clé d'API n'apparaît pas dans le JSON. Le script
l'écrit sur stderr (`ERREUR sonarr : …`), qui s'affiche à côté de la sortie
ci-dessus : c'est une action requise.

Restitution, **succincte** : une ligne de tableau par titre bloqué. Les
releases multiples d'un même épisode tiennent sur une ligne (`×2`). Pas de
chemins, de hash ni de `queueId` : c'est le travail du skill
`manual-import`, que l'utilisateur lancera s'il y a quelque chose à faire.

| Famille (`kind`) | Statut dans le rapport |
|---|---|
| `importable`, `assign` (à rattacher) | action requise (lancer `/manual-import`) |
| `refuse` (vrai rejet : doublon, pas une amélioration) | action requise (purge à confirmer) |
| `temporaire` : *« Episode has a TBA title and recently aired »* | à surveiller. Préciser la date de levée : au plus tard 48 h après la diffusion, ou plus tôt si TVDB publie le titre (`episodeTitleRequired = always`). Ne passer en action requise que si cette date est dépassée. |
| `erreur` (l'appel `manualimport` a échoué) | action requise, rapporter le message tel quel |

Rien en attente → le mentionner dans la ligne de couverture (« aucun import
en attente »), pas de ligne de tableau.

Le script range le motif TBA en `refuse`, au même rang qu'un vrai rejet :
c'est le filtre `TEMP` ci-dessus qui le requalifie. Si un autre motif
temporaire apparaît, l'ajouter à `TEMP` plutôt que de le remonter comme un
refus.

## 6. Git

```bash
git status --porcelain=v1
git status -sb   # confirme l'alignement avec origin/<branche>
```

## 7. Format du rapport — anomalies seulement

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
