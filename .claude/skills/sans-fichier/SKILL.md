---
name: sans-fichier
description: Fait l'inventaire des séries, animés et films suivis qui n'ont aucun fichier en bibliothèque, en écartant ce qui n'est pas encore sorti (ou seulement en salle) et ce qui est sorti depuis moins de 7 jours, puis vérifie titre par titre ce qu'il est possible de faire (release acceptable disponible, rejetée, ou inexistante). À utiliser quand l'utilisateur demande ce qui manque dans la bibliothèque, les titres sans fichier, pourquoi une série ou un film n'a jamais été téléchargé, ou lance /sans-fichier.
---

# sans-fichier — titres suivis, sortis et toujours absents

## Contexte

Le périmètre est le **titre entier** : une série ou un anime sans aucun
épisode en bibliothèque (`episodeFileCount == 0`), un film sans fichier
(`hasFile == false`). Un épisode isolé manquant dans une série entamée
n'en fait pas partie. Ces épisodes-là sont traités par `search-missing.py`
(lundi, 5 h).

**Seuls les titres suivis comptent** (choix de l'utilisateur, 2026-10-14) :
ceux que Sonarr/Radarr cherchent sans rien trouver. Un titre sorti dont
rien n'est suivi (ajout en « épisodes futurs uniquement », film non suivi)
est un choix, pas un manque. Le script l'écarte du rapport, ne pas le
présenter ni proposer de le suivre.

## Critères (fixés par l'utilisateur le 2026-10-14)

| Écarté | Règle |
|---|---|
| Rien de suivi | aucun épisode voulu déjà diffusé, ou film non suivi (visible seulement en `--json`) |
| Pas encore sorti (série) | aucun épisode voulu déjà diffusé, et une saison suivie attend sa diffusion |
| Pas encore sorti (film) | pas de sortie numérique ou physique passée. **Une sortie en salle seule ne compte pas** (il n'en existe que des CAM, refusées par les profils), même si Radarr dit `isAvailable` (`minimumAvailability: inCinemas`) |
| Trop récent | sorti depuis moins de **7 jours** (`GRACE_DAYS`, `--grace N`). Mesuré le 2026-10-23 : une release qui existe est grabée à J+1 (délai de 24 h), 4,1 j au pire. Au-delà, rien n'existe chez nos indexeurs. Était 14 j (« attendre une VF », caduc depuis la VOSTFR minimum) |
| Dans la file | listé à part, renvoyer au skill `manual-import` |

Dates de référence :

- **série** : le plus ancien épisode voulu (suivi, diffusé) ;
- **film** : la première sortie numérique ou physique. Sans aucune date,
  on se fie au statut Radarr `released` (vieux documentaires).

Les spéciaux (saison 0) sont ignorés.

## Étape 1 — collecter

```bash
python3 .claude/skills/sans-fichier/sans_fichier.py          # rapport
python3 .claude/skills/sans-fichier/sans_fichier.py --json   # brut, toutes catégories
```

Lecture seule, ~10 s : aucune requête aux indexeurs, aucun suivi modifié.
La « dernière recherche auto » vient de l'état de `search-missing.py`
(`$DATA_ROOT/.search-missing-state.json`).

## Étape 2 — vérifier chaque titre

Pour chaque titre, établir ce qu'il est possible de faire. La recherche
interactive coûte une requête par indexeur actif et par titre (voir skill
`indexer-quota`). Elle est acceptable pour une poignée de titres, pas pour
des dizaines.

1. **Recherche interactive**, sans grab : `GET /release?movieId=<id>`
   (Radarr) ou `GET /release?seriesId=<id>&seasonNumber=<n>` (Sonarr,
   **par saison** : c'est la seule qui remonte les packs). Lire pour chaque
   release `approved`, `rejections`, `customFormatScore`, `seeders`.
2. **Historique du titre** : `GET /history/movie?movieId=<id>` ou
   `/history/series?seriesId=<id>`. Le paramètre `?movieId=` sur `/history`
   est **ignoré sans erreur** : il renvoie tout l'historique.
3. Si un grab apparaît sans import : chercher le torrent dans Transmission
   et la release dans les logs arr (`/config/logs/sonarr.txt`, `radarr.txt` =
   fichier courant ; les `.0.txt`, `.1.txt`… sont les rotations).

Classer chaque titre :

| Constat | Action à proposer |
|---|---|
| Release `approved` disponible | recherche ciblée (`MoviesSearch` / `SeasonSearch`), qui grabera la meilleure. Expliquer pourquoi l'auto l'a ratée (voir Pièges) |
| Releases FR rejetées à tort (`Unknown Movie`/`Unknown Series`, nom alternatif) | grab manuel de la release précise, en nommant taille, langue, seeders : `POST /release` avec `guid`, `indexerId` et `movieId` (rattache la release au film), dans les 30 min qui suivent la recherche (cache Radarr) |
| Releases rejetées à raison (pas de FR, taille, 0 seeder) | rien : `search-missing` le repasse chaque lundi |
| Aucune release, même en texte libre (`GET` Prowlarr `/search?type=search&query=`) | rien à faire chez nos indexeurs ; proposer de le laisser ou de ne plus le suivre |

## Étape 3 — présenter et proposer (rien sans accord)

Un tableau : type, titre, sortie, dernière recherche auto, constat,
action possible. Les écartés tiennent en une ligne chacun (titre, date).

Aucune recherche, aucun grab, aucun changement de suivi sans accord,
titre par titre. Commit seulement si l'utilisateur le demande.

## Pièges

- **Les packs de saison échappent à la recherche par épisode** : *Man vs.
  Baby* le 2026-10-14, 1 résultat par épisode contre 25 en recherche de
  saison. Corrigé dans `search-missing.py` pour une saison diffusée sans
  aucun fichier (`SeasonSearch`). Une saison partiellement présente reste
  cherchée épisode par épisode : y vérifier à la main par saison.
- **Un grab sans import ni échec** peut disparaître de la file sans laisser
  de trace côté arr (*Man vs. Baby*, pack Amen grabé le 2026-09-22, absent
  de Transmission). Le titre redevient « manquant » mais n'est recherché
  qu'au passage suivant de `search-missing`.
- **Radarr ne reconnaît pas un titre enrichi** : `Gimme.Danger.(The
  Stooges).2016…` → `Unknown Movie`, score 0. Pas d'alias possible côté
  Radarr : seul un grab manuel le prend.
- **Une saison suivie ne veut pas dire des épisodes suivis.** Sonarr suit
  par épisode : une saison cochée dont les épisodes déjà diffusés sont
  décochés ne cherche rien (cas *Frieren* S2 le 2026-10-14). Le script
  regarde le suivi de chaque épisode, pas celui de la saison.
- **`isAvailable` de Radarr n'est pas « sorti »** : avec
  `minimumAvailability: inCinemas`, un film en salle est « disponible ».
  Le skill et `search-missing.py` (depuis le 2026-10-20) l'écartent tous
  deux, avec la même fonction `movie_release()`.
- Un titre absent du catalogue n'apparaît évidemment pas ici. Pour un
  téléchargement fini mais non importé, voir `manual-import`.
