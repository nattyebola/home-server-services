// localStorage lève une SecurityError quand le stockage du site est bloqué
// (navigation privée sur d'anciens Safari, Firefox « bloquer les cookies »,
// politique d'entreprise). Non gardé, l'appel interrompait l'IIFE AVANT que son
// addEventListener soit posé : la section Monitoring restait masquée ET son
// switch ne répondait plus, sans message. Le dashboard étant public, le
// navigateur du visiteur n'est pas maîtrisé.
function storageGet(key) {
  try { return localStorage.getItem(key); } catch (e) { return null; }
}
function storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* stockage bloqué */ }
}

// Grise les cartes LAN-only quand l'appelant est sur le WAN : leur
// ipallowlist Traefik répond 403 à la sonde ci-dessous, ce que <img>
// distingue d'un chargement réussi sans dépendre de CORS.
document.querySelectorAll('.card[data-probe]').forEach(function (card) {
  var probe = new Image();
  probe.onerror = function () {
    card.classList.add('card-lan-blocked');
    card.removeAttribute('href');
    var host = card.querySelector('.host');
    if (host) { host.textContent = 'accessible en LAN uniquement'; }
  };
  probe.src = card.dataset.probe + '?t=' + Date.now();
});

// Contenu de la section Monitoring masqué par défaut (voir
// .monitoring-hidden) — le titre + switch restent toujours visibles (voir
// section-transmission.html), seul le switch montre/masque le contenu en
// dessous. État retenu en localStorage pour survivre à un rechargement (la
// page est régénérée par cron toutes les 5 min, perdre le choix à chaque
// refresh serait pénible).
(function () {
  var content = document.getElementById('monitoring-content');
  var toggle = document.getElementById('monitoring-toggle');
  if (!content || !toggle) { return; }
  var STORAGE_KEY = 'dashboard-monitoring-visible';
  var apply = function (visible) {
    content.classList.toggle('monitoring-hidden', !visible);
    toggle.checked = visible;
  };
  apply(storageGet(STORAGE_KEY) === '1');
  toggle.addEventListener('change', function () {
    storageSet(STORAGE_KEY, toggle.checked ? '1' : '0');
    apply(toggle.checked);
  });
})();

// Table "Ratio par tracker" : n'affiche par défaut que les trackers privés
// (indexeur configuré dans Prowlarr et annoncé non public — voir
// render_tracker_row() dans generate-dashboard.py), les seuls où le ratio
// compte ; masque donc aussi bien les trackers publics bruts embarqués dans
// un .torrent (un torrent multi-tracker en aligne ~20 à lui seul) que les
// indexeurs publics type Nyaa.si. Switch
// pour tout afficher, état retenu en localStorage (même raison que le switch
// Monitoring ci-dessus : la page est régénérée par cron toutes les 5 min).
(function () {
  var card = document.querySelector('.stat-tracker');
  var toggle = document.getElementById('tracker-all-toggle');
  if (!card || !toggle) { return; }
  var STORAGE_KEY = 'dashboard-trackers-show-all';
  var apply = function (showAll) {
    card.classList.toggle('show-all-trackers', showAll);
    toggle.checked = showAll;
  };
  apply(storageGet(STORAGE_KEY) === '1');
  toggle.addEventListener('change', function () {
    storageSet(STORAGE_KEY, toggle.checked ? '1' : '0');
    apply(toggle.checked);
  });
})();

// Signale que la page servie est périmée, c'est-à-dire que le cron de
// régénération ne tourne plus. C'est la SEULE vérification d'état qui ne
// dépend pas de generate-dashboard.py : la carte « Tâches planifiées » est
// rendue par lui, donc s'il casse, nginx continue de servir le dernier
// index.html valide — avec toutes ses pastilles au vert, et masquant du même
// coup les pannes qu'il aurait dû rapporter (sauvegarde périmée, cron
// Nextcloud arrêté, conteneur unhealthy). Point unique de défaillance de
// toute l'observabilité, d'où ce contrôle côté client.
(function () {
  var footer = document.querySelector('.updated[data-generated]');
  if (!footer) { return; }
  var generated = parseInt(footer.dataset.generated, 10);
  var staleAfter = parseInt(footer.dataset.staleAfter, 10);
  if (!generated || !staleAfter) { return; }
  var warn = footer.querySelector('.updated-warning');
  var check = function () {
    var age = Math.floor(Date.now() / 1000) - generated;
    var stale = age > staleAfter;
    footer.classList.toggle('updated-stale', stale);
    if (!warn) { return; }
    warn.hidden = !stale;
    if (stale) {
      warn.textContent = 'Page périmée depuis ' + Math.floor(age / 60)
        + ' min — la régénération automatique ne tourne plus, les états affichés '
        + 'ci-dessus ne sont plus à jour. ';
    }
  };
  check();
  // Re-vérifie pendant que l'onglet reste ouvert : la page ne se recharge pas
  // toute seule, donc sans ça un dashboard laissé affiché resterait crédible
  // indéfiniment après l'arrêt du cron.
  setInterval(check, 60000);
})();
