---
name: remplacer-release
description: Remplace la release en place d'un film, d'un épisode ou d'une saison (désigné par un nom approximatif, avec éventuellement le numéro et le défaut constaté) par une autre release trouvée en recherche interactive Sonarr/Radarr, puis suit le téléchargement jusqu'à l'import. À utiliser quand l'utilisateur dit qu'un fichier est mauvais (mauvais épisode, sous-titres ou audio décalés, pas de VF, image abîmée, coupé), demande une autre version d'un titre, ou lance /remplacer-release.
argument-hint: <titre approximatif> [SxxEyy | saison N | ép N] [défaut constaté]
model: sonnet
---

# remplacer-release — changer la release d'un titre déjà présent

## Contexte

Les profils n'acceptent **jamais d'upgrade** (`upgradeAllowed: false`) :
pour un titre déjà en bibliothèque, Sonarr/Radarr rejettent toutes les
releases, la bonne comme les autres. Rien ne remplace un fichier mauvais
sans intervention. Cas fondateur, le 2026-10-19 : *Reborn as a Goblin* E01
était en fait l'E02 (erreur de Crunchyroll au lancement, reprise par TLC).
Tous les groupes avaient sorti un REPACK, ignoré par Sonarr.

Le script sépare les rejets qui ne tiennent **qu'au fichier en place**
(« Existing file… », « does not allow upgrades », délai de 24 h) des vrais
(langue, taille, codec, score sous le minimum). Une release dont tous les
rejets sont du premier type est classée `candidate`.

Argument : `$ARGUMENTS`.

## Étape 1 — trouver le titre

```bash
python3 .claude/skills/remplacer-release/remplacer_release.py find "<texte>"
```

Les repères `S01E02`, `ép 2`, `saison 3` sont retirés de la recherche, un
nombre seul est gardé (« dune 2 »). Les titres alternatifs comptent.

- Une correspondance nette (score ≥ 0,9, loin devant les autres) : la
  prendre.
- Plusieurs proches (les trois *Seigneur des anneaux*, deux saisons
  possibles) ou rien au-dessus de 0,7 : **demander** à l'utilisateur.
  Jamais deviner. Le skill finit par remplacer un fichier.
- Titre absent du catalogue : le dire et s'arrêter. Ce skill n'ajoute rien.

## Étape 2 — fixer la cible

| Demande | Arguments de `releases` |
|---|---|
| film | `--movie-id N` |
| épisode | `--series-id N --season S --episode E` |
| saison entière | `--series-id N --season S` (seuls les packs sont gardés) |

- Une série à une seule saison et « ép 5 » sans saison : saison 1.
- **Anime en numérotation absolue** (« One Piece 1172 ») : lire
  `GET /api/v3/episode?seriesId=<id>` et matcher `absoluteEpisodeNumber`,
  pas le numéro affiché.
- Si l'utilisateur dit « l'épisode 1 est en fait le 2 », la cible est
  l'épisode **1** (celui qui porte le mauvais fichier).

## Étape 3 — lister

```bash
python3 .claude/skills/remplacer-release/remplacer_release.py releases <cible>
```

