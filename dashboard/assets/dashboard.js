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
