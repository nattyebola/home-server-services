// JS maison minimal (couvre exactement les 3 interactions dont clearr a
// besoin : navigation par lien data-get, soumission de formulaire data-post,
// filtre en direct data-live) — la modale de confirmation, elle, utilise le
// composant Modal natif de Bootstrap (bootstrap.min.js, vendoré à côté de
// bootstrap.min.css) plutôt qu'être réimplémentée à la main : focus trap,
// touche Échap, clic sur le fond, aria-* sont déjà corrects dans son JS,
// les réécrire ici aurait été strictement moins bien. Recharge toujours le
// fragment ciblé en entier depuis le serveur — même principe "on relance
// tout puis on redessine" que la TUI curses.
(function () {
  function formParams(form) {
    return new URLSearchParams(new FormData(form)).toString();
  }

  function showModalIfTarget(target) {
    if (target === "#modal-body") {
      bootstrap.Modal.getOrCreateInstance(document.getElementById("modal")).show();
    }
  }

  function hideModal() {
    var instance = bootstrap.Modal.getInstance(document.getElementById("modal"));
    if (instance) instance.hide();
  }

  // Zone permanente hors de #tab-content (voir page.html) : y écrire une erreur
  // plutôt que dans la cible évite d'effacer la barre d'onglets ou les boutons
  // de la modale, ce qui laissait l'utilisateur sans aucun moyen de réessayer.
  function showAlert(html) {
    var zone = document.getElementById("alert-zone");
    if (!zone) return;
    zone.innerHTML = html;
    zone.scrollIntoView({ block: "nearest" });
  }

  function clearAlert() {
    var zone = document.getElementById("alert-zone");
    if (zone) zone.innerHTML = "";
  }

  function failed(message) {
    showAlert('<div class="alert alert-danger m-0 mb-3">' + message + "</div>");
  }

  async function swapInto(target, resp) {
    var body = await resp.text();
    // Une réponse d'erreur (503 quand transmission-vpn est injoignable, 403
    // cross-origin) NE doit pas remplacer la cible : le handler renvoie un
    // fragment nu, et #tab-content contient la barre d'onglets.
    if (!resp.ok) {
      showAlert(body);
      return;
    }
    var el = document.querySelector(target);
    if (!el) return;
    clearAlert();
    el.innerHTML = body;
    showModalIfTarget(target);
  }

  // Désactive le déclencheur le temps de la requête et affiche un spinner
  // Bootstrap (déjà dans le CSS vendoré, aucune dépendance ajoutée). Sans ça,
  // le bouton « Confirmer la suppression » restait cliquable pendant les
  // quelques secondes d'une suppression de série : un second POST partait
  // vraiment, tombait sur « déjà supprimée » et ÉCRASAIT le message de succès
  // du premier — l'utilisateur croyait que rien n'avait été supprimé alors que
  // tout l'avait été.
  function busy(el) {
    if (!el || el.dataset.busy) return function () {};
    el.dataset.busy = "1";
    el.setAttribute("aria-busy", "true");
    el.classList.add("disabled");
    var spinner = document.createElement("span");
    spinner.className = "spinner-border spinner-border-sm ms-2";
    spinner.setAttribute("aria-hidden", "true");
    el.appendChild(spinner);
    return function () {
      delete el.dataset.busy;
      el.removeAttribute("aria-busy");
      el.classList.remove("disabled");
      spinner.remove();
    };
  }

  // --- Jaquette au survol ---------------------------------------------------
  // Un seul conteneur flottant réutilisé, attaché au <body> : les lignes vivent
  // dans .table-responsive (overflow), qui découperait une vignette positionnée
  // à l'intérieur du tableau. L'image n'est demandée qu'au premier survol
  // (data-poster porte l'URL, pas un <img> déjà présent — voir
  // templates/_meta.html) et reste ensuite dans le cache navigateur.
  var POSTER_WIDTH = 180;
  var posterPop = null;

  function posterElement() {
    if (!posterPop) {
      posterPop = document.createElement("div");
      posterPop.id = "poster-pop";
      posterPop.innerHTML = '<img alt="">';
      posterPop.querySelector("img").addEventListener("error", hidePoster);
      document.body.appendChild(posterPop);
    }
    return posterPop;
  }

  function showPoster(host) {
    var pop = posterElement();
    var img = pop.querySelector("img");
    var url = host.getAttribute("data-poster");
    if (img.getAttribute("src") !== url) img.setAttribute("src", url);
    img.alt = host.getAttribute("data-poster-title") || "";
    pop.classList.add("visible");
    // Ancré sur la cellule survolée (pas sur le curseur) : pas de vignette qui
    // suit la souris, et la position reste stable pendant qu'on lit.
    var rect = host.getBoundingClientRect();
    var height = pop.offsetHeight || POSTER_WIDTH * 1.5;
    var left = rect.right + 12;
    if (left + POSTER_WIDTH > window.innerWidth - 8) {
      left = Math.max(8, rect.left - POSTER_WIDTH - 12);
    }
    var top = rect.top + rect.height / 2 - height / 2;
    top = Math.min(Math.max(8, top), Math.max(8, window.innerHeight - height - 8));
    pop.style.left = left + "px";
    pop.style.top = top + "px";
  }

  function hidePoster() {
    if (posterPop) posterPop.classList.remove("visible");
  }

  document.addEventListener("mouseover", function (e) {
    var host = e.target.closest("[data-poster]");
    if (host) showPoster(host);
  });

  document.addEventListener("mouseout", function (e) {
    if (e.target.closest("[data-poster]")) hidePoster();
  });

  // Un swap de fragment ou un défilement peut faire disparaître la cellule
  // survolée sans qu'aucun mouseout ne soit émis — la vignette resterait
  // affichée dans le vide.
  window.addEventListener("scroll", hidePoster, true);

  document.addEventListener("click", function (e) {
    // Les liens IMDb/TVDB/TMDB/Sonarr/Radarr sont de vrais liens externes
    // (target=_blank) : ils ne doivent pas être interceptés par le handler
    // data-get ci-dessous, et n'ont aucune raison de le déclencher.
    if (e.target.closest(".meta-link")) return;
    hidePoster();
    var get = e.target.closest("[data-get]");
    if (get) {
      e.preventDefault();
      if (get.dataset.busy) return;
      var doneGet = busy(get);
      fetch(get.getAttribute("data-get")).then(function (resp) {
        return swapInto(get.getAttribute("data-target"), resp);
      }).catch(function () {
        // Rejet réseau réel (conteneur en redémarrage après make update,
        // Wi-Fi coupé) : sans .catch(), la promesse était rejetée sans que
        // rien n'apparaisse à l'écran.
        failed("Requête échouée — clearr est-il joignable ? Réessayez.");
      }).finally(doneGet);
      return;
    }
    var post = e.target.closest("[data-post]");
    if (post) {
      e.preventDefault();
      if (post.dataset.busy) return;   // garde anti double-soumission
      var donePost = busy(post);
      // data-form : le déclencheur n'est pas toujours DANS le formulaire qu'il
      // soumet — le pied d'une modale Bootstrap est un frère de son corps, et
      // un <form> qui envelopperait les deux casserait le flex de .modal-content
      // (imbriquer deux <form> étant par ailleurs invalide en HTML). Sans ça, le
      // choix des saisons partait vide : closest("form") ne trouvait rien.
      var selector = post.getAttribute("data-form");
      var form = selector ? document.querySelector(selector) : post.closest("form");
      fetch(post.getAttribute("data-post"), {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: form ? formParams(form) : null,
      }).then(function (resp) {
        hideModal();
        return swapInto(post.getAttribute("data-target"), resp);
      }).catch(function () {
        hideModal();
        failed("La suppression n'a pas pu être envoyée — rien n'a été supprimé. "
               + "Vérifiez que clearr répond, puis réessayez.");
      }).finally(donePost);
      return;
    }
  });

  // Le formulaire de filtre porte data-live-get/data-live-target (PAS
  // data-get/data-target) exprès : le handler de clic ci-dessus fait
  // closest("[data-get]") depuis l'élément cliqué, qui remonte jusqu'à
  // n'importe quel ancêtre portant cet attribut — si le <form> lui-même
  // avait porté data-get (comme avant le 2026-08-01), cliquer n'importe où
  // dedans, y compris dans le champ texte, déclenchait immédiatement une
  // navigation/swap au clic, avant même de taper quoi que ce soit (symptôme
  // rapporté : le focus se perdait "tout de suite après" le clic, pas après
  // une frappe). data-live-get/data-live-target ne sont lus qu'ici, jamais
  // par le handler de clic.
  //
  // Le champ filtre est en plus DANS la zone remplacée à chaque frappe (le
  // fragment renvoyé par le serveur inclut le formulaire de filtre
  // lui-même) — un swap nu détruit et recrée l'input à chaque appel, donc
  // perdrait le focus après la première frappe une fois le clic lui-même
  // corrigé. On retrouve l'input équivalent dans le nouveau HTML par son
  // name et on lui rend le focus + la position du curseur.
  var liveTimer;
  var liveAbort = null;
  document.addEventListener("input", function (e) {
    var input = e.target.closest("[data-live]");
    if (!input) return;
    var form = input.closest("form");
    var name = input.getAttribute("name");
    var cursor = input.selectionStart;
    clearTimeout(liveTimer);
    liveTimer = setTimeout(function () {
      var url = form.getAttribute("data-live-get") + "?" + formParams(form);
      var target = form.getAttribute("data-live-target");
      // Le débounce espace les frappes mais n'annule pas les requêtes en vol.
      // Taper dans le filtre puis cliquer aussitôt sur un onglet mettait deux
      // réponses en course vers #tab-content : si celle du filtre (qui recharge
      // tout l'état Transmission) arrivait après, elle écrasait la vue Séries
      // par du contenu Torrents, sans action de l'utilisateur — et la ligne
      // visée pouvait avoir changé entre la lecture et le clic sur une croix.
      if (liveAbort) liveAbort.abort();
      liveAbort = new AbortController();
      fetch(url, { signal: liveAbort.signal }).then(function (resp) {
        if (!resp.ok) throw new Error("http");
        return resp.text();
      }).then(function (html) {
        var el = document.querySelector(target);
        if (!el) return;
        clearAlert();
        el.innerHTML = html;
        showModalIfTarget(target);
        var newInput = el.querySelector('[name="' + name + '"]');
        if (newInput) {
          newInput.focus();
          if (typeof cursor === "number") newInput.setSelectionRange(cursor, cursor);
        }
      }).catch(function (err) {
        if (err && err.name === "AbortError") return;   // annulation voulue
        failed("Le filtre n'a pas pu être appliqué — réessayez.");
      });
    }, 300);
  });
})();