Affiche le fichier en place (release, qualité, langue, torrent d'origine),
puis les releases numérotées `#n`, classées `candidate`, `rejetée`,
`identique` (même release que le fichier en place). `--all` montre aussi
les releases `hors cible` (autre épisode, pack sur une demande d'épisode).

Coût, par indexeur interrogé (historique Prowlarr, sept.–oct. 2026) :
**1 à 3 requêtes pour un film**, **5 à 12 pour un épisode d'anime**,
jusqu'à 24 pour un anime à nombreux titres alternatifs (*One Punch Man*).
Sonarr croise chaque variante du titre (anglais, romaji, avec et sans
ponctuation) avec deux modes (`S01E02` et numéro absolu `<titre> 02`) :
un épisode d'anime sur 7 indexeurs ≈ **60 requêtes**. Lancer `releases`
**une seule fois** par cible (voir skill `indexer-quota`) : l'arr garde la
liste **30 min**, `grab` s'en sert. Passé ce délai, `grab` refuse, et là
seulement relancer `releases`.

## Étape 4 — choisir et grabber

Choisir parmi les `candidate`, sans demander confirmation : l'utilisateur a
demandé le remplacement, et l'ancien fichier ne part qu'à l'import du
nouveau. Règles, dans l'ordre :

1. **Corriger le défaut cité.** Mauvais épisode → un `REPACK`/`PROPER` du
   groupe, sinon une autre source. Sous-titres, audio ou image défectueux →
   un **autre groupe** (même groupe = même défaut, en général). Sans défaut
   cité : meilleur score parmi les groupes différents de l'actuel.
2. **Ne pas perdre en langue** : une VF ne se remplace pas par une VOSTFR,
   ni une VOSTFR par une VO sans sous-titres FR.
3. Score de custom format le plus haut, puis seeders (au moins 5).
4. Une `identique` ne corrige rien, même sur un autre indexeur.

Une `rejetée` ne se prend que si l'utilisateur la demande nommément, après
lui avoir dit pourquoi elle est rejetée. Aucune `candidate` : le dire, avec
le motif des meilleures rejetées, et s'arrêter.

```bash
python3 .claude/skills/remplacer-release/remplacer_release.py grab <n>
```

`--episode-ids 3973` force les épisodes visés quand le titre de la release
porte un mauvais numéro. Les ids d'épisode sont **globaux à Sonarr** : un id
faux écrase un épisode d'une autre série.

> [!WARNING]
> Le `guid` d'une release contient la clé API de l'indexeur. `grab` le lit
> dans `~/.cache/remplacer-release/derniere-recherche.json` (mode 600) :
> ne jamais l'afficher ni le passer en argument.

## Étape 5 — suivre le téléchargement

```bash
python3 .claude/skills/remplacer-release/remplacer_release.py watch
```

Lancer en arrière-plan (`run_in_background`) : jusqu'à 9 min (`--timeout`).
Codes de sortie : 0 sorti de la file, 2 bloqué, 3 délai dépassé (torrent
lent : le dire, l'arr importera seul ou se bloquera plus tard).

**Bloqué** (`importBlocked`, souvent *« Not an upgrade for existing… »*) :
c'est le refus d'upgrade, attendu. Forcer l'import de **ce** téléchargement
avec le skill `manual-import` :

```bash
python3 scripts/manual-import.py list --download-id <hash>
python3 scripts/manual-import.py assign sonarr --download-id <hash> --episode-ids <id>
python3 scripts/manual-import.py assign radarr --download-id <hash> --movie-id <id>
```

Pour un pack de saison : un `assign` par fichier (`--path <nom>`), après
avoir vérifié la correspondance fichier → épisode.

## Étape 6 — vérifier

1. Le fichier en place a changé : relancer
   `GET /api/v3/episodefile?seriesId=<id>` ou `GET /api/v3/movie/<id>`
   (`sceneName` = la nouvelle release). Un `ManualImport` `queued` ne
   prouve rien.
2. **Le défaut est corrigé**, contrôlé sur le fichier et pas sur le nom :
   - mauvais épisode : comparer les chapitres (`ffprobe -show_chapters`)
     et les premières lignes de sous-titres
     (`ffmpeg -i f -map 0:s:0 -f srt -`) avec l'épisode voisin. Deux
     épisodes différents n'ont jamais les mêmes chapitres au millième ;
   - langue : `ffprobe -show_entries stream_tags=language`.
3. Rapporter : titre, ancienne release → nouvelle, vérification faite.

## Étape 7 — l'ancien torrent

Le torrent d'origine (hash affiché à l'étape 3) continue de partager, mais
il n'a plus de fichier en bibliothèque. Le signaler et proposer de le
supprimer depuis clearr. **Ne rien supprimer soi-même.** Commit seulement
si l'utilisateur le demande.

## Pièges

- **Un REPACK est une autre release** : il garde le même groupe, mais il
  corrige justement le contenu. Le script ne le classe donc pas `identique`.
- **Une recherche par épisode remonte toute la série** : les autres épisodes
  arrivent avec *« Episode wasn't requested »*. Le script les range en
  `hors cible`.
- **Un `ManualImport` sans `downloadId` déplace le fichier** et casse le
  seed (voir `.claude/docs/arr-pieges.md`). `manual-import.py assign` le
  passe toujours.
- Radarr ne renvoie pas de `customFormatScore` sur le fichier en place
  (`score=None`) : comparer sur la qualité et la langue.
