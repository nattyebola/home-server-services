---
name: vpn-bench
description: Compare latence/débit descendant/débit montant entre le serveur AirVPN actuellement configuré (container vpn-transmission-vpn-1) et un ou plusieurs autres pays AirVPN, sur un tracker fixe pour que la comparaison soit valide. À utiliser quand l'utilisateur demande de comparer des serveurs VPN, de vérifier si changer de pays/serveur AirVPN améliorerait la vitesse, ou de refaire ce bench après un moment pour voir si le classement a changé.
---

# vpn-bench — comparer des serveurs AirVPN

## Contexte

Le container `vpn/transmission-vpn` (image `haugene/transmission-openvpn`,
`OPENVPN_PROVIDER=CUSTOM`) utilise `vpn/custom/default.ovpn`, un fichier
AirVPN dont le certificat client (`<cert>`/`<key>`) est lié au **compte**,
pas au serveur — seule la ligne `remote <pays>.vpn.airdns.org <port>` cible
un pays précis (AirVPN route ce hostname vers un serveur recommandé dans ce
pays). Changer de serveur pour tester revient donc à changer cette seule
ligne et redémarrer le container, sans rien régénérer depuis le site AirVPN.

`scripts/vpn-bench.py` automatise ça : bascule la config, redémarre,
attend que le container soit `healthy`, mesure latence (ping vers un
tracker) + débit descendant/montant (via l'endpoint de test Cloudflare),
répète pour chaque pays demandé, **restaure systématiquement la config
d'origine à la fin** (y compris en cas d'erreur ou de Ctrl+C — backup sur
disque en plus de la restauration en mémoire, pour survivre à un kill dur).

Root historique de ce script : un bench manuel Belgique/Pays-Bas/Allemagne/
Suisse le 2026-07-29, où la Belgique s'est avérée meilleure que les trois
alternatives sur les trois métriques à la fois (voir aussi
`CLAUDE.md` si une entrée y a été ajoutée depuis).

## Comment l'utiliser

Depuis la racine du repo (`server/`) :

```bash
python3 scripts/vpn-bench.py nl de ch
```

Chaque code pays est un code AirVPN à 2 lettres (`nl`, `de`, `ch`, `at`,
`se`, `es`, `gb`, ...). Le serveur **actuellement configuré** est toujours
testé en premier (baseline), donc pas besoin de le lister explicitement.

Options utiles :
- `--tracker <host>` : fixe le tracker cible au lieu d'en tirer un au hasard
  parmi les torrents actifs (utile pour reproduire exactement un bench
  précédent, ou si aucun torrent n'est actif au moment du test).
- `--down-bytes`/`--up-bytes` : taille des tests de débit (défaut 50 Mo /
  25 Mo) — augmenter si un lien est trop rapide pour que le test soit fiable
  (temps total < ~1s), réduire si un serveur est visiblement très lent et
  qu'on veut juste confirmer rapidement qu'il ne vaut pas le coup.
- `--ping-count` : nombre de pings (défaut 5).

Le script imprime le résultat de chaque pays au fur et à mesure, puis un
tableau récapitulatif à la fin. Chaque changement de serveur implique un
redémarrage du container (~1-2 min pour reconnecter + devenir healthy) :
compter large, 3-5 min par pays testé, coupure brève des téléchargements/
seed à chaque redémarrage.

## Avant de lancer

- **Prévenir l'utilisateur** que ça va interrompre brièvement les
  téléchargements/seed en cours à chaque changement de serveur (autant de
  fois que de pays testés + 1 pour la restauration finale) — action
  reversible mais avec un effet de bord réel sur un service en cours
  d'usage, à confirmer avant de lancer si ce n'est pas déjà explicitement
  demandé.
- Vérifier qu'aucun fichier `vpn/custom/.default.ovpn.bench-backup` ne
  traîne déjà (`ls vpn/custom/`) — s'il y en a un, un run précédent a été
  interrompu sans pouvoir restaurer proprement ; l'inspecter et restaurer
  la bonne config à la main avant de relancer (le script refuse de démarrer
  tant que ce fichier existe, pour ne pas écraser un état déjà incertain).

## Après le run

Rapporter le tableau à l'utilisateur avec une recommandation courte (garder
le serveur actuel / passer à tel pays), en notant explicitement que c'est
une photo instantanée (la charge des serveurs AirVPN varie dans le temps) —
pas une garantie que le classement reste stable.

## Limites connues

- Le test de débit utilise `speed.cloudflare.com`, qui répond 403 sans
  User-Agent de navigateur (géré par le script) — si Cloudflare change son
  comportement anti-bot, les résultats `speed_bps: None`/`http_code`
  différent de 200 signalent qu'il faut revoir l'endpoint utilisé.
- Le ping cible souvent un tracker derrière un CDN (Cloudflare) plutôt que
  l'IP réelle du tracker — mesure la qualité de routage du tunnel jusqu'à ce
  CDN, pas jusqu'au tracker lui-même. Suffisant pour comparer des serveurs
  VPN entre eux (même cible à chaque fois dans un run donné), pas pour
  connaître la latence réelle vers le tracker.
- Ne couvre que les pays où AirVPN a un hostname `[pays].vpn.airdns.org` —
  vérifier la liste des pays disponibles sur AirVPN si un code testé échoue
  à se connecter (`wait_healthy` timeout après ~3 min → `error: unhealthy`
  dans le tableau).
