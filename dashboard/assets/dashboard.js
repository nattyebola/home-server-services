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

// Le dashboard n'est régénéré que par cron (5 min, scripts/crontab) — passé
// 10 min (2 cycles manqués) sans nouvelle génération, un problème silencieux
// est plus probable qu'un simple délai normal (cron désactivé, script en
// échec) : on repasse le timestamp en rouge. Recalculé en continu (pas
// seulement au chargement) pour virer au rouge même si la page reste
// ouverte sans être rechargée.
(function () {
  var updated = document.querySelector('.updated[data-generated]');
  if (!updated) { return; }
  var STALE_MS = 10 * 60 * 1000;
  var check = function () {
    updated.classList.toggle('updated-stale', Date.now() - Number(updated.dataset.generated) > STALE_MS);
  };
  check();
  setInterval(check, 30000);
})();
