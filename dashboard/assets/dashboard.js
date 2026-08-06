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
  apply(localStorage.getItem(STORAGE_KEY) === '1');
  toggle.addEventListener('change', function () {
    localStorage.setItem(STORAGE_KEY, toggle.checked ? '1' : '0');
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
  apply(localStorage.getItem(STORAGE_KEY) === '1');
  toggle.addEventListener('change', function () {
    localStorage.setItem(STORAGE_KEY, toggle.checked ? '1' : '0');
    apply(toggle.checked);
  });
})();
