---
name: anime-vf
description: Bascule un anime déjà présent dans Sonarr sur le profil qualité qui préfère l'audio français (VF/MULTi/TRUEFRENCH), relance une recherche, et rapporte ce qui a été trouvé/grabé. À utiliser quand l'utilisateur demande une version VF (ou plus généralement une version différente de celle actuellement téléchargée) pour un anime dans Sonarr.
---

# anime-vf — basculer un anime sur le profil qualité VF et relancer la recherche

## Contexte

Sonarr choisit une release parmi celles disponibles chez les indexers (via
Prowlarr) en fonction du profil qualité assigné à la série : chaque profil a
un barème de scores par Custom Format (`formatItems`), et Sonarr grab/importe
automatiquement une release si son score bat celui du fichier déjà présent
(`upgradeAllowed: true` sur ces deux profils).

Deux profils existent pour ça, tous deux définis directement via l'API
Sonarr (pas gérés par recyclarr — `arr/recyclarr/recyclarr.yml` ne gère que
"WEB-2160p (Combined)" côté Sonarr, ces deux-là ne sont donc jamais
resynchronisés/écrasés par `recyclarr sync`) :

- **`Anime (Fansub)`** — profil par défaut des séries anime, Custom Format
  `FRENCH` scoré à 0 (neutre, sans effet).
- **`Anime (Fansub) VF`** — même profil, mais `FRENCH` scoré à 200 (au-dessus
  de `MULTi`=100 et `VOSTFR`=50 déjà présents par défaut sur ce profil).
  `VOSTFR` ici veut dire *japonais + sous-titres français* (regex
  `VOST.*?FR`), **pas** de l'audio français — ne pas confondre les deux.

Le Custom Format `FRENCH` (créé une fois, id à résoudre par nom, ne pas
supposer un id fixe) matche `\b(TRUEFRENCH|FRENCH|VFF|VFQ)\b` dans le titre
de la release — ce sont les tags scène courants pour de l'audio français.

Ne PAS modifier `Anime (Fansub)` directement pour biaiser son scoring — le
principe retenu est bien deux profils distincts + bascule par série, pas un
seul profil partagé par tous les animes.

## Prérequis pour appeler l'API

Depuis la racine du repo (`server/`) :

```bash
SONARR_KEY=$(grep '^SONARR_API_KEY=' arr/.env | cut -d= -f2-)
```

