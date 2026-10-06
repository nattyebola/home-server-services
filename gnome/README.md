# gnome/ — extension GNOME Shell « Govee »

Un menu dans la barre de GNOME Shell avec un interrupteur on/off par appareil
Govee du compte (lampes, bandeaux…). Comme [`kodi/`](../kodi/), ce n'est pas un
service de la stack : c'est un outil **poste de travail**, installé dans la
session de l'utilisateur.

Testée sur GNOME Shell 50 (Wayland).

## Installation

1. Demander une **clé API** dans l'app Govee Home : Profil → Réglages →
   « Apply for API Key ». Elle arrive par e-mail.
2. L'enregistrer dans le **trousseau GNOME** (jamais sur disque en clair) :
   ```
   gnome/govee@local/set-api-key.sh
   ```
   Saisie masquée dans un terminal, fenêtre `zenity` sinon. La clé passe par
   stdin : ni historique du shell, ni `ps`.
3. Vérifier que l'API répond, hors GNOME Shell :
   ```
   gjs -m gnome/govee@local/tools/check.js
   ```
4. Installer et activer :
   ```
   make gnome-install
   # première installation : se déconnecter / reconnecter (Wayland), puis
   gnome-extensions enable govee@local
   ```

`make gnome-install` crée un **lien symbolique** vers ce dossier : modifier le
code ici suffit, mais GNOME Shell ne recharge une extension qu'à l'ouverture de
session (Wayland) — désactiver/réactiver ne recharge pas les modules JS.

## Choix et limites

- **API cloud, pas l'API LAN.** Govee a une API locale (UDP), mais elle doit
  être activée appareil par appareil (« LAN Control » dans l'app) et
  l'option n'existe pas pour les H6008 et H6199 utilisés ici, même firmware à
  jour. Conséquences : il faut Internet, ~1 s par commande, quota ~10 000
  requêtes/jour (l'extension en fait ~4 par ouverture du menu).
- **Les groupes de l'app (`BaseGroup`) sont masqués** : l'API accepte leur
  on/off mais `/device/state` répond « devices not exist », l'interrupteur
  afficherait un état faux.
- **Le menu reste ouvert quand on bascule un interrupteur**
  (`StickySwitchMenuItem`) ; Échap ou un clic à côté le ferme.
- Noms affichés = noms donnés dans l'app Govee.
- **Ampoule pleine dans la barre** dès qu'au moins un appareil joignable est
  allumé, vide sinon. L'état n'est relu qu'à l'ouverture du menu : un appareil
  allumé depuis l'app ou la télécommande ne change l'icône qu'à ce moment-là.
- **Un clic pendant cette relecture l'emporte** : l'état lu avant la commande
  est ignoré (compteur `clicks` par appareil), sinon l'interrupteur et
  l'ampoule revenaient sur l'ancienne position.

## Interrupteur global et raccourci clavier

En tête du menu, **« Tout »** allume ou éteint tous les appareils joignables
d'un coup. Sa position suit la **majorité** des appareils joignables ; une
égalité compte comme allumé (un appui éteint alors tout). La commande part
vers chaque appareil, même ceux déjà dans l'état voulu, en parallèle.

Le même basculement est exposé en **D-Bus**, pour un raccourci clavier ou une
touche de télécommande :

```
gdbus call --session --dest org.gnome.Shell \
  --object-path /org/gnome/Shell/Extensions/Govee \
  --method org.gnome.Shell.Extensions.Govee.ToggleAll
```

À poser dans Paramètres → Clavier → Raccourcis personnalisés (commande sur une
seule ligne, sans les `\`). Le raccourci relit d'abord l'état des appareils
(le menu est fermé, l'état connu peut dater), bascule vers l'inverse de la
majorité et l'annonce par une bulle à l'écran (comme le volume) : ~2 s au
total, 1 + 2×N requêtes. Un appui pendant une bascule en cours est ignoré.

| Situation | Bulle |
|---|---|
| Majorité éteinte | « Tout allumer » |
| Majorité allumée ou égalité | « Tout éteindre » |
| Aucun appareil joignable / clé absente | message d'erreur |

> [!NOTE]
> Toute application de la session peut appeler cette méthode. C'est le même
> niveau d'accès que la clé API dans le trousseau : rien de plus exposé.

Diagnostic : `journalctl --user -f | grep govee:` — une ligne par clic et par
requête API (code HTTP, durée). Un clic doit donner exactement une ligne
`POST /device/control` (une par appareil joignable pour « Tout ») ; plusieurs, c'est la boucle `setToggleState` → `toggled`
qui revient (voir plus bas).

## Montée de version de GNOME Shell

`metadata.json` déclare `"shell-version": ["50"]` : à la version majeure
suivante, GNOME Shell **désactive l'extension d'office** (« incompatible »),
l'ampoule disparaît de la barre, rien d'autre ne casse. Sur Ubuntu 26.04 LTS,
GNOME reste en 50 jusqu'à une montée de version de la distribution.

Pour la remettre en service :

1. ajouter la nouvelle version dans `shell-version` ;
2. se reconnecter, ouvrir le menu, basculer un interrupteur en surveillant les
   logs (ci-dessus) ;
3. si ça casse, les deux points fragiles sont dans `extension.js` :
   - **`StickySwitchMenuItem`** surcharge `activate()` et lit `_switch`, des
     internes de `PopupSwitchMenuItem` (`resource:///org/gnome/shell/ui/popupMenu.js`,
     extractible avec `gresource extract /usr/lib/gnome-shell/libshell-*.so
     /org/gnome/shell/ui/popupMenu.js`) : le plus susceptible de changer ;
   - **le garde `_syncing`** existe parce qu'en GNOME 50 `setToggleState()`
     émet `toggled`. Sans lui, chaque resynchro depuis l'API renvoie une
     commande, et un 429 fait basculer l'appareil en boucle. Si GNOME cesse
     d'émettre ce signal, le garde devient inutile mais inoffensif ;
   - **`Main.osdWindowManager.showAll()`** (bulle du raccourci), interne
     lui aussi : si elle casse, le raccourci bascule quand même, sans bulle,
     avec une erreur `govee: bulle OSD` dans les logs.

`govee.js` ne dépend que de libsoup 3 et libsecret, stables d'une version à
l'autre.
