# komga — lecture BD / comics / mangas

Chargé à la demande depuis `CLAUDE.md`. À lire avant de toucher à `komga/`,
à la catégorie `bd` du client de téléchargement Prowlarr, ou avant d'ajouter
une vue BD à clearr.

Ajouté le 2026-09-22.

## Le choix structurant : pas de hardlink, pas d'arr

**Komga lit directement les données seedées.** Sa bibliothèque est
`${DATA_ROOT}/.transmission/data/completed/bd`, alimentée par la recherche
**manuelle** de Prowlarr. Il n'y a ni arr, ni import, ni hardlink vers
`library/`, ni renommage.

Arbitré explicitement le 2026-09-22, après avoir comparé avec l'option
« hardlinks sous `library/bd/` » : **le but est de pouvoir lire des BD, pas
d'avoir une bibliothèque bien rangée.** Ne pas rouvrir le sujet sans que ce
soit redemandé.

Ce que ça achète :

- Aucun script d'import à écrire, aucune traduction de chemin, aucun
  renommage.
- **`library/` n'est pas touché**, donc `_arr_covered_paths()` et le bouton
  « Orphelins library/ » de clearr restent justes. C'était le vrai danger de
  l'option avec hardlinks : un `.cbz` sous `library/` n'est connu d'aucun arr,
  donc serait tombé orphelin par construction, et le bouton aurait proposé de
  supprimer toute la bibliothèque BD.

Ce que ça coûte, et qui est assumé :

- **L'arborescence est celle de l'uploadeur, définitivement.** Komga déduit
  ses séries des dossiers (voir plus bas) et on ne peut pas réorganiser des
  données seedées.
- **Supprimer un torrent = perdre la BD**, et garder la BD = seeder à vie. Il
  n'y a pas de second exemplaire. Une future vue BD dans clearr doit le dire
  dans ces termes : ce n'est pas une vue « ce que je peux récupérer » comme
  les trois autres, c'est une vue « supprimer cette BD ».

## Le montage est en lecture seule, et ce n'est pas négociable

```yaml
- ${DATA_ROOT}/.transmission/data/completed/bd:/data_root/.transmission/data/completed/bd:ro
```

Les fichiers de cette bibliothèque **sont** les données seedées. Toute
écriture invalide le hash du torrent, fait repartir la vérification et coûte
du ratio sur des trackers privés. Le `:ro` transforme un dégât silencieux et
massif en erreur bruyante. Il désactive de fait deux fonctions de Komga :

- la suppression de fichier de livre (`DELETE /api/v1/books/{id}/file`) ;
- l'outil **Import** de Komga, qui déplace des fichiers — **ne jamais
  l'utiliser sur cette bibliothèque**.

**Corollaire pour Komf**, si jamais il est ajouté un jour (écarté le
2026-09-22, jugé trop compliqué pour le bénéfice) : son mode
`updateModes: [COMIC_INFO]` écrit un `ComicInfo.xml` **dans** chaque archive.
Sur cette architecture, ça réécrirait des dizaines de Go de données seedées.
Le défaut `[API]` est le seul mode acceptable ici.

## Chemin identique des deux côtés du montage

Le chemin est **le même dans le conteneur et dans l'espace de chemins de
clearr**, à dessein. clearr calcule
`/data_root/.transmission/data/completed/...` (`container_path_to_host()`) et
Komga expose le sien dans `BookDto.url`. Les faire coïncider évite de
réintroduire la traduction de chemin que clearr a justement supprimée (cf.
`.claude/docs/clearr.md`).

Et c'est le **sous-dossier** qui est monté, pas `${DATA_ROOT}` entier : Komga
n'a aucune raison de voir Nextcloud ou `library/`.

## L'onglet BD de clearr

**Livré le 2026-09-23** — détail d'implémentation dans
`.claude/docs/clearr.md`. La vue **liste les torrents dont le `downloadDir`
est sous `completed/bd`**, sans jamais interroger Komga : un filtre, rien de
plus. Ce choix supprime d'un coup la traduction de chemin, le besoin d'un
compte admin Komga et le trou anti-traversal sur les jaquettes.

Ce qu'il faut retenir côté komga :