Puis tous les appels passent par `docker exec` vers le container Sonarr
(pas d'accès direct, réseau `traefik-public` non exposé en dehors) :

```bash
docker exec -i arr-sonarr-1 curl -s -H "X-Api-Key: $SONARR_KEY" "http://localhost:8989/api/v3/<path>"
```

Pour un POST/PUT avec corps JSON, écrire le JSON dans un fichier (via l'outil
Write, pas un heredoc bash — piégeux avec les guillemets), puis :

```bash
docker exec -i arr-sonarr-1 curl -s -X PUT -H "X-Api-Key: $SONARR_KEY" \
  -H "Content-Type: application/json" --data @- "http://localhost:8989/api/v3/<path>" < fichier.json
```

## Étapes

1. **Résoudre la série** — `GET /api/v3/series`, chercher par titre
   (insensible à la casse, substring). Si plusieurs séries correspondent,
   demander à l'utilisateur de préciser plutôt que de deviner. Noter l'`id`
   et le `qualityProfileId` actuel (pour pouvoir dire d'où on vient dans le
   rapport final).

2. **Capturer les fichiers déjà présents AVANT de lancer quoi que ce soit** —
   `GET /api/v3/episodefile?seriesId=<id>` : pour chaque fichier, noter
   `episodeId`/`episodeFileId` et son `path` (vu par le conteneur Sonarr,
   ex. `/data_root/library/anime/.../S01E01...`). Traduire vers le chemin
   hôte en remplaçant le préfixe `/data_root` par `$DATA_ROOT` (lu depuis
   `.env.shared` à la racine du repo — même logique inverse que
   `host_to_arr_path()` dans `scripts/torrent-cleanup.py`), puis `os.stat()`
   ce chemin hôte pour capturer `(st_dev, st_ino)`. Garder cette table
   episodeId → (path host, dev, ino) en mémoire pour les étapes 6-7 —
   **indispensable de le faire avant l'import** : une fois la nouvelle
   release importée, Sonarr peut avoir déjà supprimé ce chemin côté
   `library/`, un stat a posteriori échouerait et la correspondance vers
   l'ancien torrent serait perdue.

3. **Résoudre l'id du profil `Anime (Fansub) VF`** — `GET
   /api/v3/qualityprofile`, matcher par `name` exact. Ne jamais coder l'id en
   dur (rien ne garantit qu'il reste `10` si le profil est un jour recréé —
   même piège que les ids Torznab cross-seed, voir CLAUDE.md).

4. **Basculer la série sur ce profil** — `GET /api/v3/series/{id}` pour
   récupérer l'objet complet, changer `qualityProfileId`, puis `PUT
   /api/v3/series/{id}` avec l'objet entier (Sonarr exige le corps complet,
   pas un patch partiel — même remarque que pour `qualityprofile` PUT).
   Le changement reste permanent : les épisodes futurs de cette série
   continueront de préférer l'audio français, pas seulement la recherche
   déclenchée ici.

5. **Relancer une recherche** — `POST /api/v3/command` avec `{"name":
   "SeriesSearch", "seriesId": <id>}`. Note : ça recherche tous les épisodes
   surveillés de la série, pas seulement ceux déjà téléchargés — un épisode
   manquant peut donc aussi être grabé au passage, pas seulement les
   remplacements VF. Poller `GET /api/v3/command/{commandId}` jusqu'à
   `status == "completed"`.

6. **Constater les grabs** — `GET
   /api/v3/history?seriesId=<id>&eventType=1&sortKey=date&sortDirection=descending&pageSize=100`.
   Le paramètre `seriesId` de cet endpoint n'est **pas fiable** (vérifié le
   2026-07-29 sur un test réel : la réponse contenait des entrées d'autres
   séries) — filtrer nous-même côté client sur `record["seriesId"] ==
   <id>`, ne jamais faire confiance au filtrage serveur. `eventType=1` (pas
   la chaîne `"grabbed"`, qui renvoie une réponse vide dans nos tests) =
   grabbed. Ne garder que les entrées dont `date` est postérieure au
   déclenchement de la recherche. Chaque entrée donne le titre de la
   release, la qualité et le score custom-format — vérifier que le titre
   contient bien FRENCH/VFF/VFQ/TRUEFRENCH avant de la traiter comme un
   remplacement VF
   (une release grabée peut aussi être un simple épisode manquant sans
   rapport avec la VF, cf. note à l'étape 5).

7. **Attendre l'import, puis supprimer l'ancien torrent** — pour chaque
   épisode dont une release FR a été grabée à l'étape 6 : reposer `GET
   /api/v3/episodefile?seriesId=<id>` de temps en temps (quelques essais
   espacés de 30-60s suffisent en général, le temps du téléchargement — ne
   pas boucler indéfiniment) jusqu'à ce que l'`episodeFileId` de cet épisode
   ait changé par rapport à la table capturée à l'étape 2 (= nouvel import
   fait). Une fois confirmé, supprimer l'ANCIEN torrent (celui d'avant, dont
   on a le `(dev, ino)` capturé à l'étape 2) :

   ```bash
   python3 scripts/torrent-cleanup.py delete-by-inode <dev> <ino>
   ```

   Cette commande (voir `delete_by_inode_cli()`/`find_torrent_by_inode()`
   dans `scripts/torrent-cleanup.py`) retrouve le torrent Transmission dont
   un fichier correspond à cet inode et le supprime avec ses données —
   exactement l'action de suppression de `make cleanup`, mais sans passer
   par la TUI. Contrairement à une suppression manuelle dans `make cleanup`,
   elle **ne touche pas au monitoring Sonarr/Radarr** : l'épisode reste
   surveillé, on vient de le remplacer par une meilleure release, pas de le
   retirer. Elle affiche un JSON (`{"found": bool, "deleted": bool,
   "torrent": str|None}`) — `found: false` signifie que le torrent n'a pas
   été retrouvé (déjà supprimé par ailleurs, ou fichier jamais réellement
   remplacé) : à signaler tel quel, pas une erreur bloquante. Si l'import
   n'est toujours pas fait après quelques essais, le dire à l'utilisateur
   plutôt que d'attendre indéfiniment — il pourra redemander plus tard.

8. **Rapporter à l'utilisateur** :
   - si des releases FR ont été grabées et importées : lesquelles (titre,
     qualité, indexeur), et confirmer que l'ancien torrent correspondant a
     bien été supprimé (ou signaler `found: false` si ce n'était pas
     retrouvable).
   - si des releases FR ont été grabées mais pas encore importées au moment
     du rapport : le dire, et que l'ancien torrent sera à nettoyer une fois
     l'import fait (redemander plus tard, ou via `make cleanup` à la main).
   - si rien n'a été trouvé : le dire clairement plutôt que rapporter un faux
     succès — aucune release FR n'était disponible chez les indexeurs
     configurés au moment de la recherche. La série reste sur le profil VF,
     donc une prochaine RSS sync/recherche automatique la retentera d'elle-même.

## Limites connues

- Fonctionne seulement si une release FRENCH/VFF/VFQ/TRUEFRENCH existe
  réellement chez les indexeurs Prowlarr configurés — pas de garantie de
  disponibilité, notamment sur des animes peu répandus en scène FR.
- Le Custom Format `FRENCH` ne couvre que les tags scène habituels. Une
  release avec un tag différent (rare) ne sera pas reconnue comme française
  et ne sera pas préférée par le profil VF.
- `delete-by-inode` ne peut retrouver l'ancien torrent que si le `(dev, ino)`
  a bien été capturé à l'étape 2 avant le remplacement — un épisode ajouté
  en cours de route après cette capture (recherche relancée entre-temps,
  etc.) ne sera pas nettoyé automatiquement, `make cleanup` reste le
  filet de sécurité manuel dans ce cas.
