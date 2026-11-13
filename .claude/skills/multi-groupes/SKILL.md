---
name: multi-groupes
description: Tient la liste noire du custom format « Langue : MULTi (groupe non exclu) », qui fait compter un MULTi anime comme de la VF sauf pour les groupes exclus, à partir des pistes audio réellement lues dans la bibliothèque — propose les groupes à exclure et les fichiers MULTi sans audio français à remplacer. À utiliser quand l'utilisateur demande quels groupes exclure, pourquoi une MULTi anime est rejetée ou acceptée, si un groupe donné sort bien de la VF, signale un anime MULTi grabé sans audio français, quand /server-report remonte un MULTi sans FR, ou lance /multi-groupes.
---

# multi-groupes — tenir la liste noire des MULTi anime

## Contexte

Sur les profils `Anime` et `Anime VF`, un titre `MULTi` vaut VF (3000)
**sauf si son groupe de release est exclu** : custom format
`Langue : MULTi (groupe non exclu)` de `arr/profiles/custom-formats.json`,
spec `ReleaseGroupSpecification` **niée**, regex `^(A|B|…)$`. Un MULTi
sans groupe parsé est accepté (une spec de groupe niée laisse passer).

Historique (détail dans `.claude/docs/arr-config.md`, entrée « Profils de
qualité ») :

- **2026-10-07 — liste blanche.** Sur Nyaa, 12 % des `MULTi` étaient sans
  français, **tous de VARYG** ; tous les autres groupes mesurés avaient
  l'audio FR. Un groupe inconnu restait rejeté tant qu'on n'avait pas vu
  ses fichiers.
- **2026-11-13 — liste noire.** La liste blanche s'auto-bloquait : un
  groupe hors liste n'était jamais téléchargé, donc ne pouvait jamais
  prouver qu'il était bon (FDRY y était même entré sur des fichiers
  `MULTi.VFF` qui ne prouvaient rien). Choix utilisateur : accepter par
  défaut, et **détecter après coup** au lieu de prouver avant.

Le prix de la liste noire : **aucun upgrade** (`upgradeAllowed: false`), un
MULTi sans FR grabé comme VF reste en place jusqu'à ce qu'on le remplace à
la main. D'où le filet `--controle`, branché sur `/server-report`.

## Règles

