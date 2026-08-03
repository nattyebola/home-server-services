---
name: indexer-quota
description: Diagnostique la cause racine des 429/désactivations d'indexeurs Prowlarr vus depuis Sonarr/Radarr — distingue un vrai quota tracker d'un backoff d'échec, attribue le volume de requêtes à sa source (RSS sync vs recherches vs cross-seed) et chiffre le rendement de chaque couple app/indexeur. À utiliser quand un indexeur est désactivé, qu'un quota semble atteint, que les logs arr parlent de "API Request Limit reached", ou pour refaire le point sur la consommation d'API des indexeurs.
---

# indexer-quota — cause racine des quotas/désactivations d'indexeurs

## Objectif

Ne jamais conclure « quota atteint » sur la foi du libellé d'un log. Trois
questions, dans cet ordre :

1. **Est-ce vraiment un quota ?** (le plus souvent : non)
2. **Qui envoie les requêtes ?** (RSS sync, recherches, cross-seed)
3. **Lesquelles sont inutiles ?** (rendement grabs/requêtes par couple
   app × indexeur)

## 0. Prérequis — récupérer les clés d'API

Les images linuxserver.io embarquent BusyBox : **`grep -P` n'existe pas**,
utiliser `sed`. Piège rencontré le 2026-08-03.

```bash
PAK=$(docker exec arr-prowlarr-1 sed -n 's|.*<ApiKey>\([^<]*\)</ApiKey>.*|\1|p' /config/config.xml)
SAK=$(docker exec arr-sonarr-1   sed -n 's|.*<ApiKey>\([^<]*\)</ApiKey>.*|\1|p' /config/config.xml)
RAK=$(docker exec arr-radarr-1   sed -n 's|.*<ApiKey>\([^<]*\)</ApiKey>.*|\1|p' /config/config.xml)
```

Préférer les **fichiers de log** (`/config/logs/*.txt` dans chaque
conteneur) à `docker logs` : ils sont horodatés à la seconde, conservés plus
longtemps, et non tronqués par `max-size`/`max-file`.

## 1. Le piège central : Sonarr/Radarr mentent sur la cause

Le client Torznab de Sonarr/Radarr étiquette **tout** 429 reçu de Prowlarr
en `API Request Limit reached for X (Prowlarr). Disabled for HH:MM:SS`.
C'est faux dans la grande majorité des cas. La vraie cause est dans la
ligne `<error>` juste au-dessus :

```bash
docker exec arr-sonarr-1 sh -c 'grep -hE "429|API Request Limit" /config/logs/*.txt | tail -40'
```

| Description dans le `<error>` | Cause réelle |
|---|---|
| `Indexer is disabled till … **due to recent failures**` | **backoff d'échec de Prowlarr**, pas un quota |
| `Download failed` | échec de récupération du `.torrent`, pas un quota |
| (quota) | voir point 2 — ne se lit pas ici |

Le backoff de Prowlarr escalade : **1 min → 15 min → 30 min → 1 h → 3 h →
6 h → 10 h**. Une désactivation de plusieurs heures n'est donc pas le signe
d'un quota généreusement dépassé, juste d'échecs répétés.

## 2. Reconnaître un vrai quota

Un vrai plafond tracker est journalisé par **Prowlarr lui-même**, avec un
libellé différent :

```bash
docker exec arr-prowlarr-1 sh -c 'grep -hE "Request Limit reached" /config/logs/*.txt'
# -> "Warn|Cardigann|Request Limit reached for <indexeur>. Disabled for 01:00:00"
```

Les fichiers de log tournent, donc **dédupliquer** : la même occurrence
apparaît dans plusieurs fichiers.

Deux vérifications de contexte :

- **Prowlarr n'applique aucun plafond local par défaut** :
  `baseSettings.queryLimit`/`grabLimit` sont à `None` sur tous nos
  indexeurs, donc tout 429 de quota vient du tracker.
  ```bash
  docker exec arr-prowlarr-1 curl -s "http://localhost:9696/api/v1/indexer?apikey=$PAK" | python3 -c "
  import sys,json
  for i in json.load(sys.stdin):
      f={x['name']:x.get('value') for x in i['fields']}
      print('%3d %-12s %s' % (i['id'], i['name'], {k:v for k,v in f.items() if 'imit' in k}))"
  ```
- **Ne pas confondre avec le `limit:` des définitions Cardigann**
  (`/config/Definitions/<id>.yml`) : ces `limit: 1` / `limit: 100` sont des
  tailles de page de résultats, pas des quotas. Aucune de nos définitions ne
  déclare de bloc `limits:` — Prowlarr ne connaît donc aucun plafond
  officiel pour ces trackers.

