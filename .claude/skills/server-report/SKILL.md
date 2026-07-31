---
name: server-report
description: Passe en revue l'état complet du serveur (containers, cron, disque, sauvegarde, logs de tous les services) et produit un rapport synthétique en français, distinguant ce qui nécessite une action de ce qui est du bruit résolu. À utiliser quand l'utilisateur demande un rapport d'état, un check de santé du serveur, ou de repasser sur les logs/l'état du projet.
---

# server-report — audit d'état + logs du serveur

## Objectif

Ne pas juste dumper des logs bruts : chaque anomaly trouvée doit être
**corrélée dans le temps** (avec un événement connu : redémarrage d'une
stack, réinstallation du crontab, etc.) et **classée** — one-off résolu,
récurrent à surveiller, ou action requise. Le rapport final doit tenir en
un tableau/quelques paragraphes lisibles, pas en pages de grep.

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

Ne jamais reporter une info sensible trouvée dans les logs (domaine réel,
IP, etc.) telle quelle si ce n'est pas nécessaire — décrire génériquement
("le domaine principal") plutôt que de la recopier, même si ce n'est pas
un secret à proprement parler (cf. `CLAUDE.md` : repo public, jamais de
PII/valeur propre au déploiement en clair dans un fichier versionné — le
même réflexe s'applique par prudence à ce qu'on écrit dans une réponse).

## 5. Git

```bash
git status --porcelain=v1
git status -sb   # confirme l'alignement avec origin/<branche>
```

## 6. Format du rapport

Un tableau `Service | Constat | Statut` pour les trouvailles de logs
(Statut = résolu / à surveiller / action requise), plus une section courte
pour containers/cron/disque/backup/git. Terminer par un résumé d'une
phrase : rien de cassé, ou la liste des actions réellement nécessaires.
Ne pas alarmer sur du bruit déjà résolu — le signaler pour mémoire, pas
comme un problème actif.
