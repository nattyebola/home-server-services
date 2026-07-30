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