## 3. Trouver la cause racine de chaque backoff

Pour chaque indexeur incriminé, sur la fenêtre du backoff :

```bash
docker exec arr-prowlarr-1 sh -c 'grep -hE "2026-XX-XX" /config/logs/*.txt \
  | grep -iE "<indexeur>" | grep -iE "warn|error|fail|unable|timeout|429"' | head -20
```

Causes déjà rencontrées, et comment les lire :

| Signature | Cause | Action |
|---|---|---|
| `401:Unauthorized` sur l'URL torznab du tracker | clé/session refusée côté tracker, souvent transitoire (constaté en rafales isolées les 26/07 et 02/08, sans intervention) | vérifier que ça ne devient pas permanent ; si oui, régénérer la clé chez le tracker |
| `530:530` + `error code: 1033` | Cloudflare côté tracker : le tracker est down, pas nous | rien à faire |
| `502:BadGateway` | idem, indisponibilité tracker | rien à faire |
| `Request Limit reached for X` | vrai quota, aller au point 4 | voir point 4 |

Confirmer l'état courant plutôt que de conclure sur des logs passés :

```bash
# liste vide = aucun indexeur en échec actuellement
docker exec arr-prowlarr-1 curl -s "http://localhost:9696/api/v1/indexerstatus?apikey=$PAK"
# test réel des 4 indexeurs
docker exec arr-prowlarr-1 curl -s -X POST "http://localhost:9696/api/v1/indexer/testall?apikey=$PAK" \
  | python3 -c "import sys,json;[print(r.get('id'), r.get('isValid'), r.get('validationFailures') or '') for r in json.load(sys.stdin)]"
```

## 4. Vrai quota : c'est une limite de débit, pas un plafond journalier

**Conclusion établie le 2026-08-03 sur C411, à ne pas re-litiger sans
données contraires.** Les 4 déclenchements connus ont tous été précédés
d'une **rafale de recherches**, et le volume journalier ne prédit rien :

| Jour | Requêtes C411 / jour | Quota |
|---|---|---|
| 01/08 | 177 | ok |
| 31/07 | 180 | ok |
| 02/08 | 139 | **atteint** |
| 23/07 | 111 | **atteint** |

Les deux journées de plus fort volume sont propres ; la plus faible des
journées à incident a déclenché. Dans les 60 min précédant chaque 429, le
RSS ne pesait que 5–6 requêtes sur 21–37 — un plancher constant, présent
aussi les jours propres. **Réduire le RSS ne règle donc pas un quota** ;
ça réduit le volume total, ce qui est un autre sujet (politesse envers le
tracker, bruit en moins).

Refaire ce test avant toute conclusion — c'est lui qui tranche :

```bash
# Fenêtre de 60 min avant un déclenchement + contrôle volume/jour.
# ATTENTION FUSEAUX : les logs Prowlarr sont en heure locale (CEST = UTC+2),
# les dates de /api/v1/history sont en UTC. Convertir avant de comparer,
# sinon la fenêtre tombe à côté et le RSS semble seul en cause.
```

Voir la structure d'interrogation de l'historique au point 5.

Motif à reconnaître dans la rafale : des recherches espacées de
**2 secondes exactement** = le `requestDelay` que Prowlarr applique déjà,
une requête par épisode. C'est une recherche de saison / d'épisodes
manquants, ou une boucle de regrab (cf. `cutoffFormatScore` dans
`CLAUDE.md`) — pas un usage interactif.

## 5. Attribuer le volume à sa source

Vue agrégée, la plus rapide :

```bash
docker exec arr-prowlarr-1 curl -s "http://localhost:9696/api/v1/indexerstats?apikey=$PAK" | python3 -m json.tool
```

Donne, par indexeur, `numberOfQueries` (recherches) vs
`numberOfRssQueries` vs `numberOfGrabs` — et surtout les blocs
`userAgents`/`hosts` qui attribuent tout à `Sonarr`/`Radarr`/`CrossSeed`.

Vue par jour et par source, via l'historique paginé :

```python
# /api/v1/history?page=N&pageSize=1000&sortKey=date&sortDirection=descending
# Champs utiles : r['date'] (UTC), r['indexerId'], r['eventType'],
#                 (r['data'] or {})['source'], ['query'], ['indexer']
# PIÈGE : eventType est une CHAÎNE ('indexerRss' | 'indexerQuery' |
# 'releaseGrabbed'), pas l'entier documenté ailleurs — un test `et == 3`
# classe tout en 'grab' silencieusement et le tableau ressort à zéro.
# Boucler jusqu'à page*1000 >= d['totalRecords'].
```

