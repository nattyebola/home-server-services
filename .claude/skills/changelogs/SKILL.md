---
name: changelogs
description: Fait l'inventaire des montées de version notées par `make update` / `make update-all` et pas encore traitées, résume pour chacune les notes de version de toutes les releases intermédiaires (nouveautés utiles à CE serveur, changements cassants, actions à faire), puis marque les entrées traitées. À utiliser quand l'utilisateur demande ce qui a changé après une mise à jour, les nouveautés des images, les changelogs en attente, ou lance /changelogs.
---

# changelogs — ce qu'apportent les dernières mises à jour

## Contexte

`make update` note la version de chaque service avant le pull et la
compare après le `up` (`scripts/image-versions.py`). Chaque version qui a
changé ajoute une ligne à `$DATA_ROOT/.update-journal/journal.jsonl`, avec
`processed: null`. Ce skill lit ces lignes, résume, puis les marque.

Une montée sans changement de version (rebuild linuxserver `-lsNNN`,
modif de code de clearr) n'est pas notée : il n'y a rien à lire.

## 1. Entrées en attente

```bash
python3 scripts/image-versions.py pending
```

Liste vide : le dire en une ligne et s'arrêter. Champs utiles : `service`,
`from`, `to`, `repo`, `notes`, `tier`, `date`.

Plusieurs entrées pour un même service (plusieurs updates sans lecture
entre deux) : les fusionner, du plus ancien `from` au plus récent `to`.

## 2. Notes de version

Récupérer **toutes** les releases strictement après `from`, jusqu'à `to`
incluse, pas seulement la dernière :

```bash
curl -s "https://api.github.com/repos/<repo>/releases?per_page=50" \
  | python3 -c 'import json,sys; [print("##", r["tag_name"], r["published_at"][:10], "\n", r["body"] or "", "\n") for r in json.load(sys.stdin)]'
```

`gh` n'est pas installé. L'API anonyme suffit (60 requêtes/h). Les tags
ont parfois un préfixe (`v3.7.13`) ou un suffixe que le journal n'a pas :
comparer les numéros.

Cas particuliers :

- **`from` ou `to` à `null`** (version illisible) : retrouver la release
  d'après la date de l'entrée et les `published_at`, et le dire.
- **`notes` renseigné, `repo` à `null`** (postgres) : WebFetch sur `notes`,
  section de la version mineure concernée.
- **Release sans texte** (nextcloud/server, nginx) : se rabattre sur le
  changelog officiel (`https://nextcloud.com/changelog/`,
  `https://nginx.org/en/CHANGES`).
- **`haugene/docker-transmission-openvpn`** : sa version n'est pas celle de
  Transmission, lire aussi ce que dit la release sur la version embarquée.

## 3. Trier selon CE serveur

Lire la note en ayant la config sous les yeux, pas en résumé générique.
Pour juger si un point concerne le serveur : le compose de la stack, la
page `docs/` du thème et le `.claude/docs/` du domaine (voir CLAUDE.md).

Garder :

- **⚠️ Action requise** : changement cassant, option retirée ou renommée
  que le dépôt utilise (`grep` dans le dépôt pour le vérifier), migration
  manuelle, fin de support. Toujours en premier.
- **🔒 Sécurité** : CVE corrigée, pour tout `tier`.
- **✨ Nouveauté utile** : ce qui sert un usage réel d'ici (ex. lecture de
  BD pour Komga, chaîne arr → Jellyfin → Kodi). Une phrase sur l'intérêt
  concret, et la décision CLAUDE.md qu'elle touche s'il y en a une.

Écarter : traductions, refactos internes, dépendances, correctifs de
fonctions non utilisées ici.

`tier: infra` (nginx, postgres, python, socket-proxy) : une ligne de
version, rien de plus, sauf ⚠️ ou 🔒.

Ne pas proposer de revenir sur une décision de CLAUDE.md parce qu'une
nouveauté le permettrait : la signaler, sans plus.

## 4. Restitution

En français, une section par service `tier: app`, du plus important au
moins important ; l'infra regroupée en fin, en une ligne chacune. Un
service sans rien à retenir : « rien de notable » sur une ligne. Lien vers
les releases à la fin de chaque section.

## 5. Marquer traité

Seulement **après** avoir restitué, et seulement les entrées couvertes :

```bash
python3 scripts/image-versions.py mark <id> [<id>...]
```

`mark --all` uniquement si tout a été couvert. Une entrée dont les notes
n'ont pas pu être lues reste en attente ; le dire.