- **`_linked` est toujours `False`** sur ces torrents (aucun hardlink
  `library/` n'existe), d'où la colonne BIB retirée de cet onglet : ailleurs
  son absence signifie « jamais importé », ici elle ne signifierait rien.
- **La modale de suppression dit explicitement qu'il n'y a pas d'autre
  exemplaire**, et elle le dit d'après le torrent et non d'après l'onglet —
  donc aussi depuis la vue Torrents.
- **Si un jour la vue veut rattacher un torrent à une entrée Komga**, c'est
  `BookDto.url` (chemin exact) et `SeriesDto.url` (préfixe de dossier), même
  structure que `build_arr_meta_index()`. Mais alors **le compte Komga doit
  être ADMIN** :

  ```kotlin
  // BookController.kt, 7 sites d'appel
  .map { it.restrictUrl(!principal.user.isAdmin) }
  // BookDto.kt   : url réduit au nom de fichier
  // SeriesDto.kt : url réduit à la chaîne vide
  ```

  Avec un compte non-admin, ça répond **200 normalement** et tous les
  rattachements échouent en silence. Faux négatif silencieux, même famille que
  les 202 de Servarr (cf. `CLAUDE.md`). Et les ids Komga étant des chaînes,
  la garantie anti-traversal que `int()` donne à `/poster/{kind}/{arr_id}`
  disparaîtrait : il faudrait valider explicitement le format d'id avant de
  construire une URL de vignette.

## Comment Komga voit l'arborescence (vérifié dans `FileSystemScanner.kt`)

- **Une série = le dossier parent des livres.** `pathToBooks.merge(file.parent,
  …)`, puis la série prend `dir.name`.
- **Extensions scannées** : `cbz, zip, cbr, rar, pdf, epub`. Les dossiers
  commençant par `.` sont ignorés.
- **Un fichier nu à la racine de la bibliothèque est rattaché au dossier
  racine** — donc tous les torrents mono-fichier fusionnent dans **une seule
  série nommée « bd »**. Accepté tel quel (2026-09-22). Pour en sortir un, un
  `torrent-set-location` vers `completed/bd/<nom>/` déplace les données sans
  interrompre le seed.
- **L'option « One-Shots directory » NE RÉSOUT PAS ce cas ici**, elle
  l'aggrave :
  ```kotlin
  if (!oneshotsDir.isNullOrBlank() && dir.pathString.contains(oneshotsDir, true))
  ```
  C'est un `contains` sur le **chemin complet**. La bibliothèque s'appelant
  `.../completed/bd`, régler `oneshotsDir = "bd"` matcherait *tous* les
  sous-dossiers et ferait exploser un pack de 41 tomes en 41 séries d'un tome.
  Et comme la racine est contenue dans le chemin de tous ses enfants, aucune
  valeur ne vise la racine seule. Ne pas y revenir en croyant que c'était la
  solution évidente.
- **Séries et livres sont identifiés par leur URL, c'est-à-dire leur chemin** :
  ```kotlin
  seriesRepository.findNotDeletedByLibraryIdAndUrlOrNull(library.id, newSeries.url)
  existingBooks.find { it.url == newBook.url && it.deletedDate == null }
  ```
  Donc **renommer un dossier crée une nouvelle série et perd les corrections de
  métadonnées** saisies dans l'UI. Renommer d'abord (`torrent-rename-path`,
  qui ne casse pas le seed), éditer les titres ensuite. Jamais l'inverse.

## Pas de métadonnées automatiques

Komf a été écarté. Les séries portent donc le nom du dossier de release, sans
résumé ni auteur. Deux nuances qui rendent ça vivable :

- les **couvertures sont générées** par Komga depuis la première page ;
- éditer titre/résumé dans l'UI Komga est **persistant et n'écrit aucun
  fichier** (tout va dans sa base, avec un verrou par champ) — donc compatible
  avec le `:ro`.

## `.cbr` : RAR5 est une impasse sèche

Komga s'appuie sur junrar, qui ne lit ni RAR5 ni les archives solides
([gotson/komga#52](https://github.com/gotson/komga/issues/52)). Sur cette
architecture la conversion `cbr → cbz` n'est **pas** une sortie : le fichier
converti serait hors torrent, et le `:ro` interdit de l'écrire à côté.

Un `.cbr` illisible = ce titre est à reprendre dans une autre release.

**Le marqueur RAR4 ne fait que 7 octets**, celui de RAR5 en fait 8 — comparer
8 octets dans les deux cas fait passer un RAR4 parfaitement valide pour une
signature inconnue (erreur commise le 2026-09-22) :

```
52 61 72 21 1a 07 00        RAR4   ← le 8e octet appartient déjà à l'en-tête suivant
52 61 72 21 1a 07 01 00     RAR5   ← refusé par junrar
```

Et RAR4 ne suffit pas : junrar ne lit pas non plus les archives **solid**. Le
drapeau est dans l'en-tête principal, juste après le marqueur —
`MHD_SOLID = 0x0008` dans les deux octets de `HEAD_FLAGS` (offset 10-11,
petit-boutiste).

Testable **pendant** le téléchargement, sans attendre : les fichiers déjà
complets d'un torrent multi-fichiers sont lisibles dans
`.transmission/data/incomplete/<torrent>/` (ceux qui restent portent un
suffixe `.part`). Fait sur la release C411 de Sillage : 21 `.cbr` RAR4
non-solid, et les 30 livres sont ressortis `READY` côté Komga.

## Le réflexe qui remplace tout le reste : inspecter le `.torrent` avant de grab

Récupérer un `.torrent` via Prowlarr n'est **pas** un snatch, et son bencode
donne la liste des fichiers. Trente secondes d'inspection répondent d'un coup
à « est-ce que ça fera une série propre ? », « y a-t-il des `.cbr` ? » et
« est-ce un fichier nu ? ».

Mesuré le 2026-09-22 sur 11 releases : même titre, structures très
différentes. Une release de Sillage arrive à plat (30 fichiers mélangeant
Sillage, Nävis et les Chroniques), une autre du même titre a **déjà les
sous-dossiers** qui donnent 4 séries correctes. Ça ne se devine pas au nom.

## Prowlarr : la catégorie `bd` est un MAPPING, jamais un second client

`scripts/provision.py` (`PROWLARR_BD_CATEGORY`,
`provision_prowlarr_bd_category`) ajoute au client Transmission **existant** de
Prowlarr un mapping `bd` sur les catégories newznab `[7000, 7020, 7030]`.

**Ne jamais ajouter un second client Transmission pour ça.** Prowlarr choisit
son client en round-robin sur le groupe de priorité le plus bas, **sans
regarder les catégories** :

```csharp
// DownloadClientProvider.GetDownloadClient
availableProviders = availableProviders.GroupBy(Priority).OrderBy(Key).First()...
var provider = availableProviders.FirstOrDefault(v => v.Definition.Id > lastId)
               ?? availableProviders.First();
```

Deux clients à priorité égale enverraient donc **une grab sur deux** dans
`completed/bd`, films et séries compris. C'est le mapping qui route :

```csharp
// TransmissionBase
var category = GetCategoryForRelease(release) ?? Settings.Category;
var downloadDirectory = GetDownloadDirectory(category);   // "<destDir>/<category>"
```

Une release hors mapping retombe sur `Settings.Category` (vide ici) et garde
donc exactement le comportement actuel.

Les trois ids plutôt que le seul parent `7000` : `GetCategoryForRelease` teste
d'abord l'intersection directe des ids et ne retombe sur les sous-catégories du
parent qu'ensuite — les relever évite de dépendre de ce second passage. Vérifié
le 2026-09-22 : sur les 5 indexeurs, toute release BD pertinente porte 7000,
7020 ou 7030.

Rappel : la section Download Clients de Prowlarr est **indépendante** de celle
de Sonarr/Radarr, elle ne sert que ses recherches manuelles. C'est justement
pour ça qu'elle convient à la BD, qu'aucun arr ne gère.

## cross-seed : impact mesuré, et il est faible

`dataDirs: ["/data/completed"]` englobe `completed/bd`, donc les torrents BD
deviennent candidats. Mais `excludeOlder: "30 days"` et
`excludeRecentSearch: "9 days"` filtrent l'essentiel — mesuré le 2026-09-22 :

```
[search] Found 477 torrents, 4 suitable to search for matches using 3 unique queries
```

Un run de cadence fait 3 requêtes, pas des centaines. Quelques torrents BD
ajouteraient de l'ordre de 2 recherches chacun sur leur premier mois, puis
plus rien. **Aucun rapport avec l'incident de quota C411 de juillet**, qui
venait de rafales de recherches. Restreindre `dataDirs` reste possible par
propreté, ce n'est pas une urgence.

## Sauvegarde : la base Komga tient lieu d'inventaire

`${DATA_ROOT}/.komga/config` est sauvegardé par `scripts/backup.sh`. La
bibliothèque elle-même, non : elle vit sous `.transmission/data`, exclu comme
le reste des téléchargements.

Mais contrairement aux vidéos, **aucun arr ne sait reconstituer la liste des
BD**. Ce qui tient lieu d'inventaire, c'est la base de Komga — elle porte le
nom de chaque série et de chaque tome. Ne pas la retirer de la sauvegarde en
croyant n'y perdre que des vignettes.

## Divers

- **Image `gotson/komga:latest`** — le tag existe et suit (1.27.1 au
  2026-09-22), pas d'exception à la règle du repo comme `recyclarr:8`.
- **`user: "${PUID}:${PGID}"`** : l'image n'a pas de directive `USER` (elle
  tourne en root) et ne chown rien. Le dossier de config doit donc exister et
  appartenir à l'utilisateur **avant** le premier démarrage — c'est `make up`
  qui le crée, comme pour seerr. Il crée aussi le dossier de bibliothèque, et
  là c'est indispensable : monté en `:ro`, Docker ne peut pas le créer.
- **Healthcheck sur `/actuator/health`**, exposé sans authentification
  (`HealthEndpoint` en `permitAll` dans la `SecurityConfiguration` de Komga) —
  le même rôle que le `/ping` des Servarr. `start_period: 90s` pour le
  démarrage JVM + Spring Boot (mesuré ~20 s à vide, marge gardée pour un scan
  au démarrage).
- **`JAVA_TOOL_OPTIONS: -Xmx1g`** : sans plafond la JVM prend 25 % de la RAM de
  l'hôte comme maximum. À relever si le scan d'un `.cbz` très lourd finit en
  `OutOfMemoryError` — les tomes font couramment 400 Mo.
- **Exposé au WAN** (`rate-limit@docker,security-headers@docker,hsts@docker`,
  même chaîne que jellyfin et seerr), décidé le 2026-09-22 après audit de la
  `SecurityConfiguration` de Komga. Il n'est donc PAS concerné par
  `make switch-lan-only-middleware`. Vérifié à l'exposition : `/` → 200,
  `/api/v1/series` → 401, `/opds/v2/catalog` → 401, `/actuator/env` → 401,
  `/actuator/health` → 200 mais ne rend que `{"status":"UP"}`
  (`show-details: when-authorized`).
  **Le `rate-limit` est le seul rempart anti-force-brute** : Komga journalise
  les tentatives (`AuthenticationActivity`) mais n'a ni verrouillage ni
  ralentissement après N échecs. Ne pas le retirer de la chaîne.
  Piège de diagnostic croisé en chemin : `/actuator/heapdump` répond **200**
  en anonyme, mais c'est la page du SPA servie par la route attrape-tout
  (1236 octets de `text/html`), pas un dump — vérifier le `Content-Type`
  avant de crier à la fuite.
  Note : `security-headers` pose `X-Frame-Options: DENY` alors que Komga
  demande `SAMEORIGIN` pour les iframes de son lecteur epub. Sans effet sur
  les CBZ/CBR ; à revoir le jour où un `.epub` entre dans la bibliothèque.
- **Premier démarrage** : Komga n'a pas de compte prédéfini et ne lit aucune
  variable d'env pour ça — la première visite de l'UI propose de « réclamer »
  le serveur en créant le compte admin. D'où l'absence de `komga/.env`.
- **Le dashboard** sonde `/favicon.ico` (200, `image/x-icon` — vérifié), donc
  pas d'entrée dans `PROBE_PATH` de `scripts/generate-dashboard.py`.