- **Seule preuve valable : l'audio des fichiers en bibliothèque**
  (`mediaInfo.audioLanguages`, lu par Sonarr/Radarr à l'import). Les
  fichiers de séries live et de films comptent : on juge la pratique du
  groupe.
- **Un seul MULTi sans FR suffit pour exclure** un groupe (verdict
  `À EXCLURE`), après avoir écarté la piste FR non taguée (voir Pièges).
- **Un MULTi qui annonce aussi la VF (`MULTi.VFF`, `MULTi.VF2`…) ne compte
  pas en FR** : le CF est nié sur ces titres. Il compte bien en « sans FR »
  s'il n'a pas d'audio FR (le groupe a menti sur la VF).
- **`NO-VF` dans le titre n'est jamais une VF** (`MULTI.NO-VF`, Anime-DL) :
  spec niée sur `Langue : VF` et `Langue : VF (hors MULTi)`. Avant le
  2026-11-13, `\bVF\b` y lisait une VF, et 8 épisodes Anime-DL sans audio
  FR ont été grabés à 3000.
- **`EXCLUSION_NOTES` du script fait mémoire** : un fichier sans FR finit
  remplacé et sa trace disparaît (VARYG ressortait « 4 FR / 0 sans FR » le
  soir même de sa mesure). Y inscrire le groupe, la date et la mesure
  **avant** de remplacer le fichier. Le script signale toute divergence
  entre ce dict et la regex.
- **La description d'une page Nyaa n'est qu'un indice** : une déclaration
  « audio sans FR » d'un groupe non exclu le désigne à surveiller, pas à
  exclure.
- **Rien n'est modifié sans l'accord de l'utilisateur.**

## Étape 1 — collecter

```bash
python3 .claude/skills/multi-groupes/multi_groupes.py --controle   # le filet
python3 .claude/skills/multi-groupes/multi_groupes.py --pages      # bilan par groupe
```

Lecture seule. `--controle` ne liste que les fichiers MULTi sans audio FR
qui appellent une action (`--tout` pour voir aussi les autres) :

| Action | Quand |
|---|---|
| `exclure le groupe` | le groupe n'est pas encore dans la regex |
| `remplacer` | pas de sous-titres FR non plus (ne vaut même pas une VOSTFR), ou profil `… VF` |
| `confirmer par ffprobe` | une piste audio sans tag de langue (peut être du FR) |

Un fichier sans audio FR mais sous-titré FR, d'un groupe déjà exclu, sur
`Anime` : rien à faire, il vaut le minimum du profil (VOSTFR). C'est le cas
des 8 Anime-DL au 2026-11-13.

Le bilan (`--pages`) parcourt aussi une recherche `MULTi` sur Nyaa via
Prowlarr (tracker public, pas de quota) ; `--pages` lit deux descriptions
par groupe non exclu, en séquentiel (Nyaa répond 429 en parallèle).

Le groupe affiché est celui **parsé par l'arr**, exactement ce que voit la
spec.

## Étape 2 — proposer

| Verdict | Proposition |
|---|---|
| `À EXCLURE` | ajouter à la regex + `EXCLUSION_NOTES` |
| `à confirmer (piste non taguée)` | `ffprobe` d'abord |
| `accepté (FR constaté)` / `accepté (non vérifié)` | rien |
| `sans groupe (non excluable)` | un MULTi sans groupe et sans FR : rien à exclure, remplacer le fichier |
| `exclu` | rien |

Présenter **uniquement** les `À EXCLURE` et les fichiers à remplacer, avec
les chiffres. Demander groupe par groupe. Le remplacement d'un fichier
passe par `/remplacer-release`.

## Étape 3 — appliquer (après accord)

1. Ajouter le groupe à la regex `ReleaseGroupSpecification` (niée) du CF
   dans `arr/profiles/custom-formats.json` (insensible à la casse ; nom tel
   que parsé, `-?` si variantes avec et sans tiret), **et** à
   `EXCLUSION_NOTES` avec date, effectif et exemple.
2. `make arr-overrides` (≥ 2 min, normal : `settle()` des tailles).
3. Vérifier que Sonarr/Radarr calculent le CF comme prévu :
   ```bash
   python3 .claude/skills/multi-groupes/multi_groupes.py --verifier
   ```
   Exigé : **0 désaccord** (.NET contre Python, sur des titres réels de
   chaque groupe, sans groupe compris).
4. Mettre à jour la liste des groupes exclus dans `docs/telechargement.md`
   (encadré sous « Règles de sélection ») et `.claude/docs/arr-config.md`.
5. Remplacer les fichiers fautifs (`/remplacer-release`) **après** l'étape 1.
6. Commit seulement si l'utilisateur le demande.

## Pièges

- **Un « sans FR » peut être une piste FR sans tag de langue** :
  `audioLanguages` ne voit que les pistes taguées (*The Simpsons*
  S37E11/E13 ont une piste « French » non taguée). Le script le repère
  (`und`, ou plus de pistes que de langues) ; confirmer par `ffprobe`
  (titre de piste) avant d'exclure.
- **`Multi-Subs` / `MSubs` n'est pas un MULTi** (la regex l'écarte) : un
  fichier de ce type sans audio FR ne dit rien du groupe.
- **Les MULTi d'un tracker français ne sont pas un cas à part** : la spec
  ne voit pas l'indexeur, seul le groupe compte.
- **Renommer le CF dans le JSON laisse l'ancien dans les arr** :
  `apply-arr-overrides.py` crée les CF absents mais ne supprime pas les
  orphelins (score remis à 0, mais ils restent affichés et calculés).
  Renommer d'abord sur place (`PUT /customformat/<id>` avec le nouveau
  nom), comme le 2026-11-13.