**Calculer le plancher RSS attendu** et le comparer à l'observé : c'est ce
qui révèle si le trafic est du RSS ou des recherches.

```
plancher RSS/jour = (1440 / rssSyncInterval) × nombre d'indexeurs activés en RSS
```

```bash
for s in sonarr:8989:$SAK radarr:7878:$RAK; do
  svc=${s%%:*}; port=$(echo $s|cut -d: -f2); ak=$(echo $s|cut -d: -f3)
  docker exec arr-$svc-1 curl -s "http://localhost:$port/api/v3/config/indexer?apikey=$ak" | python3 -c "import sys,json;print('$svc rssSyncInterval =', json.load(sys.stdin)['rssSyncInterval'])"
  docker exec arr-$svc-1 curl -s "http://localhost:$port/api/v3/indexer?apikey=$ak" | python3 -c "
import sys,json
for i in json.load(sys.stdin): print('   %-26s rss=%s autoSearch=%s' % (i['name'], i['enableRss'], i['enableAutomaticSearch']))"
done
```

## 6. Chiffrer les appels inutiles — rendement par couple app × indexeur

La métrique qui tranche : **grabs pour 1000 requêtes**, croisé
source × indexeur (depuis l'historique, cf. point 5). Un couple à quelques
‰ interroge un tracker qui n'a pas le contenu demandé.

Référence mesurée le **2026-08-03** (sur toute la vie de l'instance), à
comparer lors d'un prochain passage :

| Source | C411 | TR4KER | Nyaa.si | YggReborn |
|---|---|---|---|---|
| Sonarr | 2,7 ‰ (751→2) | 3,4 ‰ (5912→20) | 24,8 ‰ (6742→167) | 24,5 ‰ (611→15) |
| Radarr | 15,4 ‰ (389→6) | 16,0 ‰ (562→9) | **0 ‰ (1188→0)** | 13,9 ‰ (361→5) |
| CrossSeed | 362 ‰ | 523 ‰ | 556 ‰ | 313 ‰ |

Lectures déjà faites, à ne pas refaire de zéro :

- **cross-seed est de loin le plus efficace** (313–556 ‰) — ne jamais le
  désigner comme coupable d'un volume excessif sans données nouvelles. Son
  seul gros pic (995 requêtes le 2026-07-28) est le balayage historique
  manuel documenté dans `CLAUDE.md`, un one-off assumé.
- **Radarr × Nyaa.si : 1188 requêtes, 0 grab** — Nyaa est un tracker
  d'anime indexé en catégories TV, Radarr l'interroge en cat 2000/2020 pour
  rien depuis le début.
- **Sonarr × C411/TR4KER à ~3 ‰** — séries anime que ces trackers FR
  généralistes ne portent pas. C'est la source des rafales qui font sauter
  la limite de C411.

## 7. Leviers, et ce qu'ils règlent vraiment

Ne pas les présenter comme interchangeables — ils n'adressent pas le même
problème :

| Levier | Effet | Règle un quota ? |
|---|---|---|
| Tags d'indexeur (anime → Nyaa/Ygg, non-anime → C411/TR4KER) | supprime les recherches vouées à l'échec, donc les rafales | **oui**, c'est le correctif de fond |
| `baseSettings.queryLimit` sur l'indexeur | Prowlarr refuse localement au lieu de laisser le tracker répondre 429 → plus de backoff d'1 h, les autres indexeurs restent servis | change la nature de l'échec, ne le supprime pas |
| `rssSyncInterval` 15 → 30 min | −(nb indexeurs × 48) requêtes/jour | **non** (démontré au point 4) |
| Désactiver le RSS d'un couple à 0 ‰ | −48 req/jour par couple, sans perte | non |

État au 2026-08-03 : aucun de ces leviers n'a été appliqué, décision
explicite de l'utilisateur d'observer l'évolution d'abord. Les pics
historiques (24/07 : 4606 req Sonarr ; 27–28/07 : 1818 puis 2394) ont
disparu d'eux-mêmes avec les correctifs `cutoffFormatScore` + lookahead
parenthèses du 02/08 — régime stabilisé à ~600–700 req/jour, dont
l'essentiel est du RSS. Vérifier si ça tient avant de proposer autre chose.

## 8. Format du rapport

Répondre aux trois questions du début, dans l'ordre, en chiffrant. Un
tableau `Indexeur | Cause réelle | État` pour les incidents de la période,
et **toujours** dire explicitement quand un libellé de log a été écarté
comme trompeur (cf. point 1) — sinon la conclusion « ce n'est pas un
quota » ressemble à une opinion.

Ne pas recopier de valeur propre au déploiement au-delà du nécessaire
(clés d'API, domaine réel) — cf. `CLAUDE.md`, repo public.
